import logging
import json
from typing import Dict, Any, List, Literal
from langgraph.graph import StateGraph, END

from app.config import (
    MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION,
    LLM_FIELD_EXCLUSIONS
)
from app.langgraph.graph_prompts import (
    EMPLOYMENT_DATA_COMPARATOR_AGENT_GOAL,
    EMPLOYMENT_DATA_COMPARATOR_AGENT_BACKSTORY,
    EMPLOYMENT_DATA_COMPARISON_TASK_DESCRIPTION,
    EMPLOYMENT_DATA_COMPARISON_EXPECTED_OUTPUT,
    EMPLOYMENT_DOC_CLASSIFIER_GOAL,
    EMPLOYMENT_DOC_CLASSIFICATION_TASK,
)
from app.langgraph.state import VerificationState
from app.langgraph.graph_utils import (
    get_llm, parse_json_from_response,
    format_record_for_llm, format_fields_for_llm
)
from app.langgraph.report_builder import build_verification_report, parse_comparison_json
from app.langgraph.schemas import ValidatedCrewReport
logger = logging.getLogger(__name__)

FIELDS_TO_EXCLUDE_FROM_PROCESSING: List[str] = LLM_FIELD_EXCLUSIONS

_EMPLOYMENT_CRITICAL_FIELDS = {
    "applicantName",
    "Employee Name",
    "Company Name",
    "Employer Name",
    "Organization",
    "startDate",
    "Start Date",
    "endDate",
    "End Date",
    "Compensation",
    "Salary",
    "CTC",
    "Payslip Recency",
    "Payslip",
}

# Prompt-mandated synthetic rows that are not record fields but must survive
# the allowed-fields safety net in the reporter.
_EMPLOYMENT_SYNTHETIC_ROWS = {"Payslip Recency", "Payslip"}


def _route_after_classification(state: VerificationState) -> Literal["bank_statement_reporter", "comparator"]:
    """Conditional edge: route bank statements away from the normal comparator."""
    doc_type = state.get("document_type", "")
    if doc_type in ["BANK_STATEMENT", "BANK STATEMENT"]:
        return "bank_statement_reporter"
    return "comparator"


