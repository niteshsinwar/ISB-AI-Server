import logging
import json
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.config import (
    EDUCATION_LOG_OBJECT_API_NAME,      # Use this as the key for Apex call
    EDUCATION_DETAIL_OBJECT_API_NAME,   # Use this for contextual logging
    APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME,
    MAX_SALESFORCE_REPORT_LENGTH
)

if TYPE_CHECKING:
    from app.services.salesforce_service import SalesforceService
    from app.services.document_extraction_service import extract_text_from_file
    from app.crew.education_crew import EducationVerificationCrewOrchestrator

logger = logging.getLogger(__name__)

async def process_single_education_history_detail(
    sf_service: 'SalesforceService',
    education_log_id: str, # This is ISB_Education_Log__c ID
    parent_application_id: str,
    item_index: Optional[int] = None
):
    """
    Processes a single ISB_Education_Log__c record.
    The Apex endpoint is expected to fetch the related hed__Education_History__c
    data and return it in 'recordData'.
    """
    record_sobject_api_name_key_for_apex = EDUCATION_LOG_OBJECT_API_NAME
    detail_sobject_context_name = EDUCATION_DETAIL_OBJECT_API_NAME # For logging and clarity

    logger.info(f"Background task started for Education Log (via {record_sobject_api_name_key_for_apex}) ID: {education_log_id} "
                f"(related to Application ID: {parent_application_id}, Index: {item_index}) "
                f"targeting {detail_sobject_context_name} details.")

    from app.services.document_extraction_service import extract_text_from_file
    from app.crew.education_crew import EducationVerificationCrewOrchestrator

    final_report_to_salesforce = f"Error: Initial processing failure for Education Log ID {education_log_id}."
    overall_status_log_message = "Processing started."
    actual_education_detail_id: Optional[str] = None # Renamed for clarity

    try:
        details: Optional[Dict[str, Any]] = sf_service.get_record_detail_from_apex(
            education_log_id,
            record_sobject_api_name_key_for_apex
        )

        if not details:
            logger.warning(f"No details received from Apex for Education Log ID: {education_log_id}.")
            final_report_to_salesforce = "Error: No details received from the data API for verification."
            overall_status_log_message = "Failed - No details from API."
        else:
            record_data = details.get("recordData")
            document_payload = details.get("documentPayload")

            if not record_data:
                logger.warning(f"No 'recordData' (expected {detail_sobject_context_name} details) found for Education Log ID: {education_log_id}.")
                final_report_to_salesforce = f"Error: Salesforce {detail_sobject_context_name} data was missing, cannot perform detailed verification."
                overall_status_log_message = f"Failed - Missing recordData ({detail_sobject_context_name})."
            else:
                actual_education_detail_id = record_data.get("Id")
                if not actual_education_detail_id:
                    logger.error(f"CRITICAL: {detail_sobject_context_name} ID not found in recordData for Education Log {education_log_id}. "
                                 f"{APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME} record cannot be reliably linked.")
                    final_report_to_salesforce = f"Error: Could not identify underlying {detail_sobject_context_name} ID from data linked to Log {education_log_id}."
                    overall_status_log_message = f"Failed - Missing {detail_sobject_context_name} ID in recordData."
                
                elif document_payload and isinstance(document_payload, dict):
                    file_name = document_payload.get("fileName", "N/A")
                    file_extension = document_payload.get("fileExtension")
                    base64_data = document_payload.get("base64Data")
                    logger.info(f"Processing documentPayload '{file_name}' for Education Log ID: {education_log_id} (Actual {detail_sobject_context_name} ID: {actual_education_detail_id})")

                    if base64_data and file_extension:
                        try:
                            document_text_string = await extract_text_from_file(base64_data, file_extension)
                            if document_text_string.startswith("Error:"):
                                final_report_to_salesforce = f"Verification Error (Education): Text extraction failed for document '{file_name}'. Reason: {document_text_string}"
                                overall_status_log_message = "Failed - Document text extraction error."
                            elif document_text_string.startswith("Note: No text found"):
                                final_report_to_salesforce = f"Verification Info (Education): No text found in document '{file_name}'. Unable to perform crew verification."
                                overall_status_log_message = "Completed - No text in document."
                            else:
                                edu_crew = EducationVerificationCrewOrchestrator(record_data_dict=record_data, document_text=document_text_string)
                                verification_report = edu_crew.run()
                                final_report_to_salesforce = str(verification_report) if verification_report is not None else "Error: Education Crew produced no report."
                                overall_status_log_message = "Completed - Education document verification by crew." if "Error:" not in final_report_to_salesforce else "Completed - Education crew reported an error."
                        except Exception as doc_proc_err:
                            final_report_to_salesforce = f"Verification Error (Education): Failed during document processing or AI crew. Details: {str(doc_proc_err)}"
                            overall_status_log_message = "Failed - Document processing/crew error."
                    else: # No base64_data or file_extension
                        final_report_to_salesforce = "Verification Info (Education): Document data missing in payload. Cannot verify."
                        overall_status_log_message = "Completed - No document data in payload."
                elif actual_education_detail_id: # ID was found, but no document payload
                    final_report_to_salesforce = "Verification Info (Education): No document found to verify against."
                    overall_status_log_message = "Completed - No document payload."
            
            if record_data: logger.debug(f"RecordData ({detail_sobject_context_name}) for Log ID {education_log_id}: {json.dumps(record_data, indent=2)}")

        if not isinstance(final_report_to_salesforce, str): final_report_to_salesforce = str(final_report_to_salesforce)
        if len(final_report_to_salesforce) > MAX_SALESFORCE_REPORT_LENGTH:
            final_report_to_salesforce = final_report_to_salesforce[:MAX_SALESFORCE_REPORT_LENGTH - 3] + "..."

        summary_name_base = "Education History Analysis"
        summary_name_id_part = actual_education_detail_id or education_log_id
        summary_name = f"{summary_name_base} ({item_index})" if item_index is not None else f"{summary_name_base} - {summary_name_id_part}"
        
        success_update: bool = False
        if actual_education_detail_id:
            success_update = sf_service.upsert_verification_summary(
                application_id=parent_application_id,
                report_content=final_report_to_salesforce,
                name_value=summary_name,
                education_history_id=actual_education_detail_id
            )
        else: logger.error(f"Cannot update AVS for Education Log {education_log_id} because {detail_sobject_context_name} ID is missing.")
        
        if success_update: logger.info(f"Successfully updated AVS for Education (Detail ID: {actual_education_detail_id}). Status: {overall_status_log_message}")
        elif actual_education_detail_id : logger.error(f"Failed to update AVS for Education (Detail ID: {actual_education_detail_id}). Status: {overall_status_log_message}")

    except Exception as e:
        logger.error(f"Unexpected error for Education Log ID {education_log_id}: {e}", exc_info=True)
        error_report_content = f"Critical Error for Education Log ID {education_log_id}:\n{str(e)[:1000]}"
        retrieved_detail_id = locals().get('actual_education_detail_id')
        summary_name_id_part_on_error = retrieved_detail_id or education_log_id
        summary_name_on_error = f"Education History Analysis - CRITICAL ERROR ({item_index})" if item_index is not None else f"Education History Analysis - CRITICAL ERROR - {summary_name_id_part_on_error}"
        try:
            if locals().get('sf_service') and retrieved_detail_id:
                locals()['sf_service'].upsert_verification_summary(parent_application_id, error_report_content, summary_name_on_error, education_history_id=retrieved_detail_id)
            else: logger.error(f"Cannot update AVS with critical error for Education Log {education_log_id}: sf_service or detail ID missing.")
        except Exception as update_err: logger.error(f"Failed to update AVS with critical error for Education Log {education_log_id}: {update_err}", exc_info=True)