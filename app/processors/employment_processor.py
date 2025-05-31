import logging
import json
from typing import TYPE_CHECKING, Any, Dict

# Import config for SObject API name & analysis report field
from app.config import (
    ISB_EMPLOYMENT_LOG_OBJECT_API_NAME,
    EMPLOYMENT_LOG_ANALYSIS_REPORT_FIELD
)

# Use TYPE_CHECKING to avoid circular imports at runtime
if TYPE_CHECKING:
    from app.services.salesforce_service import SalesforceService
    from app.services.document_extraction_service import extract_text_from_file # Assuming this is generic
    from app.crew.employment_crew import EmploymentVerificationCrewOrchestrator

logger = logging.getLogger(__name__)

async def process_single_employment_detail(
    sf_service: 'SalesforceService',
    employment_log_id: str,
    parent_application_id: str # For logging context and DCI lookup
):
    """
    Processes a single ISB_Employment_Log__c record.
    Fetches related Affiliation data for details and the document via DocumentChecklistItem
    linked to the parent Application.
    """
    # record_sobject_api_name_key is used for the APEX_ENDPOINT_PATHS map in SalesforceService
    record_sobject_api_name_key = ISB_EMPLOYMENT_LOG_OBJECT_API_NAME
    # report_update_sobject_api_name is the actual SObject API name where the report field resides
    report_update_sobject_api_name = ISB_EMPLOYMENT_LOG_OBJECT_API_NAME # Report will be on the Log record

    logger.info(f"Background task started for {record_sobject_api_name_key} ID: {employment_log_id} (related to Application ID: {parent_application_id})")

    # Functional imports to be resolved at runtime
    from app.services.document_extraction_service import extract_text_from_file
    from app.crew.employment_crew import EmploymentVerificationCrewOrchestrator

    final_report_to_salesforce = f"Error: Initial processing failure for {record_sobject_api_name_key} ID {employment_log_id}."
    overall_status_log_message = "Processing started."

    try:
        # The Apex endpoint for employment now takes ISB_Employment_Log__c ID
        # The key ISB_EMPLOYMENT_LOG_OBJECT_API_NAME must map to "documentVerification/employment"
        # in APEX_ENDPOINT_PATHS config.
        details: Dict[str, Any] | None = sf_service.get_record_detail_from_apex(employment_log_id, record_sobject_api_name_key)

        if not details:
            logger.warning(f"No details received from Apex for {record_sobject_api_name_key} ID: {employment_log_id}.")
            final_report_to_salesforce = "Error: No details received from the data API for verification."
            overall_status_log_message = "Failed - No details from API."
            sf_service.update_record_analysis_report(employment_log_id, report_update_sobject_api_name, final_report_to_salesforce)
            return

        record_data = details.get("recordData") # This is expected to be data from hed__Affiliation__c
        document_payload = details.get("documentPayload")

        if not record_data:
            logger.warning(f"No 'recordData' (expected Affiliation details) found for {record_sobject_api_name_key} ID: {employment_log_id}.")
            final_report_to_salesforce = "Error: Salesforce record data (Affiliation) was missing, cannot perform detailed verification."
            overall_status_log_message = "Failed - Missing recordData (Affiliation)."
        
        elif document_payload and isinstance(document_payload, dict):
            file_name = document_payload.get("fileName", "N/A")
            file_extension = document_payload.get("fileExtension")
            base64_data = document_payload.get("base64Data")
            logger.info(f"Processing documentPayload '{file_name}' for {record_sobject_api_name_key} ID: {employment_log_id}")

            if base64_data and file_extension:
                try:
                    logger.info(f"Extracting text from embedded document: {file_name}")
                    document_text_string = await extract_text_from_file(base64_data, file_extension)

                    if document_text_string.startswith("Error:"):
                        logger.error(f"Text extraction failed for doc {file_name} ({employment_log_id}): {document_text_string}")
                        final_report_to_salesforce = f"Verification Error (Employment): Text extraction failed for document '{file_name}'. Reason: {document_text_string}"
                        overall_status_log_message = "Failed - Document text extraction error."
                    elif document_text_string.startswith("Note: No text found"):
                        logger.info(f"No text in doc {file_name} ({employment_log_id}).")
                        final_report_to_salesforce = f"Verification Info (Employment): No text found in document '{file_name}'. Unable to perform crew verification."
                        overall_status_log_message = "Completed - No text in document."
                    else:
                        logger.info(f"Text extracted from {file_name} for {employment_log_id}. Length: {len(document_text_string)}. Applying Employment Verification Crew.")
                        
                        emp_crew = EmploymentVerificationCrewOrchestrator(
                            record_data_dict=record_data, # record_data (Affiliation details)
                            document_text=document_text_string
                        )
                        verification_report = emp_crew.run()
                        
                        final_report_to_salesforce = str(verification_report) if verification_report is not None else "Error: Employment Verification Crew produced no report."
                        if "Error:" in final_report_to_salesforce:
                            overall_status_log_message = "Completed - Employment crew reported an error."
                            logger.warning(f"Employment crew reported an error for {employment_log_id}: {final_report_to_salesforce}")
                        else:
                            overall_status_log_message = "Completed - Employment document verification by crew."
                        
                except Exception as doc_proc_err:
                    logger.error(f"Error during document processing or crew execution for {employment_log_id}, doc {file_name}: {doc_proc_err}", exc_info=True)
                    final_report_to_salesforce = f"Verification Error (Employment): Failed during document processing or AI crew. Details: {str(doc_proc_err)}"
                    overall_status_log_message = "Failed - Document processing/crew error."
            else:
                logger.info(f"No document data (base64/extension) in payload for {employment_log_id}.")
                final_report_to_salesforce = "Verification Info (Employment): Document data (base64 or extension) missing in payload. Cannot verify."
                overall_status_log_message = "Completed - No document data in payload."
        else: # No document_payload
            logger.info(f"No document payload found in API response for {employment_log_id}. Cannot verify against document.")
            final_report_to_salesforce = "Verification Info (Employment): No document found to verify against."
            overall_status_log_message = "Completed - No document payload."
        
        if record_data: # Log record_data if it existed, useful for debugging
             logger.debug(f"RecordData (Affiliation) for {record_sobject_api_name_key} ID {employment_log_id}: {json.dumps(record_data, indent=2)}")


        if not isinstance(final_report_to_salesforce, str):
            final_report_to_salesforce = str(final_report_to_salesforce)

        max_report_length = 32000 # Salesforce long text area limit (approx)
        if len(final_report_to_salesforce) > max_report_length:
            logger.warning(f"Compiled report for {report_update_sobject_api_name} ID {employment_log_id} is too long ({len(final_report_to_salesforce)} chars). Truncating.")
            final_report_to_salesforce = final_report_to_salesforce[:max_report_length - 3] + "..."

        # Update report on the SObject specified by report_update_sobject_api_name
        success_update: bool = sf_service.update_record_analysis_report(employment_log_id, report_update_sobject_api_name, final_report_to_salesforce)

        if success_update:
            logger.info(f"Successfully updated analysis report for {report_update_sobject_api_name} ID: {employment_log_id}. Status: {overall_status_log_message}")
        else:
            logger.error(f"Failed to update analysis report for {report_update_sobject_api_name} ID: {employment_log_id}. Attempted status: {overall_status_log_message}")

    except Exception as e:
        logger.error(f"Unexpected error in main processing for {ISB_EMPLOYMENT_LOG_OBJECT_API_NAME} ID {employment_log_id}: {e}", exc_info=True)
        error_report = f"Critical Error during processing {ISB_EMPLOYMENT_LOG_OBJECT_API_NAME} ID {employment_log_id}:\n{str(e)[:1000]}"
        try:
            current_sf_service = locals().get('sf_service') # Safely check if sf_service is defined
            if current_sf_service is not None:
                 current_sf_service.update_record_analysis_report(employment_log_id, ISB_EMPLOYMENT_LOG_OBJECT_API_NAME, error_report)
            else:
                logger.error(f"sf_service not available to update Salesforce with critical error for {ISB_EMPLOYMENT_LOG_OBJECT_API_NAME} ID {employment_log_id}")
        except Exception as update_err:
            logger.error(f"Failed to even update Salesforce with critical error for {ISB_EMPLOYMENT_LOG_OBJECT_API_NAME} ID {employment_log_id}: {update_err}", exc_info=True)