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
    EMPLOYMENT_DATA_COMPARATOR_AGENT_GOAL,
    EMPLOYMENT_DATA_COMPARATOR_AGENT_BACKSTORY,
    FINAL_REPORT_GENERATOR_AGENT_GOAL,
    FINAL_REPORT_GENERATOR_AGENT_BACKSTORY,
    EMPLOYMENT_DATA_COMPARISON_TASK_DESCRIPTION,
    FINAL_REPORT_GENERATION_TASK_DESCRIPTION,
    EMPLOYMENT_DATA_COMPARISON_EXPECTED_OUTPUT,
    FINAL_REPORT_GENERATION_EXPECTED_OUTPUT
)
from app.langgraph.state import VerificationState
from app.langgraph.graph_utils import get_llm, parse_json_from_response
from app.langgraph.schemas import ValidatedCrewReport

logger = logging.getLogger(__name__)

# Fields to Exclude (Copied from EmploymentCrew)
FIELDS_TO_EXCLUDE_FROM_PROCESSING: List[str] = [
     'Applicant__c', 'type', 'Contact', 'recordId', 'Task_Id','triggeringLogId','Id', 'DocumentchecklistItem_Id'
]

class EmploymentGraphNodes:
    def __init__(self):
        # Using 3.0 Flash / 2.5 Flash per config
        self.llm_comparator = get_llm(MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION)
        self.llm_reporter = get_llm(MODEL_HTML_SYNTHESIS, TEMP_HTML_SYNTHESIS)

    def comparator_node(self, state: VerificationState) -> Dict[str, Any]:
        logger.info("Executing Employment Comparator Node")
        
        prompt = f"""
        {EMPLOYMENT_DATA_COMPARATOR_AGENT_GOAL}
        {EMPLOYMENT_DATA_COMPARATOR_AGENT_BACKSTORY}
        
        TASK:
        {EMPLOYMENT_DATA_COMPARISON_TASK_DESCRIPTION.format(
            verifiable_fields=state['verifiable_fields'],
            record_data=state['record_data'],
            document_text=state['document_text']
        )}
        
        EXPECTED OUTPUT:
        {EMPLOYMENT_DATA_COMPARISON_EXPECTED_OUTPUT}
        """
        response = self.llm_comparator.invoke(prompt)
        return {"comparison_task_output": response.content}

    def reporter_node(self, state: VerificationState) -> Dict[str, Any]:
        logger.info("Executing Employment Reporter Node")
        
        prompt = f"""
        {FINAL_REPORT_GENERATOR_AGENT_GOAL}
        {FINAL_REPORT_GENERATOR_AGENT_BACKSTORY}
        
        TASK:
        {FINAL_REPORT_GENERATION_TASK_DESCRIPTION.format(
            context=state['comparison_task_output']
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
            logger.error(f"Error in Employment Reporter Node: {e}")
            raise

def build_employment_graph():
    nodes = EmploymentGraphNodes()
    workflow = StateGraph(VerificationState)

    workflow.add_node("comparator", nodes.comparator_node)
    workflow.add_node("reporter", nodes.reporter_node)

    workflow.set_entry_point("comparator")
    workflow.add_edge("comparator", "reporter")
    workflow.add_edge("reporter", END)

    return workflow.compile()

class EmploymentGraphOrchestrator:
    def __init__(self, record_data: Dict[str, Any], document_text: str):
        self.record_data = record_data
        self.document_text = document_text
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
            raise ValueError("Employment Graph failed to produce final report.")
            
        # Standardize return signature
        # Replicate Usage Metrics
        from app.crew.crew_utils import _GLOBAL_TOKEN_USAGE
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
