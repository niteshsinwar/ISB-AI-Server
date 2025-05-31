# project_root/app/endpoints/admin_endpoints.py
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from pydantic import BaseModel

from fastapi import APIRouter, HTTPException, Body, Depends, FastAPI

from app.services.document_extraction_service import get_text_extractor, DocumentTextExtractor
from app.services.salesforce_service import SalesforceService, get_sf_service_dependency
from app.config import (
    APP_VERSION, MAX_CONCURRENT_PROCESSING_SLOTS, ACTIVE_PROCESSING_TIMEOUT_SECONDS,
    RECENTLY_PROCESSED_TTL_SECONDS, MAX_GLOBAL_REQUESTS_PER_WINDOW, GLOBAL_RATE_LIMIT_WINDOW_SECONDS,
    MAX_CLIENT_REQUESTS_PER_WINDOW, CLIENT_RATE_LIMIT_WINDOW_SECONDS,
    MIN_SECONDS_BETWEEN_SAME_APP_REQUESTS, SUSPICIOUS_BLOCK_DURATION_SECONDS
)
from app.core.rate_limit_state import (
    get_active_processing_slots_count, get_all_processing_statuses,
    get_global_request_timestamps_count, get_suspicious_clients_info,
    admin_clear_processing_status, admin_unblock_client
)
# Import app instance getter from app.main
from app.core.app_instance import get_app_instance

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health", tags=["Health & Status"])
async def health_check_endpoint(
    app: FastAPI = Depends(get_app_instance), # Get app instance via dependency
    # Optional: include sf_service dependency to check its status too
    # sf_service: SalesforceService = Depends(get_sf_service_dependency) 
):
    extractor_available = "Unknown"
    try:
        # Assuming get_text_extractor is now a proper dependency or singleton accessor
        extractor = await get_text_extractor() 
        if extractor and extractor.gemini_model: 
            extractor_available = "Gemini OCR Model Initialized"
        elif extractor: 
            extractor_available = "Extractor Initialized, Gemini OCR Model NOT Initialized (check GOOGLE_API_KEY)"
        else: 
            extractor_available = "Text Extractor NOT Initialized"
    except Exception as e:
        extractor_available = f"Error checking text extractor: {str(e)}"
        logger.error(f"Health check: Error with text_extractor: {e}", exc_info=True)

    sf_service_status = "Unknown"
    try:
        # To check SalesforceService, we'd ideally have a lightweight check method
        # For now, just trying to instantiate it via its dependency function can be a basic check
        # Note: This will attempt to connect to Salesforce on each health check if not careful.
        # Consider a cached status or a more specific health check method in SalesforceService.
        # For this refactor, we'll assume get_sf_service_dependency handles errors.
        temp_sf_service = await get_sf_service_dependency() # Use await if it's async
        if temp_sf_service and temp_sf_service.sf and temp_sf_service.instance_url:
             sf_service_status = f"SalesforceService appears initialized (Instance Host: {temp_sf_service.instance_url})"
        else:
             sf_service_status = "SalesforceService initialized but connection/instance details missing."
    except HTTPException as http_e: # If dependency raises HTTPException (e.g., 503)
        sf_service_status = f"SalesforceService unavailable: {http_e.detail}"
        logger.warning(f"Health check: Salesforce service dependency raised HTTPException: {http_e.detail}")
    except Exception as e:
        sf_service_status = f"SalesforceService initialization failed with unexpected error: {str(e)}"
        logger.error(f"Health check: Error with SalesforceService: {e}", exc_info=True)
        
    app_version_from_instance = app.version if hasattr(app, 'version') and isinstance(app.version, str) else APP_VERSION
    
    active_slots = await get_active_processing_slots_count()
    all_statuses = await get_all_processing_statuses()
    processing_counts = {"processing": 0, "completed": 0, "failed": 0, "other": 0}
    for data in all_statuses.values():
        status = data.get("status", "other")
        processing_counts[status] = processing_counts.get(status, 0) + 1
        
    global_req_count = await get_global_request_timestamps_count()
    suspicious_clients = await get_suspicious_clients_info()

    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "application_version": app_version_from_instance,
        "text_extractor_status": extractor_available,
        "salesforce_service_status": sf_service_status,
        "processing_info": {
            "active_processing_slots": f"{active_slots}/{MAX_CONCURRENT_PROCESSING_SLOTS}",
            "tracked_applications": len(all_statuses),
            "status_counts": processing_counts,
            "config_processing_timeout_seconds": ACTIVE_PROCESSING_TIMEOUT_SECONDS,
            "config_recently_processed_ttl_seconds": RECENTLY_PROCESSED_TTL_SECONDS
        },
        "rate_limiting_info": {
            "global_requests_in_window": global_req_count,
            "global_limit": f"{MAX_GLOBAL_REQUESTS_PER_WINDOW} per {GLOBAL_RATE_LIMIT_WINDOW_SECONDS}s",
            "client_limit": f"{MAX_CLIENT_REQUESTS_PER_WINDOW} per {CLIENT_RATE_LIMIT_WINDOW_SECONDS}s",
            "suspicious_clients_currently_blocked": len(suspicious_clients),
            "config_rapid_fire_protection_seconds": MIN_SECONDS_BETWEEN_SAME_APP_REQUESTS,
            "config_suspicious_block_duration_seconds": SUSPICIOUS_BLOCK_DURATION_SECONDS
        }
    }

