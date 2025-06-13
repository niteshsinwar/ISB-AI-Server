# project_root/app/endpoints/admin_endpoints.py
import logging
from datetime import datetime, timezone
from typing import List
from pydantic import BaseModel

from fastapi import APIRouter, HTTPException, Body, Depends, FastAPI

from app.services.document_extraction_service import get_text_extractor
from app.services.salesforce_service import get_sf_service_dependency
from app.config import (
    APP_VERSION, SUSPICIOUS_BLOCK_DURATION_SECONDS
)
from app.core.rate_limit_state import (
    get_suspicious_clients_info,
    admin_clear_processing_status, admin_unblock_client
)
from app.core.app_instance import get_app_instance
from app.schemas.responses import (
    HealthResponse, DependencyStatus, SuspiciousClientsResponse, BlockedClientInfo,
    ClearStatusResponse, UnblockClientResponse, JobStatusResponse
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health",
            response_model=HealthResponse,
            summary="Service Health Check",
            tags=["Health & Status"])
async def health_check_endpoint(
    app: FastAPI = Depends(get_app_instance)
):
    """
    Provides a fast and lightweight health check of the service and its critical dependencies.
    This endpoint should not be used for operational metrics, see /queue-overview instead.
    """
    checks = []
    overall_status = "ok"

    # Check 1: Gemini/Text Extractor
    try:
        # The dependency function `get_text_extractor` will raise an exception if
        # the extractor isn't initialized, which is caught below.
        extractor = await get_text_extractor()
        
        # If we get here, the extractor is initialized. We can provide more detail.
        model_name = "unknown"
        if hasattr(extractor, 'ocr_processor') and hasattr(extractor.ocr_processor, 'llm'):
            model_name = extractor.ocr_processor.llm.model
            
        checks.append(DependencyStatus(name="Gemini API", status="ok", details=f"Extractor ready. Using model: {model_name}"))

    except Exception as e:
        # This catches any failure during extractor initialization.
        overall_status = "degraded"
        logger.error(f"Gemini API health check failed: {e}", exc_info=True)
        checks.append(DependencyStatus(name="Gemini API", status="unavailable", details=f"Initialization Error: {str(e)}"))

    # Check 2: Salesforce
    try:
        sf_service = await get_sf_service_dependency()
        if sf_service and sf_service.instance_url:
            checks.append(DependencyStatus(name="Salesforce", status="ok", details=f"Instance: {sf_service.instance_url}"))
        else:
            overall_status = "degraded"
            checks.append(DependencyStatus(name="Salesforce", status="unavailable", details="Salesforce service initialized but connection details are missing."))
    except HTTPException as http_e:
        overall_status = "degraded"
        checks.append(DependencyStatus(name="Salesforce", status="unavailable", details=http_e.detail))
    except Exception as e:
        overall_status = "degraded"
        checks.append(DependencyStatus(name="Salesforce", status="unavailable", details=f"Initialization failed: {str(e)}"))

    return HealthResponse(
        status=overall_status,
        timestamp=datetime.now(timezone.utc),
        application_version=app.version if hasattr(app, 'version') else APP_VERSION,
        checks=checks
    )


@router.delete("/processing-status/{application_id}",
               response_model=ClearStatusResponse,
               summary="Admin: Clear Processing Status",
               tags=["Admin"])
async def admin_clear_status_endpoint(application_id: str):
    """Manually clears the processing status of a stuck job, releasing its processing slot if active."""
    if not (len(application_id) == 15 or len(application_id) == 18):
        raise HTTPException(status_code=400, detail="Invalid application_id format.")
    
    cleared, old_status_data = await admin_clear_processing_status(application_id)
    if not cleared or not old_status_data:
        raise HTTPException(status_code=404, detail=f"No processing record found for application ID: {application_id}")

    # Adapt the raw dict to the JobStatusResponse model for a clean, consistent response
    previous_status = JobStatusResponse(
        job_id=old_status_data.get('job_id', 'N/A'),
        application_id=application_id,
        status=old_status_data.get('status', 'unknown'),
        message=old_status_data.get('error_message'),
        created_at=old_status_data.get('timestamp', datetime.now(timezone.utc)),
        last_updated_at=old_status_data.get('timestamp', datetime.now(timezone.utc)),
        progress=old_status_data.get('detailed_summary')
    )
    
    return ClearStatusResponse(
        message=f"Processing status cleared for application ID: {application_id}",
        previous_status_info=previous_status
    )


class ClientFingerprintBody(BaseModel):
    client_fingerprint: str

@router.post("/unblock-client",
             response_model=UnblockClientResponse,
             summary="Admin: Unblock Client",
             tags=["Admin"])
async def admin_unblock_client_endpoint(body: ClientFingerprintBody):
    """Manually unblocks a client that was blocked due to suspicious activity."""
    client_fingerprint = body.client_fingerprint
    if not client_fingerprint or len(client_fingerprint) < 8:
        raise HTTPException(status_code=400, detail="Valid client_fingerprint is required.")
        
    unblocked, old_block_info = await admin_unblock_client(client_fingerprint)
    if not unblocked:
        raise HTTPException(status_code=404, detail=f"Client fingerprint '{client_fingerprint[:8]}...' not found in the blocked list.")
    
    return UnblockClientResponse(
        message=f"Client {client_fingerprint[:8]}... has been unblocked.",
        previous_block_info=old_block_info or {}
    )


@router.get("/suspicious-clients",
            response_model=SuspiciousClientsResponse,
            summary="Admin: List Suspicious Clients",
            tags=["Admin"])
async def admin_get_suspicious_clients_endpoint():
    """Retrieves a list of all currently blocked client fingerprints."""
    suspicious_clients_data = await get_suspicious_clients_info()
    now = datetime.now(timezone.utc)
    
    client_list: List[BlockedClientInfo] = []
    for fp, info in suspicious_clients_data.items():
        blocked_at_dt = info.get("blocked_at", now)
        time_remaining = max(0, SUSPICIOUS_BLOCK_DURATION_SECONDS - int((now - blocked_at_dt).total_seconds()))

        client_list.append(BlockedClientInfo(
            client_fingerprint_prefix=fp[:8] + "...",
            full_fingerprint_for_unblock=fp,
            blocked_at=blocked_at_dt,
            reason=info.get("reason", "Unknown"),
            original_request_count=info.get("request_count", 0),
            block_time_remaining_seconds=time_remaining
        ))
        
    return SuspiciousClientsResponse(
        currently_blocked_count=len(client_list),
        configured_block_duration_seconds=SUSPICIOUS_BLOCK_DURATION_SECONDS,
        blocked_clients=client_list
    )
