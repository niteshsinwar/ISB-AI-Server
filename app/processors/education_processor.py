# project_root/app/processors/education_processor.py
import logging
import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.config import (
    APPLICATION_OBJECT_API_NAME,
    EDUCATION_LOG_OBJECT_API_NAME,
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
    category = READABLE_OBJECT_NAMES[EDUCATION_LOG_OBJECT_API_NAME]
    return f"({category})-({record_id})-({component})-({reason}): {technical_error}"

async def process_single_education_history_detail(
    sf_service: "SalesforceService",
    education_log_id: str,
    parent_application_id: str,
    item_index: Optional[int] = None,
    extractor_instance: "FastDocumentExtractor" = None,
):
    readable_name = READABLE_OBJECT_NAMES.get(EDUCATION_LOG_OBJECT_API_NAME, "Education Record")
    logger.info(f"Starting {readable_name} processing for Log ID: {education_log_id}")

    from app.services.document_extraction_service import extract_text_from_file, create_text_extractor
    # Use provided extractor or create a per-job extractor
    if extractor_instance is None:
        extractor_instance = create_text_extractor()

    try:
        details = await asyncio.to_thread(
            sf_service.get_record_detail_from_apex, education_log_id, EDUCATION_LOG_OBJECT_API_NAME
        )
        
        record_data = details.get("recordData", {})
        document_payload = details.get("documentPayload")
        salesforce_data_issue = details.get("Salesforce_data_issue_Summary")
        actual_education_detail_id = record_data.get("Id")

        # Cost optimization: Check if record should be skipped (100% verified, no changes)
        if actual_education_detail_id:
            existing_avs = await asyncio.to_thread(
                sf_service.get_existing_avs_metadata,
                application_id=parent_application_id,
                education_history_id=actual_education_detail_id
            )
            skip, reason = should_skip_processing(
                existing_avs=existing_avs,
                record_last_modified=record_data.get("LastModifiedDate"),
                document_last_modified=document_payload.get("lastModifiedDate") if document_payload else None  # Apex returns lowercase 'l'
            )
            if skip:
                logger.info(f"Skipping {readable_name} {education_log_id}: {reason}")
                
                # Log skipped status
                job_logger = get_job_logger()
                job_logger.add_detailed_record_log(
                    record_type=f"Education_{record_data.get('degreeLevel') or item_index or education_log_id}",
                    doc_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                    crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                    status="skipped",
                    error=reason
                )
                return f"Skipped {readable_name} - already 100% verified with no changes."

        # Check for graceful fallback scenario (from top level or record data)
        fallback_summary = salesforce_data_issue or record_data.get("Salesforce_data_issue_Summary")
        if fallback_summary:
            logger.warning(f"Salesforce data issue detected for {education_log_id}: {fallback_summary}")
            name_suffix = record_data.get('degreeLevel') or item_index or education_log_id
            summary_name = f"Education Analysis ({name_suffix})"
            summary_id = await asyncio.to_thread(
                sf_service.upsert_verification_summary,
                application_id=parent_application_id,
                report_content=None,
                name_value=summary_name,
                overall_feedback=fallback_summary,
                confidence_range="0",
                mismatched_field_list=None,
                education_history_id=actual_education_detail_id
            )
            job_logger = get_job_logger()
            job_logger.add_detailed_record_log(
                record_type=f"Education_{name_suffix}",
                doc_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                status="failed",
                error=fallback_summary
            )
            logger.info(f"Created fallback AVS for {readable_name} {education_log_id}. AVS ID: {summary_id}")
            return f"Processed {readable_name} with data issue fallback."

        if not document_payload:
            raise ValueError("Document payload was missing from the Salesforce record.")
        if not actual_education_detail_id:
            raise ValueError("Could not identify the underlying EducationHistory__c ID.")

        base64_data = document_payload.get("base64Data")
        file_extension = document_payload.get("fileExtension")
        if not base64_data or not file_extension:
            raise ValueError("Document content (base64) or file extension missing.")

        # Reset usage before doc extraction to track separately
        reset_global_usage()

        # Smart extraction: pass record type and data for context-aware extraction
        document_text_string = await extract_text_from_file(
            base64_data,
            file_extension,
            record_id=education_log_id,
            extractor=extractor_instance,
            record_type="education",
            record_data=record_data
        )

        extraction_failure = detect_extraction_failure(document_text_string)
        if extraction_failure:
            logger.warning(f"Extraction failure for {readable_name} {education_log_id}: {extraction_failure}")
            fallback_summary = extraction_failure
            name_suffix = record_data.get('degreeLevel') or item_index or education_log_id
            summary_name = f"Education Analysis ({name_suffix})"
            summary_record_id = await asyncio.to_thread(
                sf_service.upsert_verification_summary,
                application_id=parent_application_id,
                report_content=None,
                name_value=summary_name,
                overall_feedback=fallback_summary,
                confidence_range="0",
                mismatched_field_list=None,
                education_history_id=actual_education_detail_id
            )
            job_logger = get_job_logger()
            job_logger.add_detailed_record_log(
                record_type="Education",
                doc_usage=_capture_usage(),
                crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                status="failed",
                error=fallback_summary
            )
            return f"Processed {readable_name} - Missing Document (0% Confidence)."

        # Capture doc extraction usage
        doc_usage = _capture_usage()
        logger.info(f"Doc extraction usage for {education_log_id}: {doc_usage}")

        # Reset usage before crew processing
        reset_global_usage()

        # LangGraph Execution
        logger.info(f"Using LangGraph for {readable_name} {education_log_id}")
        from app.langgraph.education_graph import EducationGraphOrchestrator

        # Fetch application submission date for recency checks
        app_submission_date = None
        try:
            app_details = await asyncio.to_thread(
                sf_service.get_record_detail_from_apex, parent_application_id, APPLICATION_OBJECT_API_NAME
            )
            if app_details:
                app_submission_date = app_details.get("recordData", {}).get("hed__Application_Date__c")
        except Exception as e:
            logger.warning(f"Could not fetch application submission date: {e}")

        edu_orchestrator = EducationGraphOrchestrator(record_data, document_text_string, app_submission_date)
        report_dict = await asyncio.to_thread(edu_orchestrator.run)

        if not report_dict:
            raise ValueError("Graph execution did not return a valid report.")

        # Capture processing usage
        crew_usage = _capture_usage()
        logger.info(f"Graph processing usage for {education_log_id}: {crew_usage}")

        # Log detailed usage to job logger
        job_logger = get_job_logger()
        name_suffix = record_data.get('degreeLevel') or item_index or education_log_id
        job_logger.add_detailed_record_log(
            record_type=f"Education_{name_suffix}",
            doc_usage=doc_usage,
            crew_usage=crew_usage,
            status="completed"
        )

        name_suffix = record_data.get('degreeLevel') or item_index or education_log_id
        summary_name = f"Education Analysis ({name_suffix})"
        summary_id = await asyncio.to_thread(
            sf_service.upsert_verification_summary,
            application_id=parent_application_id,
            report_content=report_dict.get('field_comparison_summary', '')[:MAX_SALESFORCE_REPORT_LENGTH],
            name_value=summary_name,
            overall_feedback=report_dict.get('overall_feedback'),
            confidence_range=report_dict.get('confidence_range'),
            mismatched_field_list=report_dict.get('mismatched_field_list'),
            education_history_id=actual_education_detail_id
        )
        
        logger.info(f"Successfully processed {readable_name} {education_log_id}. AVS ID: {summary_id}")

        # Create tasks for task-worthy mismatches (college name, cgpa, etc.).
        # Task creation must never fail the record: the AVS is already written.
        try:
            from app.core.task_builder import extract_task_worthy_mismatches, build_task_from_mismatch
            task_worthy = extract_task_worthy_mismatches(report_dict, report_dict.get('mismatched_field_list', ''))

            if task_worthy:
                # Assign to whoever owns the DocumentChecklistItem (fallback: application owner)
                owner_id = await sf_service.get_task_assignee_for_application(
                    parent_application_id,
                    dci_id=record_data.get("DocumentchecklistItem_Id"),
                )
                if not owner_id:
                    no_owner_msg = (
                        f"Mismatch tasks NOT created for {education_log_id}: no task assignee "
                        "(checklist owner and application owner both unavailable)."
                    )
                    logger.warning(no_owner_msg)
                    job_logger.add_detailed_record_log(
                        record_type=f"Education_Tasks_{item_index or education_log_id[:8]}",
                        doc_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                        crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                        status="warning",
                        error=no_owner_msg,
                    )

                for mismatch in task_worthy:
                    task_data = build_task_from_mismatch(
                        field_name=mismatch['field_name'],
                        record_value=mismatch['record_value'],
                        document_value=mismatch['document_value'],
                        notes=mismatch['notes'],
                        confidence=mismatch['confidence'],
                        dci_id=education_log_id,
                        application_id=parent_application_id,
                        record_type_name="Education",
                    )
                    await asyncio.to_thread(
                        sf_service.create_verification_task,
                        education_log_id,
                        task_data,
                        owner_id,
                    )
        except Exception as task_error:
            task_err_msg = f"Task creation failed for {education_log_id} (verification already saved): {task_error}"
            logger.error(task_err_msg)
            get_job_logger().add_detailed_record_log(
                record_type=f"Education_Tasks_{item_index or education_log_id[:8]}",
                doc_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "error"},
                status="warning",
                error=task_err_msg[:500],
            )

    except SalesforceAPIError as e:
        error_msg = str(e)
        job_logger = get_job_logger()
        # Ensure we have a valid record_type even if it failed early
        rt = f"Education_{education_log_id}"
        if 'record_data' in locals():
             rt = f"Education_{record_data.get('degreeLevel') or item_index or education_log_id}"
             
        job_logger.add_detailed_record_log(
            record_type=rt,
            doc_usage=_capture_usage(),
            crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "error"},
            status="failed",
            error=error_msg
        )
        if '500 Server Error' in error_msg:
            reason = "Data provider failed. This is likely due to a missing document or invalid data on the Salesforce record. Please verify the record and its attachments."
            raise ValueError(_format_error(education_log_id, "Salesforce Data", reason, str(e)))
        else:
            raise ValueError(_format_error(education_log_id, "Salesforce API", "An API error occurred", str(e)))
    except DocumentExtractionError as e:
        error_msg = str(e)
        job_logger = get_job_logger()
        rt = f"Education_{education_log_id}"
        if 'record_data' in locals():
             rt = f"Education_{record_data.get('degreeLevel') or item_index or education_log_id}"
        job_logger.add_detailed_record_log(
            record_type=rt,
            doc_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "failed"},
            crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
            status="failed",
            error=error_msg
        )
        raise ValueError(_format_error(education_log_id, "Document Extraction", "Failed to extract text from document", str(e)))
    except Exception as e:
        error_msg = str(e)
        job_logger = get_job_logger()
        rt = f"Education_{education_log_id}"
        if 'record_data' in locals():
             rt = f"Education_{record_data.get('degreeLevel') or item_index or education_log_id}"
        job_logger.add_detailed_record_log(
            record_type=rt,
            doc_usage=_capture_usage(),
            crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "error"},
            status="failed",
            error=error_msg
        )
        raise ValueError(_format_error(education_log_id, "Processing", "An unexpected error occurred", str(e)))
