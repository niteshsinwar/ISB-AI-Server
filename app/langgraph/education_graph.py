import logging
import json
import re
from typing import Dict, Any, List
from langgraph.graph import StateGraph, END

from app.config import (
    MODEL_COMPLEX_REASONING, TEMP_COMPLEX_REASONING,
    LLM_FIELD_EXCLUSIONS
)
from app.langgraph.graph_prompts import (
    EDUCATION_DATA_COMPARATOR_AGENT_GOAL,
    EDUCATION_DATA_COMPARATOR_AGENT_BACKSTORY,
    EDUCATION_DATA_COMPARISON_TASK_DESCRIPTION,
    EDUCATION_DATA_COMPARISON_EXPECTED_OUTPUT,
)
from app.langgraph.state import VerificationState
from app.langgraph.graph_utils import (
    get_llm, format_record_for_llm, format_fields_for_llm
)
from app.langgraph.report_builder import build_verification_report, parse_comparison_json
from app.langgraph.schemas import ValidatedCrewReport

logger = logging.getLogger(__name__)

FIELDS_TO_EXCLUDE_FROM_PROCESSING: List[str] = LLM_FIELD_EXCLUSIONS

_REQUIRED_EDUCATION_COMPARISONS = {
    "Degree/Qualification": {"degreequalification", "degreename", "degree"},
    "SF Field of Study": {"sffieldofstudy", "fieldofstudy"},
    "Major/Specialization": {"majorspecialization", "specialization", "major"},
}

_CRITICAL_EDUCATION_FIELDS = {
    "studentname", "sffullname",
    "institutionname", "schoolinstitutecampus",
    "degreequalification", "degreename", "degree",
    "sffieldofstudy", "fieldofstudy",
    "majorspecialization", "specialization", "major",
    "enddate", "to", "sfpassingyear",
    "gpa", "sfcgpapercentage", "percentage", "cgpa",
    "numberofsemesters", "cgpascale",
}

# Prompt-mandated synthetic rows that are not record fields but must survive
# the allowed-fields safety net in the reporter.
_EDUCATION_SYNTHETIC_ROWS = {"Number of Semesters", "CGPA Scale"}


def _normalized_field_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _numeric_confidence(value: Any) -> int:
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or "0"))
    confidence = int(float(match.group())) if match else 0
    if confidence < 0:
        confidence = 100 + confidence
    return max(0, min(100, confidence))


