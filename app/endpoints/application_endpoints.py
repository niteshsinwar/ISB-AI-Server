import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple
import asyncio
import importlib
import json # For logging dicts
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Body, Depends, BackgroundTasks, Request
from pydantic import BaseModel, Field

from app.services.salesforce_service import SalesforceService, get_sf_service_dependency
from app.processors.application_processor import process_single_application_detail
from app.config import (
    RELATED_RECORD_PROCESSING_CONFIG,
    APPLICATION_OBJECT_API_NAME,
    MAX_CONCURRENT_PROCESSING_SLOTS,
    ACTIVE_PROCESSING_TIMEOUT_SECONDS
)
from app.core.rate_limit_state import (
    generate_client_fingerprint,
    check_and_update_global_rate_limit, check_and_update_client_rate_limit,
    check_rapid_fire_protection, check_processing_slots,
    acquire_processing_slot, release_processing_slot,
    update_processing_status, get_processing_status,
    get_all_processing_statuses, get_active_processing_slots_count
)
# from app.core.app_instance import get_app_instance # Assuming not directly used in this file


logger = logging.getLogger(__name__)
router = APIRouter()

class AnalyzeApplicationBodyRequest(BaseModel):
    record_id: str = Field(..., description="The 15 or 18 character ID of the Salesforce Application__c record.")

class AnalyzeApplicationResponse(BaseModel):
    request_id: str = Field(serialization_alias="_id")
    created_at: str
    last_updated_at: str
    application_record_id: str
    message: str
    status: str # Overall status of the request acceptance
    application_analysis_status: Optional[str] = None # Status of the background job
    processed_record_types: Optional[Dict[str, Any]] = Field(default_factory=dict) # Populated by BG task
    error: Optional[str] = None

    # New fields for immediate response
    related_records_metadata: Optional[List[Dict[str, Any]]] = None
    status_url: Optional[str] = None
    estimated_completion_info: Optional[str] = None


