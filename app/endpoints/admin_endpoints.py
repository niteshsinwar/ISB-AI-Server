import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, FastAPI

from app.services.salesforce_service import SalesforceService
from app.services.document_extraction_service import get_text_extractor
from app.core.job_manager import JobManager, get_job_manager_dependency
from app.core.app_instance import get_app_instance
from app.schemas.responses import (
    HealthResponse, DependencyStatus, ClearStatusResponse, JobStatusResponse
)

logger = logging.getLogger(__name__)

# MODIFIED: Create a "Router Factory" function
def create_admin_router(sf_service_dependency: Depends) -> APIRouter:
    """
    This factory creates and returns a router with all admin endpoints.
    It uses the provided dependency to inject the correct SalesforceService.
    """
    router = APIRouter()

    @router.get("/health",
                response_model=HealthResponse,
                summary="Service Health Check")
    async def health_check_endpoint(
        app: FastAPI = Depends(get_app_instance),
        # MODIFIED: Use the dependency passed to the factory
        sf_service: SalesforceService = Depends(sf_service_dependency)
    ):
        """Provides a health check against a specific Salesforce org's connection."""
        checks = []
        overall_status = "ok"

        # Check 1: Gemini/Text Extractor (Global)
        try:
            extractor = await get_text_extractor()
            model_name = "unknown"
            if hasattr(extractor, 'ocr_processor') and hasattr(extractor.ocr_processor, 'llm'):
                model_name = extractor.ocr_processor.llm.model
            checks.append(DependencyStatus(name="Gemini API", status="ok", details=f"Extractor ready. Using model: {model_name}"))
        except Exception as e:
            overall_status = "degraded"
            checks.append(DependencyStatus(name="Gemini API", status="unavailable", details=f"Initialization Error: {str(e)}"))

        # Check 2: Salesforce (Org-Specific)
        if sf_service and sf_service.instance_url:
            checks.append(DependencyStatus(name="Salesforce", status="ok", details=f"Instance: {sf_service.instance_url}"))
        else:
            overall_status = "degraded"
            checks.append(DependencyStatus(name="Salesforce", status="unavailable", details="Salesforce service connection failed."))

        return HealthResponse(
            status=overall_status,
            timestamp=datetime.now(timezone.utc),
            application_version=app.version,
            checks=checks
        )

    @router.delete("/processing-status/{application_id}",
                   response_model=ClearStatusResponse,
                   summary="Admin: Clear Stuck Job")
    async def admin_clear_status_endpoint(
        application_id: str,
        job_manager: JobManager = Depends(get_job_manager_dependency),
        # MODIFIED: Use the dependency passed to the factory
        sf_service: SalesforceService = Depends(sf_service_dependency)
    ):
        """Manually clears a stuck job from the active queue for a specific org."""
        if not (len(application_id) == 15 or len(application_id) == 18):
            raise HTTPException(status_code=400, detail="Invalid application_id format.")

        # CRITICAL: Pass the org-specific sf_service to the job manager
        cleared, old_status_data = await job_manager.admin_clear_job(application_id, sf_service=sf_service)

        if not cleared or not old_status_data:
            raise HTTPException(status_code=404, detail=f"No active job found for application ID: {application_id}")

        previous_status = JobStatusResponse(**old_status_data)
        return ClearStatusResponse(
            message=f"Processing status cleared for application ID: {application_id}",
            previous_status_info=previous_status
        )

    return router