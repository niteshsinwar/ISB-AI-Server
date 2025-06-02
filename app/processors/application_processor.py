import logging
import json
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
    parent_application_id: str, # For main app, same as application_id
    application_object_api_name: str 
):
    """
    Processes a single Application record's details and its associated ID document.
    application_object_api_name: The SObject API name of the main application record.
    """
    logger.info(f"Background task started for MAIN APPLICATION record: {application_object_api_name} ID: {application_id} (Parent App: {parent_application_id})")

    from app.services.document_extraction_service import extract_text_from_file
    from app.crew.application_crew import ApplicationVerificationCrewOrchestrator

    final_report_to_salesforce = f"Error: Initial processing failure for application {application_id}."
    processing_summary_for_response = "Processing started."
    contact_id_for_summary: Optional[str] = None

    try:
        details: Optional[Dict[str, Any]] = sf_service.get_record_detail_from_apex(application_id, application_object_api_name)

        if not details:
            logger.warning(f"No details received from Apex for {application_object_api_name} ID: {application_id}.")
            final_report_to_salesforce = "Error: No application/contact details received from the data API for verification."
            processing_summary_for_response = "Failed - No details from API."
            # Cannot update AVS without application_id and contact_id (which would come from details)
            return processing_summary_for_response 

        record_data = details.get("recordData")
        document_payload = details.get("documentPayload")

        if not record_data:
            logger.warning(f"No 'recordData' (application/contact details) found for {application_object_api_name} ID: {application_id}.")
            final_report_to_salesforce = "Error: Salesforce application/contact data was missing, cannot perform detailed verification."
            processing_summary_for_response = "Failed - Missing application/contact recordData."
            logger.debug(f"recordData for {application_id} (evaluated as missing): {json.dumps(record_data, indent=2) if record_data is not None else 'None'}")
        else:
            # Extract Contact ID from Application's recordData
            contact_id_for_summary = record_data.get(APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP)
            if not contact_id_for_summary:
                logger.warning(f"Contact ID ('{APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP}') not found in recordData for Application {application_id}. AVS record cannot be reliably linked to Contact.")
                # Optionally, append to report if processing continues:
                # final_report_to_salesforce += f" (Warning: Contact ID '{APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP}' missing in application data)"
        
            if document_payload and isinstance(document_payload, dict):
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
                                record_data_dict=record_data,
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
                else: 
                    logger.info(f"No document data (base64/extension) in ID document payload for {application_id}.")
                    final_report_to_salesforce = "Verification Info (Application ID Proof): Document data (base64 or extension) missing in payload. Cannot verify."
                    processing_summary_for_response = "Completed - No ID document data in payload."
            else: 
                logger.info(f"No ID document payload found in API response for {application_id}. Cannot verify against document.")
                final_report_to_salesforce = "Verification Info (Application ID Proof): No ID document found to verify against."
                processing_summary_for_response = "Completed - No ID document payload."
                if record_data: 
                     logger.debug(f"recordData for {application_id} (no document): {json.dumps(record_data, indent=2)}")

        # --- Update/Create Application_Verification_Summary__c Record ---
        if not isinstance(final_report_to_salesforce, str): 
            final_report_to_salesforce = str(final_report_to_salesforce)

        if len(final_report_to_salesforce) > MAX_SALESFORCE_REPORT_LENGTH:
            logger.warning(f"Compiled report for {application_object_api_name} ID {application_id} is too long ({len(final_report_to_salesforce)} chars). Truncating.")
            final_report_to_salesforce = final_report_to_salesforce[:MAX_SALESFORCE_REPORT_LENGTH - 3] + "..."

        summary_name = "Personal Detail Analysis"
        success_update: bool = False

        if contact_id_for_summary:
            success_update = sf_service.upsert_verification_summary(
                application_id=parent_application_id, 
                report_content=final_report_to_salesforce,
                name_value=summary_name,
                contact_id=contact_id_for_summary
            )
        else:
            logger.error(f"Cannot update/create {APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME} for Application {application_id} because Contact ID ('{APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP}') is missing from recordData.")
            # Append to the response summary, as this is a key part of the new logic
            processing_summary_for_response += " (AVS Update Failed - Missing Contact ID)"
            # Optionally, also modify final_report_to_salesforce if it's intended for display elsewhere,
            # though its primary destination is now AVS.
            # final_report_to_salesforce += f" (Error: Could not link to Contact for {APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME})"


        if success_update:
            logger.info(f"Successfully updated/created {APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME} for Application ID: {application_id} linked to Contact ID: {contact_id_for_summary}. Status: {processing_summary_for_response}")
        else:
            # Log error only if an attempt was made (i.e., contact_id_for_summary was present)
            if contact_id_for_summary:
                logger.error(f"Failed to update/create {APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME} for Application ID: {application_id} linked to Contact ID: {contact_id_for_summary}. Attempted status: {processing_summary_for_response}")
                # Avoid double-appending "AVS update failed" if already added due to missing ID
                if "AVS Update Failed" not in processing_summary_for_response:
                    processing_summary_for_response = f"{processing_summary_for_response} (AVS update failed)"


    except Exception as e:
        logger.error(f"Unexpected error in main processing for {application_object_api_name} ID {application_id}: {e}", exc_info=True)
        error_report_content = f"Critical Error during processing {application_object_api_name} ID {application_id}:\n{str(e)[:1000]}"
        processing_summary_for_response = f"Failed - Unexpected error: {str(e)[:100]}"
        try:
            # Attempt to log critical error to AVS if possible
            current_sf_service = locals().get('sf_service')
            retrieved_contact_id = locals().get('contact_id_for_summary') # Check if contact_id was retrieved before error
            if current_sf_service is not None and retrieved_contact_id:
                 current_sf_service.upsert_verification_summary(
                     application_id=parent_application_id, 
                     report_content=error_report_content,
                     name_value="Personal Detail Analysis - CRITICAL ERROR",
                     contact_id=retrieved_contact_id
                 )
            else:
                logger.error(f"sf_service or Contact ID not available to update {APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME} with critical error for Application {application_id}")
        except Exception as update_err:
            logger.error(f"Failed to even update {APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME} with critical error for Application {application_id}: {update_err}", exc_info=True)
    
    return processing_summary_for_response