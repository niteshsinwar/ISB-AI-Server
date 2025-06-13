# project_root/app/endpoints/application_endpoints.py
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple
import asyncio
import importlib
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Body, Depends, BackgroundTasks, Request

from app.services.salesforce_service import SalesforceService, get_sf_service_dependency
from app.config import (
    RELATED_RECORD_PROCESSING_CONFIG,
    APPLICATION_OBJECT_API_NAME,
    MAX_CONCURRENT_PROCESSING_SLOTS
)
from app.core.rate_limit_state import (
    generate_client_fingerprint, check_and_update_global_rate_limit,
    check_and_update_client_rate_limit, check_rapid_fire_protection,
    acquire_processing_slot, release_processing_slot,
    update_processing_status, get_processing_status, get_all_processing_statuses,
    get_active_processing_slots_count
)
from app.schemas.responses import (
    AnalyzeApplicationBodyRequest, AnalyzeApplicationResponse, JobStatusResponse,
    QueueOverviewResponse, RelatedRecordMetadata, EstimatedCompletion
)

logger = logging.getLogger(__name__)
router = APIRouter()


async def fetch_initial_related_records_metadata(
    sf_service: SalesforceService,
    application_record_id: str
) -> Tuple[List[RelatedRecordMetadata], Dict[str, Dict[str, Any]]]:
    metadata_list: List[RelatedRecordMetadata] = []
    data_for_bg: Dict[str, Dict[str, Any]] = {}
    
    for config in RELATED_RECORD_PROCESSING_CONFIG:
        target_type = config["target_record_type"]
        retrieval_method = config["retrieval_method"] # Get the retrieval method
        ids, error = [], None
        try:
            ids = await asyncio.to_thread(
                sf_service.get_directly_related_record_ids,
                parent_record_id=application_record_id,
                parent_object_api_name=APPLICATION_OBJECT_API_NAME,
                child_object_api_name=target_type,
                lookup_field_on_child_to_parent=config["lookup_on_child_to_parent"]
            )
        except Exception as e:
            error = str(e)
        
        metadata_list.append(RelatedRecordMetadata(
            target_record_type=target_type,
            retrieval_method=retrieval_method,
            count=len(ids),
            status="fetch_error" if error else "ids_fetched",
            sample_ids=ids[:5]
        ))
        data_for_bg[target_type] = {
            "ids": ids, "processor_module": config["processor_module"],
            "processor_function_name": config["processor_function_name"]
        }
    return metadata_list, data_for_bg


async def process_application_and_related_records_bg(
    app_id: str, sf: SalesforceService, job_id: str, client_fp: str, prefetched_data: Dict[str, Dict[str, Any]]
):
    from app.processors.application_processor import process_single_application_detail
    
    await acquire_processing_slot()
    progress = {}

    try:
        # Step 1: Process Main Application
        progress["main_application"] = {"status": "processing", "details": "Starting main application analysis."}
        await update_processing_status(app_id, "processing", job_id, client_fingerprint=client_fp, progress=progress)
        
        app_summary = await process_single_application_detail(
            sf_service=sf, application_id=app_id, parent_application_id=app_id,
            application_object_api_name=APPLICATION_OBJECT_API_NAME
        )
        progress["main_application"] = {"status": "completed", "details": app_summary}
        await update_processing_status(app_id, "processing", job_id, progress=progress)

        # Step 2: Process Related Records
        for record_type, data in prefetched_data.items():
            if not data.get("ids"):
                progress[record_type] = {"status": "skipped", "details": "No records to process."}
                continue
            
            progress[record_type] = {
                "status": "processing",
                "total": len(data["ids"]),
                "processed": 0,
                "errors": 0,
                "details": f"Starting processing for {len(data['ids'])} records."
            }
            await update_processing_status(app_id, "processing", job_id, progress=progress)
            
            try:
                module = importlib.import_module(data["processor_module"])
                func = getattr(module, data["processor_function_name"])
                
                for i, r_id in enumerate(data["ids"]):
                    progress[record_type]["details"] = f"Processing record {i+1} of {len(data['ids'])} (ID: {r_id[:8]}...)"
                    await update_processing_status(app_id, "processing", job_id, progress=progress)
                    
                    await func(sf, r_id, app_id, item_index=(i + 1))
                    progress[record_type]["processed"] += 1

            except Exception as e:
                # Catch any error during the loop for this record type
                error_msg = f"Failed on {record_type} (ID: {r_id}): {str(e)}"
                logger.error(f"Job {job_id}: {error_msg}", exc_info=True)
                progress[record_type]["status"] = "failed"
                progress[record_type]["details"] = error_msg
                await update_processing_status(app_id, "failed", job_id, message=error_msg, progress=progress)
                return # Exit the entire background task

            progress[record_type]["status"] = "completed"
            progress[record_type]["details"] = "All records processed successfully."
            await update_processing_status(app_id, "processing", job_id, progress=progress)

        # Final success update
        await update_processing_status(app_id, "completed", job_id, message="All tasks completed successfully.", progress=progress)

    except Exception as e:
        # This is the top-level catch-all for any failure
        error_msg = f"Job failed: {str(e)}"
        logger.error(f"Job {job_id}: CRITICAL error for App ID {app_id}: {e}", exc_info=True)
        # Update progress with the failure details
        failed_stage = next((k for k, v in progress.items() if v.get("status") == "processing"), "unknown_stage")
        progress[failed_stage] = {"status": "failed", "details": error_msg}
        await update_processing_status(app_id, "failed", job_id, client_fingerprint=client_fp, message=error_msg, progress=progress)
    finally:
        await release_processing_slot()
        logger.info(f"Job {job_id}: Released processing slot for App ID: {app_id}")


