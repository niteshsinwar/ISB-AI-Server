# project_root/app/processors/application_processor.py
import logging
import json
import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.config import (
    APPLICATION_OBJECT_API_NAME,
    APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP,
    MAX_SALESFORCE_REPORT_LENGTH,
    READABLE_OBJECT_NAMES
)
from app.services.document_extraction_service import DocumentExtractionError
from app.services.salesforce_service import SalesforceAPIError

if TYPE_CHECKING:
    from app.services.salesforce_service import SalesforceService
    from app.crew.application_crew import ApplicationVerificationCrewOrchestrator

logger = logging.getLogger(__name__)

def _format_error(record_id: str, component: str, reason: str, technical_error: str) -> str:
    """Creates a standardized error message for logging and reporting."""
    category = READABLE_OBJECT_NAMES[APPLICATION_OBJECT_API_NAME]
    return f"({category})-({record_id})-({component})-({reason}): {technical_error}"

async def process_single_application_detail(
    sf_service: "SalesforceService",
    application_id: str,
    parent_application_id: str,
    application_object_api_name: str,
    item_index: Optional[int] = None,
    extractor_instance: "FastDocumentExtractor" = None,
) -> str:
    """
    Processes the main Application record (Personal Detail) and its associated ID document.
    """
    readable_name = READABLE_OBJECT_NAMES.get(application_object_api_name, "Application")
    logger.info(f"Starting {readable_name} processing for ID: {application_id}")

    from app.services.document_extraction_service import extract_text_from_file, create_text_extractor
    from app.crew.application_crew import ApplicationVerificationCrewOrchestrator

    # Use provided extractor or create a per-job extractor
    if extractor_instance is None:
        extractor_instance = create_text_extractor()

    try:
        details = await asyncio.to_thread(
            sf_service.get_record_detail_from_apex, application_id, application_object_api_name
        )
        # Note: With reverted Apex, `details` will be None on error. The exception is now the primary signal.
        
        record_data = details.get("recordData", {})
        document_payload = details.get("documentPayload")
        salesforce_data_issue = details.get("Salesforce_data_issue_Summary")
        contact_id = record_data.get(APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP)

        # Check for graceful fallback scenario (from top level or record data)
        fallback_summary = salesforce_data_issue or record_data.get("Salesforce_data_issue_Summary")
        if fallback_summary:
            logger.warning(f"Salesforce data issue detected for {application_id}: {fallback_summary}")
            summary_name = "Personal Detail Analysis"
            summary_record_id = await asyncio.to_thread(
                sf_service.upsert_verification_summary,
                application_id=parent_application_id,
                report_content=None,
                name_value=summary_name,
                overall_feedback=fallback_summary,
                confidence_range="0",
                mismatched_field_list=None,
                contact_id=contact_id
            )
            logger.info(f"Created fallback AVS for {readable_name} {application_id}. AVS ID: {summary_record_id}")
            return f"Processed {readable_name} with data issue fallback."

        if not document_payload:
            raise ValueError("Document payload was missing from the Salesforce record.")
        if not contact_id:
            raise ValueError("Contact ID is missing from the application data.")

        base64_data = document_payload.get("base64Data")
        file_extension = document_payload.get("fileExtension")
        if not base64_data or not file_extension:
            raise ValueError("Document content (base64) or file extension missing.")

        document_text_string = await extract_text_from_file(base64_data, file_extension, record_id=application_id, extractor=extractor_instance)

        app_crew = ApplicationVerificationCrewOrchestrator(record_data, document_text_string)
        report_dict = await asyncio.to_thread(app_crew.run)
        if not report_dict:
            raise ValueError("Crew did not return a valid report.")

        summary_name = "Personal Detail Analysis"
        summary_record_id = await asyncio.to_thread(
            sf_service.upsert_verification_summary,
            application_id=parent_application_id,
            report_content=report_dict.get('field_comparison_summary', '')[:MAX_SALESFORCE_REPORT_LENGTH],
            name_value=summary_name,
            overall_feedback=report_dict.get('overall_feedback'),
            confidence_range=report_dict.get('confidence_range'),
            mismatched_field_list=report_dict.get('mismatched_field_list'),
            contact_id=contact_id
        )
        
        logger.info(f"Successfully processed {readable_name} {application_id}. AVS ID: {summary_record_id}")
        return f"Successfully processed {readable_name} details."

    # --- MODIFIED: "Smart" Error Handling Logic ---
    except SalesforceAPIError as e:
        # If we get a generic 500 error, provide a more helpful message.
        if '500 Server Error' in str(e):
            reason = "Data provider failed. This is likely due to a missing document or invalid data on the Salesforce record. Please verify the record and its attachments."
            raise ValueError(_format_error(application_id, "Salesforce Data", reason, str(e)))
        else:
            # For other specific SF API errors, report them directly.
            raise ValueError(_format_error(application_id, "Salesforce API", "An API error occurred", str(e)))
    except DocumentExtractionError as e:
        raise ValueError(_format_error(application_id, "Document Extraction", "Failed to extract text from document", str(e)))
    except Exception as e:
        # Catch-all for other errors (e.g., Crew, local data validation)
        raise ValueError(_format_error(application_id, "Processing", "An unexpected error occurred", str(e)))
