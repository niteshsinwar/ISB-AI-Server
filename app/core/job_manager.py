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

logger = logging.getLogger(__name__)

class Job(BaseModel):
    job_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    salesforce_job_record_id: Optional[str] = None
    application_id: str
    status: str = "queued"
    message: Optional[str] = "Waiting for available slot."
    client_fingerprint: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    progress: Dict[str, Any] = {}
    is_stale: bool = False

class JobManager:
    def __init__(self):
        self._active_jobs: Dict[str, Job] = {}
        self._lock = asyncio.Lock()

    async def create_job(self, application_id: str, client_fingerprint: str, sf_service: SalesforceService) -> Job:
        async with self._lock:
            if application_id in self._active_jobs:
                self._active_jobs[application_id].is_stale = True
            
            new_job = Job(application_id=application_id, client_fingerprint=client_fingerprint)
            
            sf_job_id = await sf_service.upsert_ai_server_job(
                job_id=new_job.job_id,
                application_id=new_job.application_id,
                status=new_job.status,
                client_fingerprint=new_job.client_fingerprint
            )
            if not sf_job_id:
                raise RuntimeError("Failed to create initial job record in Salesforce.")
            
            new_job.salesforce_job_record_id = sf_job_id
            self._active_jobs[application_id] = new_job
            logger.info(f"Created job {new_job.job_id} for App {application_id} in org {sf_service.instance_url}")
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

    async def update_status(self, application_id: str, job_id: str, status: str, sf_service: SalesforceService, message: Optional[str] = None, progress: Optional[Dict[str, Any]] = None):
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

        await sf_service.upsert_ai_server_job(
            job_id=job.job_id,
            application_id=job.application_id,
            status=job.status,
            message=job.message,
            progress_details=json.dumps(job.progress) if job.progress else None
        )

    async def get_job_status(self, application_id: str, sf_service: SalesforceService) -> Optional[Dict[str, Any]]:
        async with self._lock:
            if active_job := self._active_jobs.get(application_id):
                return active_job.model_dump()
        
        logger.info(f"No active job for App {application_id} in memory. Querying Salesforce org {sf_service.instance_url}.")
        return await sf_service.get_latest_ai_server_job(application_id)

    async def get_queue_overview(self) -> Dict[str, Any]:
        async with self._lock:
            jobs = [job.model_dump() for job in self._active_jobs.values()]
        return {
            "active_jobs": await get_active_processing_slots_count(),
            "tracked_jobs_total": len(jobs),
            "all_jobs": sorted(jobs, key=lambda x: x['last_updated_at'], reverse=True)
        }

    async def is_job_active(self, application_id: str) -> bool:
        async with self._lock:
            return application_id in self._active_jobs and not self._active_jobs[application_id].is_stale

    async def admin_clear_job(self, application_id: str, sf_service: SalesforceService) -> tuple[bool, Optional[Dict[str, Any]]]:
        async with self._lock:
            job = self._active_jobs.get(application_id)
            if not job:
                return False, None
            
            original_status = job.model_dump()
            logger.warning(f"Admin clearing job {job.job_id} for App {application_id} in org {sf_service.instance_url}")
            
            await sf_service.upsert_ai_server_job(
                job_id=job.job_id,
                application_id=job.application_id,
                status="failed",
                message="Manually cleared by admin."
            )
            
            if job.status == "processing":
                await release_processing_slot()
                
            del self._active_jobs[application_id]
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