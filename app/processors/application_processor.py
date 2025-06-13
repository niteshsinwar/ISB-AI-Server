# project_root/app/processors/application_processor.py
import logging
import json
import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional, List

from app.config import (
    APPLICATION_OBJECT_API_NAME,
    APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP,
    MAX_SALESFORCE_REPORT_LENGTH
)

if TYPE_CHECKING:
    from app.services.salesforce_service import SalesforceService
    from app.services.document_extraction_service import extract_text_from_file
    from app.crew.application_crew import ApplicationVerificationCrewOrchestrator

logger = logging.getLogger(__name__)

async def process_single_application_detail(
    sf_service: "SalesforceService",
    application_id: str,
    parent_application_id: str,
    application_object_api_name: str 
) -> str:
    """
    Processes a single Application record's details and its associated ID document.
    Raises exceptions on failure.
    """
    logger.info(f"Background task started for MAIN APPLICATION record: {application_object_api_name} ID: {application_id} (Parent App: {parent_application_id})")

    from app.services.document_extraction_service import extract_text_from_file
    from app.crew.application_crew import ApplicationVerificationCrewOrchestrator

    details: Optional[Dict[str, Any]] = await asyncio.to_thread(
        sf_service.get_record_detail_from_apex, application_id, application_object_api_name
    )

    if not details:
        raise ValueError("Failed to retrieve application details from Salesforce API.")

    record_data = details.get("recordData")
    document_payload = details.get("documentPayload")

    if not record_data:
        raise ValueError("Missing application/contact recordData from Salesforce.")
    
    contact_id_for_summary = record_data.get(APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP)
    if not contact_id_for_summary:
        logger.warning(f"Contact ID ('{APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP}') not found for App {application_id}.")

    task_id_for_lookup = record_data.get('Task_Id')
    dci_id_for_lookup = record_data.get('DocumentchecklistItem_Id')
    
    if not document_payload or not isinstance(document_payload, dict):
        raise ValueError("No ID document found for verification.")
        
    file_name = document_payload.get("fileName", "N/A")
    file_extension = document_payload.get("fileExtension")
    base64_data = document_payload.get("base64Data")

    if not base64_data or not file_extension:
        raise ValueError("Document data missing in payload.")
        
    document_text_string: str | List[str] = await extract_text_from_file(base64_data, file_extension)

    # FIX: Handle cases where the extractor returns a list of strings (e.g., for multi-page docs)
    if isinstance(document_text_string, list):
        document_text_string = "\n\n--- Page Break ---\n\n".join(document_text_string)

    if document_text_string.startswith("Error:") or "No text found" in document_text_string:
        raise ValueError(f"Document could not be read or was empty: {document_text_string}")
    
    logger.info(f"Text extracted from ID document {file_name}. Applying Application Verification Crew.")
    
    app_crew = ApplicationVerificationCrewOrchestrator(
        record_data_dict=record_data,
        document_text=document_text_string
    )
    
    verification_report_dict = await asyncio.to_thread(app_crew.run)
    
    field_summary_report = verification_report_dict.get('field_comparison_summary')
    overall_feedback_report = verification_report_dict.get('overall_feedback')
    confidence_report = verification_report_dict.get('confidence_range')
    
    if not all([field_summary_report, overall_feedback_report, confidence_report is not None]):
        raise ValueError("Crew failed to return a valid report structure.")

    if len(field_summary_report) > MAX_SALESFORCE_REPORT_LENGTH:
        field_summary_report = field_summary_report[:MAX_SALESFORCE_REPORT_LENGTH - 3] + "..."

    summary_name = "Personal Detail Analysis"
    
    if not contact_id_for_summary:
        raise ValueError("Missing Contact ID, cannot create summary record.")

    summary_record_id = await asyncio.to_thread(
        sf_service.upsert_verification_summary,
        application_id=parent_application_id,
        report_content=field_summary_report,
        name_value=summary_name,
        overall_feedback=overall_feedback_report,
        confidence_range=confidence_report,
        contact_id=contact_id_for_summary
    )
    
    if not summary_record_id:
        raise ValueError("Failed to upsert summary record in Salesforce.")
        
    logger.info(f"Upserted summary record {summary_record_id}. Now linking to related Task/DCI.")
    await asyncio.to_thread(
        sf_service.link_summary_to_related_items,
        summary_id=summary_record_id,
        task_id=task_id_for_lookup,
        dci_id=dci_id_for_lookup,
        overall_feedback=overall_feedback_report
    )

    return "Successfully processed application details."
