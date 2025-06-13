# project_root/app/processors/test_score_processor.py
import logging
import json
import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional, List

from app.config import (
    TEST_SCORE_OBJECT_API_NAME,
    MAX_SALESFORCE_REPORT_LENGTH
)

if TYPE_CHECKING:
    from app.services.salesforce_service import SalesforceService
    from app.services.document_extraction_service import extract_text_from_file
    from app.crew.test_score_crew import TestScoreVerificationCrewOrchestrator

logger = logging.getLogger(__name__)

async def process_single_test_score_detail(
    sf_service: "SalesforceService",
    test_score_id: str,
    parent_application_id: str,
    item_index: Optional[int] = None
):
    """
    Processes a single Test Score record and its document.
    Raises ValueError on any processing failure.
    """
    record_sobject_api_name = TEST_SCORE_OBJECT_API_NAME
    logger.info(f"Starting Test Score processing for ID: {test_score_id} (App: {parent_application_id})")

    from app.services.document_extraction_service import extract_text_from_file 
    from app.crew.test_score_crew import TestScoreVerificationCrewOrchestrator

    # 1. Fetch data from Salesforce
    details: Optional[Dict[str, Any]] = await asyncio.to_thread(
        sf_service.get_record_detail_from_apex, test_score_id, record_sobject_api_name
    )
    if not details:
        raise ValueError("Failed to receive details from Salesforce API for Test Score.")

    record_data = details.get("recordData")
    document_payload = details.get("documentPayload")

    if not record_data:
        raise ValueError("Salesforce Test Score data was missing in API response.")

    task_id_for_lookup = record_data.get('Task_Id')
    dci_id_for_lookup = record_data.get('DocumentchecklistItem_Id')

    # 2. Extract text from the document
    if not document_payload or not isinstance(document_payload, dict):
        raise ValueError("No document payload found to verify against.")

    file_name = document_payload.get("fileName", "N/A")
    file_extension = document_payload.get("fileExtension")
    base64_data = document_payload.get("base64Data")

    if not base64_data or not file_extension:
        raise ValueError("Document data (base64) or file extension missing in payload.")
        
    document_text: str | List[str] = await extract_text_from_file(base64_data, file_extension)

    # FIX: Handle cases where the extractor returns a list of strings
    if isinstance(document_text, list):
        document_text = "\n\n--- Page Break ---\n\n".join(document_text)

    if document_text.startswith("Error:") or "No text found" in document_text:
        raise ValueError(f"Text extraction failed for '{file_name}': {document_text}")

    # 3. Run the verification crew
    ts_crew = TestScoreVerificationCrewOrchestrator(record_data, document_text)
    report_dict = await asyncio.to_thread(ts_crew.run)

    field_summary_report = report_dict.get('field_comparison_summary')
    overall_feedback_report = report_dict.get('overall_feedback')
    confidence_report = report_dict.get('confidence_range')

    if not all([field_summary_report, overall_feedback_report, confidence_report is not None]):
        raise ValueError("Crew failed to return a valid report. Check crew logs for details.")

    if len(field_summary_report) > MAX_SALESFORCE_REPORT_LENGTH:
        field_summary_report = field_summary_report[:MAX_SALESFORCE_REPORT_LENGTH - 3] + "..."

    # 4. Upsert the summary record to Salesforce
    name_suffix = record_data.get('RecordTypeName__c') or item_index or test_score_id
    summary_name = f"Test Score Analysis ({name_suffix})"
    
    summary_id = await asyncio.to_thread(
        sf_service.upsert_verification_summary,
        application_id=parent_application_id,
        report_content=field_summary_report,
        name_value=summary_name,
        overall_feedback=overall_feedback_report,
        confidence_range=confidence_report,
        test_id=test_score_id
    )
    if not summary_id:
        raise ValueError(f"Failed to upsert Application_Verification_Summary__c for Test Score ID {test_score_id}.")
        
    logger.info(f"Upserted AVS {summary_id} for Test Score {test_score_id}. Linking to related items.")
    
    # 5. Link the summary to other items (Task, DCI)
    await asyncio.to_thread(
        sf_service.link_summary_to_related_items,
        summary_id, task_id_for_lookup, dci_id_for_lookup, overall_feedback_report
    )
