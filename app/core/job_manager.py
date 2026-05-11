# project_root/app/core/job_manager.py

import asyncio
import logging
import uuid
import json
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field

from app.services.salesforce_service import SalesforceService
from .rate_limit_state import acquire_processing_slot, release_processing_slot, get_active_processing_slots_count
from .process_manager import get_process_manager

logger = logging.getLogger(__name__)

class Job(BaseModel):
    job_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    salesforce_job_record_id: Optional[str] = None
    application_id: str
    opportunity_id: Optional[str] = None  # Set for EEDL jobs; triggers EEDL SF methods
    org_alias: Optional[str] = None
    status: str = "queued"
    message: Optional[str] = "Waiting for available slot."
    client_fingerprint: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    progress: Dict[str, Any] = {}
    is_stale: bool = False
    logs: Optional[str] = None  # Store existing logs for retry logic

class JobManager:
    def __init__(self):
        self._active_jobs: Dict[str, Job] = {}
        self._lock = asyncio.Lock()

    async def create_job(self, application_id: str, client_fingerprint: str, sf_service: SalesforceService, opportunity_id: Optional[str] = None) -> Job:
        """
        Create a new job for an application.

        LOGGING STRATEGY:
        - Existing logs are fetched and preserved during initial job creation
        - During processing: NO logs DML - logs field is OMITTED from updates
        - At job completion: Worker fetches fresh logs, appends new attempt, and saves
        - This ensures logs are NEVER accidentally cleared
        """
        stale_job_id: Optional[str] = None

        org_alias = getattr(sf_service, "org_alias", None)
        is_eedl = opportunity_id is not None

        # Fetch existing logs to preserve them during the initial "queued" status upsert
        if is_eedl:
            existing_job_data = await sf_service.get_latest_eedl_ai_server_job(opportunity_id)
        else:
            existing_job_data = await sf_service.get_latest_ai_server_job(application_id)
        existing_logs = existing_job_data.get("logs") if existing_job_data else None

        async with self._lock:
            if application_id in self._active_jobs:
                stale_job = self._active_jobs[application_id]
                stale_job.is_stale = True
                stale_job_id = stale_job.job_id

            new_job = Job(
                application_id=application_id,
                opportunity_id=opportunity_id,
                client_fingerprint=client_fingerprint,
                org_alias=org_alias,
                logs=existing_logs
            )

            # Initial upsert — route to EEDL or admission SF method
            if is_eedl:
                sf_job_id = await sf_service.upsert_eedl_ai_server_job(
                    job_id=new_job.job_id,
                    opportunity_id=opportunity_id,
                    status=new_job.status,
                    client_fingerprint=new_job.client_fingerprint,
                    logs=existing_logs,
                )
            else:
                sf_job_id = await sf_service.upsert_ai_server_job(
                    job_id=new_job.job_id,
                    application_id=new_job.application_id,
                    status=new_job.status,
                    client_fingerprint=new_job.client_fingerprint,
                    logs=existing_logs,
                )
            if not sf_job_id:
                raise RuntimeError("Failed to create initial job record in Salesforce.")
            
            new_job.salesforce_job_record_id = sf_job_id
            self._active_jobs[application_id] = new_job

        if stale_job_id:
            process_manager = await get_process_manager()
            await process_manager.kill_job_worker(stale_job_id)
            logger.info(f"Marked stale and terminated previous job {stale_job_id} for App {application_id}")

        logger.info(f"Created job {new_job.job_id} for App {application_id} in org {sf_service.instance_url}. Hydrated logs: {bool(existing_logs)}")
        return new_job

    async def begin_processing(self, job: Job, sf_service: SalesforceService):
        await acquire_processing_slot()
        logger.info(f"Acquired processing slot for job {job.job_id} (App: {job.application_id})")
        

        await self.update_status(
            job.application_id,
            job.job_id,
            "processing",
            message="Acquired slot, initializing analysis.",
            sf_service=sf_service
        )

    async def release_and_finalize(self, job: Job):
        await release_processing_slot()
        logger.info(f"Released processing slot for job {job.job_id} (App: {job.application_id})")
        async with self._lock:
            if job.application_id in self._active_jobs and self._active_jobs[job.application_id].job_id == job.job_id:
                del self._active_jobs[job.application_id]
                logger.info(f"Job {job.job_id} finalized and removed from active memory.")

    async def update_status(self, application_id: str, job_id: str, status: str, sf_service: SalesforceService, message: Optional[str] = None, progress: Optional[Dict[str, Any]] = None, logs: Optional[str] = None):
        async with self._lock:
            job = self._active_jobs.get(application_id)
            if not job or job.job_id != job_id or job.is_stale:
                return
            
            job.status = status
            job.last_updated_at = datetime.now(timezone.utc)
            if message:
                job.message = message
            if progress:
                job.progress = progress

        if job.opportunity_id:
            await sf_service.upsert_eedl_ai_server_job(
                job_id=job.job_id,
                opportunity_id=job.opportunity_id,
                status=job.status,
                message=job.message,
                progress_details=json.dumps(job.progress) if job.progress else None,
                logs=logs,
            )
        else:
            await sf_service.upsert_ai_server_job(
                job_id=job.job_id,
                application_id=job.application_id,
                status=job.status,
                message=job.message,
                progress_details=json.dumps(job.progress) if job.progress else None,
                logs=logs,
            )

    async def get_job_status(self, application_id: str, sf_service: SalesforceService) -> Optional[Dict[str, Any]]:
        async with self._lock:
            active_job = self._active_jobs.get(application_id)

        if active_job:
            if active_job.opportunity_id:
                sf_status = await sf_service.get_latest_eedl_ai_server_job(active_job.opportunity_id)
            else:
                sf_status = await sf_service.get_latest_ai_server_job(application_id)
            if sf_status:
                return sf_status
            return active_job.model_dump()

        logger.info(f"No active job for {application_id} in memory. Querying Salesforce org {sf_service.instance_url}.")
        # Without an active job we don't know the type — try admission first, then EEDL
        try:
            result = await sf_service.get_latest_ai_server_job(application_id)
            if result:
                return result
        except Exception:
            pass
        return await sf_service.get_latest_eedl_ai_server_job(application_id)

    async def get_queue_overview(self, org_alias: Optional[str] = None) -> Dict[str, Any]:
        async with self._lock:
            job_dicts = [
                job.model_dump()
                for job in self._active_jobs.values()
                if org_alias is None or job.org_alias == org_alias
            ]

        job_dicts.sort(key=lambda x: x['last_updated_at'], reverse=True)

        return {
            "active_jobs": await get_active_processing_slots_count(),
            "tracked_jobs_total": len(job_dicts),
            "all_jobs": job_dicts
        }

    async def is_job_active(self, application_id: str) -> bool:
        async with self._lock:
            return application_id in self._active_jobs and not self._active_jobs[application_id].is_stale

    async def get_all_active_jobs(self) -> Dict[str, Job]:
        """Returns a copy of all active jobs (non-stale only)."""
        async with self._lock:
            return {
                app_id: job
                for app_id, job in self._active_jobs.items()
                if not job.is_stale
            }

    async def admin_clear_job(self, application_id: str, sf_service: SalesforceService) -> tuple[bool, Optional[Dict[str, Any]]]:
        async with self._lock:
            job = self._active_jobs.get(application_id)
            if not job:
                return False, None
            job.is_stale = True
            original_status = job.model_dump()

        logger.warning(f"Admin clearing job {job.job_id} for App {application_id} in org {sf_service.instance_url}")
        if job.opportunity_id:
            await sf_service.upsert_eedl_ai_server_job(
                job_id=job.job_id,
                opportunity_id=job.opportunity_id,
                status="failed",
                message="Manually cleared by admin.",
            )
        else:
            await sf_service.upsert_ai_server_job(
                job_id=job.job_id,
                application_id=job.application_id,
                status="failed",
                message="Manually cleared by admin.",
            )

        process_manager = await get_process_manager()
        await process_manager.kill_job_worker(job.job_id)

        async with self._lock:
            self._active_jobs.pop(application_id, None)

        return True, original_status

_job_manager_instance: Optional[JobManager] = None
_job_manager_lock = asyncio.Lock()
async def get_job_manager_dependency() -> JobManager:
    """Provides a singleton instance of the JobManager."""
    global _job_manager_instance
    if not _job_manager_instance:
        async with _job_manager_lock:
            if not _job_manager_instance:
                _job_manager_instance = JobManager()
    return _job_manager_instance
