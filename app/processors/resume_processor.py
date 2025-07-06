# project_root/app/processors/resume_processor.py
import logging
import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.config import DCI_OBJECT_API_NAME, READABLE_OBJECT_NAMES
from app.services.document_extraction_service import DocumentExtractionError
from app.services.salesforce_service import SalesforceAPIError

if TYPE_CHECKING:
    from app.services.salesforce_service import SalesforceService
    from app.crew.resume_crew import ResumeVerificationCrewOrchestrator

logger = logging.getLogger(__name__)

def _format_error(record_id: str, component: str, reason: str, technical_error: str) -> str:
    """Creates a standardized error message for logging and reporting."""
    category = READABLE_OBJECT_NAMES[DCI_OBJECT_API_NAME]
    return f"({category})-({record_id})-({component})-({reason}): {technical_error}"

async def process_single_resume_detail(
    sf_service: "SalesforceService",
    resume_dci_id: str,
    parent_application_id: str,
    **kwargs
) -> str:
    readable_name = READABLE_OBJECT_NAMES.get(DCI_OBJECT_API_NAME, "Resume Detail")
    logger.info(f"Starting {readable_name} processing for DCI ID: {resume_dci_id}")

    from app.services.document_extraction_service import extract_text_from_file
    from app.crew.resume_crew import ResumeVerificationCrewOrchestrator

    try:
        details = await asyncio.to_thread(sf_service.get_dci_document_data, resume_dci_id)

        document_payload = details.get("documentPayload")
        if not document_payload:
            raise ValueError("No attached document found for this resume DCI record.")

        file_extension = document_payload.get("fileExtension")
        base64_data = document_payload.get("base64Data")

        if not base64_data or not file_extension:
            raise ValueError("Document content (base64) or file extension missing.")
            
        document_text_string = await extract_text_from_file(base64_data, file_extension, record_id=resume_dci_id)
        
        resume_crew = ResumeVerificationCrewOrchestrator(document_text=document_text_string)
        report_dict = await asyncio.to_thread(resume_crew.run)
        if not report_dict:
            raise ValueError("Crew did not return a valid report.")

        status = report_dict.get("status")
        reason = report_dict.get("reason")
        confidence = 100 if status == "Accepted" else 30
        feedback = "All good" if status == "Accepted" else (reason or "PII or other issues detected.")

        summary_record_id = await asyncio.to_thread(
            sf_service.upsert_verification_summary,
            application_id=parent_application_id,
            report_content="",
            name_value="Resume Detail Analysis",
            overall_feedback=feedback,
            confidence_range=confidence
        )

        await asyncio.to_thread(
            sf_service.link_summary_to_related_items,
            summary_id=summary_record_id,
            task_id=None,
            dci_id=resume_dci_id
        )
        
        logger.info(f"Successfully processed {readable_name} {resume_dci_id}. AVS ID: {summary_record_id}")
        return f"Successfully processed resume {resume_dci_id}."

    except SalesforceAPIError as e:
        if '500 Server Error' in str(e):
            reason = "Data provider failed. This is likely due to a missing document or invalid data on the Salesforce record. Please verify the record and its attachments."
            raise ValueError(_format_error(resume_dci_id, "Salesforce Data", reason, str(e)))
        else:
            raise ValueError(_format_error(resume_dci_id, "Salesforce API", "An API error occurred", str(e)))
    except DocumentExtractionError as e:
        raise ValueError(_format_error(resume_dci_id, "Document Extraction", "Failed to extract text from document", str(e)))
    except Exception as e:
        raise ValueError(_format_error(resume_dci_id, "Processing", "An unexpected error occurred", str(e)))