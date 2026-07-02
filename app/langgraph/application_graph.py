import logging
import json
from typing import Dict, Any, List
from langgraph.graph import StateGraph, END

from app.config import (
    MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION,
    LLM_FIELD_EXCLUSIONS
)
from app.langgraph.graph_prompts import (
    APPLICATION_DATA_COMPARATOR_AGENT_GOAL,
    APPLICATION_DATA_COMPARATOR_AGENT_BACKSTORY,
    APPLICATION_DATA_COMPARISON_TASK_DESCRIPTION,
    APPLICATION_DATA_COMPARISON_EXPECTED_OUTPUT,
    APPLICATION_DOC_CLASSIFIER_GOAL,
    APPLICATION_DOC_CLASSIFICATION_TASK,
)
from app.langgraph.state import VerificationState
from app.langgraph.graph_utils import (
    get_llm, parse_json_from_response, format_record_for_llm, format_fields_for_llm
)
from app.langgraph.report_builder import build_verification_report, parse_comparison_json
from app.langgraph.schemas import ValidatedCrewReport

logger = logging.getLogger(__name__)

FIELDS_TO_EXCLUDE_FROM_PROCESSING: List[str] = LLM_FIELD_EXCLUSIONS

# Document type → allowed ID fields mapping
_ID_DOCUMENT_FIELD_MAP = {
    "PASSPORT": {
        "Passport", "Passport Number", "Passport_Number__c", "Passport Number__c",
        "Passport Expiry", "Passport_Expiry__c", "PassportExpiryDate", "Expiry Date",
        "Nationality", "Passport Details"
    },
    "AADHAAR": {
        "Aadhaar", "Aadhar", "Aadhaar Number", "Aadhar Number", "Aadhar Card Number",
        "Aadhaar Card Number", "Aadhar_Number__c", "Aadhaar_Number__c", "Adhaar",
        "Adhaar Number", "Adhaar Card Details", "Aadhar Card Details", "Adhaar_Number__c",
        "Adhaar_Card_Number__c"
    },
    "DRIVING_LICENSE": {
        "Driving License", "License Number", "License_Number__c", "Driving_License__c",
        "License Expiry", "License_Expiry__c"
    },
    "VOTER_ID": {
        "Voter ID", "Voter_ID__c", "Voter_Card__c"
    },
}

# Always include these fields regardless of document type
_UNIVERSAL_ID_FIELDS = {
    "Full Name", "fullName", "applicantName", "Applicant Name",
    "Birthdate", "Date of Birth", "DOB", "Gender"
}

_APPLICATION_CRITICAL_FIELDS = {
    "Full Name", "fullName", "applicantName", "Applicant Name",
    "Birthdate", "Date of Birth", "Aadhar", "Aadhaar",
    "Aadhar Card Number", "Aadhaar Card Number",
    "Passport", "Passport Number", "ID Number", "Gender",
    "Passport Expiry", "PassportExpiryDate", "Nationality",
}


def _filter_fields_by_document_type(verifiable_fields: List[str], doc_type: str) -> List[str]:
    """Filter fields based on detected ID document type."""
    if not doc_type:
        return verifiable_fields

    doc_type_upper = doc_type.upper()
    allowed_fields = _ID_DOCUMENT_FIELD_MAP.get(doc_type_upper, set())

    # Always include universal fields + document-specific fields
    allowed_all = _UNIVERSAL_ID_FIELDS | allowed_fields

    return [f for f in verifiable_fields if f in allowed_all]


