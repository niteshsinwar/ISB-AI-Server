import logging
import asyncio
import importlib
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks, Request

from app.services.salesforce_service import SalesforceService
from app.config import (
    RELATED_RECORD_PROCESSING_CONFIG,
    APPLICATION_OBJECT_API_NAME,
    MAX_CONCURRENT_PROCESSING_SLOTS
)
from app.core.rate_limit_state import (
    generate_client_fingerprint,
    is_client_blocked,
    check_rapid_fire_protection,
    check_and_update_client_rate_limit,
    check_and_update_global_rate_limit
)
from app.core.job_manager import Job, JobManager, get_job_manager_dependency
from app.schemas.responses import (
    AnalyzeApplicationBodyRequest, AnalyzeApplicationResponse, JobStatusResponse,
    QueueOverviewResponse, RelatedRecordMetadata, EstimatedCompletion
)
from app.processors.application_processor import process_single_application_detail

logger = logging.getLogger(__name__)

def create_application_router(sf_service_dependency: Depends) -> APIRouter:
    """
    This factory creates and returns a router with all application endpoints.
    It uses the provided dependency to inject the correct SalesforceService.
    """
    router = APIRouter()

    # MODIFIED: This helper function now reads 'order_by' and 'limit' from the config
    # and passes them to the Salesforce service.
    async def fetch_initial_related_records_metadata(
        sf_service: SalesforceService,
        application_record_id: str
    ) -> Tuple[List[RelatedRecordMetadata], Dict[str, Dict[str, Any]]]:
        metadata_list: List[RelatedRecordMetadata] = []
        data_for_bg: Dict[str, Dict[str, Any]] = {}
        
        for config in RELATED_RECORD_PROCESSING_CONFIG:
            target_type = config["target_record_type"]
            retrieval_method = config["retrieval_method"]
            
            ids, error = [], None
            try:
                # MODIFIED: Pass the new sorting and limiting arguments from the config.
                # The .get() method is used to safely retrieve the optional keys.
                ids = await asyncio.to_thread(
                    sf_service.get_directly_related_record_ids,
                    parent_record_id=application_record_id,
                    child_object_api_name=target_type,
                    lookup_field_on_child_to_parent=config["lookup_on_child_to_parent"],
                    filtering_criteria=config.get("filtering_criteria"),
                    order_by=config.get("order_by"),
                    limit=config.get("limit")
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
        job: Job,
        sf_service: SalesforceService,
        job_manager: JobManager,
        prefetched_data: Dict[str, Dict[str, Any]]
    ):
        await job_manager.begin_processing(job, sf_service=sf_service)
        progress = job.progress

        try:
            # Step 1: Process Main Application
            app_summary = await process_single_application_detail(
                sf_service=sf_service, application_id=job.application_id, parent_application_id=job.application_id,
                application_object_api_name=APPLICATION_OBJECT_API_NAME
            )
            progress["main_application"] = {"status": "completed", "details": app_summary}
            await job_manager.update_status(job.application_id, job.job_id, "processing", sf_service, progress=progress)

            # Step 2: Process Related Records
            for record_type, data in prefetched_data.items():
                if job.is_stale: raise asyncio.CancelledError("Job has become stale and was cancelled.")
                
                if not data.get("ids"):
                    progress[record_type] = {"status": "skipped", "details": "No records to process."}
                    continue
                
                progress[record_type] = {"status": "processing", "total": len(data["ids"]), "processed": 0, "errors": 0}
                await job_manager.update_status(job.application_id, job.job_id, "processing", sf_service, progress=progress)
                
                module = importlib.import_module(data["processor_module"])
                func = getattr(module, data["processor_function_name"])
                
                for i, r_id in enumerate(data["ids"]):
                    if job.is_stale: raise asyncio.CancelledError("Job has become stale and was cancelled.")
                    
                    progress[record_type]["details"] = f"Processing record {i+1} of {len(data['ids'])} (ID: {r_id[:8]}...)"
                    await job_manager.update_status(job.application_id, job.job_id, "processing", sf_service, progress=progress)
                    
                    await func(sf_service, r_id, job.application_id, item_index=(i + 1))
                    progress[record_type]["processed"] += 1
                
                progress[record_type]["status"] = "completed"
                progress[record_type]["details"] = "All records processed successfully."
                await job_manager.update_status(job.application_id, job.job_id, "processing", sf_service, progress=progress)

            await job_manager.update_status(job.application_id, job.job_id, "completed", sf_service, message="All tasks completed successfully.", progress=progress)

        except asyncio.CancelledError:
            logger.warning(f"Job {job.job_id} for App {job.application_id} was cancelled because it became stale.")
            await job_manager.update_status(job.application_id, job.job_id, "failed", sf_service, message="Job cancelled due to a newer request.", progress=progress)
        except Exception as e:
            error_msg = f"Job failed: {str(e)}"
            logger.error(f"Job {job.job_id}: CRITICAL error for App ID {job.application_id}: {e}", exc_info=True)
            failed_stage = next((k for k, v in progress.items() if v.get("status") == "processing"), "unknown_stage")
            progress[failed_stage] = {"status": "failed", "details": error_msg}
            await job_manager.update_status(job.application_id, job.job_id, "failed", sf_service, message=error_msg, progress=progress)
        finally:
            await job_manager.release_and_finalize(job)

    @router.post("/analyze", response_model=AnalyzeApplicationResponse)
    async def analyze_application_endpoint(
        req: Request,
        bg_tasks: BackgroundTasks,
        body: AnalyzeApplicationBodyRequest,
        sf_service: SalesforceService = Depends(sf_service_dependency),
        job_manager: JobManager = Depends(get_job_manager_dependency)
    ):
        client_fp = generate_client_fingerprint(dict(req.headers), req.client.host)
        app_id = body.record_id.strip()

        if not (isinstance(app_id, str) and len(app_id) in [15, 18]):
            raise HTTPException(status_code=400, detail="Invalid Salesforce record_id format.")

        if block_message := await is_client_blocked(client_fp):
            raise HTTPException(status_code=429, detail=block_message)
        if await job_manager.is_job_active(app_id):
            raise HTTPException(status_code=409, detail=f"A job for Application ID {app_id} is already active.")
        ok, msg = await check_rapid_fire_protection(client_fp, app_id)
        if not ok: raise HTTPException(status_code=429, detail=msg)
        ok, msg = await check_and_update_client_rate_limit(client_fp)
        if not ok: raise HTTPException(status_code=429, detail=msg)
        ok, msg = await check_and_update_global_rate_limit()
        if not ok: raise HTTPException(status_code=429, detail=msg)
            
        try:
            # CRITICAL: Pass sf_service to create the job in the correct org
            new_job = await job_manager.create_job(app_id, client_fp, sf_service=sf_service)
            metadata, bg_data = await fetch_initial_related_records_metadata(sf_service, app_id)
            bg_tasks.add_task(process_application_and_related_records_bg, new_job, sf_service, job_manager, bg_data)
            total_items = 1 + sum(m.count for m in metadata)
            
            return AnalyzeApplicationResponse(
                request_id=new_job.job_id,
                application_record_id=app_id,
                status="processing_queued",
                message="Request accepted and queued for processing.",
                created_at=new_job.created_at,
                status_url=str(req.url_for('get_application_processing_status_endpoint', application_id=app_id)),
                related_records_metadata=metadata,
                estimated_completion=EstimatedCompletion(
                    total_items=total_items, min_seconds=total_items * 60, max_seconds=total_items * 240,
                    human_readable=f"Approx. {total_items * 1.5:.0f} - {total_items * 4:.0f} minutes"
                )
            )
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/status/{application_id}", response_model=JobStatusResponse)
    async def get_application_processing_status_endpoint(
        application_id: str,
        sf_service: SalesforceService = Depends(sf_service_dependency),
        job_manager: JobManager = Depends(get_job_manager_dependency)
    ):
        if not (isinstance(application_id, str) and len(application_id) in [15, 18]):
            raise HTTPException(status_code=400, detail="Invalid application_id format.")
        
        # CRITICAL: Pass sf_service to get the job status from the correct org
        status_info = await job_manager.get_job_status(application_id, sf_service=sf_service)
        if not status_info:
            raise HTTPException(status_code=404, detail=f"No processing record found for {application_id}")
        return JobStatusResponse(**status_info)

    @router.get("/queue-overview", response_model=QueueOverviewResponse)
    async def get_processing_queue_overview_endpoint(
        job_manager: JobManager = Depends(get_job_manager_dependency)
    ):
        overview_data = await job_manager.get_queue_overview()
        return QueueOverviewResponse(
            active_jobs=overview_data['active_jobs'],
            tracked_jobs_total=overview_data['tracked_jobs_total'],
            slot_utilization={
                "active_slots": overview_data['active_jobs'],
                "max_slots": MAX_CONCURRENT_PROCESSING_SLOTS,
                "load_percent": round((overview_data['active_jobs'] / MAX_CONCURRENT_PROCESSING_SLOTS) * 100, 2) if MAX_CONCURRENT_PROCESSING_SLOTS > 0 else 0
            },
            all_jobs=[JobStatusResponse(**job) for job in overview_data['all_jobs']]
        )

    return router