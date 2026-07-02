# project_root/app/processors/application_processor.py
import logging
import os
import json
import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.config import (
    APPLICATION_OBJECT_API_NAME,
    APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP,
    MAX_SALESFORCE_REPORT_LENGTH,
    READABLE_OBJECT_NAMES
)
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
    category = READABLE_OBJECT_NAMES[APPLICATION_OBJECT_API_NAME]
    return f"({category})-({record_id})-({component})-({reason}): {technical_error}"

async def process_single_application_detail(
    sf_service: "SalesforceService",
    application_id: str,
    parent_application_id: str,
    application_object_api_name: str,
    item_index: Optional[int] = None,
    extractor_instance: "FastDocumentExtractor" = None,
) -> str:
    """
    Processes the main Application record (Personal Detail) and its associated ID document.
    """
    readable_name = READABLE_OBJECT_NAMES.get(application_object_api_name, "Application")
    logger.info(f"Starting {readable_name} processing for ID: {application_id}")

    from app.services.document_extraction_service import extract_text_from_file, create_text_extractor
    # Use provided extractor or create a per-job extractor
    if extractor_instance is None:
        extractor_instance = create_text_extractor()

    try:
        details = await asyncio.to_thread(
            sf_service.get_record_detail_from_apex, application_id, application_object_api_name
        )
        # Note: With reverted Apex, `details` will be None on error. The exception is now the primary signal.
        
        record_data = details.get("recordData", {})
        document_payload = details.get("documentPayload")
        salesforce_data_issue = details.get("Salesforce_data_issue_Summary")
        contact_id = record_data.get(APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP)

        # Cost optimization: Check if record should be skipped (100% verified, no changes)
        if contact_id:
            existing_avs = await asyncio.to_thread(
                sf_service.get_existing_avs_metadata,
                application_id=parent_application_id,
                contact_id=contact_id
            )
            skip, reason = should_skip_processing(
                existing_avs=existing_avs,
                record_last_modified=record_data.get("LastModifiedDate"),
                document_last_modified=document_payload.get("lastModifiedDate") if document_payload else None  # Apex returns lowercase 'l'
            )
            if skip:
                logger.info(f"Skipping {readable_name} {application_id}: {reason}")
                
                # Log skipped status
                job_logger = get_job_logger()
                job_logger.add_detailed_record_log(
                    record_type="Personal_Detail",
                    doc_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                    crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                    status="skipped",
                    error=reason
                )
                return f"Skipped {readable_name} - already 100% verified with no changes."

        # Check for graceful fallback scenario (from top level or record data)
        fallback_summary = salesforce_data_issue or record_data.get("Salesforce_data_issue_Summary")
        if fallback_summary:
            logger.warning(f"Salesforce data issue detected for {application_id}: {fallback_summary}")
            summary_name = "Personal Detail Analysis"
            summary_record_id = await asyncio.to_thread(
                sf_service.upsert_verification_summary,
                application_id=parent_application_id,
                report_content=None,
                name_value=summary_name,
                overall_feedback=fallback_summary,
                confidence_range="0",
                mismatched_field_list=None,
                contact_id=contact_id
            )
            job_logger = get_job_logger()
            job_logger.add_detailed_record_log(
                record_type="Personal_Detail",
                doc_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                status="failed",
                error=fallback_summary
            )
            logger.info(f"Created fallback AVS for {readable_name} {application_id}. AVS ID: {summary_record_id}")
            return f"Processed {readable_name} with data issue fallback."

        if not document_payload:
            raise ValueError("Document payload was missing from the Salesforce record.")
        if not contact_id:
            raise ValueError("Contact ID is missing from the application data.")

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
            record_id=application_id,
            extractor=extractor_instance,
            record_type="application",
            record_data=record_data
        )

        if not document_text_string or not document_text_string.strip():
            logger.warning(f"No text extracted from document for {readable_name} {application_id}.")
            fallback_summary = "Uploaded document contains no readable text or is missing."
            summary_name = "Personal Detail Analysis"
            summary_record_id = await asyncio.to_thread(
                sf_service.upsert_verification_summary,
                application_id=parent_application_id,
                report_content=None,
                name_value=summary_name,
                overall_feedback=fallback_summary,
                confidence_range="0",
                mismatched_field_list=None,
                contact_id=contact_id
            )
            job_logger = get_job_logger()
            job_logger.add_detailed_record_log(
                record_type="Personal_Detail",
                doc_usage=_capture_usage(),
                crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                status="failed",
                error=fallback_summary
            )
            return f"Processed {readable_name} - Missing Document (0% Confidence)."

        # Capture doc extraction usage
        doc_usage = _capture_usage()
        logger.info(f"Doc extraction usage for {application_id}: {doc_usage}")

        # Reset usage before crew processing
        reset_global_usage()

        # LangGraph Execution
        logger.info(f"Using LangGraph for {readable_name} {application_id}")
        from app.langgraph.application_graph import ApplicationGraphOrchestrator
        app_orchestrator = ApplicationGraphOrchestrator(record_data, document_text_string)
        report_dict = await asyncio.to_thread(app_orchestrator.run)

        if not report_dict:
            raise ValueError("Graph execution did not return a valid report.")

        # Capture processing usage
        crew_usage = _capture_usage()
        logger.info(f"Graph processing usage for {application_id}: {crew_usage}")

        # Log detailed usage to job logger
        job_logger = get_job_logger()
        job_logger.add_detailed_record_log(
            record_type="Personal_Detail",
            doc_usage=doc_usage,
            crew_usage=crew_usage,
            status="completed"
        )

        summary_name = "Personal Detail Analysis"
        summary_record_id = await asyncio.to_thread(
            sf_service.upsert_verification_summary,
            application_id=parent_application_id,
            report_content=report_dict.get('field_comparison_summary', '')[:MAX_SALESFORCE_REPORT_LENGTH],
            name_value=summary_name,
            overall_feedback=report_dict.get('overall_feedback'),
            confidence_range=report_dict.get('confidence_range'),
            mismatched_field_list=report_dict.get('mismatched_field_list'),
            contact_id=contact_id
        )
        
        logger.info(f"Successfully processed {readable_name} {application_id}. AVS ID: {summary_record_id}")
        return f"Successfully processed {readable_name} details."

    # --- MODIFIED: "Smart" Error Handling Logic ---
    except SalesforceAPIError as e:
        error_msg = str(e)
        job_logger = get_job_logger()
        job_logger.add_detailed_record_log(
            record_type="Personal_Detail",
            doc_usage=_capture_usage(), # Capture partial usage if any
            crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "error"},
            status="failed",
            error=error_msg
        )
        # If we get a generic 500 error, provide a more helpful message.
        if '500 Server Error' in error_msg:
            reason = "Data provider failed. This is likely due to a missing document or invalid data on the Salesforce record. Please verify the record and its attachments."
            raise ValueError(_format_error(application_id, "Salesforce Data", reason, error_msg))
        else:
            # For other specific SF API errors, report them directly.
            raise ValueError(_format_error(application_id, "Salesforce API", "An API error occurred", error_msg))
    except DocumentExtractionError as e:
        error_msg = str(e)
        job_logger = get_job_logger()
        job_logger.add_detailed_record_log(
            record_type="Personal_Detail",
            doc_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "failed"},
            crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
            status="failed",
            error=error_msg
        )
        raise ValueError(_format_error(application_id, "Document Extraction", "Failed to extract text from document", error_msg))
    except Exception as e:
        error_msg = str(e)
        job_logger = get_job_logger()
        job_logger.add_detailed_record_log(
            record_type="Personal_Detail",
            doc_usage=_capture_usage(),
            crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "error"},
            status="failed",
            error=error_msg
        )
        # Catch-all for other errors (e.g., Crew, local data validation)
        raise ValueError(_format_error(application_id, "Processing", "An unexpected error occurred", error_msg))