class ApplicationGraphNodes:
    def __init__(self):
        self.llm_classifier = get_llm(MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION)
        self.llm_comparator = get_llm(MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION)

    # ------------------------------------------------------------------
    # NODE 1: Document Type Classifier
    # ------------------------------------------------------------------
    def classifier_node(self, state: VerificationState) -> Dict[str, Any]:
        """Identifies the type of ID document (Passport, Aadhaar, etc.)."""
        logger.info("Executing Application Document Classifier Node")

        prompt = f"""
{APPLICATION_DOC_CLASSIFIER_GOAL}

TASK:
{APPLICATION_DOC_CLASSIFICATION_TASK.format(document_text=state['document_text'])}
"""
        response = self.llm_classifier.invoke(prompt)

        try:
            result = parse_json_from_response(response.content)
            doc_type = result.get("document_type", "OTHER").upper()
            reasoning = result.get("reasoning", "")
            logger.info(f"Document classified as: {doc_type} — {reasoning}")
            return {
                "document_type": doc_type,
                "document_type_reasoning": reasoning,
            }
        except Exception as e:
            logger.warning(f"Document classifier failed to parse response, defaulting to OTHER: {e}")
            return {"document_type": "OTHER", "document_type_reasoning": "Classification failed; proceeding with all available fields."}

    # ------------------------------------------------------------------
    # NODE 2: Comparator with field filtering
    # ------------------------------------------------------------------
    def comparator_node(self, state: VerificationState) -> Dict[str, Any]:
        """Uses LLM-friendly plain text formatting for efficient token usage."""
        logger.info("Executing Application Comparator Node")

        # Filter fields based on detected ID document type
        filtered_fields = _filter_fields_by_document_type(
            state['verifiable_fields'],
            state.get('document_type')
        )

        if len(filtered_fields) < len(state['verifiable_fields']):
            excluded = set(state['verifiable_fields']) - set(filtered_fields)
            logger.info(f"Excluded {len(excluded)} fields based on {state.get('document_type')} document type: {excluded}")

        # Format data as LLM-friendly plain text (not JSON)
        formatted_fields = format_fields_for_llm(filtered_fields)
        formatted_record = format_record_for_llm(state['record_data'], filtered_fields)

        prompt = f"""
{APPLICATION_DATA_COMPARATOR_AGENT_GOAL}
{APPLICATION_DATA_COMPARATOR_AGENT_BACKSTORY}

TASK:
{APPLICATION_DATA_COMPARISON_TASK_DESCRIPTION.format(
    verifiable_fields=formatted_fields,
    record_data=formatted_record,
    document_text=state['document_text']
)}

EXPECTED OUTPUT:
{APPLICATION_DATA_COMPARISON_EXPECTED_OUTPUT}
"""
        comparisons = None
        for attempt in range(2):
            response = self.llm_comparator.invoke(prompt)
            try:
                comparisons = parse_comparison_json(response.content)
                break
            except ValueError:
                if attempt == 1:
                    raise
                logger.warning("Application comparator returned malformed JSON; retrying once")
                prompt += (
                    "\n\nRETRY REQUIREMENT: Your previous response was not valid JSON. "
                    "Return only the required JSON object with `verification_analysis_report` and no surrounding prose."
                )

        return {
            "comparison_task_output": json.dumps(
                {"verification_analysis_report": comparisons},
                ensure_ascii=False,
            )
        }

    def reporter_node(self, state: VerificationState) -> Dict[str, Any]:
        logger.info("Executing Application Reporter Node")

        try:
            comparisons = parse_comparison_json(state['comparison_task_output'])

            # Recalculate filtered fields so hallucinated irrelevant ID fields are dropped.
            # If verifiable_fields is unavailable, skip filtering rather than crash.
            verifiable_fields = state.get('verifiable_fields')
            filtered_fields = _filter_fields_by_document_type(
                verifiable_fields,
                state.get('document_type')
            ) if verifiable_fields else None

            final_json = build_verification_report(
                comparisons,
                critical_field_names=_APPLICATION_CRITICAL_FIELDS,
                allowed_fields=filtered_fields,
            )
            validated = ValidatedCrewReport(**final_json)
            return {"final_report": validated.model_dump()}
        except Exception as e:
            logger.error(f"Error in Application Reporter Node: {e}")
            raise

def build_application_graph():
    nodes = ApplicationGraphNodes()
    workflow = StateGraph(VerificationState)

    workflow.add_node("classifier", nodes.classifier_node)
    workflow.add_node("comparator", nodes.comparator_node)
    workflow.add_node("reporter", nodes.reporter_node)

    workflow.set_entry_point("classifier")
    workflow.add_edge("classifier", "comparator")
    workflow.add_edge("comparator", "reporter")
    workflow.add_edge("reporter", END)

    return workflow.compile()

class ApplicationGraphOrchestrator:
    def __init__(self, record_data: Dict[str, Any], document_text: str):
        self.record_data = record_data
        self.document_text = document_text
        self.app = build_application_graph()

    def run(self) -> Dict[str, Any]:
        verifiable_fields = [
            f for f in self.record_data.keys() 
            if f not in FIELDS_TO_EXCLUDE_FROM_PROCESSING
        ]
        
        initial_state = VerificationState(
            record_data=self.record_data,
            document_text=self.document_text,
            verifiable_fields=verifiable_fields,
            application_submission_date=None,
            document_type=None,
            document_type_reasoning=None,
            comparison_task_output=None,
            final_report=None,
            usage_metrics={},
            model_config={
                "comparator_model": MODEL_STANDARD_VERIFICATION,
                "reporter_model": "deterministic-python"
            }
        )
        
        result_state = self.app.invoke(initial_state)
        report_data = result_state.get("final_report")
        if not report_data:
            raise ValueError("Application Graph failed to produce final report.")
            
        # Replicate Usage Metrics
        from app.langgraph.llm_utils import _GLOBAL_TOKEN_USAGE
        usage_metrics = {
            "total_tokens": _GLOBAL_TOKEN_USAGE["total_tokens"],
            "prompt_tokens": _GLOBAL_TOKEN_USAGE["prompt_tokens"],
            "completion_tokens": _GLOBAL_TOKEN_USAGE["completion_tokens"],
            "successful_requests": _GLOBAL_TOKEN_USAGE["successful_requests"],
            "total_cost_usd": _GLOBAL_TOKEN_USAGE["total_cost_usd"],
            "source": "LangGraph",
            "model_config": result_state["model_config"]
        }
        
        return {
            **report_data, 
            "usage_metrics": usage_metrics
        }
