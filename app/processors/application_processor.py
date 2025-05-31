# project_root/app/processors/application_processor.py
import logging
import json
from typing import TYPE_CHECKING, Any, Dict

# Import config for SObject API name (though passed as arg now)
from app.config import APPLICATION_OBJECT_API_NAME # For default or reference

# Use TYPE_CHECKING to avoid circular imports at runtime
if TYPE_CHECKING:
    from app.services.salesforce_service import SalesforceService
    # Corrected import paths for services and crew
    from app.services.document_extraction_service import extract_text_from_file
    from app.crew.application_crew import ApplicationVerificationCrewOrchestrator

logger = logging.getLogger(__name__)

async def process_single_application_detail(
    sf_service: 'SalesforceService',
    application_id: str,
    parent_application_id: str, # Kept for consistency, for main app it's same as application_id
    application_object_api_name: str # Explicitly pass the SObject API name
):
    """
    Processes a single Application record's details and its associated ID document.
    application_object_api_name: The SObject API name of the main application record being processed.
    """
    logger.info(f"Background task started for MAIN APPLICATION record: {application_object_api_name} ID: {application_id} (Parent App: {parent_application_id})")

    # Functional imports to avoid circular dependencies at module load time
    from app.services.document_extraction_service import extract_text_from_file
    from app.crew.application_crew import ApplicationVerificationCrewOrchestrator

    final_report_to_salesforce = f"Error: Initial processing failure for application {application_id}."
    processing_summary_for_response = "Processing started."

    try:
        # Fetch details from Apex using the provided application_object_api_name
        details: Dict[str, Any] | None = sf_service.get_record_detail_from_apex(application_id, application_object_api_name)

        if not details:
            logger.warning(f"No details received from Apex for {application_object_api_name} ID: {application_id}.")
            final_report_to_salesforce = "Error: No application/contact details received from the data API for verification."
            processing_summary_for_response = "Failed - No details from API."
            sf_service.update_record_analysis_report(application_id, application_object_api_name, final_report_to_salesforce)
            return processing_summary_for_response # Return summary

        record_data = details.get("recordData")
        document_payload = details.get("documentPayload")

        if not record_data:
            logger.warning(f"No 'recordData' (application/contact details) found for {application_object_api_name} ID: {application_id}.")
            final_report_to_salesforce = "Error: Salesforce application/contact data was missing, cannot perform detailed verification."
            processing_summary_for_response = "Failed - Missing application/contact recordData."
            # Log the problematic record_data if it's not None but still considered "missing" (e.g. empty dict)
            logger.debug(f"recordData for {application_id} (evaluated as missing): {json.dumps(record_data, indent=2) if record_data is not None else 'None'}")
        
        elif document_payload and isinstance(document_payload, dict):
            file_name = document_payload.get("fileName", "N/A")
            file_extension = document_payload.get("fileExtension")
            base64_data = document_payload.get("base64Data")
            logger.info(f"Processing ID documentPayload '{file_name}' for {application_object_api_name} ID: {application_id}")

            if base64_data and file_extension:
                try:
                    logger.info(f"Extracting text from embedded ID document: {file_name}")
                    document_text_string = await extract_text_from_file(base64_data, file_extension)

                    if document_text_string.startswith("Error:"):
                        logger.error(f"Text extraction failed for ID doc {file_name} ({application_id}): {document_text_string}")
                        final_report_to_salesforce = f"Verification Error (Application ID Proof): Text extraction failed for document '{file_name}'. Reason: {document_text_string}"
                        processing_summary_for_response = "Failed - ID Document text extraction error."
                    elif document_text_string.startswith("Note: No text found"):
                        logger.info(f"No text in ID doc {file_name} ({application_id}).")
                        final_report_to_salesforce = f"Verification Info (Application ID Proof): No text found in document '{file_name}'. Unable to perform crew verification."
                        processing_summary_for_response = "Completed - No text in ID document."
                    else: 
                        logger.info(f"Text extracted from ID document {file_name} for {application_id}. Length: {len(document_text_string)}. Applying Application Verification Crew.")
                        
                        app_crew = ApplicationVerificationCrewOrchestrator(
                            record_data_dict=record_data, # record_data is confirmed to exist here
                            document_text=document_text_string
                        )
                        verification_report = app_crew.run()
                        
                        final_report_to_salesforce = str(verification_report) if verification_report is not None else "Error: Application Verification Crew produced no report."
                        if "Error:" in final_report_to_salesforce:
                            processing_summary_for_response = "Completed - Application crew reported an error."
                            logger.warning(f"Application crew reported an error for {application_id}: {final_report_to_salesforce}")
                        else:
                            processing_summary_for_response = "Completed - Application ID document verification by crew."
                        
                except Exception as doc_proc_err:
                    logger.error(f"Error during ID document processing or crew execution for {application_id}, doc {file_name}: {doc_proc_err}", exc_info=True)
                    final_report_to_salesforce = f"Verification Error (Application ID Proof): Failed during document processing or AI crew. Details: {str(doc_proc_err)}"
                    processing_summary_for_response = "Failed - ID Document processing/crew error."
            else: # No base64_data or file_extension
                logger.info(f"No document data (base64/extension) in ID document payload for {application_id}.")
                final_report_to_salesforce = "Verification Info (Application ID Proof): Document data (base64 or extension) missing in payload. Cannot verify."
                processing_summary_for_response = "Completed - No ID document data in payload."
        else: # No document_payload at all
            logger.info(f"No ID document payload found in API response for {application_id}. Cannot verify against document.")
            final_report_to_salesforce = "Verification Info (Application ID Proof): No ID document found to verify against."
            processing_summary_for_response = "Completed - No ID document payload."
            if record_data: # Log record_data if no document was there for context
                 logger.debug(f"recordData for {application_id} (no document): {json.dumps(record_data, indent=2)}")

        # --- Update Salesforce Application Record ---
        if not isinstance(final_report_to_salesforce, str): # Ensure it's a string
            final_report_to_salesforce = str(final_report_to_salesforce)

        # Max length for Salesforce long text area (check your field's actual limit)
        # Standard Long Text Area is 32,768 characters. Rich Text can be more.
        max_report_length = 32000 
        if len(final_report_to_salesforce) > max_report_length:
            logger.warning(f"Compiled report for {application_object_api_name} ID {application_id} is too long ({len(final_report_to_salesforce)} chars). Truncating.")
            final_report_to_salesforce = final_report_to_salesforce[:max_report_length - 3] + "..."

        # Update the record using the provided application_object_api_name
        success_update: bool = sf_service.update_record_analysis_report(application_id, application_object_api_name, final_report_to_salesforce)

        if success_update:
            logger.info(f"Successfully updated analysis report for {application_object_api_name} ID: {application_id}. Status: {processing_summary_for_response}")
        else:
            logger.error(f"Failed to update analysis report for {application_object_api_name} ID: {application_id}. Attempted status: {processing_summary_for_response}")
            processing_summary_for_response = f"{processing_summary_for_response} (Salesforce update failed)"

    except Exception as e:
        logger.error(f"Unexpected error in main processing for {application_object_api_name} ID {application_id}: {e}", exc_info=True)
        error_report = f"Critical Error during processing {application_object_api_name} ID {application_id}:\n{str(e)[:1000]}"
        processing_summary_for_response = f"Failed - Unexpected error: {str(e)[:100]}"
        try:
            # Ensure sf_service is available and attempt to update with error
            if 'sf_service' in locals() and sf_service is not None:
                 sf_service.update_record_analysis_report(application_id, application_object_api_name, error_report)
            else: # Should not happen if sf_service is a mandatory arg
                logger.error(f"sf_service not available to update Salesforce with critical error for {application_object_api_name} ID {application_id}")
        except Exception as update_err:
            logger.error(f"Failed to even update Salesforce with critical error for {application_object_api_name} ID {application_id}: {update_err}", exc_info=True)
    
    return processing_summary_for_response
