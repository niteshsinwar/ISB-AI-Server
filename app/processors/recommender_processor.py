"""Recommender detail verification processor for ISB applications."""
import logging
import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.config import (
    MAX_SALESFORCE_REPORT_LENGTH,
    RECOMMENDER_DETAIL_OBJECT_API_NAME,
    APPLICATION_OBJECT_API_NAME,
)
from app.core.job_run_logger import get_job_logger
from app.langgraph.llm_utils import reset_global_usage, get_job_cost_summary
from app.services.document_extraction_service import DocumentExtractionError
from app.services.salesforce_service import SalesforceAPIError

if TYPE_CHECKING:
    from app.services.salesforce_service import SalesforceService

logger = logging.getLogger(__name__)

RECOMMENDER_DETAIL_OBJECT = RECOMMENDER_DETAIL_OBJECT_API_NAME
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
    extractor_instance=None,
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
            SELECT Id, First_Name__c, Last_Name__c, Email__c, MobilePhone__c, Status__c,
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
            FROM {APPLICATION_OBJECT_API_NAME}
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

            # 3b. Dynamically Extract Parent Name from Applicant's Identity Document using AI
            logger.info("Fetching applicant's identity document to parse parent name")
            parent_names = []
            
            try:
                # Use standard apex endpoint to get the Personal Detail doc (hed__Application__c)
                app_details = await asyncio.to_thread(
                    sf_service.get_record_detail_from_apex, application_id, APPLICATION_OBJECT_API_NAME
                )
                
                if app_details and app_details.get("documentPayload"):
                    doc_payload = app_details["documentPayload"]
                    base64_data = doc_payload.get("base64Data")
                    file_extension = doc_payload.get("fileExtension")
                    
                    if base64_data and file_extension:
                        logger.info(f"Extracting text from applicant document for parent name parsing")
                        from app.services.document_extraction_service import extract_text_from_file, create_text_extractor
                        
                        extractor = create_text_extractor()
                        doc_text = await extract_text_from_file(
                            base64_data,
                            file_extension,
                            record_id=application_id,
                            extractor=extractor,
                            record_type="application",
                            record_data={}
                        )
                        
                        if doc_text and doc_text.strip():
                            logger.info("Document text extracted successfully. Running AI zero-shot extraction for parent name.")
                            from app.langgraph.graph_utils import get_llm
                            
                            # Use complex reasoning model for better zero-shot extraction
                            llm = get_llm("gemini-2.5-flash", temperature=0.0)
                            prompt = f"""
You are an expert document parser. Read the following text extracted from an Indian Government ID (like Aadhaar, Passport, PAN, or Voter ID).
Extract the Father's or Mother's name (or Husband/Guardian's name) as written on the ID.

Return ONLY the full name of the parent/guardian. Do NOT include any prefixes like "S/O", "D/O", "W/O", "Father's Name:", etc.
If no parent/guardian name can be found in the text, return exactly the word "Unknown".

DOCUMENT TEXT:
{doc_text}
"""
                            response = llm.invoke(prompt)
                            extracted_name = (response.content if hasattr(response, 'content') else str(response)).strip()
                            
                            if extracted_name and extracted_name.lower() != "unknown" and len(extracted_name) > 2:
                                parent_names.append(extracted_name)
                                logger.info(f"AI Successfully extracted parent name from document: {extracted_name}")
                            else:
                                logger.info("AI could not find a parent name in the document text.")
                else:
                    logger.info("No identity document found attached to the applicant's record.")
                    
            except Exception as e:
                logger.warning(f"Failed to dynamically extract parent name from document: {e}")
                
            applicant_personal_detail['Parents_Name_From_Government_ID__c'] = ', '.join(parent_names)
            applicant_personal_detail['Parents_Name__c'] = ', '.join(parent_names)
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
