"""Recommender detail verification processor for ISB applications."""
import logging
import os
import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.config import (
    READABLE_OBJECT_NAMES,
    MAX_SALESFORCE_REPORT_LENGTH,
)
from app.core.processing_utils import should_skip_processing
from app.core.job_run_logger import get_job_logger
from app.langgraph.llm_utils import reset_global_usage, get_job_cost_summary
from app.services.document_extraction_service import DocumentExtractionError
from app.services.salesforce_service import SalesforceAPIError

if TYPE_CHECKING:
    from app.services.salesforce_service import SalesforceService

logger = logging.getLogger(__name__)

RECOMMENDER_DETAIL_OBJECT = "ISB_Recommender_Details__c"
READABLE_NAME = "Recommender Detail"


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
    return f"({READABLE_NAME})-({record_id})-({component})-({reason}): {technical_error}"


async def process_single_recommender_detail(
    sf_service: "SalesforceService",
    recommender_detail_id: str,
    application_id: str,
    item_index: Optional[int] = None,
):
    """
    Process recommender detail verification.

    Steps:
    1. Fetch recommender detail record
    2. Fetch recommender responses (ISB_Recommender_Response__c)
    3. Fetch applicant personal details (for parents name from govt ID)
    4. Run LangGraph verification
    5. Upsert Application_Verification_Summary__c
    """

    logger.info(f"Starting {READABLE_NAME} processing for ID: {recommender_detail_id}")

    try:
        # ============================================================================
        # STEP 1: Fetch Recommender Detail from Salesforce
        # ============================================================================
        logger.info(f"Fetching recommender detail: {recommender_detail_id}")

        recommender_data = await asyncio.to_thread(
            sf_service.sf.query,
            f"""
            SELECT Id, First_Name__c, Last_Name__c, Email__c, Mobile__c, Status__c,
                   Application__c, Relationship_Type__c, Other_Relationship__c
            FROM {RECOMMENDER_DETAIL_OBJECT}
            WHERE Id = '{recommender_detail_id}'
            LIMIT 1
            """
        )

        if not recommender_data.get('records'):
            raise ValueError(f"Recommender detail not found: {recommender_detail_id}")

        recommender_record = recommender_data['records'][0]
        logger.info(f"Recommender detail fetched: {recommender_record.get('Name', recommender_detail_id)}")

        # ============================================================================
        # STEP 2: Fetch Recommender Responses
        # ============================================================================
        logger.info(f"Fetching recommendation responses")

        responses_data = await asyncio.to_thread(
            sf_service.sf.query,
            f"""
            SELECT Id, Question__c, Section_Name__c, Answer__c, Score__c
            FROM ISB_Recommender_Response__c
            WHERE ISB_Recommender_Details__c = '{recommender_detail_id}'
            """
        )

        responses = responses_data.get('records', [])
        logger.info(f"Found {len(responses)} responses for recommender")

        # ============================================================================
        # STEP 3: Fetch Applicant Details (name from Application/Contact, parents from ISB_Relationships)
        # ============================================================================
        logger.info(f"Fetching applicant details for name matching and family detection")

        applicant_personal_detail = {}

        # 3a. Get applicant name from Application → Contact
        app_data = await asyncio.to_thread(
            sf_service.sf.query,
            f"""
            SELECT Id, hed__Applicant__c, hed__Applicant__r.FirstName, hed__Applicant__r.LastName,
                   hed__Applicant__r.Email, hed__Applicant__r.MobilePhone
            FROM hed__Application__c
            WHERE Id = '{application_id}'
            LIMIT 1
            """
        )

        if app_data.get('records'):
            app_record = app_data['records'][0]
            applicant_contact = app_record.get('hed__Applicant__r') or {}
            applicant_personal_detail['First_Name__c'] = applicant_contact.get('FirstName', '')
            applicant_personal_detail['Last_Name__c'] = applicant_contact.get('LastName', '')
            applicant_personal_detail['Email'] = applicant_contact.get('Email', '')
            applicant_personal_detail['MobilePhone'] = applicant_contact.get('MobilePhone', '')
            contact_id = app_record.get('hed__Applicant__c')
            logger.info(f"Applicant: {applicant_personal_detail.get('First_Name__c')} {applicant_personal_detail.get('Last_Name__c')}")

            # 3b. Get parents name from ISB_Relationships__c
            if contact_id:
                parents_data = await asyncio.to_thread(
                    sf_service.sf.query,
                    f"""
                    SELECT Name_of_the_Person__c, Type__c
                    FROM ISB_Relationships__c
                    WHERE Contact__c = '{contact_id}'
                      AND Type__c IN ('Father', 'Mother', 'Parent', 'Guardian')
                    """
                )
                parent_names = []
                for rel in parents_data.get('records', []):
                    name = rel.get('Name_of_the_Person__c', '')
                    if name:
                        parent_names.append(name)
                applicant_personal_detail['Parents_Name_From_Government_ID__c'] = ', '.join(parent_names)
                applicant_personal_detail['Parents_Name__c'] = ', '.join(parent_names)
                if parent_names:
                    logger.info(f"Parents found: {', '.join(parent_names)}")
                else:
                    logger.info("No parent relationship records found for applicant")
        else:
            logger.warning(f"Application record not found: {application_id}")
            applicant_personal_detail = None

        # ============================================================================
        # STEP 4: Reset usage and run LangGraph verification
        # ============================================================================
        reset_global_usage()

        logger.info(f"Running LangGraph verification for {READABLE_NAME}")
        from app.langgraph.recommender_graph import RecommenderGraphOrchestrator

        orchestrator = RecommenderGraphOrchestrator(
            recommender_record=recommender_record,
            responses=responses,
            applicant_personal_detail=applicant_personal_detail
        )
        report_dict = await asyncio.to_thread(orchestrator.run)

        if not report_dict:
            raise ValueError("Graph execution did not return a valid report.")

        logger.info(f"Verification complete for {recommender_detail_id}")

        # Capture processing usage
        crew_usage = _capture_usage()
        logger.info(f"Graph processing usage for {recommender_detail_id}: {crew_usage}")

        # ============================================================================
        # STEP 5: Log to job logger
        # ============================================================================
        job_logger = get_job_logger()
        job_logger.add_detailed_record_log(
            record_type=f"Recommender_{item_index or recommender_detail_id[:8]}",
            doc_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
            crew_usage=crew_usage,
            status="completed"
        )

        # ============================================================================
        # STEP 6: Upsert Verification Summary
        # ============================================================================
        summary_name = f"Recommender Analysis ({item_index})" if item_index else "Recommender Analysis"

        # NOTE: Do NOT pass affiliation_id here. The AVS field Affiliation__c is a lookup
        # to hed__Affiliation__c only. Passing an ISB_Recommender_Details__c ID causes
        # "id value of incorrect type" errors because Salesforce validates the ID prefix.
        summary_id = await asyncio.to_thread(
            sf_service.upsert_verification_summary,
            application_id=application_id,
            report_content=report_dict.get('field_comparison_summary', '')[:MAX_SALESFORCE_REPORT_LENGTH],
            name_value=summary_name,
            overall_feedback=report_dict.get('overall_feedback'),
            confidence_range=report_dict.get('confidence_range'),
            mismatched_field_list=report_dict.get('mismatched_field_list'),
        )

        logger.info(f"Successfully processed {READABLE_NAME} {recommender_detail_id}. AVS ID: {summary_id}")
        return f"Processed {READABLE_NAME} successfully."

    except SalesforceAPIError as e:
        error_msg = str(e)
        job_logger = get_job_logger()
        job_logger.add_detailed_record_log(
            record_type=f"Recommender_{item_index or recommender_detail_id[:8]}" if 'item_index' in locals() else f"Recommender_{recommender_detail_id[:8]}",
            doc_usage=_capture_usage(),
            crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "error"},
            status="failed",
            error=error_msg
        )
        raise ValueError(_format_error(recommender_detail_id, "Salesforce API", "An API error occurred", str(e)))

    except DocumentExtractionError as e:
        error_msg = str(e)
        job_logger = get_job_logger()
        job_logger.add_detailed_record_log(
            record_type=f"Recommender_{item_index or recommender_detail_id[:8]}" if 'item_index' in locals() else f"Recommender_{recommender_detail_id[:8]}",
            doc_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "failed"},
            crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "skipped"},
            status="failed",
            error=error_msg
        )
        raise ValueError(_format_error(recommender_detail_id, "Document Extraction", "Failed to extract text from document", str(e)))

    except Exception as e:
        error_msg = str(e)
        job_logger = get_job_logger()
        job_logger.add_detailed_record_log(
            record_type=f"Recommender_{item_index or recommender_detail_id[:8]}" if 'item_index' in locals() else f"Recommender_{recommender_detail_id[:8]}",
            doc_usage=_capture_usage(),
            crew_usage={"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "model": "error"},
            status="failed",
            error=error_msg
        )
        raise ValueError(_format_error(recommender_detail_id, "Processing", "An unexpected error occurred", str(e)))
