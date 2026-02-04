import logging
import os
import json
from typing import Dict, Any, List
from langgraph.graph import StateGraph, END

from app.config import (
    MODEL_COMPLEX_REASONING, TEMP_COMPLEX_REASONING,
    MODEL_HTML_SYNTHESIS, TEMP_HTML_SYNTHESIS
)
from app.crew.crew_utils import get_crew_usage_metrics
from app.langgraph.graph_prompts import (
    EDUCATION_DATA_COMPARATOR_AGENT_GOAL,
    EDUCATION_DATA_COMPARATOR_AGENT_BACKSTORY,
    FINAL_REPORT_GENERATOR_AGENT_GOAL,
    FINAL_REPORT_GENERATOR_AGENT_BACKSTORY,
    EDUCATION_DATA_COMPARISON_TASK_DESCRIPTION,
    FINAL_REPORT_GENERATION_TASK_DESCRIPTION,
    EDUCATION_DATA_COMPARISON_EXPECTED_OUTPUT,
    FINAL_REPORT_GENERATION_EXPECTED_OUTPUT
)
from app.langgraph.state import VerificationState
from app.langgraph.graph_utils import get_llm, parse_json_from_response
from app.langgraph.schemas import ValidatedCrewReport

logger = logging.getLogger(__name__)

# Fields to Exclude (Copied from EducationCrew)
FIELDS_TO_EXCLUDE_FROM_PROCESSING: List[str] = [
    'Applicant__c', 'type', 'Contact', 'recordId', 'Task_Id','triggeringLogId','Id', 'DocumentchecklistItem_Id'
]

class EducationGraphNodes:
    def __init__(self):
        # We explicitly use Gemini 3.0 Flash (or whatever is in env) here
        self.llm_comparator = get_llm(MODEL_COMPLEX_REASONING, TEMP_COMPLEX_REASONING)
        self.llm_reporter = get_llm(MODEL_HTML_SYNTHESIS, TEMP_HTML_SYNTHESIS)

    def comparator_node(self, state: VerificationState) -> Dict[str, Any]:
        """
        Comparison Step: Compares Record Data vs Document Text.
        """
        logger.info("Executing Education Comparator Node")
        
        # specific to Education
        prompt = f"""
        {EDUCATION_DATA_COMPARATOR_AGENT_GOAL}
        {EDUCATION_DATA_COMPARATOR_AGENT_BACKSTORY}
        
        TASK:
        {EDUCATION_DATA_COMPARISON_TASK_DESCRIPTION.format(
            verifiable_fields=state['verifiable_fields'],
            record_data=state['record_data'],
            document_text=state['document_text']
        )}
        
        EXPECTED OUTPUT:
        {EDUCATION_DATA_COMPARISON_EXPECTED_OUTPUT}
        """
        
        response = self.llm_comparator.invoke(prompt)
        return {"comparison_task_output": response.content}

    def reporter_node(self, state: VerificationState) -> Dict[str, Any]:
        """
        Reporting Step: Generates the final JSON report.
        """
        logger.info("Executing Education Reporter Node")
        
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
        
        # Parse output immediately
        try:
            final_json = parse_json_from_response(response.content)
            # Validate against Pydantic model
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
    def __init__(self, record_data: Dict[str, Any], document_text: str):
        self.record_data = record_data
        self.document_text = document_text
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
            comparison_task_output=None,
            final_report=None,
            usage_metrics={},
            model_config={
                "comparator_model": MODEL_COMPLEX_REASONING,
                "reporter_model": MODEL_HTML_SYNTHESIS
            }
        )
        
        # Execute Graph
        result_state = self.app.invoke(initial_state)
        
        # Extract Results
        report_data = result_state.get("final_report")
        if not report_data:
            raise ValueError("Graph execution failed to produce final report.")
            
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