def _ensure_required_education_comparisons(
    comparisons: List[Dict[str, Any]],
    record_data: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Guarantee independent degree, field-of-study, and specialization rows."""
    reconciled = [dict(item) for item in comparisons]

    for source_field, aliases in _REQUIRED_EDUCATION_COMPARISONS.items():
        source_value = record_data.get(source_field)
        if source_value is None or source_value == "":
            continue

        matching_index = next(
            (
                index
                for index, item in enumerate(reconciled)
                if _normalized_field_name(item.get("field_name")) in aliases
            ),
            None,
        )

        if matching_index is None:
            reconciled.append({
                "field_name": source_field,
                "record_value": source_value,
                "document_value": None,
                "status": "NOT_FOUND",
                "confidence": 60,
                "notes": "Required independent comparison was not returned by the comparator.",
                "_is_critical": True,
            })
            continue

        comparison = reconciled[matching_index]
        comparison["field_name"] = source_field
        comparison["_is_critical"] = True
        returned_record_value = comparison.get("record_value")
        if _normalized_field_name(returned_record_value) != _normalized_field_name(source_value):
            comparison.update(
                record_value=source_value,
                document_value=None,
                status="NOT_FOUND",
                confidence=60,
                notes=(
                    "Comparator substituted a different record field; "
                    "independent document verification is unavailable."
                ),
            )

    for comparison in reconciled:
        normalized_name = _normalized_field_name(comparison.get("field_name"))
        if normalized_name in _CRITICAL_EDUCATION_FIELDS:
            comparison["_is_critical"] = True

        if normalized_name == "sffieldofstudy":
            notes = str(comparison.get("notes") or "").casefold()
            if "infer" in notes:
                comparison["confidence"] = min(
                    90,
                    _numeric_confidence(comparison.get("confidence")),
                )

        status = str(comparison.get("status") or "NOT_FOUND").strip().upper()
        if comparison.get("_is_critical") and status not in {"MATCH", "MATCHED", "PASS", "PASSED", "VERIFIED"}:
            comparison["confidence"] = min(
                70,
                _numeric_confidence(comparison.get("confidence")),
            )

    return reconciled


class EducationGraphNodes:
    def __init__(self):
        self.llm_comparator = get_llm(MODEL_COMPLEX_REASONING, TEMP_COMPLEX_REASONING)

    def comparator_node(self, state: VerificationState) -> Dict[str, Any]:
        """
        Comparison Step: Compares Record Data vs Document Text.
        Uses LLM-friendly plain text formatting for efficient token usage.
        """
        logger.info("Executing Education Comparator Node")

        # Format data as LLM-friendly plain text (not JSON)
        formatted_fields = format_fields_for_llm(state['verifiable_fields'])
        formatted_record = format_record_for_llm(state['record_data'], state['verifiable_fields'])

        prompt = f"""
{EDUCATION_DATA_COMPARATOR_AGENT_GOAL}
{EDUCATION_DATA_COMPARATOR_AGENT_BACKSTORY}

TASK:
{EDUCATION_DATA_COMPARISON_TASK_DESCRIPTION.format(
    verifiable_fields=formatted_fields,
    record_data=formatted_record,
    document_text=state['document_text']
)}

EXPECTED OUTPUT:
{EDUCATION_DATA_COMPARISON_EXPECTED_OUTPUT}
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
                logger.warning("Education comparator returned malformed JSON; retrying once")
                prompt += (
                    "\n\nRETRY REQUIREMENT: Your previous response was not valid JSON. "
                    "Return only the required JSON object with `verification_analysis_report` and no surrounding prose."
                )

        comparisons = _ensure_required_education_comparisons(
            comparisons,
            state["record_data"],
        )
        return {
            "comparison_task_output": json.dumps(
                {"verification_analysis_report": comparisons},
                ensure_ascii=False,
            )
        }

    def reporter_node(self, state: VerificationState) -> Dict[str, Any]:
        """
        Reporting Step: Generates HTML and report metadata deterministically.
        """
        logger.info("Executing Education Reporter Node")

        try:
            comparisons = parse_comparison_json(state['comparison_task_output'])
            final_json = build_verification_report(
                comparisons,
                allowed_fields=state.get('verifiable_fields'),
                extra_allowed_fields=_EDUCATION_SYNTHETIC_ROWS,
            )
            validated = ValidatedCrewReport(**final_json)
            return {"final_report": validated.model_dump()}
        except Exception as e:
            logger.error(f"Error in Reporter Node: {e}")
            raise


def build_education_graph():
    nodes = EducationGraphNodes()
    workflow = StateGraph(VerificationState)

    workflow.add_node("comparator", nodes.comparator_node)
    workflow.add_node("reporter", nodes.reporter_node)

    workflow.set_entry_point("comparator")
    workflow.add_edge("comparator", "reporter")
    workflow.add_edge("reporter", END)

    return workflow.compile()


class EducationGraphOrchestrator:
    def __init__(self, record_data: Dict[str, Any], document_text: str, application_submission_date: str = None):
        self.record_data = record_data
        self.document_text = document_text
        self.application_submission_date = application_submission_date
        self.app = build_education_graph()

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
            comparison_task_output=None,
            final_report=None,
            usage_metrics={},
            model_config={
                "comparator_model": MODEL_COMPLEX_REASONING,
                "reporter_model": "deterministic-python"
            }
        )
        
        # Execute Graph
        result_state = self.app.invoke(initial_state)
        
        # Extract Results
        report_data = result_state.get("final_report")
        if not report_data:
            raise ValueError("Graph execution failed to produce final report.")
            
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
