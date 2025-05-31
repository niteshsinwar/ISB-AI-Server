# project_root/app/endpoints/legacy_endpoints.py
import logging
from fastapi import APIRouter, File, UploadFile, Form, HTTPException
from typing import Optional
from pydantic import BaseModel
import base64

# Assuming the old /extract endpoint used the document_text_extractor directly
from app.services.document_extraction_service import extract_text_from_file as service_extract_text_from_file

logger = logging.getLogger(__name__)
router = APIRouter()

# This is a placeholder for the legacy /extract endpoint mentioned in the original main.py
# The original main.py had:
# from legacy_endpoint_controllers import legacy_router
# app.include_router(legacy_router)
#
# The actual implementation of the old /extract endpoint is not fully known from the provided files,
# but it likely involved taking a file (or base64 data) and its extension.

class LegacyExtractionResponse(BaseModel):
    filename: Optional[str] = None
    file_extension: Optional[str] = None
    extracted_text: Optional[str] = None
    error: Optional[str] = None
    message: str


@router.post("/extract", response_model=LegacyExtractionResponse, summary="Legacy Text Extraction")
async def legacy_extract_text_endpoint(
    file_extension: str = Form(...),
    file_base64_data: Optional[str] = Form(None), # For base64 input
    upload_file: Optional[UploadFile] = File(None) # For direct file upload
):
    """
    Legacy endpoint for extracting text from a document.
    Accepts either base64 encoded file data or a direct file upload.
    `file_extension` is mandatory.
    """
    logger.info(f"Legacy /extract endpoint called. Extension: {file_extension}, Has Base64: {file_base64_data is not None}, Has UploadFile: {upload_file is not None}")

    current_base64_data: Optional[str] = file_base64_data
    filename_for_response: str = "uploaded_file"

    if not current_base64_data and not upload_file:
        raise HTTPException(status_code=400, detail="Either 'file_base64_data' or 'upload_file' must be provided.")
    if current_base64_data and upload_file:
        raise HTTPException(status_code=400, detail="Provide either 'file_base64_data' or 'upload_file', not both.")

    if upload_file:
        if upload_file.filename:
             filename_for_response = upload_file.filename
        try:
            file_bytes = await upload_file.read()
            current_base64_data = base64.b64encode(file_bytes).decode('utf-8')
            logger.info(f"Legacy /extract: Read {len(file_bytes)} bytes from uploaded file '{filename_for_response}'.")
        except Exception as e:
            logger.error(f"Legacy /extract: Error reading uploaded file '{filename_for_response}': {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error reading uploaded file: {str(e)}")
        finally:
            await upload_file.close()
            
    if not current_base64_data: # Should be caught above, but as a safeguard
        logger.error("Legacy /extract: No base64 data available after processing inputs.")
        raise HTTPException(status_code=500, detail="Internal error: No file data to process.")

    try:
        # Call the refactored document extraction service
        # The service_extract_text_from_file function is what was originally imported in main.py
        # and it internally uses the DocumentTextExtractor class instance.
        extracted_text_result = await service_extract_text_from_file(current_base64_data, file_extension)
        
        if extracted_text_result.startswith("Error:"):
            logger.warning(f"Legacy /extract: Text extraction service reported an error: {extracted_text_result}")
            return LegacyExtractionResponse(
                filename=filename_for_response,
                file_extension=file_extension,
                error=extracted_text_result,
                message="Text extraction failed."
            )
        elif extracted_text_result.startswith("Note: No text found"):
            logger.info(f"Legacy /extract: Text extraction service found no text for '{filename_for_response}'.")
            return LegacyExtractionResponse(
                filename=filename_for_response,
                file_extension=file_extension,
                extracted_text="", # Or the note itself if preferred
                message="No text found in the document."
            )
        else:
            logger.info(f"Legacy /extract: Successfully extracted text from '{filename_for_response}'. Length: {len(extracted_text_result)}")
            return LegacyExtractionResponse(
                filename=filename_for_response,
                file_extension=file_extension,
                extracted_text=extracted_text_result,
                message="Text extracted successfully."
            )
            
    except Exception as e:
        logger.error(f"Legacy /extract: Unexpected error during text extraction for '{filename_for_response}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred during text extraction: {str(e)}")

# Add other legacy routes here if they existed.