async def fetch_initial_related_records_metadata(
    sf_service: SalesforceService,
    application_record_id: str,
    job_id: str  # For logging
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """
    Fetches initial metadata about related records for the immediate API response
    and prepares data for the background task.

    Returns:
        Tuple: (metadata_for_api_response, data_for_background_task)
               data_for_background_task maps a unique key (e.g., target_record_type) to
               {"ids": [], "processor_module": "", "processor_function_name": "", "error": optional_error_string}
    """
    metadata_for_api_response: List[Dict[str, Any]] = []
    # Using target_record_type as key, assuming it's unique enough for processor configs
    data_for_background_task: Dict[str, Dict[str, Any]] = {}

    for config_item_idx, config_item in enumerate(RELATED_RECORD_PROCESSING_CONFIG):
        target_record_type = config_item["target_record_type"]
        retrieval_method = config_item["retrieval_method"]
        processor_module_name = config_item["processor_module"]
        processor_function_name = config_item["processor_function_name"]
        
        # Unique key for this processor configuration for the background task
        # If target_record_type can be repeated with different processors, a more unique key is needed.
        # For now, assuming target_record_type is sufficient for differentiation or it's okay if it overwrites
        # if multiple configs point to the same target_record_type (which would be unusual design).
        # Let's use a combination of index and target_record_type if true uniqueness is needed,
        # but for simplicity, using target_record_type as the key for data_for_background_task.
        # If RELATED_RECORD_PROCESSING_CONFIG can have multiple entries for the SAME target_record_type
        # but different processors, then target_record_type alone as a key is problematic for data_for_background_task.
        # Let's assume target_record_type in RELATED_RECORD_PROCESSING_CONFIG is effectively a unique identifier for a processing stream.

        item_api_metadata: Dict[str, Any] = {
            "target_record_type": target_record_type,
            "retrieval_method": retrieval_method,
            "processor_module": processor_module_name, # For info
            "count": 0,
            "status": "pending_fetch",
            "sample_ids": []
        }
        related_ids: List[str] = []
        fetch_error_str: Optional[str] = None

        try:
            logger.info(f"Job ID {job_id}: Endpoint pre-fetch: Fetching related '{target_record_type}' for App ID {application_record_id} via '{retrieval_method}'.")
            if retrieval_method == "direct":
                related_ids = await asyncio.to_thread(
                    sf_service.get_directly_related_record_ids,
                    parent_record_id=application_record_id,
                    parent_object_api_name=APPLICATION_OBJECT_API_NAME,
                    child_object_api_name=target_record_type,
                    lookup_field_on_child_to_parent=config_item["lookup_on_child_to_parent"]
                )
            elif retrieval_method == "via_junction":
                related_ids = await asyncio.to_thread(
                    sf_service.get_target_ids_via_junction,
                    parent_record_id=application_record_id,
                    parent_object_api_name=APPLICATION_OBJECT_API_NAME,
                    junction_object_api_name=config_item["junction_object"],
                    junction_field_to_parent=config_item["junction_field_to_parent"],
                    junction_field_to_target=config_item["junction_field_to_target"]
                )
            item_api_metadata["count"] = len(related_ids)
            item_api_metadata["sample_ids"] = related_ids[:5]
            item_api_metadata["status"] = "ids_fetched" if related_ids else "no_records_found"

        except Exception as e_fetch:
            logger.error(f"Job ID {job_id}: Endpoint pre-fetch: Error fetching '{target_record_type}' for App ID {application_record_id}: {e_fetch}", exc_info=True)
            fetch_error_str = str(e_fetch)
            item_api_metadata["status"] = f"fetch_error: {fetch_error_str[:100]}"
        
        metadata_for_api_response.append(item_api_metadata)
        data_for_background_task[target_record_type] = { # Use target_record_type as key
            "ids": related_ids, # Will be empty if error or no records
            "processor_module": processor_module_name,
            "processor_function_name": processor_function_name,
            "config_item_original": config_item, # Pass original config if needed later
            "error_during_prefetch": fetch_error_str
        }
    return metadata_for_api_response, data_for_background_task


async def process_application_and_related_records_bg(
    application_record_id: str,
    sf_service_instance: SalesforceService,
    job_id: str,
    client_fingerprint: str,
    prefetched_related_data: Dict[str, Dict[str, Any]] # New parameter
):
    """
    Core background logic using pre-fetched related record IDs.
    Waits for all related record processors to complete before setting final status.
    """
    any_processing_errors_local = False
    application_analysis_completed_successfully_local = False
    app_analysis_summary_local = "Processing error or not run."
    processed_record_types_summary_local: Dict[str, Any] = {} # For detailed status per type in job status

    related_processing_tasks_with_desc: List[Dict[str, Any]] = []

    try:
        logger.info(f"Job ID {job_id}: BG Task: Starting main application detail analysis for App ID: {application_record_id} (Client: {client_fingerprint[:8]}...)")
        app_analysis_summary_local = await process_single_application_detail(
            sf_service=sf_service_instance,
            application_id=application_record_id,
            parent_application_id=application_record_id,
            application_object_api_name=APPLICATION_OBJECT_API_NAME
        )
        logger.info(f"Job ID {job_id}: BG Task: Main application analysis completed for App ID {application_record_id}. Summary: {app_analysis_summary_local}")

        if "Error:" in app_analysis_summary_local or "Failed" in app_analysis_summary_local:
            any_processing_errors_local = True
        else:
            application_analysis_completed_successfully_local = True

        # Process related records using prefetched_related_data
        for target_record_type_key, processor_data in prefetched_related_data.items():
            related_ids = processor_data.get("ids", [])
            processor_module_name = processor_data["processor_module"]
            processor_function_name = processor_data["processor_function_name"]
            prefetch_error = processor_data.get("error_during_prefetch")

            # Initialize summary for this type
            processed_record_types_summary_local[target_record_type_key] = {
                "count_prefetched": len(related_ids),
                "sample_ids_prefetched": related_ids[:5],
                "status": "pending_processing"
            }

            if prefetch_error:
                logger.error(f"Job ID {job_id}: BG Task: Skipping processing for {target_record_type_key} due to pre-fetch error: {prefetch_error}")
                processed_record_types_summary_local[target_record_type_key]["status"] = f"skipped_due_to_prefetch_error: {prefetch_error}"
                any_processing_errors_local = True # Pre-fetch error means this part of processing failed
                continue

            if not related_ids:
                logger.info(f"Job ID {job_id}: BG Task: No related IDs for {target_record_type_key} from pre-fetch.")
                processed_record_types_summary_local[target_record_type_key]["status"] = "no_records_to_process_from_prefetch"
                continue
            
            try:
                module = importlib.import_module(processor_module_name)
                processor_func = getattr(module, processor_function_name)
                
                current_type_tasks_prepared = 0
                for idx, r_id in enumerate(related_ids):
                    coro = processor_func(
                        sf_service_instance, r_id, application_record_id, item_index=(idx + 1)
                    )
                    description = f"Processor for {target_record_type_key} ID {r_id} (App {application_record_id}, Job {job_id})"
                    related_processing_tasks_with_desc.append({"coro": coro, "description": description, "type": target_record_type_key})
                    current_type_tasks_prepared +=1
                
                processed_record_types_summary_local[target_record_type_key]["tasks_prepared_for_gather"] = current_type_tasks_prepared
                processed_record_types_summary_local[target_record_type_key]["status"] = "sub_tasks_prepared_for_concurrent_run"

            except (AttributeError, ImportError) as e_load:
                logger.error(f"Job ID {job_id}: BG Task: Error loading processor {processor_module_name}.{processor_function_name} for {target_record_type_key}: {e_load}", exc_info=True)
                processed_record_types_summary_local[target_record_type_key]["status"] = f"processor_load_error: {str(e_load)[:100]}"
                any_processing_errors_local = True
                continue # Skip this type if processor can't be loaded

        if related_processing_tasks_with_desc:
            actual_coroutines = [task_info["coro"] for task_info in related_processing_tasks_with_desc]
            logger.info(f"Job ID {job_id}: BG Task: Starting concurrent execution of {len(actual_coroutines)} related record processing sub-tasks.")
            results = await asyncio.gather(*actual_coroutines, return_exceptions=True)
            logger.info(f"Job ID {job_id}: BG Task: All {len(actual_coroutines)} related record sub-tasks finished.")

            type_completion_status: Dict[str, Dict[str, int]] = defaultdict(lambda: {"succeeded": 0, "failed": 0, "total_attempted_in_gather":0})
            for i, result_item in enumerate(results):
                task_info = related_processing_tasks_with_desc[i]
                task_description = task_info["description"]
                target_record_type = task_info["type"] # This is the key used before (e.g. "Education_History__c")
                type_completion_status[target_record_type]["total_attempted_in_gather"] += 1

                if isinstance(result_item, Exception):
                    any_processing_errors_local = True
                    logger.error(f"Job ID {job_id}: Sub-task '{task_description}' failed: {result_item}", exc_info=result_item)
                    type_completion_status[target_record_type]["failed"] += 1
                else:
                    # logger.info(f"Job ID {job_id}: Sub-task '{task_description}' completed successfully.") # Can be too verbose
                    type_completion_status[target_record_type]["succeeded"] += 1
            
            for type_key, counts in type_completion_status.items():
                if type_key in processed_record_types_summary_local:
                    summary_entry = processed_record_types_summary_local[type_key]
                    summary_entry["succeeded_sub_tasks"] = counts["succeeded"]
                    summary_entry["failed_sub_tasks"] = counts["failed"]
                    summary_entry["total_sub_tasks_run_in_gather"] = counts["total_attempted_in_gather"]
                    if counts["total_attempted_in_gather"] == 0 and summary_entry.get("tasks_prepared_for_gather",0) > 0 :
                         summary_entry["status"] = "no_sub_tasks_actually_run_in_gather_despite_preparation" # Should be rare
                    elif counts["failed"] == counts["total_attempted_in_gather"] and counts["total_attempted_in_gather"] > 0:
                        summary_entry["status"] = f"all_sub_tasks_failed ({counts['failed']}/{counts['total_attempted_in_gather']})"
                    elif counts["failed"] > 0:
                        summary_entry["status"] = f"completed_with_some_sub_task_errors ({counts['succeeded']}/{counts['total_attempted_in_gather']} succeeded)"
                    elif counts["succeeded"] == counts["total_attempted_in_gather"] and counts["total_attempted_in_gather"] > 0:
                        summary_entry["status"] = f"all_sub_tasks_completed ({counts['succeeded']}/{counts['total_attempted_in_gather']})"
                    elif counts["total_attempted_in_gather"] == 0 and summary_entry.get("tasks_prepared_for_gather",0) == 0:
                        # This state was likely already set (e.g. no_records_to_process, prefetch_error)
                        pass # Keep existing status if no tasks were even prepared for gather
                    else: # Should not be reached if logic is correct
                        summary_entry["status"] = "unknown_sub_task_completion_state"


        final_job_status = "completed"
        error_detail_for_status = None
        if not application_analysis_completed_successfully_local:
            final_job_status = "failed"
            error_detail_for_status = f"Main application analysis failed: {app_analysis_summary_local}"
        elif any_processing_errors_local: # Covers pre-fetch, processor load, or any sub-task failure
            final_job_status = "failed"
            error_detail_for_status = "Errors occurred during processing."
            if application_analysis_completed_successfully_local:
                 error_detail_for_status = "Main application analysis OK, but errors occurred in processing some related records, their setup, or prefetch."
        
        logger.info(
            f"Job ID {job_id}: BG Task: Processing logic for App ID {application_record_id} concluded. "
            f"Overall result: {final_job_status}. Main app summary: {app_analysis_summary_local}. "
            f"Related records summary: {json.dumps(processed_record_types_summary_local)}"
        )
        # The processed_record_types_summary_local can be stored with the job status if needed,
        # but update_processing_status currently only takes a single error_message string.
        # For now, it's logged. It could be added to the _application_processing_status dict.
        await update_processing_status(
            application_record_id, final_job_status, job_id, client_fingerprint, error_detail_for_status,
            # Potentially add processed_record_types_summary_local here if `update_processing_status` is extended
            # detailed_summary=processed_record_types_summary_local 
        )

    except Exception as e_bg:
        logger.error(f"Job ID {job_id}: BG Task: CRITICAL error during background processing for App ID {application_record_id}: {e_bg}", exc_info=True)
        await update_processing_status(application_record_id, "failed", job_id, client_fingerprint, f"Critical background task error: {str(e_bg)}")
    finally:
        await release_processing_slot()


@router.post("/analyze", response_model=AnalyzeApplicationResponse)
async def analyze_application_endpoint(
    fastapi_request_obj: Request,
    background_tasks: BackgroundTasks,
    request_body: AnalyzeApplicationBodyRequest = Body(...),
    sf_service: SalesforceService = Depends(get_sf_service_dependency)
):
    job_id_str = str(uuid.uuid4())
    current_time_utc_iso = datetime.now(timezone.utc).isoformat()
    client_fingerprint = generate_client_fingerprint(dict(fastapi_request_obj.headers), fastapi_request_obj.client.host if fastapi_request_obj.client else "unknown_host")

    application_input_id = request_body.record_id
    application_record_id: str
    if not (isinstance(application_input_id, str) and (len(application_input_id) == 15 or len(application_input_id) == 18)):
        if '/' in application_input_id:
            try:
                application_record_id = application_input_id.split('/')[-1]
                if not (len(application_record_id) == 15 or len(application_record_id) == 18):
                    raise ValueError("Parsed ID is not valid length.")
            except Exception:
                raise HTTPException(status_code=400, detail=f"Invalid record_id format after parsing URL: {application_input_id}")
        else:
            raise HTTPException(status_code=400, detail=f"Invalid record_id format: {application_input_id}. Must be 15/18 char Salesforce ID or path containing it.")
    else:
        application_record_id = application_input_id
    logger.info(f"Job ID {job_id_str}: Request for /analyze App ID: {application_record_id}, Client: {client_fingerprint[:8]}...")

    # --- Standard Checks (Rate Limits, Slots) ---
    check_results = [
        (await check_and_update_global_rate_limit(), 429, "Global rate limit exceeded."),
        (await check_and_update_client_rate_limit(client_fingerprint), 429, "Client rate limit exceeded."),
        (await check_rapid_fire_protection(client_fingerprint, application_record_id), 429, "Rapid fire protection: Same application requested too soon by client."),
        (await check_processing_slots(), 503, "Service busy: No available processing slots."),
    ]
    for check_output, status_code, default_reason_msg in check_results:
        allowed = check_output[0] if isinstance(check_output, tuple) else check_output
        message_from_check = check_output[1] if isinstance(check_output, tuple) and len(check_output) > 1 else default_reason_msg
        if not allowed:
            logger.warning(f"Job ID {job_id_str}: Check failed for App ID {application_record_id}. Reason: {message_from_check}")
            raise HTTPException(status_code=status_code, detail=message_from_check)

    existing_status = await get_processing_status(application_record_id)
    if existing_status:
        status_age = (datetime.now(timezone.utc) - existing_status["timestamp"]).total_seconds()
        if existing_status["status"] == "processing" and status_age < ACTIVE_PROCESSING_TIMEOUT_SECONDS:
            rejection_reason = f"Application ID {application_record_id} is currently being processed (Job ID: {existing_status.get('job_id', 'N/A')}). Status: {existing_status['status']}. Try after {ACTIVE_PROCESSING_TIMEOUT_SECONDS - status_age:.0f}s."
            raise HTTPException(status_code=409, detail=rejection_reason)
        logger.info(f"Job ID {job_id_str}: Previous status for App ID {application_record_id}: {existing_status['status']} (age: {status_age:.0f}s). Proceeding.")

    # --- Pre-fetch related records metadata for immediate response ---
    # This involves I/O, ensure sf_service methods are efficient or async_to_thread is used
    logger.info(f"Job ID {job_id_str}: Pre-fetching related records metadata for App ID {application_record_id}.")
    related_records_metadata_list, prefetched_data_for_bg = await fetch_initial_related_records_metadata(
        sf_service, application_record_id, job_id_str
    )
    logger.info(f"Job ID {job_id_str}: Pre-fetching complete. Found metadata for {len(related_records_metadata_list)} types of related records.")

    await acquire_processing_slot() # Acquire slot AFTER pre-fetch is done and we decide to proceed

    status_url = str(fastapi_request_obj.url_for('get_application_processing_status_endpoint', application_id=application_record_id))
    # Simple estimation logic based on number of items; can be refined
    total_items_to_process = 1 + sum(meta.get("count", 0) for meta in related_records_metadata_list)
    estimated_time_info = f"Processing {total_items_to_process} item(s). Estimated time: {total_items_to_process * 1.5}-{total_items_to_process * 5} minutes (varies by complexity)."


    response_payload = AnalyzeApplicationResponse(
        request_id=job_id_str, created_at=current_time_utc_iso, last_updated_at=current_time_utc_iso,
        application_record_id=application_record_id,
        message=f"Request accepted. Processing initiated for Application ID {application_record_id}. See status_url.",
        status="processing_initiated", # Status of the API request itself
        application_analysis_status="queued", # Initial status of the background job
        related_records_metadata=related_records_metadata_list,
        status_url=status_url,
        estimated_completion_info=estimated_time_info
    )

    try:
        # Initial status update for the background job
        await update_processing_status(application_record_id, "processing", job_id_str, client_fingerprint, "Background processing started (awaits all sub-tasks).")

        background_tasks.add_task(
            process_application_and_related_records_bg,
            application_record_id,
            sf_service,
            job_id_str,
            client_fingerprint,
            prefetched_data_for_bg # Pass the pre-fetched data
        )
        logger.info(f"Job ID {job_id_str}: Successfully queued background processing for App ID {application_record_id} with pre-fetched data.")

    except Exception as e: # Catch errors during the setup of background task
        await release_processing_slot() # Release slot if setup fails
        await update_processing_status(application_record_id, "failed_setup", job_id_str, client_fingerprint, f"Error during background task setup: {str(e)}")
        logger.exception(f"Job ID {job_id_str}: Unexpected server error during setup for App ID {application_record_id}.")
        # Return a more specific error than just a generic 500 if possible,
        # but for now, this covers unexpected issues during the immediate endpoint logic.
        raise HTTPException(status_code=500, detail=f"Failed to initiate processing due to an unexpected server error: {str(e)}")

    return response_payload # Returns immediately


@router.get("/status/{application_id}", tags=["Application Analysis Status"])
async def get_application_processing_status_endpoint(application_id: str):
    if not (isinstance(application_id, str) and (len(application_id) == 15 or len(application_id) == 18)):
        raise HTTPException(status_code=400, detail="Invalid application_id format. Must be a 15 or 18 character Salesforce ID.")
    
    status_info = await get_processing_status(application_id)
    if not status_info:
        raise HTTPException(status_code=404, detail=f"No processing record found for application ID: {application_id}")
    
    public_status = {k: v for k, v in status_info.items() if k != "client_fingerprint"}
    if isinstance(public_status.get("timestamp"), datetime):
        public_status["timestamp"] = public_status["timestamp"].isoformat()
        
    # If you stored detailed_summary in update_processing_status, you could retrieve it here
    # public_status["processed_record_types_summary"] = status_info.get("detailed_summary", {})

    return public_status

@router.get("/queue-overview", tags=["Application Analysis Status"])
async def get_processing_queue_overview_endpoint():
    all_statuses = await get_all_processing_statuses()
    active_slots = await get_active_processing_slots_count()
    
    now = datetime.now(timezone.utc)
    queue_details = []
    for app_id, data in all_statuses.items():
        age_seconds = -1
        timestamp_val = data.get("timestamp")
        if isinstance(timestamp_val, datetime): # Ensure it's a datetime object
            age_seconds = (now - timestamp_val).total_seconds()
        
        queue_details.append({
            "application_id": app_id,
            "status": data.get("status"),
            "job_id": data.get("job_id"),
            "age_seconds": int(age_seconds),
            "client_fingerprint_hash_prefix": (data.get("client_fingerprint", "")[:8] + "..." if data.get("client_fingerprint") else "N/A"),
            "error_message": data.get("error_message")
            # "details": data.get("detailed_summary") # If you add this to the status
        })
        
    return {
        "active_processing_slots": active_slots,
        "max_concurrent_slots": MAX_CONCURRENT_PROCESSING_SLOTS,
        "slots_available": MAX_CONCURRENT_PROCESSING_SLOTS - active_slots,
        "tracked_applications_count": len(queue_details),
        "applications_overview": sorted(queue_details, key=lambda x: x['age_seconds'], reverse=True)
    }