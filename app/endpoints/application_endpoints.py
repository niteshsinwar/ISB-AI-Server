# project_root/app/endpoints/application_endpoints.py
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple
import asyncio
import importlib # For dynamically importing processor functions

from fastapi import APIRouter, HTTPException, Body, Depends, BackgroundTasks, Request
from pydantic import BaseModel, Field

from app.services.salesforce_service import SalesforceService, get_sf_service_dependency
from app.processors.application_processor import process_single_application_detail
# Processors for related records will be dynamically imported based on config
from app.config import (
    RELATED_RECORD_PROCESSING_CONFIG,
    APPLICATION_OBJECT_API_NAME, # Used for the main application processing
    MAX_CONCURRENT_PROCESSING_SLOTS
)
from app.core.rate_limit_state import (
    generate_client_fingerprint,
    check_and_update_global_rate_limit, check_and_update_client_rate_limit,
    check_rapid_fire_protection, check_processing_slots,
    acquire_processing_slot, release_processing_slot,
    update_processing_status, get_processing_status,
    get_all_processing_statuses, get_active_processing_slots_count
)
# Import app instance getter from app.main
from app.core.app_instance import get_app_instance


logger = logging.getLogger(__name__)
router = APIRouter()

# --- Pydantic Models for active endpoints ---
class AnalyzeApplicationBodyRequest(BaseModel):
    record_id: str = Field(..., description="The 15 or 18 character ID of the Salesforce Application__c record.")

class AnalyzeApplicationResponse(BaseModel):
    request_id: str = Field(serialization_alias="_id") # Job ID
    created_at: str
    last_updated_at: str
    application_record_id: str
    message: str
    status: str # e.g., "processing_initiated", "failed_validation"
    application_analysis_status: Optional[str] = None # More detailed status of the main app analysis
    processed_record_types: Optional[Dict[str, Any]] = Field(default_factory=dict)
    error: Optional[str] = None


async def process_application_and_related_records_bg(
    application_record_id: str,
    sf_service_instance: SalesforceService,
    job_id: str,
    client_fingerprint: str,
    background_tasks_manager: BackgroundTasks # To queue sub-tasks for related records
):
    """
    Core background logic for processing an application and its related records.
    """
    any_processing_errors_local = False
    application_analysis_completed_successfully_local = False
    app_analysis_summary_local = "Processing error or not run."
    processed_record_types_summary_local: Dict[str, Any] = {}

    try:
        logger.info(f"Job ID {job_id}: BG Task: Starting main application detail analysis for App ID: {application_record_id} (Client: {client_fingerprint[:8]}...)")
        
        # Process the main application record (e.g., hed__Application__c)
        app_analysis_summary_local = await process_single_application_detail(
            sf_service=sf_service_instance,
            application_id=application_record_id,
            # For the main application, parent_application_id is the same as application_id
            # This parameter is more for context in related record processors.
            parent_application_id=application_record_id,
            # Pass the SObject API name for the main application from config
            application_object_api_name=APPLICATION_OBJECT_API_NAME
        )
        logger.info(f"Job ID {job_id}: BG Task: Main application analysis completed for App ID {application_record_id}. Summary: {app_analysis_summary_local}")
        
        if "Error:" in app_analysis_summary_local or "Failed" in app_analysis_summary_local: # Broader check for failure indicators
            any_processing_errors_local = True
        else:
            application_analysis_completed_successfully_local = True
        
        # Process related records based on RELATED_RECORD_PROCESSING_CONFIG
        tasks_initiated_count_local = 0
        for config_item in RELATED_RECORD_PROCESSING_CONFIG:
            target_record_type = config_item["target_record_type"]
            retrieval_method = config_item["retrieval_method"]
            processor_module_name = config_item["processor_module"]
            processor_function_name = config_item["processor_function_name"]
            
            related_ids: List[str] = []
            current_type_task_count = 0

            logger.info(f"Job ID {job_id}: BG Task: Fetching related '{target_record_type}' records for App ID {application_record_id} using method '{retrieval_method}'.")
            try:
                if retrieval_method == "direct":
                    related_ids = sf_service_instance.get_directly_related_record_ids(
                        parent_record_id=application_record_id,
                        parent_object_api_name=APPLICATION_OBJECT_API_NAME, # Parent is Application__c
                        child_object_api_name=target_record_type,
                        lookup_field_on_child_to_parent=config_item["lookup_on_child_to_parent"]
                    )
                elif retrieval_method == "via_junction":
                    related_ids = sf_service_instance.get_target_ids_via_junction(
                        parent_record_id=application_record_id,
                        parent_object_api_name=APPLICATION_OBJECT_API_NAME, # Parent is Application__c
                        junction_object_api_name=config_item["junction_object"],
                        junction_field_to_parent=config_item["junction_field_to_parent"],
                        junction_field_to_target=config_item["junction_field_to_target"]
                    )
                
                processed_record_types_summary_local[target_record_type] = {"count": len(related_ids), "status": "fetched_ids", "ids_found": related_ids[:5]} # Log first 5 IDs
                if not related_ids:
                    processed_record_types_summary_local[target_record_type]["status"] = "no_records_found"
                else:
                    # Dynamically import the processor function
                    module = importlib.import_module(processor_module_name)
                    processor_func = getattr(module, processor_function_name)

                    for r_id in related_ids:
                        background_tasks_manager.add_task(
                            processor_func,
                            sf_service_instance,
                            r_id,
                            application_record_id # Parent application ID for context
                        )
                        current_type_task_count += 1
                    tasks_initiated_count_local += current_type_task_count
                    processed_record_types_summary_local[target_record_type]["tasks_queued"] = current_type_task_count
                    processed_record_types_summary_local[target_record_type]["status"] = "tasks_queued"
            
            except (ValueError, RuntimeError, AttributeError, ImportError) as e_inner:
                 logger.error(f"Job ID {job_id}: BG Task: Error during ID retrieval or processor loading for {target_record_type} (App {application_record_id}): {e_inner}", exc_info=True)
                 processed_record_types_summary_local[target_record_type] = {"count": 0, "status": f"Error: {str(e_inner)[:100]}"}
                 any_processing_errors_local = True
        
        final_job_status = "completed"
        error_detail_for_status = None
        if any_processing_errors_local or not application_analysis_completed_successfully_local:
            final_job_status = "failed" # Can be 'partially_completed' if needed
            error_detail_for_status = app_analysis_summary_local if not application_analysis_completed_successfully_local else "Errors in related record processing."
        
        logger.info(f"Job ID {job_id}: BG Task: Processing logic for App ID {application_record_id} concluded. Overall result: {final_job_status}. Main app summary: {app_analysis_summary_local}. Related records summary: {processed_record_types_summary_local}")
        await update_processing_status(application_record_id, final_job_status, job_id, client_fingerprint, error_detail_for_status)

    except Exception as e_bg:
        logger.error(f"Job ID {job_id}: BG Task: CRITICAL error during background processing for App ID {application_record_id}: {e_bg}", exc_info=True)
        await update_processing_status(application_record_id, "failed", job_id, client_fingerprint, f"Critical background task error: {str(e_bg)}")
    finally:
        await release_processing_slot()


