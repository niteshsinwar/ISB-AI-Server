# project_root/app/processors/resume_processor.py
import logging
import os
import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.config import DCI_OBJECT_API_NAME, READABLE_OBJECT_NAMES
from app.core.processing_utils import should_skip_processing
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
    category = READABLE_OBJECT_NAMES[DCI_OBJECT_API_NAME]
    return f"({category})-({record_id})-({component})-({reason}): {technical_error}"

async def process_single_resume_detail(
    sf_service: "SalesforceService",
    resume_dci_id: str,
    parent_application_id: str,
    extractor_instance: "FastDocumentExtractor" = None,
    **kwargs
) -> str:
    readable_name = READABLE_OBJECT_NAMES.get(DCI_OBJECT_API_NAME, "Resume Detail")
    logger.info(f"Starting {readable_name} processing for DCI ID: {resume_dci_id}")

    from app.services.document_extraction_service import extract_text_from_file, create_text_extractor
    # Use provided extractor or create a per-job extractor
    if extractor_instance is None:
        extractor_instance = create_text_extractor()

    try:
        details = await asyncio.to_thread(sf_service.get_dci_document_data, resume_dci_id)

        document_payload = details.get("documentPayload")

        # Cost optimization: Check if record should be skipped (100% verified, no changes)
        existing_avs = await asyncio.to_thread(
            sf_service.get_existing_avs_metadata,
            application_id=parent_application_id,
            name_value="Resume Detail Analysis"
        )
        skip, reason = should_skip_processing(
            existing_avs=existing_avs,
            record_last_modified=details.get("LastModifiedDate"),
            document_last_modified=document_payload.get("LastModifiedDate") if document_payload else None
        )
        if skip:
            logger.info(f"Skipping {readable_name} {resume_dci_id}: {reason}")

            # Log skipped status
            job_logger = get_job_logger()
            job_logger.add_detailed_record_log(
                record_type="Resume_Detail",
                doc_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                status="skipped",
                error=reason
            )
            return f"Skipped {readable_name} - already 100% verified with no changes."

        if not document_payload:
            raise ValueError("No attached document found for this resume DCI record.")

        file_extension = document_payload.get("fileExtension")
        base64_data = document_payload.get("base64Data")

        if not base64_data or not file_extension:
            raise ValueError("Document content (base64) or file extension missing.")

        # Reset usage before doc extraction
        reset_global_usage()

        document_text_string = await extract_text_from_file(base64_data, file_extension, record_id=resume_dci_id, extractor=extractor_instance)

        # Capture doc extraction usage
        doc_usage = _capture_usage()
        logger.info(f"Doc extraction usage for {resume_dci_id}: {doc_usage}")

        # Reset usage before crew processing
        reset_global_usage()

        # LangGraph Execution
        logger.info(f"Using LangGraph for {readable_name} {resume_dci_id}")
        from app.langgraph.resume_graph import ResumeGraphOrchestrator
        resume_orchestrator = ResumeGraphOrchestrator(document_text=document_text_string)
        report_dict = await asyncio.to_thread(resume_orchestrator.run)

        if not report_dict:
            raise ValueError("Graph execution did not return a valid report.")

        # Capture processing usage
        crew_usage = _capture_usage()
        logger.info(f"Graph processing usage for {resume_dci_id}: {crew_usage}")

        # Log detailed usage to job logger
        job_logger = get_job_logger()
        job_logger.add_detailed_record_log(
            record_type="Resume_Detail",
            doc_usage=doc_usage,
            crew_usage=crew_usage
        )

        status = report_dict.get("status")
        reason = report_dict.get("reason")
        confidence = 100 if status == "Accepted" else 30
        feedback = "All good" if status == "Accepted" else (reason or "PII or other issues detected.")

        summary_record_id = await asyncio.to_thread(
            sf_service.upsert_verification_summary,
            application_id=parent_application_id,
            report_content="",
            name_value="Resume Detail Analysis",
            overall_feedback=feedback,
            confidence_range=confidence
        )

        await asyncio.to_thread(
            sf_service.link_summary_to_related_items,
            summary_id=summary_record_id,
            task_id=None,
            dci_id=resume_dci_id
        )

        # Touch AVS to ensure its LastModifiedDate > DCI's LastModifiedDate
        # This is critical for skip logic to work correctly on subsequent runs
        await asyncio.to_thread(sf_service.touch_verification_summary, summary_record_id)

        logger.info(f"Successfully processed {readable_name} {resume_dci_id}. AVS ID: {summary_record_id}")
        return f"Successfully processed resume {resume_dci_id}."

    except SalesforceAPIError as e:
        error_msg = str(e)
        job_logger = get_job_logger()
        # Check if this is a missing document error (no ContentDocumentLink found)
        if "Could not find any ContentDocumentLink" in error_msg:
            logger.warning(f"No document attached to resume DCI {resume_dci_id}, creating fallback AVS")
            summary_record_id = await asyncio.to_thread(
                sf_service.upsert_verification_summary,
                application_id=parent_application_id,
                report_content=None,
                name_value="Resume Detail Analysis",
                overall_feedback="No resume document was attached to this record.",
                confidence_range="0",
                mismatched_field_list=None
            )
            job_logger.add_detailed_record_log(
                record_type="Resume_Detail",
                doc_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                status="failed",
                error="No document attached to this record"
            )
            logger.info(f"Created fallback AVS for resume {resume_dci_id}. AVS ID: {summary_record_id}")
            return f"Processed {readable_name} with no document fallback."
        elif '500 Server Error' in error_msg:
            job_logger.add_detailed_record_log(
                record_type="Resume_Detail",
                doc_usage=_capture_usage(),
                crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "error"},
                status="failed",
                error=error_msg
            )
            reason = "Data provider failed. This is likely due to a missing document or invalid data on the Salesforce record. Please verify the record and its attachments."
            raise ValueError(_format_error(resume_dci_id, "Salesforce Data", reason, error_msg))
        else:
            job_logger.add_detailed_record_log(
                record_type="Resume_Detail",
                doc_usage=_capture_usage(),
                crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "error"},
                status="failed",
                error=error_msg
            )
            raise ValueError(_format_error(resume_dci_id, "Salesforce API", "An API error occurred", error_msg))
    except DocumentExtractionError as e:
        error_msg = str(e)
        job_logger = get_job_logger()
        job_logger.add_detailed_record_log(
            record_type="Resume_Detail",
            doc_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "failed"},
            crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
            status="failed",
            error=error_msg
        )
        raise ValueError(_format_error(resume_dci_id, "Document Extraction", "Failed to extract text from document", error_msg))
    except Exception as e:
        error_msg = str(e)
        job_logger = get_job_logger()
        job_logger.add_detailed_record_log(
            record_type="Resume_Detail",
            doc_usage=_capture_usage(),
            crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "error"},
            status="failed",
            error=error_msg
        )
        raise ValueError(_format_error(resume_dci_id, "Processing", "An unexpected error occurred", error_msg))
