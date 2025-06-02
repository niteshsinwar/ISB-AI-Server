import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any
import base64
import asyncio # Ensure asyncio is imported for the lock

from fastapi import APIRouter, HTTPException, Depends, FastAPI
from pydantic import BaseModel, Field

# Import the DocumentExtractionCrewOrchestrator
from app.crew.document_crew import DocumentExtractionCrewOrchestrator
# Import the service for OCR
from app.services.document_extraction_service import extract_text_from_file as service_ocr_extract_text
# Import the LegacySalesforceService
from app.services.legacy_salesforce_service import LegacySalesforceService
# Import app instance to get app version
from app.core.app_instance import get_app_instance
import os

logger = logging.getLogger(__name__)
router = APIRouter()

# --- Pydantic Models ---
class LegacyExtractRequestBody(BaseModel):
    user_prompt: str = Field(..., description="Natural language prompt describing what to extract.")
    content_version_id: Optional[str] = Field(None, description="Salesforce ContentVersionId (if fetching from Salesforce).")
    file_base64_data: Optional[str] = Field(None, description="Base64 encoded string of the file data.")
    file_extension: Optional[str] = Field(None, description="The file extension (e.g., 'pdf', 'jpg'). Required if not using content_version_id or if file_base64_data is provided.")

class CrewExtractionResponse(BaseModel):
    request_id: str = Field(..., serialization_alias="_id")
    created_at: str
    last_updated_at: str
    user_prompt: str
    file_name: Optional[str] = None
    file_extension: Optional[str] = None
    request_type_id: str
    extracted_data: Optional[Dict[str, Any]] = None
    message: str
    error: Optional[str] = None
    document_text_preview: Optional[str] = None


# --- Dependency for LegacySalesforceService ---
_legacy_sf_service_instance: Optional[LegacySalesforceService] = None
_legacy_sf_service_lock = asyncio.Lock()

async def get_legacy_sf_service() -> LegacySalesforceService:
    """FastAPI dependency for LegacySalesforceService."""
    global _legacy_sf_service_instance
    
    if not os.getenv("SALESFORCE_AUTH_MODE"):
        logger.warning("Legacy SF Service dependency requested, but SALESFORCE_AUTH_MODE not set. Service cannot be initialized.")
        raise HTTPException(status_code=503, detail="Legacy Salesforce Service not configured (SALESFORCE_AUTH_MODE not set).")

    if _legacy_sf_service_instance is None:
        async with _legacy_sf_service_lock:
            if _legacy_sf_service_instance is None:
                try:
                    logger.info("Initializing LegacySalesforceService singleton for dependency...")
                    _legacy_sf_service_instance = LegacySalesforceService()
                    if not _legacy_sf_service_instance.base_url:
                         logger.error("LegacySalesforceService initialized but base_url is missing. Connection likely failed.")
                         _legacy_sf_service_instance = None
                         raise HTTPException(status_code=503, detail="Legacy Salesforce Service unavailable: Connection failed during init.")
                    logger.info("LegacySalesforceService singleton created.")
                except Exception as e:
                    logger.error(f"Failed to initialize LegacySalesforceService: {e}", exc_info=True)
                    _legacy_sf_service_instance = None
                    raise HTTPException(status_code=503, detail=f"Legacy Salesforce Service unavailable: Initialization error: {str(e)}")
    try:
        _legacy_sf_service_instance._ensure_connected()
    except Exception as e:
        logger.error(f"LegacySalesforceService connection check failed: {e}", exc_info=True)
        async with _legacy_sf_service_lock:
            _legacy_sf_service_instance = None 
        raise HTTPException(status_code=503, detail=f"Legacy Salesforce Service connection issue: {str(e)}")
    return _legacy_sf_service_instance


