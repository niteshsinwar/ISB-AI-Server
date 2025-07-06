# project_root/app/processors/education_processor.py
import logging
import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.config import (
    EDUCATION_LOG_OBJECT_API_NAME,
    MAX_SALESFORCE_REPORT_LENGTH,
    READABLE_OBJECT_NAMES
)
from app.services.document_extraction_service import DocumentExtractionError
from app.services.salesforce_service import SalesforceAPIError

if TYPE_CHECKING:
    from app.services.salesforce_service import SalesforceService
    from app.crew.education_crew import EducationVerificationCrewOrchestrator

logger = logging.getLogger(__name__)

def _format_error(record_id: str, component: str, reason: str, technical_error: str) -> str:
    """Creates a standardized error message for logging and reporting."""
    category = READABLE_OBJECT_NAMES[EDUCATION_LOG_OBJECT_API_NAME]
    return f"({category})-({record_id})-({component})-({reason}): {technical_error}"

async def process_single_education_history_detail(
    sf_service: "SalesforceService",
    education_log_id: str,
    parent_application_id: str,
    item_index: Optional[int] = None
):
    readable_name = READABLE_OBJECT_NAMES.get(EDUCATION_LOG_OBJECT_API_NAME, "Education Record")
    logger.info(f"Starting {readable_name} processing for Log ID: {education_log_id}")

    from app.services.document_extraction_service import extract_text_from_file
    from app.crew.education_crew import EducationVerificationCrewOrchestrator

    try:
        details = await asyncio.to_thread(
            sf_service.get_record_detail_from_apex, education_log_id, EDUCATION_LOG_OBJECT_API_NAME
        )
        
        record_data = details.get("recordData", {})
        document_payload = details.get("documentPayload")
        actual_education_detail_id = record_data.get("Id")

        if not document_payload:
            raise ValueError("Document payload was missing from the Salesforce record.")
        if not actual_education_detail_id:
            raise ValueError("Could not identify the underlying EducationHistory__c ID.")

        base64_data = document_payload.get("base64Data")
        file_extension = document_payload.get("fileExtension")
        if not base64_data or not file_extension:
            raise ValueError("Document content (base64) or file extension missing.")
            
        document_text_string = await extract_text_from_file(base64_data, file_extension, record_id=education_log_id)

        edu_crew = EducationVerificationCrewOrchestrator(record_data, document_text_string)
        report_dict = await asyncio.to_thread(edu_crew.run)
        if not report_dict:
            raise ValueError("Crew did not return a valid report.")

        name_suffix = record_data.get('degreeLevel') or item_index or education_log_id
        summary_name = f"Education Analysis ({name_suffix})"
        summary_id = await asyncio.to_thread(
            sf_service.upsert_verification_summary,
            application_id=parent_application_id,
            report_content=report_dict.get('field_comparison_summary', '')[:MAX_SALESFORCE_REPORT_LENGTH],
            name_value=summary_name,
            overall_feedback=report_dict.get('overall_feedback'),
            confidence_range=report_dict.get('confidence_range'),
            education_history_id=actual_education_detail_id
        )
        
        logger.info(f"Successfully processed {readable_name} {education_log_id}. AVS ID: {summary_id}")

    except SalesforceAPIError as e:
        if '500 Server Error' in str(e):
            reason = "Data provider failed. This is likely due to a missing document or invalid data on the Salesforce record. Please verify the record and its attachments."
            raise ValueError(_format_error(education_log_id, "Salesforce Data", reason, str(e)))
        else:
            raise ValueError(_format_error(education_log_id, "Salesforce API", "An API error occurred", str(e)))
    except DocumentExtractionError as e:
        raise ValueError(_format_error(education_log_id, "Document Extraction", "Failed to extract text from document", str(e)))
    except Exception as e:
        raise ValueError(_format_error(education_log_id, "Processing", "An unexpected error occurred", str(e)))