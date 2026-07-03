# project_root/app/endpoints/application_endpoints.py

import logging
import asyncio
from typing import Dict, Any, List, Tuple

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks, Request

from app.services.salesforce_service import SalesforceService
from app.core.processing_utils import is_valid_salesforce_id
from app.config import (
    RELATED_RECORD_PROCESSING_CONFIG,
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

            # Consolidated processors (e.g. Recommender) verify ALL child records
            # in one pass and write ONE summary — the worker gets a single
            # application-level work item when any children exist.
            if config.get("consolidated") and ids:
                ids = [application_record_id]

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

            # Worker now fetches existing logs at completion time and merges them
            # This ensures logs are NEVER cleared during intermediate updates
            result = await process_manager.execute_job_in_worker(
                job_id=job.job_id,
                application_id=job.application_id,
                sf_config=sf_config,
                prefetched_data=prefetched_data,
                progress_callback=handle_progress_update
                # Note: existing_logs no longer passed - worker fetches at completion
            )

            final_status = result.get("status", "completed")
            final_message = result.get("message") or "All verification tasks completed successfully."
            progress = result.get("progress")

            # Log confirmation - worker already saved logs to Salesforce
            worker_logs = result.get("logs")
            if worker_logs:
                logger.info(f"Worker completed with {len(worker_logs)} log attempt(s)")
            else:
                logger.warning("Worker did not return log data")

            # Final status update - NO logs parameter needed, worker already saved them
            await job_manager.update_status(
                job.application_id,
                job.job_id,
                final_status,
                sf_service,
                message=final_message,
                progress=progress
                # logs NOT passed - worker already saved merged logs to Salesforce
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

        if not is_valid_salesforce_id(app_id):
            raise HTTPException(status_code=400, detail="Invalid Salesforce record_id format.")

        # Atomic admission: duplicate protection + queue capacity decided under
        # one lock, with a reservation held until create_job registers the job.
        # (Separate check-then-act calls raced under concurrent bursts.)
        admission = await job_manager.try_admit(app_id, MAX_CONCURRENT_PROCESSING_SLOTS)
        if admission == "duplicate":
            raise HTTPException(status_code=409, detail=f"A job for Application ID {app_id} is already active.")
        if admission == "queue_full":
            raise HTTPException(
                status_code=429,
                detail=f"Queue full: {MAX_CONCURRENT_PROCESSING_SLOTS} jobs already waiting. Please try again later."
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
            await job_manager.cancel_admission(app_id)
            raise HTTPException(status_code=500, detail=str(e))
        except Exception as e:
            await job_manager.cancel_admission(app_id)
            # A well-formed but invalid/nonexistent ID fails the initial
            # AI_Server_Job__c upsert with MALFORMED_ID / invalid cross
            # reference — report it as a client error, not a 500.
            msg = str(e)
            if any(marker in msg for marker in ("MALFORMED_ID", "id value of incorrect type", "INVALID_CROSS_REFERENCE_KEY")):
                raise HTTPException(
                    status_code=400,
                    detail=f"record_id {app_id} is not a valid Application record in this org.",
                )
            logger.error(f"Failed to queue job for {app_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to queue job: {msg[:300]}")

    @router.get("/status/{application_id}", response_model=JobStatusResponse)
    async def get_application_processing_status_endpoint(
        application_id: str,
        sf_service: SalesforceService = Depends(sf_service_dependency),
        job_manager: JobManager = Depends(get_job_manager_dependency)
    ):
        if not is_valid_salesforce_id(application_id):
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
