import logging
import json
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.config import (
    ISB_EMPLOYMENT_LOG_OBJECT_API_NAME,
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
    employment_log_id: str, # This is ISB_Employment_Log__c ID
    parent_application_id: str,
    item_index: Optional[int] = None
):
    """
    Processes a single ISB_Employment_Log__c record.
    The Apex endpoint is expected to return hed__Affiliation__c data in 'recordData'.
    """
    # This key is for the Apex call, which uses the ISB_Employment_Log__c ID
    record_sobject_api_name_key_for_apex = ISB_EMPLOYMENT_LOG_OBJECT_API_NAME 
    
    logger.info(f"Background task started for Employment Log (via {record_sobject_api_name_key_for_apex}) ID: {employment_log_id} (related to Application ID: {parent_application_id}, Index: {item_index})")

    from app.services.document_extraction_service import extract_text_from_file
    from app.crew.employment_crew import EmploymentVerificationCrewOrchestrator

    final_report_to_salesforce = f"Error: Initial processing failure for Employment Log ID {employment_log_id}."
    overall_status_log_message = "Processing started." # For internal logging
    affiliation_id_for_summary: Optional[str] = None

    try:
        # The Apex endpoint for employment is called with ISB_Employment_Log__c ID.
        # It MUST return the related hed__Affiliation__c data in `details.get("recordData")`.
        details: Optional[Dict[str, Any]] = sf_service.get_record_detail_from_apex(employment_log_id, record_sobject_api_name_key_for_apex)

        if not details:
            logger.warning(f"No details received from Apex for Employment Log ID: {employment_log_id}.")
            final_report_to_salesforce = "Error: No details received from the data API for (Affiliation) verification."
            overall_status_log_message = "Failed - No details from API."
            # Fall through to AVS update logic
        else:
            record_data = details.get("recordData") # Expected to be hed__Affiliation__c data
            document_payload = details.get("documentPayload")

            if not record_data:
                logger.warning(f"No 'recordData' (expected Affiliation details) found for Employment Log ID: {employment_log_id}.")
                final_report_to_salesforce = "Error: Salesforce Affiliation data was missing, cannot perform detailed verification."
                overall_status_log_message = "Failed - Missing recordData (Affiliation)."
            else:
                # Extract Affiliation ID from the record_data
                affiliation_id_for_summary = record_data.get("Id") # Assuming "Id" is the key for Affiliation ID in record_data
                if not affiliation_id_for_summary:
                    logger.warning(f"Affiliation ID not found in recordData for Employment Log {employment_log_id}. {APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME} record cannot be reliably linked to Affiliation.")
                
                if document_payload and isinstance(document_payload, dict):
                    file_name = document_payload.get("fileName", "N/A")
                    file_extension = document_payload.get("fileExtension")
                    base64_data = document_payload.get("base64Data")
                    logger.info(f"Processing documentPayload '{file_name}' for Employment Log ID: {employment_log_id} (Affiliation: {affiliation_id_for_summary})")

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
                                    record_data_dict=record_data, # This is Affiliation data
                                    document_text=document_text_string
                                )
                                verification_report = emp_crew.run()
                                
                                final_report_to_salesforce = str(verification_report) if verification_report is not None else "Error: Employment Verification Crew produced no report."
                                if "Error:" in final_report_to_salesforce:
                                    overall_status_log_message = "Completed - Employment crew reported an error."
                                    logger.warning(f"Employment crew reported an error for {employment_log_id} (Affiliation: {affiliation_id_for_summary}): {final_report_to_salesforce}")
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
                else: 
                    logger.info(f"No document payload found in API response for {employment_log_id}. Cannot verify against document.")
                    final_report_to_salesforce = "Verification Info (Employment): No document found to verify against."
                    overall_status_log_message = "Completed - No document payload."
            
            if record_data: 
                 logger.debug(f"RecordData (Affiliation) for Employment Log ID {employment_log_id}: {json.dumps(record_data, indent=2)}")


        # --- Update/Create Application_Verification_Summary__c Record ---
        if not isinstance(final_report_to_salesforce, str):
            final_report_to_salesforce = str(final_report_to_salesforce)

        if len(final_report_to_salesforce) > MAX_SALESFORCE_REPORT_LENGTH:
            logger.warning(f"Compiled report for Employment (Log ID {employment_log_id}, Affiliation: {affiliation_id_for_summary}) is too long. Truncating.")
            final_report_to_salesforce = final_report_to_salesforce[:MAX_SALESFORCE_REPORT_LENGTH - 3] + "..."

        summary_name_base = "Employment History Analysis"
        summary_name = f"{summary_name_base} ({item_index})" if item_index is not None else f"{summary_name_base} - ID {affiliation_id_for_summary or employment_log_id}"
        
        success_update: bool = False
        if affiliation_id_for_summary: # Only proceed if we have the Affiliation ID
            success_update = sf_service.upsert_verification_summary(
                application_id=parent_application_id,
                report_content=final_report_to_salesforce,
                name_value=summary_name,
                affiliation_id=affiliation_id_for_summary
            )
        else:
            logger.error(f"Cannot update/create {APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME} for Employment Log {employment_log_id} because Affiliation ID is missing from recordData.")
            # Optionally update overall_status_log_message or append to final_report_to_salesforce if it's stored elsewhere as a fallback
        
        if success_update:
            logger.info(f"Successfully updated/created {APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME} for Employment (Affiliation ID: {affiliation_id_for_summary}). Status: {overall_status_log_message}")
        else:
            if affiliation_id_for_summary: # Log failure only if an attempt was made
                logger.error(f"Failed to update/create {APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME} for Employment (Affiliation ID: {affiliation_id_for_summary}). Attempted status: {overall_status_log_message}")

    except Exception as e:
        logger.error(f"Unexpected error in main processing for Employment Log ID {employment_log_id}: {e}", exc_info=True)
        error_report_content = f"Critical Error during processing Employment Log ID {employment_log_id}:\n{str(e)[:1000]}"
        
        summary_name_base = "Employment History Analysis - CRITICAL ERROR"
        summary_name_on_error = f"{summary_name_base} ({item_index})" if item_index is not None else f"{summary_name_base} - ID {affiliation_id_for_summary or employment_log_id}"
        try:
            current_sf_service = locals().get('sf_service')
            retrieved_affiliation_id = locals().get('affiliation_id_for_summary')
            if current_sf_service is not None and retrieved_affiliation_id:
                 current_sf_service.upsert_verification_summary(
                     application_id=parent_application_id,
                     report_content=error_report_content,
                     name_value=summary_name_on_error,
                     affiliation_id=retrieved_affiliation_id
                 )
            else:
                logger.error(f"sf_service or Affiliation ID not available to update {APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME} with critical error for Employment Log {employment_log_id}")
        except Exception as update_err:
            logger.error(f"Failed to even update {APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME} with critical error for Employment Log {employment_log_id}: {update_err}", exc_info=True)