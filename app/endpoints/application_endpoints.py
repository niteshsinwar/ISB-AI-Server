# project_root/app/endpoints/application_endpoints.py

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
    MAX_CONCURRENT_PROCESSING_SLOTS,
    READABLE_OBJECT_NAMES
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
from app.services.document_extraction_service import create_text_extractor
from app.core.resource_manager import managed_job_resources
logger = logging.getLogger(__name__)

def create_application_router(sf_service_dependency: Depends) -> APIRouter:
    """
    This factory creates and returns a router with all application endpoints.
    It uses the provided dependency to inject the correct SalesforceService.
    """
    router = APIRouter()

    # This helper function is unchanged
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
                if retrieval_method == "self":
                    # Special case: Personal Details - return the application ID itself
                    ids = [application_record_id]
                    error = None
                else:
                    # Regular case: Fetch related record IDs
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

    # MODIFIED: The background task now implements "fail-fast" logic.
    async def process_application_and_related_records_bg(
        job: Job,
        sf_service: SalesforceService,
        job_manager: JobManager,
        prefetched_data: Dict[str, Dict[str, Any]]
    ):
        # Use managed resources with guaranteed cleanup
        async with managed_job_resources(job.job_id) as resource_manager:
            await job_manager.begin_processing(job, sf_service=sf_service)
            progress = job.progress

            # Create isolated resources for this job with resource tracking
            job_extractor = create_text_extractor(resource_manager=resource_manager)
            logger.info(f"Created isolated extractor for job {job.job_id} with resource manager")

            try:
                # 1. Initialize all items with "pending" status for immediate visibility in Salesforce
                for record_type, data in prefetched_data.items():
                    readable_name = READABLE_OBJECT_NAMES.get(record_type, record_type)
                    progress[readable_name] = {"status": "pending", "total": len(data.get("ids", [])), "processed": 0}

                await job_manager.update_status(job.application_id, job.job_id, "processing", sf_service, progress=progress, message="Initialized job scope. Starting analysis...")

                # 2. Sort records by priority for sequential processing
                sorted_records = sorted(prefetched_data.items(),
                                      key=lambda x: next((cfg["priority"] for cfg in RELATED_RECORD_PROCESSING_CONFIG
                                                        if cfg["target_record_type"] == x[0]), 999))

                # 3. Process all records uniformly with priority-based ordering
                for record_type, data in sorted_records:
                    if job.is_stale: raise asyncio.CancelledError("Job has become stale and was cancelled.")

                    readable_name = READABLE_OBJECT_NAMES.get(record_type, record_type)

                    if not data.get("ids"):
                        progress[readable_name]["status"] = "skipped"
                        await job_manager.update_status(job.application_id, job.job_id, "processing", sf_service, progress=progress)
                        continue

                    progress[readable_name]["status"] = "processing"
                    await job_manager.update_status(job.application_id, job.job_id, "processing", sf_service, progress=progress)

                    module = importlib.import_module(data["processor_module"])
                    func = getattr(module, data["processor_function_name"])

                    for i, r_id in enumerate(data["ids"]):
                        if job.is_stale: raise asyncio.CancelledError("Job has become stale and was cancelled.")

                        # Unified processor call - handle special case for application processor
                        if record_type == APPLICATION_OBJECT_API_NAME:
                            await func(sf_service, r_id, job.application_id, record_type,
                                     item_index=(i + 1), extractor_instance=job_extractor, resource_manager=resource_manager)
                        else:
                            await func(sf_service, r_id, job.application_id,
                                     extractor_instance=job_extractor, item_index=(i + 1), resource_manager=resource_manager)

                        progress[readable_name]["processed"] += 1
                        await job_manager.update_status(job.application_id, job.job_id, "processing", sf_service, progress=progress)

                    progress[readable_name]["status"] = "completed"
                    await job_manager.update_status(job.application_id, job.job_id, "processing", sf_service, progress=progress)

                # If the code reaches here, it means everything succeeded
                final_status = "completed"
                final_message = "All verification tasks completed successfully."
                await job_manager.update_status(job.application_id, job.job_id, final_status, sf_service, message=final_message, progress=progress)

            except asyncio.CancelledError:
                logger.warning(f"Job {job.job_id} for App {job.application_id} was cancelled because it became stale.")
                await job_manager.update_status(job.application_id, job.job_id, "failed", sf_service, message="Job cancelled due to a newer request.", progress=progress)
            except Exception as e:
                # This catches the FIRST critical failure and halts the job.
                error_msg = str(e) # This will contain the structured error from the processor.
                logger.error(f"Job {job.job_id}: A critical error halted processing for App ID {job.application_id}: {error_msg}", exc_info=True)
                await job_manager.update_status(job.application_id, job.job_id, "failed", sf_service, message=error_msg, progress=progress)
            finally:
                # Resource cleanup happens automatically via context manager
                await job_manager.release_and_finalize(job)

    # --- The API endpoints below are unchanged ---
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