@router.post("/analyze", response_model=AnalyzeApplicationResponse)
async def analyze_application_endpoint(
    req: Request, bg_tasks: BackgroundTasks, body: AnalyzeApplicationBodyRequest, sf: SalesforceService = Depends(get_sf_service_dependency)
):
    job_id = str(uuid.uuid4())
    client_fp = generate_client_fingerprint(dict(req.headers), req.client.host)
    app_id = body.record_id.split('/')[-1] if '/' in body.record_id else body.record_id

    if not (isinstance(app_id, str) and len(app_id) in [15, 18]):
        raise HTTPException(status_code=400, detail="Invalid Salesforce record_id format.")

    for check, msg in [
        await check_and_update_global_rate_limit(),
        await check_and_update_client_rate_limit(client_fp),
        await check_rapid_fire_protection(client_fp, app_id)
    ]:
        if not check:
            raise HTTPException(status_code=429, detail=msg)

    if (status := await get_processing_status(app_id)) and status["status"] in ["processing", "queued"]:
        raise HTTPException(status_code=409, detail=f"Job for Application ID {app_id} is already {status['status']}.")

    metadata, bg_data = await fetch_initial_related_records_metadata(sf, app_id)
    
    await update_processing_status(app_id, "queued", job_id, client_fingerprint=client_fp, message="Waiting for available slot.")
    
    bg_tasks.add_task(process_application_and_related_records_bg, app_id, sf, job_id, client_fp, bg_data)
    
    total_items = 1 + sum(m.count for m in metadata)
    
    return AnalyzeApplicationResponse(
        request_id=job_id,
        application_record_id=app_id,
        status="processing_queued",
        message="Request accepted and queued for processing.",
        created_at=datetime.now(timezone.utc),
        status_url=str(req.url_for('get_application_processing_status_endpoint', application_id=app_id)),
        related_records_metadata=metadata,
        estimated_completion=EstimatedCompletion(
            total_items=total_items, min_seconds=total_items * 60, max_seconds=total_items * 240,
            human_readable=f"Approx. {total_items * 1.5:.0f} - {total_items * 4:.0f} minutes"
        )
    )

@router.get("/status/{application_id}", response_model=JobStatusResponse)
async def get_application_processing_status_endpoint(application_id: str):
    if not (isinstance(application_id, str) and len(application_id) in [15, 18]):
        raise HTTPException(status_code=400, detail="Invalid application_id format.")
    status_info = await get_processing_status(application_id)
    if not status_info:
        raise HTTPException(status_code=404, detail=f"No processing record found for {application_id}")
    return JobStatusResponse(application_id=application_id, **status_info)


@router.get("/queue-overview", response_model=QueueOverviewResponse)
async def get_processing_queue_overview_endpoint():
    all_statuses = await get_all_processing_statuses()
    
    # Convert the dict of statuses into a list of JobStatusResponse objects
    all_jobs = [
        JobStatusResponse(application_id=app_id, **status_data)
        for app_id, status_data in all_statuses.items()
    ]

    # Sort jobs by last_updated_at descending
    all_jobs.sort(key=lambda x: x.last_updated_at, reverse=True)
    
    active_slots_count = await get_active_processing_slots_count()
    return QueueOverviewResponse(
        active_jobs=active_slots_count,
        tracked_jobs_total=len(all_jobs),
        slot_utilization={
            "active_slots": active_slots_count,
            "max_slots": MAX_CONCURRENT_PROCESSING_SLOTS,
            "load_percent": round((active_slots_count / MAX_CONCURRENT_PROCESSING_SLOTS) * 100, 2) if MAX_CONCURRENT_PROCESSING_SLOTS > 0 else 0
        },
        all_jobs=all_jobs
    )
