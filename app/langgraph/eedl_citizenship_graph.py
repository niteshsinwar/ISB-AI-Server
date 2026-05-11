import logging
from typing import Dict, Any, List
from langgraph.graph import StateGraph, END

from app.config import MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION, MODEL_HTML_SYNTHESIS, TEMP_HTML_SYNTHESIS
from app.langgraph.eedl_graph_prompts import (
    EEDL_CITIZENSHIP_COMPARATOR_GOAL, EEDL_CITIZENSHIP_COMPARATOR_BACKSTORY,
    EEDL_CITIZENSHIP_COMPARISON_TASK_DESCRIPTION, EEDL_CITIZENSHIP_COMPARISON_EXPECTED_OUTPUT,
    EEDL_CITIZENSHIP_REPORTER_GOAL, EEDL_CITIZENSHIP_REPORTER_BACKSTORY,
    EEDL_CITIZENSHIP_REPORT_TASK_DESCRIPTION, EEDL_CITIZENSHIP_REPORT_EXPECTED_OUTPUT,
)
from app.langgraph.state import VerificationState
from app.langgraph.graph_utils import (
    get_llm, parse_json_from_response,
    format_record_for_llm, format_fields_for_llm, format_comparison_for_reporter,
)
from app.langgraph.schemas import ValidatedCitizenshipReport

logger = logging.getLogger(__name__)

FIELDS_TO_EXCLUDE: List[str] = ['Id', 'LastModifiedDate', 'ContactId']


class CitizenshipGraphNodes:
    def __init__(self):
        self.llm_comparator = get_llm(MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION)
        self.llm_reporter = get_llm(MODEL_HTML_SYNTHESIS, TEMP_HTML_SYNTHESIS)

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
        response = self.llm_comparator.invoke(prompt)
        return {"comparison_task_output": response.content}

    def reporter_node(self, state: VerificationState) -> Dict[str, Any]:
        logger.info("Executing EEDL Citizenship Reporter Node")
        formatted_context = format_comparison_for_reporter(state['comparison_task_output'])
        prompt = f"""
{EEDL_CITIZENSHIP_REPORTER_GOAL}
{EEDL_CITIZENSHIP_REPORTER_BACKSTORY}

TASK:
{EEDL_CITIZENSHIP_REPORT_TASK_DESCRIPTION.format(context=formatted_context)}

EXPECTED OUTPUT:
{EEDL_CITIZENSHIP_REPORT_EXPECTED_OUTPUT}
"""
        response = self.llm_reporter.invoke(prompt)
        try:
            final_json = parse_json_from_response(response.content)
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
                "reporter_model": MODEL_HTML_SYNTHESIS,
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
