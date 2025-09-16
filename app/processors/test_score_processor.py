# project_root/app/processors/test_score_processor.py
import logging
import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.config import (
    TEST_SCORE_OBJECT_API_NAME,
    MAX_SALESFORCE_REPORT_LENGTH,
    READABLE_OBJECT_NAMES
)
from app.services.document_extraction_service import DocumentExtractionError
from app.services.salesforce_service import SalesforceAPIError

if TYPE_CHECKING:
    from app.services.salesforce_service import SalesforceService
    from app.crew.test_score_crew import TestScoreVerificationCrewOrchestrator

logger = logging.getLogger(__name__)

def _format_error(record_id: str, component: str, reason: str, technical_error: str) -> str:
    """Creates a standardized error message for logging and reporting."""
    category = READABLE_OBJECT_NAMES[TEST_SCORE_OBJECT_API_NAME]
    return f"({category})-({record_id})-({component})-({reason}): {technical_error}"

async def process_single_test_score_detail(
    sf_service: "SalesforceService",
    test_score_id: str,
    parent_application_id: str,
    item_index: Optional[int] = None,
    extractor_instance: "FastDocumentExtractor" = None,
    resource_manager=None
):
    readable_name = READABLE_OBJECT_NAMES.get(TEST_SCORE_OBJECT_API_NAME, "Test Score Record")
    logger.info(f"Starting {readable_name} processing for ID: {test_score_id}")

    from app.services.document_extraction_service import extract_text_from_file, create_text_extractor
    from app.crew.test_score_crew import TestScoreVerificationCrewOrchestrator

    # Use provided extractor or create a per-job extractor
    if extractor_instance is None:
        extractor_instance = create_text_extractor(resource_manager=resource_manager)

    try:
        details = await asyncio.to_thread(
            sf_service.get_record_detail_from_apex, test_score_id, TEST_SCORE_OBJECT_API_NAME
        )
        
        record_data = details.get("recordData", {})
        salesforce_data_issue = details.get("Salesforce_data_issue_Summary")

        # **CHECK FOR GRACEFUL FALLBACK SCENARIO FIRST** (from top level or record data)
        fallback_summary = salesforce_data_issue or record_data.get("Salesforce_data_issue_Summary")
        if fallback_summary:
            logger.warning(f"Salesforce data issue detected for {test_score_id}: {fallback_summary}")
            name_suffix = record_data.get('RecordTypeName__c') or item_index or test_score_id
            summary_name = f"Test Score Analysis ({name_suffix})"
            summary_id = await asyncio.to_thread(
                sf_service.upsert_verification_summary,
                application_id=parent_application_id,
                report_content=None,
                name_value=summary_name,
                overall_feedback=fallback_summary,
                confidence_range="0",
                mismatched_field_list=None,
                test_id=test_score_id
            )
            logger.info(f"Created fallback AVS for {readable_name} {test_score_id}. AVS ID: {summary_id}")
            return f"Processed {readable_name} with data issue fallback."

        # **EARLY CHECK FOR ONLINE TEST MODE**
        if record_data.get('Test_Mode') == 'Online':
            logger.info(f"Online test mode detected for {test_score_id}. Skipping document verification.")
            
            name_suffix = record_data.get('RecordTypeName__c') or item_index or test_score_id
            summary_name = f"Test Score Analysis ({name_suffix})"
            
            # Direct update without document processing
            summary_id = await asyncio.to_thread(
                sf_service.upsert_verification_summary,
                application_id=parent_application_id,
                report_content='',
                name_value=summary_name,
                overall_feedback="Test mode is online, Which is Invalid",
                confidence_range=40,
                test_id=test_score_id
            )
            
            logger.info(f"Successfully processed Online {readable_name} {test_score_id}. AVS ID: {summary_id}")
            return  # Exit early, skip all document processing
        
        # **CONTINUE WITH NORMAL PROCESSING FOR NON-ONLINE TESTS**
        document_payload = details.get("documentPayload")

        if not document_payload:
            raise ValueError("Document payload was missing from the Salesforce record.")

        base64_data = document_payload.get("base64Data")
        file_extension = document_payload.get("fileExtension")
        if not base64_data or not file_extension:
            raise ValueError("Document content (base64) or file extension missing.")

        document_text_string = await extract_text_from_file(base64_data, file_extension, record_id=test_score_id, extractor=extractor_instance, resource_manager=resource_manager)

        ts_crew = TestScoreVerificationCrewOrchestrator(record_data, document_text_string, resource_manager=resource_manager)
        report_dict = await asyncio.to_thread(ts_crew.run)
        
        if not report_dict:
            raise ValueError("Crew did not return a valid report.")

        name_suffix = record_data.get('RecordTypeName__c') or item_index or test_score_id
        summary_name = f"Test Score Analysis ({name_suffix})"
        
        # Normal processing results
        overall_feedback = report_dict.get('overall_feedback', 'No feedback provided.')
        confidence = report_dict.get('confidence_range', 50)
        report_content = report_dict.get('field_comparison_summary', '')[:MAX_SALESFORCE_REPORT_LENGTH]
        
        summary_id = await asyncio.to_thread(
            sf_service.upsert_verification_summary,
            application_id=parent_application_id,
            report_content=report_content,
            name_value=summary_name,
            overall_feedback=overall_feedback,
            confidence_range=confidence,
            mismatched_field_list=report_dict.get('mismatched_field_list'),
            test_id=test_score_id
        )
        
        logger.info(f"Successfully processed {readable_name} {test_score_id}. AVS ID: {summary_id}")

    except SalesforceAPIError as e:
        if '500 Server Error' in str(e):
            reason = "Data provider failed. This is likely due to a missing document or invalid data on the Salesforce record. Please verify the record and its attachments."
            raise ValueError(_format_error(test_score_id, "Salesforce Data", reason, str(e)))
        else:
            raise ValueError(_format_error(test_score_id, "Salesforce API", "An API error occurred", str(e)))
    except DocumentExtractionError as e:
        raise ValueError(_format_error(test_score_id, "Document Extraction", "Failed to extract text from document", str(e)))
    except Exception as e:
        raise ValueError(_format_error(test_score_id, "Processing", "An unexpected error occurred", str(e)))