@router.delete("/processing-status/{application_id}", summary="Admin: Clear Processing Status")
async def admin_clear_status_endpoint(application_id: str):
    if not (len(application_id) == 15 or len(application_id) == 18):
        raise HTTPException(status_code=400, detail="Invalid application_id format.")
    
    cleared, old_status_data = await admin_clear_processing_status(application_id)
    if not cleared:
        raise HTTPException(status_code=404, detail=f"No processing record found for application ID: {application_id}")
    
    return {
        "message": f"Processing status cleared for application ID: {application_id}",
        "previous_status_info": old_status_data
    }

class ClientFingerprintBody(BaseModel):
    client_fingerprint: str

@router.post("/unblock-client", summary="Admin: Unblock Client")
async def admin_unblock_client_endpoint(body: ClientFingerprintBody):
    client_fingerprint = body.client_fingerprint
    if not client_fingerprint or len(client_fingerprint) < 8: # Basic validation
        raise HTTPException(status_code=400, detail="Valid client_fingerprint is required.")
        
    unblocked, old_block_info = await admin_unblock_client(client_fingerprint)
    if not unblocked:
        raise HTTPException(status_code=404, detail=f"Client fingerprint '{client_fingerprint[:8]}...' not found in the blocked list.")
    
    return {
        "message": f"Client {client_fingerprint[:8]}... has been unblocked.",
        "previous_block_info": old_block_info
    }

@router.get("/suspicious-clients", summary="Admin: List Suspicious Clients")
async def admin_get_suspicious_clients_endpoint():
    suspicious_clients_data = await get_suspicious_clients_info()
    now = datetime.now(timezone.utc)
    
    client_list = []
    for fp, info in suspicious_clients_data.items():
        blocked_at_dt = info.get("blocked_at")
        time_remaining_seconds = -1
        if isinstance(blocked_at_dt, datetime):
            time_remaining_seconds = max(0, SUSPICIOUS_BLOCK_DURATION_SECONDS - int((now - blocked_at_dt).total_seconds()))
            blocked_at_iso = blocked_at_dt.isoformat()
        else: # Should not happen if data is consistent
            blocked_at_iso = str(blocked_at_dt)


        client_list.append({
            "client_fingerprint_prefix": fp[:8] + "...",
            "full_fingerprint_for_unblock": fp, # Provide full for admin use
            "blocked_at": blocked_at_iso,
            "reason": info.get("reason", "Unknown"),
            "original_request_count": info.get("request_count", 0),
            "block_time_remaining_seconds": time_remaining_seconds
        })
        
    return {
        "currently_blocked_count": len(client_list),
        "configured_block_duration_seconds": SUSPICIOUS_BLOCK_DURATION_SECONDS,
        "blocked_clients": client_list
    }
