import logging
import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.config import EEDL_VS_RECORD_TYPE_ID_DOCUMENT, MAX_SALESFORCE_REPORT_LENGTH
from app.core.processing_utils import should_skip_processing
from app.core.job_run_logger import get_job_logger
from app.langgraph.llm_utils import reset_global_usage, get_job_cost_summary
from app.services.document_extraction_service import DocumentExtractionError
from app.services.salesforce_service import SalesforceAPIError

if TYPE_CHECKING:
    from app.services.salesforce_service import SalesforceService

logger = logging.getLogger(__name__)

_READABLE = "ID Document Verification"


def _capture_usage() -> Dict[str, Any]:
    summary = get_job_cost_summary()
    breakdown = summary.get("detailed_breakdown", [])
    model = breakdown[-1].get("model", "unknown") if breakdown else "unknown"
    totals = summary.get("totals", {})
    return {
        "input_tokens": totals.get("prompt_tokens", 0),
        "output_tokens": totals.get("completion_tokens", 0),
        "cost": totals.get("total_cost_usd", 0.0),
        "model": model,
    }


async def process_eedl_id_document(
    sf_service: "SalesforceService",
    opportunity_id: str,
    parent_opportunity_id: str,
    item_index: Optional[int] = None,
    extractor_instance=None,
):
    logger.info(f"Starting {_READABLE} processing for Opportunity: {opportunity_id}")

    from app.services.document_extraction_service import extract_text_from_file, create_text_extractor
    if extractor_instance is None:
        extractor_instance = create_text_extractor()

    try:
        details = await asyncio.to_thread(sf_service.get_eedl_id_document_data, opportunity_id)

        record_data = details.get("recordData", {})
        document_payload = details.get("documentPayload")
        salesforce_data_issue = details.get("Salesforce_data_issue_Summary")

        # Skip check — query existing EEDL_Verification_Summary__c
        existing_vs = await asyncio.to_thread(
            sf_service.get_existing_eedl_vs_metadata,
            opportunity_id,
            EEDL_VS_RECORD_TYPE_ID_DOCUMENT,
        )
        skip, reason = should_skip_processing(
            existing_avs=existing_vs,
            record_last_modified=record_data.get("LastModifiedDate"),
            document_last_modified=document_payload.get("lastModifiedDate") if document_payload else None,
        )
        if skip:
            logger.info(f"Skipping {_READABLE} {opportunity_id}: {reason}")
            job_logger = get_job_logger()
            job_logger.add_detailed_record_log(
                record_type="ID_Document",
                doc_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                status="skipped",
                error=reason,
            )
            return f"Skipped {_READABLE} — already 100% verified with no changes."

        # Fallback: no document or data issue
        if salesforce_data_issue or not document_payload:
            fallback_msg = salesforce_data_issue or "No ID document file found on Opportunity."
            logger.warning(f"{_READABLE} fallback for {opportunity_id}: {fallback_msg}")
            await asyncio.to_thread(
                sf_service.upsert_eedl_verification_summary,
                opportunity_id,
                EEDL_VS_RECORD_TYPE_ID_DOCUMENT,
                f"ID Document Analysis",
                overall_feedback=fallback_msg,
                confidence_range=0,
                verification_status="Failed",
            )
            job_logger = get_job_logger()
            job_logger.add_detailed_record_log(
                record_type="ID_Document",
                doc_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
                status="failed",
                error=fallback_msg,
            )
            return f"Processed {_READABLE} with data issue fallback."

        base64_data = document_payload.get("base64Data")
        file_extension = document_payload.get("fileExtension")
        if not base64_data or not file_extension:
            raise ValueError("Document content (base64) or file extension missing from ID document payload.")

        reset_global_usage()
        document_text = await extract_text_from_file(
            base64_data,
            file_extension,
            record_id=opportunity_id,
            extractor=extractor_instance,
            record_type="application",
            record_data=record_data,
        )
        doc_usage = _capture_usage()
        logger.info(f"Doc extraction usage for {opportunity_id}: {doc_usage}")

        reset_global_usage()
        from app.langgraph.eedl_citizenship_graph import CitizenshipGraphOrchestrator
        orchestrator = CitizenshipGraphOrchestrator(record_data, document_text)
        report_dict = await asyncio.to_thread(orchestrator.run)

        if not report_dict:
            raise ValueError("Citizenship graph did not return a valid report.")

        crew_usage = _capture_usage()
        logger.info(f"Graph processing usage for {opportunity_id}: {crew_usage}")

        job_logger = get_job_logger()
        job_logger.add_detailed_record_log(
            record_type="ID_Document",
            doc_usage=doc_usage,
            crew_usage=crew_usage,
            status="completed",
        )

        # Write citizenship value back to Opportunity if extracted
        suggested_citizenship = report_dict.get("suggested_citizenship_value")
        if suggested_citizenship:
            await asyncio.to_thread(
                sf_service.update_opportunity_citizenship,
                opportunity_id,
                suggested_citizenship,
            )
            logger.info(f"Updated Opportunity {opportunity_id} citizenship to '{suggested_citizenship}'")

        # Persist verification result
        await asyncio.to_thread(
            sf_service.upsert_eedl_verification_summary,
            opportunity_id,
            EEDL_VS_RECORD_TYPE_ID_DOCUMENT,
            "ID Document Analysis",
            report_content=report_dict.get("field_comparison_summary", "")[:MAX_SALESFORCE_REPORT_LENGTH],
            overall_feedback=report_dict.get("overall_feedback"),
            confidence_range=report_dict.get("confidence_range"),
            mismatched_field_list=report_dict.get("mismatched_field_list"),
            verification_status=report_dict.get("verification_status"),
        )

        logger.info(f"Successfully processed {_READABLE} for Opportunity {opportunity_id}.")
        return f"Successfully processed {_READABLE}."

    except SalesforceAPIError as e:
        job_logger = get_job_logger()
        job_logger.add_detailed_record_log(
            record_type="ID_Document",
            doc_usage=_capture_usage(),
            crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "error"},
            status="failed",
            error=str(e),
        )
        raise ValueError(f"(ID_Document)-({opportunity_id})-(Salesforce API)-(An API error occurred): {e}")
    except DocumentExtractionError as e:
        job_logger = get_job_logger()
        job_logger.add_detailed_record_log(
            record_type="ID_Document",
            doc_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "failed"},
            crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
            status="failed",
            error=str(e),
        )
        raise ValueError(f"(ID_Document)-({opportunity_id})-(Document Extraction)-(Failed to extract text): {e}")
    except Exception as e:
        job_logger = get_job_logger()
        job_logger.add_detailed_record_log(
            record_type="ID_Document",
            doc_usage=_capture_usage(),
            crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "error"},
            status="failed",
            error=str(e),
        )
        raise ValueError(f"(ID_Document)-({opportunity_id})-(Processing)-(An unexpected error occurred): {e}")