@router.post("/analyze", response_model=AnalyzeApplicationResponse)
async def analyze_application_endpoint(
    fastapi_request_obj: Request, # Renamed to avoid conflict with pydantic 'Request'
    background_tasks: BackgroundTasks,
    request_body: AnalyzeApplicationBodyRequest = Body(...),
    sf_service: SalesforceService = Depends(get_sf_service_dependency) # Use the new dependency
):
    job_id_str = str(uuid.uuid4())
    current_time_utc_iso = datetime.now(timezone.utc).isoformat()
    
    client_fingerprint = generate_client_fingerprint(dict(fastapi_request_obj.headers), fastapi_request_obj.client.host if fastapi_request_obj.client else None)
    
    application_input_id = request_body.record_id
    application_record_id: str

    if not (isinstance(application_input_id, str) and (len(application_input_id) == 15 or len(application_input_id) == 18)):
        # Check if it's a path and try to extract ID
        if '/' in application_input_id:
            try:
                application_record_id = application_input_id.split('/')[-1]
                if not (len(application_record_id) == 15 or len(application_record_id) == 18):
                    raise ValueError("Parsed ID is not valid length.")
                logger.info(f"Job ID {job_id_str}: Parsed App ID {application_record_id} from input {application_input_id}")
            except Exception:
                logger.error(f"Job ID {job_id_str}: Invalid record_id format after parsing: {application_input_id}")
                raise HTTPException(status_code=400, detail=f"Invalid record_id format. Expected Salesforce ID, got: {application_input_id}")
        else:
            logger.error(f"Job ID {job_id_str}: Invalid record_id format: {application_input_id}")
            raise HTTPException(status_code=400, detail=f"Invalid record_id format. Must be 15/18 char Salesforce ID or path containing it.")
    else:
        application_record_id = application_input_id
        
    logger.info(f"Job ID {job_id_str}: Request for /analyze App ID: {application_record_id}, Client: {client_fingerprint[:8]}...")

    # --- Rate Limiting and Duplicate Checks ---
    checks = [
        (await check_and_update_global_rate_limit(), 429),
        (await check_and_update_client_rate_limit(client_fingerprint), 429),
        (await check_rapid_fire_protection(client_fingerprint, application_record_id), 429),
        (await check_processing_slots(), 503),
    ]
    for (allowed, reason), status_code in checks:
        if not allowed:
            logger.warning(f"Job ID {job_id_str}: Check failed for App ID {application_record_id}. Reason: {reason}")
            raise HTTPException(status_code=status_code, detail=reason)

    existing_status = await get_processing_status(application_record_id)
    if existing_status:
        status_age = (datetime.now(timezone.utc) - existing_status["timestamp"]).total_seconds()
        # ACTIVE_PROCESSING_TIMEOUT_SECONDS is imported from config (via rate_limit_state)
        if existing_status["status"] == "processing" and status_age < ACTIVE_PROCESSING_TIMEOUT_SECONDS: # Use config
            rejection_reason = f"Application ID {application_record_id} is currently being processed (Job ID: {existing_status.get('job_id', 'N/A')}). Please try again later."
            logger.warning(f"Job ID {job_id_str}: Duplicate processing attempt for App ID {application_record_id}.")
            raise HTTPException(status_code=409, detail=rejection_reason)
        logger.info(f"Job ID {job_id_str}: Previous status for App ID {application_record_id}: {existing_status['status']}. Proceeding with new request.")

    # --- All checks passed - Proceed ---
    await acquire_processing_slot()
    
    response_payload = AnalyzeApplicationResponse(
        request_id=job_id_str, created_at=current_time_utc_iso, last_updated_at=current_time_utc_iso,
        application_record_id=application_record_id,
        message=f"Request accepted. Processing initiated for Application ID {application_record_id} with Job ID {job_id_str}.",
        status="processing_initiated",
        application_analysis_status="Background processing queued."
    )

    try:
        await update_processing_status(application_record_id, "processing", job_id_str, client_fingerprint, "Marked for background processing.")
        
        background_tasks.add_task(
            process_application_and_related_records_bg,
            application_record_id, sf_service, job_id_str, client_fingerprint, background_tasks
        )
        logger.info(f"Job ID {job_id_str}: Successfully queued background processing for App ID {application_record_id}.")

    except Exception as e: # Catch errors during the setup of the background task
        await release_processing_slot() # Ensure slot is released if setup fails
        await update_processing_status(application_record_id, "failed_setup", job_id_str, client_fingerprint, f"Setup error: {str(e)}")
        logger.exception(f"Job ID {job_id_str}: Unexpected server error during setup for App ID {application_record_id}.")
        response_payload.status = "failed_server_error"
        response_payload.message = "Failed to initiate processing due to an unexpected server error."
        response_payload.error = str(e)
        # Do not raise HTTPException here if we want to return the payload with error info.
        # Or, if we must raise, ensure the client understands this specific error.
        # For now, let's return the modified payload.
        # To make it an error response, you'd set fastapi_request_obj.state.status_code = 500 or similar
        # and then return the payload, or just raise HTTPException.
        # Let's be explicit with HTTPException for server errors.
        raise HTTPException(status_code=500, detail=response_payload.model_dump(exclude_none=True, by_alias=True))
    
    response_payload.last_updated_at = datetime.now(timezone.utc).isoformat()
    return response_payload


