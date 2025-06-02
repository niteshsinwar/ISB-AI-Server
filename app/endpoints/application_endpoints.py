import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple
import asyncio
import importlib 

from fastapi import APIRouter, HTTPException, Body, Depends, BackgroundTasks, Request
from pydantic import BaseModel, Field

from app.services.salesforce_service import SalesforceService, get_sf_service_dependency
from app.processors.application_processor import process_single_application_detail
from app.config import (
    RELATED_RECORD_PROCESSING_CONFIG,
    APPLICATION_OBJECT_API_NAME,
    MAX_CONCURRENT_PROCESSING_SLOTS,
    ACTIVE_PROCESSING_TIMEOUT_SECONDS # Added import from config
)
from app.core.rate_limit_state import (
    generate_client_fingerprint,
    check_and_update_global_rate_limit, check_and_update_client_rate_limit,
    check_rapid_fire_protection, check_processing_slots,
    acquire_processing_slot, release_processing_slot,
    update_processing_status, get_processing_status,
    get_all_processing_statuses, get_active_processing_slots_count
)
from app.core.app_instance import get_app_instance


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
    status: str
    application_analysis_status: Optional[str] = None
    processed_record_types: Optional[Dict[str, Any]] = Field(default_factory=dict)
    error: Optional[str] = None


async def process_application_and_related_records_bg(
    application_record_id: str,
    sf_service_instance: SalesforceService,
    job_id: str,
    client_fingerprint: str,
    background_tasks_manager: BackgroundTasks 
):
    """
    Core background logic for processing an application and its related records.
    Passes item_index to related record processors.
    """
    any_processing_errors_local = False
    application_analysis_completed_successfully_local = False
    app_analysis_summary_local = "Processing error or not run."
    processed_record_types_summary_local: Dict[str, Any] = {}

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
        
        # Process related records only if main application processing was okay, or based on specific business logic
        # if application_analysis_completed_successfully_local: # Control if related records depend on main app success
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
                        parent_object_api_name=APPLICATION_OBJECT_API_NAME,
                        child_object_api_name=target_record_type,
                        lookup_field_on_child_to_parent=config_item["lookup_on_child_to_parent"]
                    )
                elif retrieval_method == "via_junction":
                    related_ids = sf_service_instance.get_target_ids_via_junction(
                        parent_record_id=application_record_id,
                        parent_object_api_name=APPLICATION_OBJECT_API_NAME,
                        junction_object_api_name=config_item["junction_object"],
                        junction_field_to_parent=config_item["junction_field_to_parent"],
                        junction_field_to_target=config_item["junction_field_to_target"]
                    )
                
                processed_record_types_summary_local[target_record_type] = {"count": len(related_ids), "status": "fetched_ids", "ids_found": related_ids[:5]}
                if not related_ids:
                    processed_record_types_summary_local[target_record_type]["status"] = "no_records_found"
                else:
                    module = importlib.import_module(processor_module_name)
                    processor_func = getattr(module, processor_function_name)

                    for idx, r_id in enumerate(related_ids): # Get 0-based index
                        background_tasks_manager.add_task(
                            processor_func,
                            sf_service_instance,
                            r_id, # ID of the related record (e.g., Education_History__c ID)
                            application_record_id, # Parent application ID
                            item_index=(idx + 1)   # Pass 1-based index for naming
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
            final_job_status = "failed" 
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
    fastapi_request_obj: Request, 
    background_tasks: BackgroundTasks,
    request_body: AnalyzeApplicationBodyRequest = Body(...),
    sf_service: SalesforceService = Depends(get_sf_service_dependency)
):
    job_id_str = str(uuid.uuid4())
    current_time_utc_iso = datetime.now(timezone.utc).isoformat()
    
    client_fingerprint = generate_client_fingerprint(dict(fastapi_request_obj.headers), fastapi_request_obj.client.host if fastapi_request_obj.client else None) # type: ignore
    
    application_input_id = request_body.record_id
    application_record_id: str

    if not (isinstance(application_input_id, str) and (len(application_input_id) == 15 or len(application_input_id) == 18)):
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

    checks = [
        (await check_and_update_global_rate_limit(), 429, "Global rate limit exceeded."),
        (await check_and_update_client_rate_limit(client_fingerprint), 429, "Client rate limit exceeded."),
        (await check_rapid_fire_protection(client_fingerprint, application_record_id), 429, "Rapid fire protection: Same application requested too soon by client."),
        (await check_processing_slots(), 503, "Service busy: No available processing slots."),
    ]
    for allowed, status_code, reason_msg in checks: # type: ignore
        if not allowed:
            logger.warning(f"Job ID {job_id_str}: Check failed for App ID {application_record_id}. Reason: {reason_msg}")
            raise HTTPException(status_code=status_code, detail=reason_msg)


    existing_status = await get_processing_status(application_record_id)
    if existing_status:
        status_age = (datetime.now(timezone.utc) - existing_status["timestamp"]).total_seconds()
        if existing_status["status"] == "processing" and status_age < ACTIVE_PROCESSING_TIMEOUT_SECONDS:
            rejection_reason = f"Application ID {application_record_id} is currently being processed (Job ID: {existing_status.get('job_id', 'N/A')}). Please try again after timeout ({ACTIVE_PROCESSING_TIMEOUT_SECONDS}s) or when status changes."
            logger.warning(f"Job ID {job_id_str}: Duplicate processing attempt for App ID {application_record_id} within active timeout. {rejection_reason}")
            raise HTTPException(status_code=409, detail=rejection_reason)
        logger.info(f"Job ID {job_id_str}: Previous status for App ID {application_record_id}: {existing_status['status']} (age: {status_age:.0f}s). Proceeding with new request as it's not actively processing or timed out.")


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

    except Exception as e: 
        await release_processing_slot() 
        await update_processing_status(application_record_id, "failed_setup", job_id_str, client_fingerprint, f"Setup error: {str(e)}")
        logger.exception(f"Job ID {job_id_str}: Unexpected server error during setup for App ID {application_record_id}.")
        # Modify response_payload before raising HTTPException or return it directly with an error status in the payload
        # For consistency with other error handling, raising HTTPException is clearer.
        raise HTTPException(status_code=500, detail=f"Failed to initiate processing due to an unexpected server error: {str(e)}")
    
    response_payload.last_updated_at = datetime.now(timezone.utc).isoformat()
    return response_payload


@router.get("/status/{application_id}", tags=["Application Analysis Status"])
async def get_application_processing_status_endpoint(application_id: str):
    if not (len(application_id) == 15 or len(application_id) == 18): # type: ignore
        raise HTTPException(status_code=400, detail="Invalid application_id format.")
    
    status_info = await get_processing_status(application_id)
    if not status_info:
        raise HTTPException(status_code=404, detail=f"No processing record found for application ID: {application_id}")
    
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
        "max_concurrent_slots": MAX_CONCURRENT_PROCESSING_SLOTS,
        "slots_available": MAX_CONCURRENT_PROCESSING_SLOTS - active_slots,
        "tracked_applications_count": len(queue_details),
        "applications_overview": sorted(queue_details, key=lambda x: x['age_seconds'], reverse=True) # Show newest first or oldest
    }