class EmploymentGraphNodes:
    def __init__(self):
        self.llm_classifier = get_llm(MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION)
        self.llm_comparator = get_llm(MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION)

    # ------------------------------------------------------------------
    # NODE 1: Document Type Classifier
    # ------------------------------------------------------------------
    def classifier_node(self, state: VerificationState) -> Dict[str, Any]:
        """Identifies the type of employment document before any field comparison."""
        logger.info("Executing Employment Document Classifier Node")

        prompt = f"""
{EMPLOYMENT_DOC_CLASSIFIER_GOAL}

TASK:
{EMPLOYMENT_DOC_CLASSIFICATION_TASK.format(document_text=state['document_text'])}
"""
        response = self.llm_classifier.invoke(prompt)

        try:
            result = parse_json_from_response(response.content)
            doc_type = result.get("document_type", "OTHER").upper()
            reasoning = result.get("reasoning", "")
            confidence = result.get("confidence", 0)
            logger.info(f"Document classified as: {doc_type} (confidence={confidence}%) — {reasoning}")
            return {
                "document_type": doc_type,
                "document_type_reasoning": reasoning,
            }
        except Exception as e:
            logger.warning(f"Document classifier failed to parse response, defaulting to OTHER: {e}")
            return {"document_type": "OTHER", "document_type_reasoning": "Classification failed; proceeding with normal verification."}

    # ------------------------------------------------------------------
    # NODE 2a: Bank Statement Reporter (no LLM — deterministic flag)
    # ------------------------------------------------------------------
    def bank_statement_reporter_node(self, state: VerificationState) -> Dict[str, Any]:
        """Directly produces a flagged report when the submitted doc is a bank statement."""
        logger.info("Executing Bank Statement Reporter Node — document type mismatch detected")

        reasoning = state.get("document_type_reasoning") or "Bank statement indicators detected in the document."

        report_dict = build_verification_report(
            [
                {
                    "field_name": "Payslip",
                    "record_value": "Required",
                    "document_value": "Bank Statement",
                    "status": "MISMATCH",
                    "confidence": 0,
                    "notes": f"Bank statement submitted instead of a Payslip. Rejected without further parameter checks. Reason: {reasoning}",
                    "_is_critical": True,
                }
            ]
        )
        report_dict["overall_feedback"] = (
            "Document type mismatch: A Bank Statement was submitted instead of a Payslip. "
            "Verification rejected as bank statements cannot verify employer name, job title, "
            "or employment dates. "
            f"Classifier reasoning: {reasoning}"
        )
        report_dict["confidence_range"] = 0
        report_dict["overall_percentage_confidence"] = 0
        report_dict["verification_status"] = "Failed"
        report = ValidatedCrewReport(**report_dict)
        return {"final_report": report.model_dump()}

    # ------------------------------------------------------------------
    # NODE 2b: Comparator (normal path)
    # ------------------------------------------------------------------
    def comparator_node(self, state: VerificationState) -> Dict[str, Any]:
        """Uses LLM-friendly plain text formatting for efficient token usage."""
        logger.info("Executing Employment Comparator Node")

        formatted_fields = format_fields_for_llm(state['verifiable_fields'])
        formatted_record = format_record_for_llm(state['record_data'], state['verifiable_fields'])

        # Add application submission date to the prompt if available
        app_submission_context = ""
        if state.get("application_submission_date"):
            app_submission_context = f"\n**Application Submission Date**: {state['application_submission_date']}\n"

        prompt = f"""
{EMPLOYMENT_DATA_COMPARATOR_AGENT_GOAL}
{EMPLOYMENT_DATA_COMPARATOR_AGENT_BACKSTORY}

TASK:
{EMPLOYMENT_DATA_COMPARISON_TASK_DESCRIPTION.format(
    verifiable_fields=formatted_fields,
    record_data=formatted_record,
    document_text=state['document_text']
)}{app_submission_context}
EXPECTED OUTPUT:
{EMPLOYMENT_DATA_COMPARISON_EXPECTED_OUTPUT}
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
                logger.warning("Employment comparator returned malformed JSON; retrying once")
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

    # ------------------------------------------------------------------
    # NODE 3: Reporter (normal path)
    # ------------------------------------------------------------------
    def reporter_node(self, state: VerificationState) -> Dict[str, Any]:
        logger.info("Executing Employment Reporter Node")

        try:
            comparisons = parse_comparison_json(state['comparison_task_output'])
            final_json = build_verification_report(
                comparisons,
                critical_field_names=_EMPLOYMENT_CRITICAL_FIELDS,
                allowed_fields=state.get('verifiable_fields'),
                extra_allowed_fields=_EMPLOYMENT_SYNTHETIC_ROWS,
            )
            validated = ValidatedCrewReport(**final_json)
            return {"final_report": validated.model_dump()}
        except Exception as e:
            logger.error(f"Error in Employment Reporter Node: {e}")
            raise


def build_employment_graph():
    nodes = EmploymentGraphNodes()
    workflow = StateGraph(VerificationState)

    workflow.add_node("classifier", nodes.classifier_node)
    workflow.add_node("comparator", nodes.comparator_node)
    workflow.add_node("reporter", nodes.reporter_node)
    workflow.add_node("bank_statement_reporter", nodes.bank_statement_reporter_node)

    workflow.set_entry_point("classifier")
    workflow.add_conditional_edges(
        "classifier",
        _route_after_classification,
        {
            "bank_statement_reporter": "bank_statement_reporter",
            "comparator": "comparator",
        },
    )
    workflow.add_edge("comparator", "reporter")
    workflow.add_edge("reporter", END)
    workflow.add_edge("bank_statement_reporter", END)

    return workflow.compile()


class EmploymentGraphOrchestrator:
    def __init__(self, record_data: Dict[str, Any], document_text: str, application_submission_date: str = None):
        self.record_data = record_data
        self.document_text = document_text
        self.application_submission_date = application_submission_date
        self.app = build_employment_graph()

    def run(self) -> Dict[str, Any]:
        verifiable_fields = [
            f for f in self.record_data.keys()
            if f not in FIELDS_TO_EXCLUDE_FROM_PROCESSING
        ]

        initial_state = VerificationState(
            record_data=self.record_data,
            document_text=self.document_text,
            verifiable_fields=verifiable_fields,
            application_submission_date=self.application_submission_date,
            document_type=None,
            document_type_reasoning=None,
            comparison_task_output=None,
            final_report=None,
            usage_metrics={},
            model_config={
                "comparator_model": MODEL_STANDARD_VERIFICATION,
                "reporter_model": "deterministic-python",
            },
        )

        result_state = self.app.invoke(initial_state)
        report_data = result_state.get("final_report")
        if not report_data:
            raise ValueError("Employment Graph failed to produce final report.")

        from app.langgraph.llm_utils import _GLOBAL_TOKEN_USAGE
        usage_metrics = {
            "total_tokens": _GLOBAL_TOKEN_USAGE["total_tokens"],
            "prompt_tokens": _GLOBAL_TOKEN_USAGE["prompt_tokens"],
            "completion_tokens": _GLOBAL_TOKEN_USAGE["completion_tokens"],
            "successful_requests": _GLOBAL_TOKEN_USAGE["successful_requests"],
            "total_cost_usd": _GLOBAL_TOKEN_USAGE["total_cost_usd"],
            "source": "LangGraph",
            "model_config": result_state["model_config"],
            "document_type": result_state.get("document_type"),
        }

        return {
            **report_data,
            "usage_metrics": usage_metrics,
        }
