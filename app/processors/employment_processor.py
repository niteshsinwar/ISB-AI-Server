# project_root/app/processors/employment_processor.py
import logging
import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.config import (
    EMPLOYMENT_LOG_OBJECT_API_NAME,
    MAX_SALESFORCE_REPORT_LENGTH,
    READABLE_OBJECT_NAMES
)
from app.services.document_extraction_service import DocumentExtractionError
from app.services.salesforce_service import SalesforceAPIError

if TYPE_CHECKING:
    from app.services.salesforce_service import SalesforceService
    from app.crew.employment_crew import EmploymentVerificationCrewOrchestrator

logger = logging.getLogger(__name__)

def _format_error(record_id: str, component: str, reason: str, technical_error: str) -> str:
    """Creates a standardized error message for logging and reporting."""
    category = READABLE_OBJECT_NAMES[EMPLOYMENT_LOG_OBJECT_API_NAME]
    return f"({category})-({record_id})-({component})-({reason}): {technical_error}"

async def process_single_employment_detail(
    sf_service: "SalesforceService",
    employment_log_id: str,
    parent_application_id: str,
    item_index: Optional[int] = None
):
    readable_name = READABLE_OBJECT_NAMES.get(EMPLOYMENT_LOG_OBJECT_API_NAME, "Employment Record")
    logger.info(f"Starting {readable_name} processing for Log ID: {employment_log_id}")

    from app.services.document_extraction_service import extract_text_from_file
    from app.crew.employment_crew import EmploymentVerificationCrewOrchestrator

    try:
        details = await asyncio.to_thread(
            sf_service.get_record_detail_from_apex, employment_log_id, EMPLOYMENT_LOG_OBJECT_API_NAME
        )

        record_data = details.get("recordData", {})
        document_payload = details.get("documentPayload")
        actual_employment_detail_id = record_data.get("Id")

        if not document_payload:
            raise ValueError("Document payload was missing from the Salesforce record.")
        if not actual_employment_detail_id:
            raise ValueError("Could not identify the underlying Affiliation__c ID.")

        base64_data = document_payload.get("base64Data")
        file_extension = document_payload.get("fileExtension")
        if not base64_data or not file_extension:
            raise ValueError("Document content (base64) or file extension missing.")
            
        document_text_string = await extract_text_from_file(base64_data, file_extension, record_id=employment_log_id)

        emp_crew = EmploymentVerificationCrewOrchestrator(record_data, document_text_string)
        report_dict = await asyncio.to_thread(emp_crew.run)
        if not report_dict:
            raise ValueError("Crew did not return a valid report.")

        summary_name = f"Employment Analysis ({item_index})" if item_index else f"Employment Analysis"
        summary_id = await asyncio.to_thread(
            sf_service.upsert_verification_summary,
            application_id=parent_application_id,
            report_content=report_dict.get('field_comparison_summary', '')[:MAX_SALESFORCE_REPORT_LENGTH],
            name_value=summary_name,
            overall_feedback=report_dict.get('overall_feedback'),
            confidence_range=report_dict.get('confidence_range'),
            affiliation_id=actual_employment_detail_id
        )

        logger.info(f"Successfully processed {readable_name} {employment_log_id}. AVS ID: {summary_id}")

    except SalesforceAPIError as e:
        if '500 Server Error' in str(e):
            reason = "Data provider failed. This is likely due to a missing document or invalid data on the Salesforce record. Please verify the record and its attachments."
            raise ValueError(_format_error(employment_log_id, "Salesforce Data", reason, str(e)))
        else:
            raise ValueError(_format_error(employment_log_id, "Salesforce API", "An API error occurred", str(e)))
    except DocumentExtractionError as e:
        raise ValueError(_format_error(employment_log_id, "Document Extraction", "Failed to extract text from document", str(e)))
    except Exception as e:
        raise ValueError(_format_error(employment_log_id, "Processing", "An unexpected error occurred", str(e)))