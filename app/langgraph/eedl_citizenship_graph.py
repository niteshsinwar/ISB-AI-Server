import logging
import json
import re
from typing import Dict, Any, List
from langgraph.graph import StateGraph, END

from app.config import MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION, LLM_FIELD_EXCLUSIONS
from app.langgraph.eedl_graph_prompts import (
    EEDL_CITIZENSHIP_COMPARATOR_GOAL, EEDL_CITIZENSHIP_COMPARATOR_BACKSTORY,
    EEDL_CITIZENSHIP_COMPARISON_TASK_DESCRIPTION, EEDL_CITIZENSHIP_COMPARISON_EXPECTED_OUTPUT,
)
from app.langgraph.state import VerificationState
from app.langgraph.graph_utils import (
    get_llm, format_record_for_llm, format_fields_for_llm,
)
from app.langgraph.report_builder import build_verification_report, parse_comparison_json
from app.langgraph.schemas import ValidatedCitizenshipReport

logger = logging.getLogger(__name__)

FIELDS_TO_EXCLUDE: List[str] = LLM_FIELD_EXCLUSIONS

_CITIZENSHIP_CRITICAL_FIELDS = {
    "Full Name",
    "Name",
    "Date of Birth",
    "DOB",
    "Birthdate",
    "ID Number",
    "Aadhaar",
    "Aadhar",
    "Passport",
    "Passport Number",
    "Citizenship",
    "Nationality",
}


def _normalize_citizenship_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"N/A", "NULL", "NONE", "NOT_FOUND"}:
        return None
    normalized = re.sub(r"[^a-z]", "", text.casefold())
    if normalized in {"india", "indian", "ind"}:
        return "Indian"
    return text


def _derive_suggested_citizenship(
    comparisons: List[Dict[str, Any]],
    document_text: str,
) -> str | None:
    for item in comparisons:
        field_name = re.sub(r"[^a-z0-9]", "", str(item.get("field_name") or "").casefold())
        if "citizenship" not in field_name and "nationality" not in field_name:
            continue
        if str(item.get("status") or "").strip().upper() in {"MISMATCH", "NOT_FOUND", "NOT_FOUND_ON_DOCUMENT"}:
            continue
        suggested = _normalize_citizenship_value(item.get("document_value"))
        if suggested:
            return suggested

    doc = str(document_text or "")
    doc_lower = doc.casefold()
    if any(marker in doc_lower for marker in ("aadhaar", "aadhar", "uidai", "unique identification", "आधार")):
        return "Indian"

    nationality_match = re.search(
        r"\b(?:nationality|citizenship)\b\s*[:\-]?\s*([A-Za-z][A-Za-z ]{1,40})",
        doc,
        flags=re.IGNORECASE,
    )
    if nationality_match:
        candidate = nationality_match.group(1).strip()
        candidate = re.split(r"\s{2,}|[\n\r]|passport|date|sex|place", candidate, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        return _normalize_citizenship_value(candidate)

    return None


class CitizenshipGraphNodes:
    def __init__(self):
        self.llm_comparator = get_llm(MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION)

    def comparator_node(self, state: VerificationState) -> Dict[str, Any]:
        logger.info("Executing EEDL Citizenship Comparator Node")
        formatted_fields = format_fields_for_llm(state['verifiable_fields'])
        formatted_record = format_record_for_llm(state['record_data'], state['verifiable_fields'])
        prompt = f"""
{EEDL_CITIZENSHIP_COMPARATOR_GOAL}
{EEDL_CITIZENSHIP_COMPARATOR_BACKSTORY}

TASK:
{EEDL_CITIZENSHIP_COMPARISON_TASK_DESCRIPTION.format(
    verifiable_fields=formatted_fields,
    record_data=formatted_record,
    document_text=state['document_text'],
)}

EXPECTED OUTPUT:
{EEDL_CITIZENSHIP_COMPARISON_EXPECTED_OUTPUT}
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
                logger.warning("EEDL Citizenship comparator returned malformed JSON; retrying once")
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
        logger.info("Executing EEDL Citizenship Reporter Node")
        try:
            comparisons = parse_comparison_json(state['comparison_task_output'])
            suggested = _derive_suggested_citizenship(
                comparisons,
                state.get("document_text") or "",
            )
            final_json = build_verification_report(
                comparisons,
                critical_field_names=_CITIZENSHIP_CRITICAL_FIELDS,
                extra_fields={"suggested_citizenship_value": suggested},
            )
            validated = ValidatedCitizenshipReport(**final_json)
            return {"final_report": validated.model_dump()}
        except Exception as e:
            logger.error(f"Error in Citizenship Reporter Node: {e}")
            raise


def build_citizenship_graph():
    nodes = CitizenshipGraphNodes()
    workflow = StateGraph(VerificationState)
    workflow.add_node("comparator", nodes.comparator_node)
    workflow.add_node("reporter", nodes.reporter_node)
    workflow.set_entry_point("comparator")
    workflow.add_edge("comparator", "reporter")
    workflow.add_edge("reporter", END)
    return workflow.compile()


class CitizenshipGraphOrchestrator:
    def __init__(self, record_data: Dict[str, Any], document_text: str):
        self.record_data = record_data
        self.document_text = document_text
        self.app = build_citizenship_graph()

    def run(self) -> Dict[str, Any]:
        verifiable_fields = [f for f in self.record_data.keys() if f not in FIELDS_TO_EXCLUDE]
        initial_state = VerificationState(
            record_data=self.record_data,
            document_text=self.document_text,
            verifiable_fields=verifiable_fields,
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
            raise ValueError("Citizenship graph execution failed to produce a final report.")
        from app.langgraph.llm_utils import _GLOBAL_TOKEN_USAGE
        usage_metrics = {
            "total_tokens": _GLOBAL_TOKEN_USAGE["total_tokens"],
            "prompt_tokens": _GLOBAL_TOKEN_USAGE["prompt_tokens"],
            "completion_tokens": _GLOBAL_TOKEN_USAGE["completion_tokens"],
            "successful_requests": _GLOBAL_TOKEN_USAGE["successful_requests"],
            "total_cost_usd": _GLOBAL_TOKEN_USAGE["total_cost_usd"],
            "source": "LangGraph",
            "model_config": result_state["model_config"],
        }
        return {**report_data, "usage_metrics": usage_metrics}