@router.post("/extract", response_model=CrewExtractionResponse, summary="Legacy Document Data Extraction using AI Crew (JSON Input)")
async def legacy_crew_extract_data_endpoint(
    payload: LegacyExtractRequestBody, # Expects a JSON body
    app: FastAPI = Depends(get_app_instance),
    # Salesforce service dependency is now unconditional in signature
    # but its usage will be conditional, and the dependency itself checks config.
    sf_service: LegacySalesforceService = Depends(get_legacy_sf_service)
):
    """
    Endpoint for extracting specific data points from a document based on a user prompt,
    using an AI crew for intelligent analysis.
    Accepts a JSON body with 'user_prompt' and one of 'content_version_id' or
    ('file_base64_data' and 'file_extension').
    """
    request_id_str = str(uuid.uuid4())
    current_time_utc_iso = datetime.now(timezone.utc).isoformat()
    app_version_major = app.version.split('.')[0] if hasattr(app, 'version') and isinstance(app.version, str) else "legacy"
    request_type_id = f"legacy_doc_crew_extract_v{app_version_major}"

    user_prompt = payload.user_prompt
    content_version_id = payload.content_version_id
    current_base64_data = payload.file_base64_data
    actual_file_extension = payload.file_extension
    
    filename_for_response: str = "input_file"
    final_extracted_text: Optional[str] = None

    logger.info(f"Request ID {request_id_str}: Legacy Crew /extract called. Prompt: '{user_prompt[:50]}...', CV_ID: {content_version_id}, Has Base64: {current_base64_data is not None}, Ext: {actual_file_extension}")

    # Determine file source and get base64 data
    if content_version_id:
        # The get_legacy_sf_service dependency would have already raised an error 
        # if SALESFORCE_AUTH_MODE was not set. sf_service will be an instance here.
        try:
            logger.info(f"Request ID {request_id_str}: Fetching file from Salesforce using CV_ID: {content_version_id}")
            file_bytes, inferred_ext, sf_filename = sf_service.download_file_in_memory(content_version_id)
            current_base64_data = base64.b64encode(file_bytes).decode('utf-8')
            actual_file_extension = inferred_ext 
            filename_for_response = sf_filename
            logger.info(f"Request ID {request_id_str}: File '{filename_for_response}' (ext: {actual_file_extension}) fetched from Salesforce.")
        except FileNotFoundError:
            logger.warning(f"Request ID {request_id_str}: File not found in Salesforce for CV_ID: {content_version_id}")
            raise HTTPException(status_code=404, detail=f"File not found in Salesforce for ContentVersionId: {content_version_id}")
        except Exception as e:
            logger.error(f"Request ID {request_id_str}: Error fetching file from Salesforce (CV_ID: {content_version_id}): {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error fetching file from Salesforce: {str(e)}")
    elif current_base64_data:
        if not actual_file_extension:
            logger.error(f"Request ID {request_id_str}: 'file_extension' is mandatory when providing 'file_base64_data'.")
            raise HTTPException(status_code=400, detail="'file_extension' is mandatory with 'file_base64_data'.")
        filename_for_response = f"base64_input.{actual_file_extension}"
    else:
        logger.error(f"Request ID {request_id_str}: No valid file input. Provide 'content_version_id' or ('file_base64_data' and 'file_extension').")
        raise HTTPException(status_code=400, detail="No file input. Provide 'content_version_id' or ('file_base64_data' and 'file_extension').")

    if not current_base64_data: 
        logger.error(f"Request ID {request_id_str}: No base64 data available after processing inputs.")
        raise HTTPException(status_code=500, detail="Internal error: No file data to process.")
    if not actual_file_extension:
        logger.error(f"Request ID {request_id_str}: File extension could not be determined.")
        raise HTTPException(status_code=400, detail="File extension is missing and could not be determined.")

    try:
        logger.info(f"Request ID {request_id_str}: Extracting raw text from '{filename_for_response}' (ext: {actual_file_extension}).")
        raw_document_text = await service_ocr_extract_text(current_base64_data, actual_file_extension)
        final_extracted_text = raw_document_text
        
        if raw_document_text.startswith("Error:"):
            ocr_error_message = raw_document_text.replace("Error: ", "", 1)
            logger.warning(f"Request ID {request_id_str}: OCR service error: {ocr_error_message}")
            return CrewExtractionResponse(
                request_id=request_id_str, created_at=current_time_utc_iso, last_updated_at=datetime.now(timezone.utc).isoformat(),
                user_prompt=user_prompt, file_name=filename_for_response, file_extension=actual_file_extension,
                request_type_id=request_type_id, error=f"OCR failed: {ocr_error_message}",
                message="Text extraction from document failed before AI crew processing."
            )
        
        if raw_document_text.startswith("Note: No text found"):
            logger.info(f"Request ID {request_id_str}: OCR found no text for '{filename_for_response}'.")

        logger.info(f"Request ID {request_id_str}: Invoking DocumentExtractionCrew. Prompt: '{user_prompt[:50]}...'. Text length: {len(raw_document_text)}")
        crew_orchestrator = DocumentExtractionCrewOrchestrator(user_prompt=user_prompt, document_content=raw_document_text)
        crew_result_dict: Dict[str, Any] = crew_orchestrator.run()

        crew_error = crew_result_dict.get("Error")
        crew_clarification = crew_result_dict.get("Clarification")

        if crew_error:
            logger.warning(f"Request ID {request_id_str}: DocumentExtractionCrew reported an error: {crew_error}")
            return CrewExtractionResponse(
                request_id=request_id_str, created_at=current_time_utc_iso, last_updated_at=datetime.now(timezone.utc).isoformat(),
                user_prompt=user_prompt, file_name=filename_for_response, file_extension=actual_file_extension,
                request_type_id=request_type_id, extracted_data=crew_result_dict, error=str(crew_error),
                message=f"AI crew processing encountered an issue. {crew_clarification if crew_clarification else ''}".strip(),
                document_text_preview=raw_document_text[:200] + "..." if raw_document_text and len(raw_document_text) > 200 else raw_document_text
            )
        else:
            logger.info(f"Request ID {request_id_str}: DocumentExtractionCrew successfully processed '{filename_for_response}'.")
            return CrewExtractionResponse(
                request_id=request_id_str, created_at=current_time_utc_iso, last_updated_at=datetime.now(timezone.utc).isoformat(),
                user_prompt=user_prompt, file_name=filename_for_response, file_extension=actual_file_extension,
                request_type_id=request_type_id, extracted_data=crew_result_dict,
                message="Document data extracted successfully by AI crew." + (f" Clarification: {crew_clarification}" if crew_clarification else ""),
                document_text_preview=raw_document_text[:200] + "..." if raw_document_text and len(raw_document_text) > 200 else raw_document_text
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Request ID {request_id_str}: Unexpected error during legacy crew extraction for '{filename_for_response}': {e}", exc_info=True)
        if final_extracted_text is not None:
            logger.debug(f"Request ID {request_id_str}: Text passed to crew (first 500 chars): {final_extracted_text[:500]}")
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred during AI crew document extraction: {str(e)}")