@router.get("/status/{application_id}", tags=["Application Analysis Status"])
async def get_application_processing_status_endpoint(application_id: str):
    if not (len(application_id) == 15 or len(application_id) == 18):
        raise HTTPException(status_code=400, detail="Invalid application_id format.")
    
    status_info = await get_processing_status(application_id)
    if not status_info:
        raise HTTPException(status_code=404, detail=f"No processing record found for application ID: {application_id}")
    
    # Exclude client_fingerprint from public response
    public_status = {k: v for k, v in status_info.items() if k != "client_fingerprint"}
    if isinstance(public_status.get("timestamp"), datetime):
        public_status["timestamp"] = public_status["timestamp"].isoformat()
        
    return public_status

@router.get("/queue-overview", tags=["Application Analysis Status"])
async def get_processing_queue_overview_endpoint():
    all_statuses = await get_all_processing_statuses()
    active_slots = await get_active_processing_slots_count()
    
    now = datetime.now(timezone.utc)
    queue_details = []
    for app_id, data in all_statuses.items():
        age_seconds = (now - data["timestamp"]).total_seconds() if isinstance(data.get("timestamp"), datetime) else -1
        queue_details.append({
            "application_id": app_id,
            "status": data.get("status"),
            "job_id": data.get("job_id"),
            "age_seconds": int(age_seconds),
            "client_fingerprint_hash_prefix": data.get("client_fingerprint", "")[:8] + "..." if data.get("client_fingerprint") else "N/A",
            "error_message": data.get("error_message")
        })
        
    return {
        "active_processing_slots": active_slots,
        "max_concurrent_slots": MAX_CONCURRENT_PROCESSING_SLOTS, # from config
        "slots_available": MAX_CONCURRENT_PROCESSING_SLOTS - active_slots,
        "tracked_applications_count": len(queue_details),
        "applications_overview": queue_details
    }
