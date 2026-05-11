import logging
import asyncio
import json
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks, Request

from app.services.salesforce_service import SalesforceService
from app.config import (
    EEDL_RECORD_PROCESSING_CONFIG,
    EEDL_READABLE_OBJECT_NAMES,
    MAX_CONCURRENT_PROCESSING_SLOTS,
)
from app.core.rate_limit_state import generate_client_fingerprint
from app.core.job_manager import Job, JobManager, get_job_manager_dependency
from app.core.process_manager import get_process_manager, WorkerProcessError
from app.schemas.responses import (
    AnalyzeEEDLBodyRequest, AnalyzeEEDLResponse, JobStatusResponse,
    QueueOverviewResponse, RelatedRecordMetadata, EstimatedCompletion,
)

logger = logging.getLogger(__name__)


def create_eedl_router(sf_service_dependency: Depends) -> APIRouter:
    router = APIRouter()

    async def fetch_eedl_related_records_metadata(
        sf_service: SalesforceService,
        opportunity_id: str,
    ) -> Tuple[List[RelatedRecordMetadata], Dict[str, Dict[str, Any]]]:
        metadata_list: List[RelatedRecordMetadata] = []
        data_for_bg: Dict[str, Dict[str, Any]] = {}

        for config in EEDL_RECORD_PROCESSING_CONFIG:
            target_type = config["target_record_type"]
            ids, error = [], None

            try:
                if target_type == "ID_Document":
                    # ID doc is always the Opportunity itself — one record
                    ids = [opportunity_id]
                else:
                    # Education__c — fetch via Contact lookup on Opportunity
                    ids = await asyncio.to_thread(
                        sf_service.get_eedl_education_ids_for_opportunity,
                        opportunity_id,
                    )
            except Exception as e:
                error = str(e)
                logger.warning(f"EEDL metadata fetch error for {target_type}: {e}")

            readable = EEDL_READABLE_OBJECT_NAMES.get(target_type, target_type)
            metadata_list.append(RelatedRecordMetadata(
                target_record_type=target_type,
                retrieval_method="content_document_link" if target_type == "ID_Document" else "via_contact",
                count=len(ids),
                status="fetch_error" if error else "ids_fetched",
                sample_ids=ids[:5],
            ))
            data_for_bg[target_type] = {
                "ids": ids,
                "processor_module": config["processor_module"],
                "processor_function_name": config["processor_function_name"],
            }

        return metadata_list, data_for_bg

    async def process_eedl_bg(
        job: Job,
        sf_service: SalesforceService,
        job_manager: JobManager,
        prefetched_data: Dict[str, Dict[str, Any]],
    ):
        process_manager = await get_process_manager()
        await job_manager.begin_processing(job, sf_service=sf_service)

        sf_config = {
            "client_id": sf_service.client_id,
            "client_secret": sf_service.client_secret,
            "token_url": sf_service.token_url,
        }

        def build_initial_progress_map() -> Dict[str, Dict[str, Any]]:
            progress: Dict[str, Dict[str, Any]] = {}
            for record_type, data in prefetched_data.items():
                readable = EEDL_READABLE_OBJECT_NAMES.get(record_type, record_type)
                total = len(data.get("ids", []))
                progress[readable] = {
                    "status": "pending" if total > 0 else "skipped",
                    "total": total,
                    "processed": 0,
                }
            return progress

        async def handle_progress_update(progress_update: Dict[str, Any]):
            if not progress_update:
                return
            await job_manager.update_status(
                job.application_id, job.job_id, "processing", sf_service, progress=progress_update,
            )

        try:
            initial_progress = build_initial_progress_map()
            if initial_progress:
                await job_manager.update_status(
                    job.application_id, job.job_id, "processing", sf_service, progress=initial_progress,
                )

            result = await process_manager.execute_job_in_worker(
                job_id=job.job_id,
                application_id=job.application_id,
                sf_config=sf_config,
                prefetched_data=prefetched_data,
                progress_callback=handle_progress_update,
                opportunity_id=job.opportunity_id,
            )

            final_status = result.get("status", "completed")
            final_message = result.get("message") or "All EEDL verification tasks completed successfully."
            progress = result.get("progress")

            await job_manager.update_status(
                job.application_id, job.job_id, final_status, sf_service,
                message=final_message, progress=progress,
            )
        except WorkerProcessError as e:
            await job_manager.update_status(
                job.application_id, job.job_id, "failed", sf_service,
                message=f"Worker process error: {e}",
            )
        except Exception as e:
            logger.exception(f"EEDL Job {job.job_id}: unexpected error: {e}")
            await job_manager.update_status(
                job.application_id, job.job_id, "failed", sf_service,
                message=f"Unexpected processing error: {e}",
            )
        finally:
            await job_manager.release_and_finalize(job)

    @router.post("/analyze", response_model=AnalyzeEEDLResponse)
    async def analyze_eedl_endpoint(
        req: Request,
        bg_tasks: BackgroundTasks,
        body: AnalyzeEEDLBodyRequest,
        sf_service: SalesforceService = Depends(sf_service_dependency),
        job_manager: JobManager = Depends(get_job_manager_dependency),
    ):
        client_fp = generate_client_fingerprint(dict(req.headers), req.client.host)
        opp_id = body.record_id.strip()

        if not (isinstance(opp_id, str) and len(opp_id) in [15, 18]):
            raise HTTPException(status_code=400, detail="Invalid Salesforce record_id format.")

        if await job_manager.is_job_active(opp_id):
            raise HTTPException(status_code=409, detail=f"A job for Opportunity {opp_id} is already active.")

        all_jobs = await job_manager.get_all_active_jobs()
        queued_count = sum(1 for j in all_jobs.values() if j.status == "queued")
        if queued_count >= MAX_CONCURRENT_PROCESSING_SLOTS:
            raise HTTPException(
                status_code=429,
                detail=f"Queue full: {queued_count}/{MAX_CONCURRENT_PROCESSING_SLOTS} jobs waiting.",
            )

        try:
            new_job = await job_manager.create_job(
                opp_id, client_fp, sf_service=sf_service, opportunity_id=opp_id,
            )
            metadata, bg_data = await fetch_eedl_related_records_metadata(sf_service, opp_id)
            bg_tasks.add_task(process_eedl_bg, new_job, sf_service, job_manager, bg_data)
            total_items = sum(m.count for m in metadata)

            return AnalyzeEEDLResponse(
                request_id=new_job.job_id,
                opportunity_record_id=opp_id,
                status="processing_queued",
                message="EEDL verification request accepted and queued for processing.",
                created_at=new_job.created_at,
                status_url=str(req.url_for("get_eedl_processing_status_endpoint", opportunity_id=opp_id)),
                related_records_metadata=metadata,
                estimated_completion=EstimatedCompletion(
                    total_items=total_items,
                    min_seconds=total_items * 60,
                    max_seconds=total_items * 240,
                    human_readable=f"Approx. {total_items * 1.5:.0f} - {total_items * 4:.0f} minutes",
                ),
            )
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/status/{opportunity_id}", response_model=JobStatusResponse, name="get_eedl_processing_status_endpoint")
    async def get_eedl_processing_status_endpoint(
        opportunity_id: str,
        sf_service: SalesforceService = Depends(sf_service_dependency),
        job_manager: JobManager = Depends(get_job_manager_dependency),
    ):
        if not (isinstance(opportunity_id, str) and len(opportunity_id) in [15, 18]):
            raise HTTPException(status_code=400, detail="Invalid opportunity_id format.")
        status_info = await job_manager.get_job_status(opportunity_id, sf_service=sf_service)
        if not status_info:
            raise HTTPException(status_code=404, detail=f"No processing record found for {opportunity_id}")
        return JobStatusResponse(**status_info)

    @router.get("/queue-overview", response_model=QueueOverviewResponse)
    async def get_eedl_queue_overview_endpoint(
        sf_service: SalesforceService = Depends(sf_service_dependency),
        job_manager: JobManager = Depends(get_job_manager_dependency),
    ):
        overview_data = await job_manager.get_queue_overview(
            org_alias=getattr(sf_service, "org_alias", None)
        )
        return QueueOverviewResponse(
            active_jobs=overview_data["active_jobs"],
            tracked_jobs_total=overview_data["tracked_jobs_total"],
            slot_utilization={
                "active_slots": overview_data["active_jobs"],
                "max_slots": MAX_CONCURRENT_PROCESSING_SLOTS,
                "load_percent": round(
                    (overview_data["active_jobs"] / MAX_CONCURRENT_PROCESSING_SLOTS) * 100, 2
                ) if MAX_CONCURRENT_PROCESSING_SLOTS > 0 else 0,
            },
            all_jobs=[JobStatusResponse(**job) for job in overview_data["all_jobs"]],
        )

    return router
