# project_root/app/processors/education_processor.py
import logging
import json
from typing import TYPE_CHECKING, Any, Dict

# Import config for SObject API name
from app.config import EDUCATION_HISTORY_OBJECT_API_NAME

# Use TYPE_CHECKING to avoid circular imports at runtime
if TYPE_CHECKING:
    from app.services.salesforce_service import SalesforceService
    from app.services.document_extraction_service import extract_text_from_file
    from app.crew.education_crew import EducationVerificationCrewOrchestrator

logger = logging.getLogger(__name__)

async def process_single_education_history_detail(
    sf_service: 'SalesforceService',
    education_history_id: str,
    parent_application_id: str # For logging context
):
    """
    Processes a single hed__Education_History__c record.
    """
    # Use the constant for the record type SObject API name
    record_sobject_api_name = EDUCATION_HISTORY_OBJECT_API_NAME
    logger.info(f"Background task started for {record_sobject_api_name} ID: {education_history_id} (related to Application ID: {parent_application_id})")

    # Functional imports
    from app.services.document_extraction_service import extract_text_from_file
    from app.crew.education_crew import EducationVerificationCrewOrchestrator

    final_report_to_salesforce = f"Error: Initial processing failure for {record_sobject_api_name}."
    overall_status_log_message = "Processing started."

    try:
        details: Dict[str, Any] | None = sf_service.get_record_detail_from_apex(education_history_id, record_sobject_api_name)

        if not details:
            logger.warning(f"No details received from Apex for {record_sobject_api_name} ID: {education_history_id}.")
            final_report_to_salesforce = "Error: No details received from the data API for verification."
            overall_status_log_message = "Failed - No details from API."
            sf_service.update_record_analysis_report(education_history_id, record_sobject_api_name, final_report_to_salesforce)
            return # Exit if no details

        record_data = details.get("recordData")
        document_payload = details.get("documentPayload")

        if not record_data:
            logger.warning(f"No 'recordData' found for {record_sobject_api_name} ID: {education_history_id}. Cannot run verification crew without record context.")
            final_report_to_salesforce = "Error: Salesforce record data was missing, cannot perform detailed verification."
            overall_status_log_message = "Failed - Missing recordData."
            logger.debug(f"recordData for {education_history_id} (evaluated as missing): {json.dumps(record_data, indent=2) if record_data is not None else 'None'}")
        
        elif document_payload and isinstance(document_payload, dict):
            file_name = document_payload.get("fileName", "N/A")
            file_extension = document_payload.get("fileExtension")
            base64_data = document_payload.get("base64Data")
            logger.info(f"Processing documentPayload '{file_name}' for {record_sobject_api_name} ID: {education_history_id}")

            if base64_data and file_extension:
                try:
                    logger.info(f"Extracting text from embedded document: {file_name}")
                    document_text_string = await extract_text_from_file(base64_data, file_extension)

                    if document_text_string.startswith("Error:"):
                        logger.error(f"Text extraction failed for doc {file_name} ({education_history_id}): {document_text_string}")
                        final_report_to_salesforce = f"Verification Error (Education): Text extraction failed for document '{file_name}'. Reason: {document_text_string}"
                        overall_status_log_message = "Failed - Document text extraction error."
                    elif document_text_string.startswith("Note: No text found"):
                        logger.info(f"No text in doc {file_name} ({education_history_id}).")
                        final_report_to_salesforce = f"Verification Info (Education): No text found in document '{file_name}'. Unable to perform crew verification."
                        overall_status_log_message = "Completed - No text in document."
                    else: 
                        logger.info(f"Text extracted from {file_name} for {education_history_id}. Length: {len(document_text_string)}. Applying Education Verification Crew.")
                        
                        edu_crew = EducationVerificationCrewOrchestrator(
                            record_data_dict=record_data, # record_data confirmed to exist
                            document_text=document_text_string
                        )
                        verification_report = edu_crew.run()
                        
                        final_report_to_salesforce = str(verification_report) if verification_report is not None else "Error: Education Verification Crew produced no report."
                        if "Error:" in final_report_to_salesforce:
                            overall_status_log_message = "Completed - Education crew reported an error."
                            logger.warning(f"Education crew reported an error for {education_history_id}: {final_report_to_salesforce}")
                        else:
                            overall_status_log_message = "Completed - Education document verification by crew."
                        
                except Exception as doc_proc_err:
                    logger.error(f"Error during document processing or crew execution for {education_history_id}, doc {file_name}: {doc_proc_err}", exc_info=True)
                    final_report_to_salesforce = f"Verification Error (Education): Failed during document processing or AI crew. Details: {str(doc_proc_err)}"
                    overall_status_log_message = "Failed - Document processing/crew error."
            else:
                logger.info(f"No document data (base64/extension) in payload for {education_history_id}.")
                final_report_to_salesforce = "Verification Info (Education): Document data (base64 or extension) missing in payload. Cannot verify."
                overall_status_log_message = "Completed - No document data in payload."
        else: # No document_payload
            logger.info(f"No document payload found in API response for {education_history_id}. Cannot verify against document.")
            final_report_to_salesforce = "Verification Info (Education): No document found to verify against."
            overall_status_log_message = "Completed - No document payload."
            if record_data:
                 logger.debug(f"recordData for {education_history_id} (no document): {json.dumps(record_data, indent=2)}")

        if not isinstance(final_report_to_salesforce, str):
            final_report_to_salesforce = str(final_report_to_salesforce)

        max_report_length = 32000 
        if len(final_report_to_salesforce) > max_report_length:
            logger.warning(f"Compiled report for {record_sobject_api_name} ID {education_history_id} is too long ({len(final_report_to_salesforce)} chars). Truncating.")
            final_report_to_salesforce = final_report_to_salesforce[:max_report_length - 3] + "..."

        success_update: bool = sf_service.update_record_analysis_report(education_history_id, record_sobject_api_name, final_report_to_salesforce)

        if success_update:
            logger.info(f"Successfully updated analysis report for {record_sobject_api_name} ID: {education_history_id}. Status: {overall_status_log_message}")
        else:
            logger.error(f"Failed to update analysis report for {record_sobject_api_name} ID: {education_history_id}. Attempted status: {overall_status_log_message}")

    except Exception as e:
        logger.error(f"Unexpected error in main processing for {record_sobject_api_name} ID {education_history_id}: {e}", exc_info=True)
        error_report = f"Critical Error during processing {record_sobject_api_name} ID {education_history_id}:\n{str(e)[:1000]}"
        try:
            if 'sf_service' in locals() and sf_service is not None:
                 sf_service.update_record_analysis_report(education_history_id, record_sobject_api_name, error_report)
            else:
                logger.error(f"sf_service not available to update Salesforce with critical error for {record_sobject_api_name} ID {education_history_id}")
        except Exception as update_err:
            logger.error(f"Failed to even update Salesforce with critical error for {record_sobject_api_name} ID {education_history_id}: {update_err}", exc_info=True)

    # This function is run by background_tasks.add_task, so it doesn't need to return the summary for an HTTP response.
    # The summary was for the main endpoint's immediate response before backgrounding.
