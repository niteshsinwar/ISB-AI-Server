# project_root/app/processors/employment_processor.py
import logging
import json
import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional, List

from app.config import (
    EMPLOYMENT_LOG_OBJECT_API_NAME,
    MAX_SALESFORCE_REPORT_LENGTH
)

if TYPE_CHECKING:
    from app.services.salesforce_service import SalesforceService
    # Updated for explicit function imports
    from app.services.document_extraction_service import extract_text_from_file, extract_text_from_pdf_limited
    from app.crew.employment_crew import EmploymentVerificationCrewOrchestrator

logger = logging.getLogger(__name__)

async def process_single_employment_detail(
    sf_service: "SalesforceService",
    employment_log_id: str,
    parent_application_id: str,
    item_index: Optional[int] = None
):
    """
    Processes a single Employment (Affiliation) record and its document.
    Raises ValueError on any processing failure.
    """
    record_sobject_api_name_key_for_apex = EMPLOYMENT_LOG_OBJECT_API_NAME
    logger.info(f"Starting Employment History processing for Log ID: {employment_log_id} (App: {parent_application_id})")

    from app.services.document_extraction_service import extract_text_from_file, extract_text_from_pdf_limited
    from app.crew.employment_crew import EmploymentVerificationCrewOrchestrator

    # 1. Fetch data from Salesforce
    details: Optional[Dict[str, Any]] = await asyncio.to_thread(
        sf_service.get_record_detail_from_apex,
        employment_log_id, 
        record_sobject_api_name_key_for_apex
    )
    if not details:
        raise ValueError("Failed to receive details from Salesforce API.")

    record_data = details.get("recordData")
    document_payload = details.get("documentPayload")

    if not record_data:
        raise ValueError("Salesforce Affiliation data was missing in API response.")

    actual_employment_detail_id = record_data.get("Id")
    task_id_for_lookup = record_data.get('Task_Id')
    dci_id_for_lookup = record_data.get('DocumentchecklistItem_Id')

    if not actual_employment_detail_id:
        raise ValueError("Could not identify the underlying Affiliation__c ID from the payload.")

    # 2. Extract text from the document
    if not document_payload or not isinstance(document_payload, dict):
        raise ValueError("No document payload found to verify against.")

    file_name = document_payload.get("fileName", "N/A")
    file_extension = document_payload.get("fileExtension")
    base64_data = document_payload.get("base64Data")

    if not base64_data or not file_extension:
        raise ValueError("Document data (base64) or file extension missing in payload.")
        
    # --- MODIFIED: Intelligent Document Extraction ---
    document_text_string: str
    if file_extension.lower() == 'pdf':
        logger.info(f"PDF document detected. Using multi-page extraction for '{file_name}'.")
        pages_text_list: List[str] = await extract_text_from_pdf_limited(base64_data)
        document_text_string = "\n\n--- Page Break ---\n\n".join(pages_text_list)
    else:
        logger.info(f"Image document detected. Using single-page extraction for '{file_name}'.")
        document_text_string = await extract_text_from_file(base64_data, file_extension)

    if document_text_string.startswith("Error:") or "No text found" in document_text_string:
        raise ValueError(f"Text extraction failed for '{file_name}': {document_text_string}")

    # 3. Run the verification crew
    emp_crew = EmploymentVerificationCrewOrchestrator(record_data, document_text_string)
    report_dict = await asyncio.to_thread(emp_crew.run)

    field_summary_report = report_dict.get('field_comparison_summary')
    overall_feedback_report = report_dict.get('overall_feedback')
    confidence_report = report_dict.get('confidence_range')

    if not all([field_summary_report, overall_feedback_report, confidence_report is not None]):
        raise ValueError("Crew failed to return a valid report. Check crew logs for details.")

    if len(field_summary_report) > MAX_SALESFORCE_REPORT_LENGTH:
        field_summary_report = field_summary_report[:MAX_SALESFORCE_REPORT_LENGTH - 3] + "..."

    # 4. Upsert the summary record to Salesforce
    summary_name = f"Employment Analysis ({item_index})" if item_index else f"Employment Analysis - {actual_employment_detail_id}"
    
    summary_id = await asyncio.to_thread(
        sf_service.upsert_verification_summary,
        application_id=parent_application_id,
        report_content=field_summary_report,
        name_value=summary_name,
        overall_feedback=overall_feedback_report,
        confidence_range=confidence_report,
        affiliation_id=actual_employment_detail_id
    )
    if not summary_id:
        raise ValueError(f"Failed to upsert Application_Verification_Summary__c for Affiliation ID {actual_employment_detail_id}.")
        
    logger.info(f"Upserted AVS {summary_id} for Employment {actual_employment_detail_id}. Linking to related items.")
    
    # 5. Link the summary to other items (Task, DCI)
    await asyncio.to_thread(
        sf_service.link_summary_to_related_items,
        summary_id, task_id_for_lookup, dci_id_for_lookup, overall_feedback_report
    )