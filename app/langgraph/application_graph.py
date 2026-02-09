import logging
import os
import json
from typing import Dict, Any, List
from langgraph.graph import StateGraph, END

from app.config import (
    MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION,
    MODEL_HTML_SYNTHESIS, TEMP_HTML_SYNTHESIS
)
from app.langgraph.graph_prompts import (
    APPLICATION_DATA_COMPARATOR_AGENT_GOAL,
    APPLICATION_DATA_COMPARATOR_AGENT_BACKSTORY,
    FINAL_REPORT_GENERATOR_AGENT_GOAL,
    FINAL_REPORT_GENERATOR_AGENT_BACKSTORY,
    APPLICATION_DATA_COMPARISON_TASK_DESCRIPTION,
    FINAL_REPORT_GENERATION_TASK_DESCRIPTION,
    APPLICATION_DATA_COMPARISON_EXPECTED_OUTPUT,
    FINAL_REPORT_GENERATION_EXPECTED_OUTPUT
)
from app.langgraph.state import VerificationState
from app.langgraph.graph_utils import (
    get_llm, parse_json_from_response,
    format_record_for_llm, format_fields_for_llm, format_comparison_for_reporter
)
from app.langgraph.schemas import ValidatedCrewReport

logger = logging.getLogger(__name__)

# Fields to Exclude (Copied from ApplicationCrew)
FIELDS_TO_EXCLUDE_FROM_PROCESSING: List[str] = [
     'Applicant__c', 'type', 'Contact', 'recordId', 'Task_Id','triggeringLogId','Id', 'DocumentchecklistItem_Id'
]

class ApplicationGraphNodes:
    def __init__(self):
        self.llm_comparator = get_llm(MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION)
        self.llm_reporter = get_llm(MODEL_HTML_SYNTHESIS, TEMP_HTML_SYNTHESIS)

    def comparator_node(self, state: VerificationState) -> Dict[str, Any]:
        """Uses LLM-friendly plain text formatting for efficient token usage."""
        logger.info("Executing Application Comparator Node")

        # Format data as LLM-friendly plain text (not JSON)
        formatted_fields = format_fields_for_llm(state['verifiable_fields'])
        formatted_record = format_record_for_llm(state['record_data'], state['verifiable_fields'])

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
        response = self.llm_comparator.invoke(prompt)
        return {"comparison_task_output": response.content}

    def reporter_node(self, state: VerificationState) -> Dict[str, Any]:
        logger.info("Executing Application Reporter Node")

        # Format comparison output for reporter
        formatted_context = format_comparison_for_reporter(state['comparison_task_output'])

        prompt = f"""
{FINAL_REPORT_GENERATOR_AGENT_GOAL}
{FINAL_REPORT_GENERATOR_AGENT_BACKSTORY}

TASK:
{FINAL_REPORT_GENERATION_TASK_DESCRIPTION.format(
    context=formatted_context
)}

EXPECTED OUTPUT:
{FINAL_REPORT_GENERATION_EXPECTED_OUTPUT}
"""
        response = self.llm_reporter.invoke(prompt)
        
        try:
            final_json = parse_json_from_response(response.content)
            validated = ValidatedCrewReport(**final_json)
            return {"final_report": validated.model_dump()}
        except Exception as e:
            logger.error(f"Error in Application Reporter Node: {e}")
            raise

def build_application_graph():
    nodes = ApplicationGraphNodes()
    workflow = StateGraph(VerificationState)

    workflow.add_node("comparator", nodes.comparator_node)
    workflow.add_node("reporter", nodes.reporter_node)

    workflow.set_entry_point("comparator")
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
            comparison_task_output=None,
            final_report=None,
            usage_metrics={},
            model_config={
                "comparator_model": MODEL_STANDARD_VERIFICATION,
                "reporter_model": MODEL_HTML_SYNTHESIS
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
