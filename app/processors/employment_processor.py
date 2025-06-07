import logging
import json
import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.config import (
    EMPLOYMENT_LOG_OBJECT_API_NAME,
    EMPLOYMENT_DETAIL_OBJECT_API_NAME,
    APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME,
    MAX_SALESFORCE_REPORT_LENGTH
)

if TYPE_CHECKING:
    from app.services.salesforce_service import SalesforceService
    from app.services.document_extraction_service import extract_text_from_file
    from app.crew.employment_crew import EmploymentVerificationCrewOrchestrator

logger = logging.getLogger(__name__)

async def process_single_employment_detail(
    sf_service: 'SalesforceService',
    employment_log_id: str,
    parent_application_id: str,
    item_index: Optional[int] = None
):
    record_sobject_api_name_key_for_apex = EMPLOYMENT_LOG_OBJECT_API_NAME
    detail_sobject_context_name = EMPLOYMENT_DETAIL_OBJECT_API_NAME

    logger.info(f"Background task started for Employment Log ID: {employment_log_id} (App: {parent_application_id})")

    from app.services.document_extraction_service import extract_text_from_file
    from app.crew.employment_crew import EmploymentVerificationCrewOrchestrator

    final_report_to_salesforce = f"Error: Initial processing failure for Employment Log ID {employment_log_id}."
    actual_employment_detail_id: Optional[str] = None

    try:
        details: Optional[Dict[str, Any]] = await asyncio.to_thread(
            sf_service.get_record_detail_from_apex,
            employment_log_id, 
            record_sobject_api_name_key_for_apex
        )

        if not details:
            final_report_to_salesforce = "Error: No details received from the data API."
        else:
            record_data = details.get("recordData")
            document_payload = details.get("documentPayload")

            if not record_data:
                final_report_to_salesforce = f"Error: Salesforce {detail_sobject_context_name} data was missing."
            else:
                actual_employment_detail_id = record_data.get("Id")
                if not actual_employment_detail_id:
                    final_report_to_salesforce = f"Error: Could not identify underlying {detail_sobject_context_name} ID."
                elif document_payload and isinstance(document_payload, dict):
                    file_name = document_payload.get("fileName", "N/A")
                    file_extension = document_payload.get("fileExtension")
                    base64_data = document_payload.get("base64Data")

                    if base64_data and file_extension:
                        document_text_string = await extract_text_from_file(base64_data, file_extension)
                        if document_text_string.startswith("Error:"):
                            final_report_to_salesforce = f"Verification Error (Employment): Text extraction failed for '{file_name}'. Reason: {document_text_string}"
                        elif document_text_string.startswith("Note: No text found"):
                            final_report_to_salesforce = f"Verification Info (Employment): No text found in document '{file_name}'."
                        else:
                            emp_crew = EmploymentVerificationCrewOrchestrator(record_data_dict=record_data, document_text=document_text_string)
                            verification_report = await asyncio.to_thread(emp_crew.run)
                            final_report_to_salesforce = str(verification_report) if verification_report is not None else "Error: Employment Crew produced no report."
                    else:
                        final_report_to_salesforce = "Verification Info (Employment): Document data missing in payload."
                elif actual_employment_detail_id:
                    final_report_to_salesforce = "Verification Info (Employment): No document found to verify against."
        
        if len(final_report_to_salesforce) > MAX_SALESFORCE_REPORT_LENGTH:
            final_report_to_salesforce = final_report_to_salesforce[:MAX_SALESFORCE_REPORT_LENGTH - 3] + "..."

        summary_name_id_part = actual_employment_detail_id or employment_log_id
        summary_name = f"Employment History Analysis ({item_index})" if item_index is not None else f"Employment History Analysis - {summary_name_id_part}"
        
        if actual_employment_detail_id:
            await asyncio.to_thread(
                sf_service.upsert_verification_summary,
                application_id=parent_application_id,
                report_content=final_report_to_salesforce,
                name_value=summary_name,
                affiliation_id=actual_employment_detail_id
            )
            logger.info(f"Successfully triggered AVS update for Employment (Detail ID: {actual_employment_detail_id}).")
        else:
            logger.error(f"Cannot update AVS for Employment Log {employment_log_id} because detail ID is missing.")

    except Exception as e:
        logger.error(f"Unexpected error for Employment Log ID {employment_log_id}: {e}", exc_info=True)
