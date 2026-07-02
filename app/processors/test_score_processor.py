# project_root/app/processors/test_score_processor.py
import logging
import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.config import (
    TEST_SCORE_OBJECT_API_NAME,
    MAX_SALESFORCE_REPORT_LENGTH,
    READABLE_OBJECT_NAMES
)
from app.core.processing_utils import should_skip_processing, detect_extraction_failure
from app.core.job_run_logger import get_job_logger
from app.langgraph.llm_utils import reset_global_usage, get_job_cost_summary
from app.services.document_extraction_service import DocumentExtractionError
from app.services.salesforce_service import SalesforceAPIError

if TYPE_CHECKING:
    from app.services.salesforce_service import SalesforceService

logger = logging.getLogger(__name__)


def _capture_usage() -> Dict[str, Any]:
    """Capture current usage from global accumulator."""
    summary = get_job_cost_summary()
    model = "unknown"
    breakdown = summary.get("detailed_breakdown", [])
    if breakdown:
        model = breakdown[-1].get("model", "unknown")
    totals = summary.get("totals", {})
    return {
        "input_tokens": totals.get("prompt_tokens", 0),
        "output_tokens": totals.get("completion_tokens", 0),
        "cost": totals.get("total_cost_usd", 0.0),
        "model": model
    }

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
):
    readable_name = READABLE_OBJECT_NAMES.get(TEST_SCORE_OBJECT_API_NAME, "Test Score Record")
    logger.info(f"Starting {readable_name} processing for ID: {test_score_id}")

    from app.services.document_extraction_service import extract_text_from_file, create_text_extractor
    # Use provided extractor or create a per-job extractor
    if extractor_instance is None:
        extractor_instance = create_text_extractor()

    try:
        details = await asyncio.to_thread(
            sf_service.get_test_score_record_data, test_score_id, parent_application_id
        )
        
        record_data = details.get("recordData", {})
        document_payload = details.get("documentPayload")
        salesforce_data_issue = details.get("Salesforce_data_issue_Summary")

        # Cost optimization: Check if record should be skipped (100% verified, no changes)
        existing_avs = await asyncio.to_thread(
            sf_service.get_existing_avs_metadata,
            application_id=parent_application_id,
            test_id=test_score_id
        )
        skip, reason = should_skip_processing(
            existing_avs=existing_avs,
            record_last_modified=record_data.get("LastModifiedDate"),
            document_last_modified=document_payload.get("lastModifiedDate") if document_payload else None  # Apex returns lowercase 'l'
        )
        if skip:
            logger.info(f"Skipping {readable_name} {test_score_id}: {reason}")
            
            # Log skipped status
            job_logger = get_job_logger()
            job_logger.add_detailed_record_log(
                record_type=f"TestScore_{record_data.get('RecordTypeName__c') or item_index or test_score_id}",
                doc_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                status="skipped",
                error=reason
            )
            return f"Skipped {readable_name} - already 100% verified with no changes."

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
            job_logger = get_job_logger()
            job_logger.add_detailed_record_log(
                record_type=f"TestScore_{name_suffix}",
                doc_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                status="failed",
                error=fallback_summary
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

            # Initialize job_logger for Online test mode logging
            job_logger = get_job_logger()
            job_logger.add_detailed_record_log(
                record_type=f"TestScore_{name_suffix}",
                doc_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                status="completed"
            )
            return  # Exit early, skip all document processing

        # **CONTINUE WITH NORMAL PROCESSING FOR NON-ONLINE TESTS**
        if not document_payload:
            raise ValueError("Document payload was missing from the Salesforce record.")

        base64_data = document_payload.get("base64Data")
        file_extension = document_payload.get("fileExtension")
        if not base64_data or not file_extension:
            raise ValueError("Document content (base64) or file extension missing.")

        # Reset usage before doc extraction
        reset_global_usage()

        # Smart extraction: pass record type and data for context-aware extraction
        document_text_string = await extract_text_from_file(
            base64_data,
            file_extension,
            record_id=test_score_id,
            extractor=extractor_instance,
            record_type="test_score",
            record_data=record_data
        )

        extraction_failure = detect_extraction_failure(document_text_string)
        if extraction_failure:
            logger.warning(f"Extraction failure for {readable_name} {test_score_id}: {extraction_failure}")
            fallback_summary = extraction_failure
            name_suffix = record_data.get('RecordTypeName__c') or item_index or test_score_id
            summary_name = f"Test Score Analysis ({name_suffix})"
            summary_record_id = await asyncio.to_thread(
                sf_service.upsert_verification_summary,
                application_id=parent_application_id,
                report_content=None,
                name_value=summary_name,
                overall_feedback=fallback_summary,
                confidence_range="0",
                mismatched_field_list=None,
                test_id=test_score_id
            )
            job_logger = get_job_logger()
            job_logger.add_detailed_record_log(
                record_type="Test_Score",
                doc_usage=_capture_usage(),
                crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                status="failed",
                error=fallback_summary
            )
            return f"Processed {readable_name} - Missing Document (0% Confidence)."

        # Capture doc extraction usage
        doc_usage = _capture_usage()
        logger.info(f"Doc extraction usage for {test_score_id}: {doc_usage}")

        # Reset usage before crew processing
        reset_global_usage()

        # LangGraph Execution
        logger.info(f"Using LangGraph for {readable_name} {test_score_id}")
        from app.langgraph.test_score_graph import TestScoreGraphOrchestrator
        ts_orchestrator = TestScoreGraphOrchestrator(record_data, document_text_string)
        report_dict = await asyncio.to_thread(ts_orchestrator.run)

        if not report_dict:
            raise ValueError("Graph execution did not return a valid report.")

        # Capture processing usage
        crew_usage = _capture_usage()
        logger.info(f"Graph processing usage for {test_score_id}: {crew_usage}")

        # Log detailed usage to job logger
        job_logger = get_job_logger()
        name_suffix = record_data.get('RecordTypeName__c') or item_index or test_score_id
        job_logger.add_detailed_record_log(
            record_type=f"TestScore_{name_suffix}",
            doc_usage=doc_usage,
            crew_usage=crew_usage,
            status="completed"
        )

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
        error_msg = str(e)
        job_logger = get_job_logger()
        rt = f"TestScore_{test_score_id}"
        if 'record_data' in locals():
            rt = f"TestScore_{record_data.get('RecordTypeName__c') or item_index or test_score_id}"
        job_logger.add_detailed_record_log(
            record_type=rt,
            doc_usage=_capture_usage(),
            crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "error"},
            status="failed",
            error=error_msg
        )
        if '500 Server Error' in error_msg:
            reason = "Data provider failed. This is likely due to a missing document or invalid data on the Salesforce record. Please verify the record and its attachments."
            raise ValueError(_format_error(test_score_id, "Salesforce Data", reason, str(e)))
        else:
            raise ValueError(_format_error(test_score_id, "Salesforce API", "An API error occurred", str(e)))
    except DocumentExtractionError as e:
        error_msg = str(e)
        job_logger = get_job_logger()
        rt = f"TestScore_{test_score_id}"
        if 'record_data' in locals():
            rt = f"TestScore_{record_data.get('RecordTypeName__c') or item_index or test_score_id}"
        job_logger.add_detailed_record_log(
            record_type=rt,
            doc_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "failed"},
            crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
            status="failed",
            error=error_msg
        )
        raise ValueError(_format_error(test_score_id, "Document Extraction", "Failed to extract text from document", str(e)))
    except Exception as e:
        error_msg = str(e)
        job_logger = get_job_logger()
        rt = f"TestScore_{test_score_id}"
        if 'record_data' in locals():
            rt = f"TestScore_{record_data.get('RecordTypeName__c') or item_index or test_score_id}"
        job_logger.add_detailed_record_log(
            record_type=rt,
            doc_usage=_capture_usage(),
            crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "error"},
            status="failed",
            error=error_msg
        )
        raise ValueError(_format_error(test_score_id, "Processing", "An unexpected error occurred", str(e)))
