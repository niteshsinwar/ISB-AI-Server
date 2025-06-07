import logging
import json
import asyncio
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
    record_sobject_api_name = TEST_SCORE_OBJECT_API_NAME
    record_type_log_name = "TestScore"
    
    logger.info(f"Background task started for {record_type_log_name} ID: {test_score_id} (App: {parent_application_id})")

    from app.services.document_extraction_service import extract_text_from_file 
    from app.crew.test_score_crew import TestScoreVerificationCrewOrchestrator

    final_report_to_salesforce = f"Error: Initial processing failure for {record_type_log_name} ID {test_score_id}."

    try:
        details: Optional[Dict[str, Any]] = await asyncio.to_thread(
            sf_service.get_record_detail_from_apex, test_score_id, record_sobject_api_name
        )

        if not details:
            final_report_to_salesforce = f"Error: No details received from the data API for {record_type_log_name}."
        else:
            record_data = details.get("recordData")
            document_payload = details.get("documentPayload")

            if not record_data:
                final_report_to_salesforce = f"Error: Salesforce {record_type_log_name} record data was missing."
            elif document_payload and isinstance(document_payload, dict):
                file_name = document_payload.get("fileName", "N/A")
                file_extension = document_payload.get("fileExtension")
                base64_data = document_payload.get("base64Data")

                if base64_data and file_extension:
                    document_text_string = await extract_text_from_file(base64_data, file_extension)
                    if document_text_string.startswith("Error:"):
                        final_report_to_salesforce = f"Verification Error ({record_type_log_name}): Text extraction failed for '{file_name}'. Reason: {document_text_string}"
                    elif document_text_string.startswith("Note: No text found"):
                        final_report_to_salesforce = f"Verification Info ({record_type_log_name}): No text found in document '{file_name}'."
                    else:
                        ts_crew = TestScoreVerificationCrewOrchestrator(
                            record_data_dict=record_data,
                            document_text=document_text_string
                        )
                        verification_report = await asyncio.to_thread(ts_crew.run)
                        final_report_to_salesforce = str(verification_report) if verification_report is not None else f"Error: {record_type_log_name} Crew produced no report."
                else:
                    final_report_to_salesforce = f"Verification Info ({record_type_log_name}): Document data missing in payload."
            else:
                final_report_to_salesforce = f"Verification Info ({record_type_log_name}): No document found to verify against."

        if len(final_report_to_salesforce) > MAX_SALESFORCE_REPORT_LENGTH:
            final_report_to_salesforce = final_report_to_salesforce[:MAX_SALESFORCE_REPORT_LENGTH - 3] + "..."

        summary_name = f"Test Score Analysis ({item_index})" if item_index is not None else f"Test Score Analysis - {test_score_id}"
        
        await asyncio.to_thread(
            sf_service.upsert_verification_summary,
            application_id=parent_application_id,
            report_content=final_report_to_salesforce,
            name_value=summary_name,
            test_id=test_score_id
        )
        logger.info(f"Successfully triggered AVS update for Test Score ID: {test_score_id}.")

    except Exception as e:
        logger.error(f"Unexpected error in main processing for {record_type_log_name} ID {test_score_id}: {e}", exc_info=True)
