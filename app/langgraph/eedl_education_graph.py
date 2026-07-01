import logging
import json
from typing import Dict, Any, List
from langgraph.graph import StateGraph, END

from app.config import MODEL_COMPLEX_REASONING, TEMP_COMPLEX_REASONING, LLM_FIELD_EXCLUSIONS
from app.langgraph.graph_prompts import (
    EDUCATION_DATA_COMPARATOR_AGENT_GOAL, EDUCATION_DATA_COMPARATOR_AGENT_BACKSTORY,
    EDUCATION_DATA_COMPARISON_TASK_DESCRIPTION,
    EDUCATION_DATA_COMPARISON_EXPECTED_OUTPUT,
)
from app.langgraph.state import VerificationState
from app.langgraph.graph_utils import (
    get_llm, format_record_for_llm, format_fields_for_llm,
)
from app.langgraph.report_builder import build_verification_report, parse_comparison_json
from app.langgraph.schemas import ValidatedCrewReport

logger = logging.getLogger(__name__)

EEDL_EDU_FIELDS_TO_EXCLUDE: List[str] = LLM_FIELD_EXCLUSIONS

_EEDL_EDUCATION_CRITICAL_FIELDS = {
    "Degree_Type__c",
    "Degree Type",
    "University_Name__c",
    "University Name",
    "GPA__c",
    "GPA",
    "From__c",
    "From",
    "To__c",
    "To",
}


class EEDLEducationGraphNodes:
    def __init__(self):
        self.llm_comparator = get_llm(MODEL_COMPLEX_REASONING, TEMP_COMPLEX_REASONING)

    def comparator_node(self, state: VerificationState) -> Dict[str, Any]:
        logger.info("Executing EEDL Education Comparator Node")
        formatted_fields = format_fields_for_llm(state['verifiable_fields'])
        formatted_record = format_record_for_llm(state['record_data'], state['verifiable_fields'])
        prompt = f"""
{EDUCATION_DATA_COMPARATOR_AGENT_GOAL}
{EDUCATION_DATA_COMPARATOR_AGENT_BACKSTORY}

TASK:
{EDUCATION_DATA_COMPARISON_TASK_DESCRIPTION.format(
    verifiable_fields=formatted_fields,
    record_data=formatted_record,
    document_text=state['document_text'],
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
                logger.warning("EEDL Education comparator returned malformed JSON; retrying once")
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
        logger.info("Executing EEDL Education Reporter Node")
        try:
            comparisons = parse_comparison_json(state['comparison_task_output'])
            final_json = build_verification_report(
                comparisons,
                critical_field_names=_EEDL_EDUCATION_CRITICAL_FIELDS,
            )
            validated = ValidatedCrewReport(**final_json)
            return {"final_report": validated.model_dump()}
        except Exception as e:
            logger.error(f"Error in EEDL Education Reporter Node: {e}")
            raise


def build_eedl_education_graph():
    nodes = EEDLEducationGraphNodes()
    workflow = StateGraph(VerificationState)
    workflow.add_node("comparator", nodes.comparator_node)
    workflow.add_node("reporter", nodes.reporter_node)
    workflow.set_entry_point("comparator")
    workflow.add_edge("comparator", "reporter")
    workflow.add_edge("reporter", END)
    return workflow.compile()


class EEDLEducationGraphOrchestrator:
    def __init__(self, record_data: Dict[str, Any], document_text: str):
        self.record_data = record_data
        self.document_text = document_text
        self.app = build_eedl_education_graph()

    def run(self) -> Dict[str, Any]:
        verifiable_fields = [f for f in self.record_data.keys() if f not in EEDL_EDU_FIELDS_TO_EXCLUDE]
        initial_state = VerificationState(
            record_data=self.record_data,
            document_text=self.document_text,
            verifiable_fields=verifiable_fields,
            comparison_task_output=None,
            final_report=None,
            usage_metrics={},
            model_config={
                "comparator_model": MODEL_COMPLEX_REASONING,
                "reporter_model": "deterministic-python",
            },
        )
        result_state = self.app.invoke(initial_state)
        report_data = result_state.get("final_report")
        if not report_data:
            raise ValueError("EEDL Education graph execution failed to produce a final report.")
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
