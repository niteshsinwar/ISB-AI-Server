import logging
import json
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.config import (
    TEST_SCORE_OBJECT_API_NAME, 
    APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME,
    MAX_SALESFORCE_REPORT_LENGTH
)

if TYPE_CHECKING:
    from app.services.salesforce_service import SalesforceService
    from app.services.document_extraction_service import extract_text_from_file
    from app.crew.test_score_crew import TestScoreVerificationCrewOrchestrator

logger = logging.getLogger(__name__)

async def process_single_test_score_detail(
    sf_service: 'SalesforceService',
    test_score_id: str,
    parent_application_id: str,
    item_index: Optional[int] = None
):
    """
    Processes a single Test Score record (e.g., hed__Test__c).
    """
    record_sobject_api_name = TEST_SCORE_OBJECT_API_NAME
    record_type_log_name = "TestScore"
    logger.info(f"Background task started for {record_type_log_name} ({record_sobject_api_name}) ID: {test_score_id} (related to Application ID: {parent_application_id}, Index: {item_index})")

    from app.services.document_extraction_service import extract_text_from_file 
    from app.crew.test_score_crew import TestScoreVerificationCrewOrchestrator

    final_report_to_salesforce = f"Error: Initial processing failure for {record_type_log_name} ID {test_score_id}."
    overall_status_log_message = "Processing started." # For internal logging

    try:
        details: Optional[Dict[str, Any]] = sf_service.get_record_detail_from_apex(test_score_id, record_sobject_api_name)

        if not details:
            logger.warning(f"No details received from Apex for {record_type_log_name} ID: {test_score_id}.")
            final_report_to_salesforce = f"Error: No details received from the data API for {record_type_log_name} verification."
            overall_status_log_message = "Failed - No details from API."
            # Fall through to AVS update logic
        else:
            record_data = details.get("recordData")
            document_payload = details.get("documentPayload")

            if not record_data:
                logger.warning(f"No 'recordData' found for {record_type_log_name} ID: {test_score_id}. Cannot run verification crew.")
                final_report_to_salesforce = f"Error: Salesforce {record_type_log_name} record data was missing, cannot perform detailed verification."
                overall_status_log_message = "Failed - Missing recordData."
            elif document_payload and isinstance(document_payload, dict):
                file_name = document_payload.get("fileName", "N/A")
                file_extension = document_payload.get("fileExtension")
                base64_data = document_payload.get("base64Data")
                logger.info(f"Processing documentPayload '{file_name}' for {record_type_log_name} ID: {test_score_id}")

                if base64_data and file_extension:
                    try:
                        logger.info(f"Extracting text from embedded document: {file_name} for {test_score_id}")
                        document_text_string = await extract_text_from_file(base64_data, file_extension)

                        if document_text_string.startswith("Error:"):
                            logger.error(f"Text extraction failed for doc {file_name} ({test_score_id}): {document_text_string}")
                            final_report_to_salesforce = f"Verification Error ({record_type_log_name}): Text extraction failed for document '{file_name}'. Reason: {document_text_string}"
                            overall_status_log_message = "Failed - Document text extraction error."
                        elif document_text_string.startswith("Note: No text found"):
                            logger.info(f"No text in doc {file_name} ({test_score_id}).")
                            final_report_to_salesforce = f"Verification Info ({record_type_log_name}): No text found in document '{file_name}'. Unable to perform crew verification."
                            overall_status_log_message = "Completed - No text in document."
                        else: 
                            logger.info(f"Text extracted from {file_name} for {test_score_id}. Length: {len(document_text_string)}. Applying {record_type_log_name} Verification Crew.")
                            
                            ts_crew = TestScoreVerificationCrewOrchestrator(
                                record_data_dict=record_data,
                                document_text=document_text_string
                            )
                            verification_report = ts_crew.run()
                            
                            final_report_to_salesforce = str(verification_report) if verification_report is not None else f"Error: {record_type_log_name} Verification Crew produced no report."
                            if "Error:" in final_report_to_salesforce:
                                overall_status_log_message = f"Completed - {record_type_log_name} crew reported an error."
                                logger.warning(f"{record_type_log_name} crew reported an error for {test_score_id}: {final_report_to_salesforce}")
                            else:
                                overall_status_log_message = f"Completed - {record_type_log_name} document verification by crew."
                            
                    except Exception as doc_proc_err:
                        logger.error(f"Error during document processing or crew execution for {test_score_id}, doc {file_name}: {doc_proc_err}", exc_info=True)
                        final_report_to_salesforce = f"Verification Error ({record_type_log_name}): Failed during document processing or AI crew. Details: {str(doc_proc_err)}"
                        overall_status_log_message = "Failed - Document processing/crew error."
                else:
                    logger.info(f"No document data (base64/extension) in payload for {test_score_id}.")
                    final_report_to_salesforce = f"Verification Info ({record_type_log_name}): Document data (base64 or extension) missing in payload. Cannot verify."
                    overall_status_log_message = "Completed - No document data in payload."
            else: 
                logger.info(f"No document payload found in API response for {test_score_id}. Cannot verify {record_type_log_name} against document.")
                final_report_to_salesforce = f"Verification Info ({record_type_log_name}): No document found to verify against."
                overall_status_log_message = "Completed - No document payload."
                if record_data: 
                     logger.debug(f"recordData for {test_score_id} (no document): {json.dumps(record_data, indent=2)}")

        # --- Update/Create Application_Verification_Summary__c Record ---
        if not isinstance(final_report_to_salesforce, str):
            final_report_to_salesforce = str(final_report_to_salesforce)

        if len(final_report_to_salesforce) > MAX_SALESFORCE_REPORT_LENGTH:
            logger.warning(f"Compiled report for {record_type_log_name} ID {test_score_id} is too long ({len(final_report_to_salesforce)} chars). Truncating.")
            final_report_to_salesforce = final_report_to_salesforce[:MAX_SALESFORCE_REPORT_LENGTH - 3] + "..."

        summary_name_base = "Test Score Analysis"
        summary_name = f"{summary_name_base} ({item_index})" if item_index is not None else f"{summary_name_base} - {test_score_id}"
        
        success_update: bool = sf_service.upsert_verification_summary(
            application_id=parent_application_id,
            report_content=final_report_to_salesforce,
            name_value=summary_name,
            test_id=test_score_id # This is the ID of the hed__Test__c record
        )

        if success_update:
            logger.info(f"Successfully updated/created {APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME} for {record_type_log_name} ID: {test_score_id}. Status: {overall_status_log_message}")
        else:
            logger.error(f"Failed to update/create {APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME} for {record_type_log_name} ID: {test_score_id}. Attempted status: {overall_status_log_message}")

    except Exception as e:
        logger.error(f"Unexpected error in main processing for {record_type_log_name} ID {test_score_id}: {e}", exc_info=True)
        error_report_content = f"Critical Error during processing {record_type_log_name} ID {test_score_id}:\n{str(e)[:1000]}"
        summary_name_base = "Test Score Analysis - CRITICAL ERROR"
        summary_name_on_error = f"{summary_name_base} ({item_index})" if item_index is not None else f"{summary_name_base} - {test_score_id}"
        try:
            current_sf_service = locals().get('sf_service')
            if current_sf_service is not None:
                 current_sf_service.upsert_verification_summary(
                     application_id=parent_application_id,
                     report_content=error_report_content,
                     name_value=summary_name_on_error,
                     test_id=test_score_id
                 )
            else:
                logger.error(f"sf_service not available to update {APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME} with critical error for {record_type_log_name} ID {test_score_id}")
        except Exception as update_err:
            logger.error(f"Failed to even update {APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME} with critical error for {record_type_log_name} ID {test_score_id}: {update_err}", exc_info=True)