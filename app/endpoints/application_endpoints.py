# project_root/app/endpoints/application_endpoints.py

import logging
import asyncio
import json
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
from app.core.rate_limit_state import generate_client_fingerprint
from app.core.job_manager import Job, JobManager, get_job_manager_dependency
from app.core.process_manager import get_process_manager, WorkerProcessError
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

    async def process_application_and_related_records_bg(
        job: Job,
        sf_service: SalesforceService,
        job_manager: JobManager,
        prefetched_data: Dict[str, Dict[str, Any]]
    ):
        process_manager = await get_process_manager()
        await job_manager.begin_processing(job, sf_service=sf_service)

        sf_config = {
            "client_id": sf_service.client_id,
            "client_secret": sf_service.client_secret,
            "token_url": sf_service.token_url,
        }

        def build_initial_progress_map() -> Dict[str, Dict[str, Any]]:
            progress_map: Dict[str, Dict[str, Any]] = {}
            for record_type, data in prefetched_data.items():
                readable_name = READABLE_OBJECT_NAMES.get(record_type, record_type)
                total_items = len(data.get("ids", []))
                status = "pending" if total_items > 0 else "skipped"
                progress_map[readable_name] = {
                    "status": status,
                    "total": total_items,
                    "processed": 0
                }
            return progress_map

        async def handle_progress_update(progress_update: Dict[str, Any]):
            if not progress_update:
                return
            await job_manager.update_status(
                job.application_id,
                job.job_id,
                "processing",
                sf_service,
                progress=progress_update
            )

        try:
            # Seed initial progress so queue-overview has immediate visibility
            initial_progress = build_initial_progress_map()
            if initial_progress:
                await job_manager.update_status(
                    job.application_id,
                    job.job_id,
                    "processing",
                    sf_service,
                    progress=initial_progress
                )

            # Fetch existing logs for persistence across API triggers
            existing_job_record = await sf_service.get_latest_ai_server_job(job.application_id)
            existing_logs = existing_job_record.get('logs') if existing_job_record else None

            result = await process_manager.execute_job_in_worker(
                job_id=job.job_id,
                application_id=job.application_id,
                sf_config=sf_config,
                prefetched_data=prefetched_data,
                progress_callback=handle_progress_update,
                existing_logs=existing_logs
            )

            final_status = result.get("status", "completed")
            final_message = result.get("message") or "All verification tasks completed successfully."
            progress = result.get("progress")
            # Extract logs from worker result and pass to update_status to prevent overwrite
            worker_logs = result.get("logs")
            logs_json = json.dumps(worker_logs) if worker_logs else None
            
            if logs_json:
                logger.info(f"Passing logs to JobManager for final update (len={len(logs_json)})")
            else:
                logger.warning("No logs received from worker to pass to JobManager!")

            await job_manager.update_status(
                job.application_id,
                job.job_id,
                final_status,
                sf_service,
                message=final_message,
                progress=progress,
                logs=logs_json
            )
        except WorkerProcessError as e:
            error_message = f"Worker process error: {e}"
            logger.error(f"Job {job.job_id}: {error_message}")
            await job_manager.update_status(
                job.application_id,
                job.job_id,
                "failed",
                sf_service,
                message=error_message
            )
        except Exception as e:
            error_message = f"Unexpected processing error: {e}"
            logger.exception(f"Job {job.job_id}: {error_message}")
            await job_manager.update_status(
                job.application_id,
                job.job_id,
                "failed",
                sf_service,
                message=error_message
            )
        finally:
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

        # Check 1: Duplicate job protection - O(1), prevents same app being processed twice
        if await job_manager.is_job_active(app_id):
            raise HTTPException(status_code=409, detail=f"A job for Application ID {app_id} is already active.")

        # Check 2: Queue capacity - O(n), prevents unbounded queue growth (allows batch processing)
        all_jobs = await job_manager.get_all_active_jobs()
        queued_count = sum(1 for j in all_jobs.values() if j.status == "queued")
        if queued_count >= MAX_CONCURRENT_PROCESSING_SLOTS:
            raise HTTPException(
                status_code=429,
                detail=f"Queue full: {queued_count}/{MAX_CONCURRENT_PROCESSING_SLOTS} jobs waiting. Please try again later."
            )
            
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
        sf_service: SalesforceService = Depends(sf_service_dependency),
        job_manager: JobManager = Depends(get_job_manager_dependency)
    ):
        overview_data = await job_manager.get_queue_overview(
            org_alias=getattr(sf_service, "org_alias", None)
        )
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
