import logging
import json
import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.config import (
    APPLICATION_OBJECT_API_NAME,
    APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP,
    APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME,
    MAX_SALESFORCE_REPORT_LENGTH
)

if TYPE_CHECKING:
    from app.services.salesforce_service import SalesforceService
    from app.services.document_extraction_service import extract_text_from_file
    from app.crew.application_crew import ApplicationVerificationCrewOrchestrator

logger = logging.getLogger(__name__)

async def process_single_application_detail(
    sf_service: 'SalesforceService',
    application_id: str,
    parent_application_id: str,
    application_object_api_name: str 
):
    """
    Processes a single Application record's details and its associated ID document.
    """
    logger.info(f"Background task started for MAIN APPLICATION record: {application_object_api_name} ID: {application_id} (Parent App: {parent_application_id})")

    from app.services.document_extraction_service import extract_text_from_file
    from app.crew.application_crew import ApplicationVerificationCrewOrchestrator

    final_report_to_salesforce = f"Error: Initial processing failure for application {application_id}."
    processing_summary_for_response = "Processing started."
    contact_id_for_summary: Optional[str] = None

    try:
        details: Optional[Dict[str, Any]] = await asyncio.to_thread(
            sf_service.get_record_detail_from_apex, application_id, application_object_api_name
        )

        if not details:
            return "Failed - No details from API."

        record_data = details.get("recordData")
        document_payload = details.get("documentPayload")

        if not record_data:
            return "Failed - Missing application/contact recordData."
        
        contact_id_for_summary = record_data.get(APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP)
        if not contact_id_for_summary:
            logger.warning(f"Contact ID ('{APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP}') not found for App {application_id}.")

        if document_payload and isinstance(document_payload, dict):
            file_name = document_payload.get("fileName", "N/A")
            file_extension = document_payload.get("fileExtension")
            base64_data = document_payload.get("base64Data")

            if base64_data and file_extension:
                document_text_string = await extract_text_from_file(base64_data, file_extension)

                if document_text_string.startswith("Error:") or document_text_string.startswith("Note: No text found"):
                    final_report_to_salesforce = f"Verification Info (Application ID Proof): {document_text_string}"
                    processing_summary_for_response = f"Completed - {document_text_string}"
                else: 
                    logger.info(f"Text extracted from ID document {file_name}. Applying Application Verification Crew.")
                    
                    app_crew = ApplicationVerificationCrewOrchestrator(
                        record_data_dict=record_data,
                        document_text=document_text_string
                    )
                    
                    verification_report = await asyncio.to_thread(app_crew.run)
                    
                    final_report_to_salesforce = str(verification_report) if verification_report is not None else "Error: Crew produced no report."
                    processing_summary_for_response = "Completed - Application ID document verified by crew."
            else:
                final_report_to_salesforce = "Verification Info: No document data in payload."
                processing_summary_for_response = "Completed - No document data."
        else: 
            final_report_to_salesforce = "Verification Info: No ID document found."
            processing_summary_for_response = "Completed - No ID document."

        if len(final_report_to_salesforce) > MAX_SALESFORCE_REPORT_LENGTH:
            final_report_to_salesforce = final_report_to_salesforce[:MAX_SALESFORCE_REPORT_LENGTH - 3] + "..."

        summary_name = "Personal Detail Analysis"
        if contact_id_for_summary:
            await asyncio.to_thread(
                sf_service.upsert_verification_summary,
                application_id=parent_application_id,
                report_content=final_report_to_salesforce,
                name_value=summary_name,
                contact_id=contact_id_for_summary
            )
        else:
            processing_summary_for_response += " (AVS Update Failed - Missing Contact ID)"

    except Exception as e:
        logger.error(f"Unexpected error in main processing for {application_object_api_name} ID {application_id}: {e}", exc_info=True)
        processing_summary_for_response = f"Failed - Unexpected error: {str(e)[:100]}"
    
    return processing_summary_for_response
