# project_root/app/endpoints/main_functional_endpoint.py
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple
import asyncio
import importlib
import json
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Body, Depends, BackgroundTasks, Request

from app.services.salesforce_service import SalesforceService, get_sf_service_dependency
from app.processors.application_processor import process_single_application_detail
from app.config import (
    RELATED_RECORD_PROCESSING_CONFIG,
    APPLICATION_OBJECT_API_NAME,
    MAX_CONCURRENT_PROCESSING_SLOTS,
    ACTIVE_PROCESSING_TIMEOUT_SECONDS
)
from app.core.rate_limit_state import (
    generate_client_fingerprint, check_and_update_global_rate_limit,
    check_and_update_client_rate_limit, check_rapid_fire_protection,
    check_processing_slots, acquire_processing_slot, release_processing_slot,
    update_processing_status, get_processing_status, get_all_processing_statuses
)
from app.schemas.responses import (
    AnalyzeApplicationBodyRequest, AnalyzeApplicationResponse, JobStatusResponse,
    QueueOverviewResponse, RelatedRecordMetadata, EstimatedCompletion
)

logger = logging.getLogger(__name__)
router = APIRouter()


async def fetch_initial_related_records_metadata(
    sf_service: SalesforceService,
    application_record_id: str,
    job_id: str
) -> Tuple[List[RelatedRecordMetadata], Dict[str, Dict[str, Any]]]:
    metadata_for_api_response: List[RelatedRecordMetadata] = []
    data_for_background_task: Dict[str, Dict[str, Any]] = {}

    for config_item in RELATED_RECORD_PROCESSING_CONFIG:
        target_type = config_item["target_record_type"]
        retrieval_method = config_item["retrieval_method"]
        related_ids: List[str] = []
        fetch_error_str: str | None = None
        status = "pending_fetch"

        try:
            if retrieval_method == "direct":
                related_ids = await asyncio.to_thread(
                    sf_service.get_directly_related_record_ids,
                    parent_record_id=application_record_id,
                    parent_object_api_name=APPLICATION_OBJECT_API_NAME,
                    child_object_api_name=target_type,
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
            status = "ids_fetched" if related_ids else "no_records_found"
        except Exception as e:
            logger.error(f"Job {job_id}: Pre-fetch error for '{target_type}': {e}", exc_info=True)
            fetch_error_str = str(e)
            status = f"fetch_error: {fetch_error_str[:100]}"
        
        metadata_for_api_response.append(RelatedRecordMetadata(
            target_record_type=target_type,
            retrieval_method=retrieval_method,
            count=len(related_ids),
            status=status,
            sample_ids=related_ids[:5]
        ))
        data_for_background_task[target_type] = {
            "ids": related_ids,
            "processor_module": config_item["processor_module"],
            "processor_function_name": config_item["processor_function_name"],
            "config_item_original": config_item,
            "error_during_prefetch": fetch_error_str
        }
    return metadata_for_api_response, data_for_background_task


async def process_application_and_related_records_bg(
    application_record_id: str,
    sf_service_instance: SalesforceService,
    job_id: str,
    client_fingerprint: str,
    prefetched_related_data: Dict[str, Dict[str, Any]]
):
    """
    Core background logic with restored functionality.
    """
    any_processing_errors_local = False
    application_analysis_completed_successfully_local = False
    app_analysis_summary_local = "Processing error or not run."
    processed_record_types_summary_local: Dict[str, Any] = {}
    related_processing_tasks: List[asyncio.Task] = []

    try:
        logger.info(f"Job {job_id}: BG Task: Starting main application detail analysis for App ID: {application_record_id}")
        app_analysis_summary_local = await process_single_application_detail(
            sf_service=sf_service_instance,
            application_id=application_record_id,
            parent_application_id=application_record_id,
            application_object_api_name=APPLICATION_OBJECT_API_NAME
        )
        logger.info(f"Job {job_id}: BG Task: Main application analysis completed. Summary: {app_analysis_summary_local}")

        if "Error:" in app_analysis_summary_local or "Failed" in app_analysis_summary_local:
            any_processing_errors_local = True
        else:
            application_analysis_completed_successfully_local = True

        # Process related records using prefetched data
        for target_record_type, processor_data in prefetched_related_data.items():
            related_ids = processor_data.get("ids", [])
            processor_module_name = processor_data["processor_module"]
            processor_function_name = processor_data["processor_function_name"]
            
            processed_record_types_summary_local[target_record_type] = {
                "count_prefetched": len(related_ids),
                "status": "pending_processing",
                "succeeded": 0,
                "failed": 0
            }

            if processor_data.get("error_during_prefetch"):
                processed_record_types_summary_local[target_record_type]["status"] = "skipped_due_to_prefetch_error"
                any_processing_errors_local = True
                continue

            if not related_ids:
                processed_record_types_summary_local[target_record_type]["status"] = "no_records_to_process"
                continue
            
            try:
                module = importlib.import_module(processor_module_name)
                processor_func = getattr(module, processor_function_name)
                
                for idx, r_id in enumerate(related_ids):
                    coro = processor_func(sf_service_instance, r_id, application_record_id, item_index=(idx + 1))
                    related_processing_tasks.append(asyncio.create_task(coro))
                
                processed_record_types_summary_local[target_record_type]["status"] = "sub_tasks_created"

            except (AttributeError, ImportError) as e_load:
                logger.error(f"Job {job_id}: Error loading processor for {target_record_type}: {e_load}", exc_info=True)
                processed_record_types_summary_local[target_record_type]["status"] = "processor_load_error"
                any_processing_errors_local = True

        if related_processing_tasks:
            logger.info(f"Job {job_id}: BG Task: Awaiting {len(related_processing_tasks)} related record processing sub-tasks.")
            results = await asyncio.gather(*related_processing_tasks, return_exceptions=True)
            logger.info(f"Job {job_id}: BG Task: All related sub-tasks finished.")
            # This is a simplified result processing. A real implementation would map results back to types.
            for result in results:
                if isinstance(result, Exception):
                    any_processing_errors_local = True

        # Determine final job status
        final_job_status = "completed"
        error_detail_for_status = app_analysis_summary_local
        if not application_analysis_completed_successfully_local:
            final_job_status = "failed"
            error_detail_for_status = f"Main application analysis failed: {app_analysis_summary_local}"
        elif any_processing_errors_local:
            final_job_status = "failed"
            error_detail_for_status = "Errors occurred during processing of related records."

        logger.info(f"Job {job_id}: BG Task: Concluded for App ID {application_record_id}. Overall result: {final_job_status}.")
        
        await update_processing_status(
            application_record_id, final_job_status, job_id, client_fingerprint,
            error_detail_for_status, detailed_summary=processed_record_types_summary_local
        )

    except Exception as e_bg:
        logger.error(f"Job {job_id}: BG Task: CRITICAL error for App ID {application_record_id}: {e_bg}", exc_info=True)
        await update_processing_status(application_record_id, "failed", job_id, client_fingerprint, f"Critical background task error: {str(e_bg)}")
    finally:
        await release_processing_slot()


@router.post("/analyze",
             response_model=AnalyzeApplicationResponse,
             summary="Submit an Application for Analysis",
             tags=["Application Analysis"])
async def analyze_application_endpoint(
    fastapi_request_obj: Request,
    background_tasks: BackgroundTasks,
    request_body: AnalyzeApplicationBodyRequest = Body(...),
    sf_service: SalesforceService = Depends(get_sf_service_dependency)
):
    job_id_str = str(uuid.uuid4())
    client_fingerprint = generate_client_fingerprint(dict(fastapi_request_obj.headers), fastapi_request_obj.client.host if fastapi_request_obj.client else "unknown_host")
    
    # --- Input Validation and ID Parsing ---
    application_input_id = request_body.record_id
    if '/' in application_input_id:
        application_record_id = application_input_id.split('/')[-1]
    else:
        application_record_id = application_input_id

    if not (isinstance(application_record_id, str) and (len(application_record_id) == 15 or len(application_record_id) == 18)):
        raise HTTPException(status_code=400, detail=f"Invalid record_id format after parsing: {application_input_id}")
    
    logger.info(f"Job {job_id_str}: Request for /analyze App ID: {application_record_id}")
    
    # --- Pre-computation and Checks ---
    check_results = [
        (await check_and_update_global_rate_limit(), 429, "Global rate limit exceeded."),
        (await check_and_update_client_rate_limit(client_fingerprint), 429, "Client rate limit exceeded."),
        (await check_rapid_fire_protection(client_fingerprint, application_record_id), 429, "Rapid fire protection."),
        (await check_processing_slots(), 503, "Service busy: No available processing slots."),
    ]
    for check_output, status_code, default_reason in check_results:
        allowed, message = (check_output[0], check_output[1]) if isinstance(check_output, tuple) else (check_output, default_reason)
        if not allowed:
            raise HTTPException(status_code=status_code, detail=message)

    existing_status = await get_processing_status(application_record_id)
    if existing_status:
        status_age = (datetime.now(timezone.utc) - existing_status["timestamp"]).total_seconds()
        if existing_status["status"] == "processing" and status_age < ACTIVE_PROCESSING_TIMEOUT_SECONDS:
            raise HTTPException(status_code=409, detail=f"Application ID {application_record_id} is already being processed.")

    related_records_metadata_list, prefetched_data_for_bg = await fetch_initial_related_records_metadata(
        sf_service, application_record_id, job_id_str
    )

    await acquire_processing_slot()

    status_url = str(fastapi_request_obj.url_for('get_application_processing_status_endpoint', application_id=application_record_id))
    
    total_items = 1 + sum(meta.count for meta in related_records_metadata_list)
    estimated_comp = EstimatedCompletion(
        total_items=total_items,
        min_seconds=int(total_items * 90),
        max_seconds=int(total_items * 300),
        human_readable=f"Approx. {total_items * 1.5:.1f} - {total_items * 5:.1f} minutes"
    )

    await update_processing_status(application_record_id, "processing", job_id_str, client_fingerprint, "Background processing queued.")

    background_tasks.add_task(
        process_application_and_related_records_bg,
        application_record_id, sf_service, job_id_str, client_fingerprint, prefetched_data_for_bg
    )

    return AnalyzeApplicationResponse(
        _id=job_id_str,
        application_record_id=application_record_id,
        status="processing_initiated",
        message="Request accepted. Processing initiated.",
        status_url=status_url,
        created_at=datetime.now(timezone.utc),
        related_records_metadata=related_records_metadata_list,
        estimated_completion=estimated_comp
    )


@router.get("/status/{application_id}",
            response_model=JobStatusResponse,
            summary="Get Detailed Job Status",
            tags=["Application Analysis Status"])
async def get_application_processing_status_endpoint(application_id: str):
    if not (isinstance(application_id, str) and (len(application_id) == 15 or len(application_id) == 18)):
        raise HTTPException(status_code=400, detail="Invalid application_id format.")
    
    status_info = await get_processing_status(application_id)
    if not status_info:
        raise HTTPException(status_code=404, detail=f"No processing record found for application ID: {application_id}")
    
    return JobStatusResponse(
        job_id=status_info.get('job_id', 'N/A'),
        application_id=application_id,
        status=status_info.get('status', 'unknown'),
        message=status_info.get('error_message'),
        created_at=status_info.get('created_at', status_info.get('timestamp')),
        last_updated_at=status_info.get('timestamp'),
        progress=status_info.get('detailed_summary')
    )


@router.get("/queue-overview",
            response_model=QueueOverviewResponse,
            summary="Get System Workload Overview",
            tags=["Application Analysis Status"])
async def get_processing_queue_overview_endpoint():
    all_statuses = await get_all_processing_statuses()
    status_counts = defaultdict(int)
    failed_jobs = []
    
    for app_id, data in all_statuses.items():
        status = data.get("status", "unknown")
        status_counts[status] += 1
        if status == "failed":
            failed_jobs.append(JobStatusResponse(
                job_id=data.get('job_id', 'N/A'),
                application_id=app_id,
                status=status,
                message=data.get('error_message'),
                created_at=data.get('created_at', data.get('timestamp')),
                last_updated_at=data.get('timestamp'),
                progress=data.get('detailed_summary')
            ))

    active_jobs = status_counts.get("processing", 0)
    slot_util = {
        "active_slots": active_jobs,
        "max_slots": MAX_CONCURRENT_PROCESSING_SLOTS,
        "load_percent": round((active_jobs / MAX_CONCURRENT_PROCESSING_SLOTS) * 100, 2) if MAX_CONCURRENT_PROCESSING_SLOTS > 0 else 0
    }
    
    recent_failed = sorted(failed_jobs, key=lambda j: j.last_updated_at, reverse=True)[:5]

    return QueueOverviewResponse(
        active_jobs=active_jobs,
        tracked_jobs_total=len(all_statuses),
        status_counts=dict(status_counts),
        slot_utilization=slot_util,
        recent_failed_jobs=recent_failed
    )
