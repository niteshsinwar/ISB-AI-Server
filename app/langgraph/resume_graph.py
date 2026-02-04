import logging
import os
import json
from typing import Dict, Any, TypedDict, Literal, Optional
from langgraph.graph import StateGraph, END

from app.config import (
    MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION
)
from app.langgraph.graph_prompts import (
    RESUME_ANALYZER_AGENT_GOAL,
    RESUME_ANALYZER_AGENT_BACKSTORY,
    RESUME_ANALYSIS_TASK_DESCRIPTION,
    RESUME_ANALYSIS_EXPECTED_OUTPUT
)
from app.langgraph.graph_utils import get_llm, parse_json_from_response
from app.langgraph.schemas import ValidatedResumeReport

logger = logging.getLogger(__name__)

# Resume has a much simpler state than VerificationState
class ResumeState(TypedDict):
    document_text: str
    final_report: Optional[Dict[str, Any]]

class ResumeGraphNodes:
    def __init__(self):
        self.llm_screener = get_llm(MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION)

    def screener_node(self, state: ResumeState) -> Dict[str, Any]:
        logger.info("Executing Resume Screener Node")
        
        prompt = f"""
        {RESUME_ANALYZER_AGENT_GOAL}
        {RESUME_ANALYZER_AGENT_BACKSTORY}
        
        TASK:
        {RESUME_ANALYSIS_TASK_DESCRIPTION.format(
            document_text=state['document_text']
        )}
        
        EXPECTED OUTPUT:
        {RESUME_ANALYSIS_EXPECTED_OUTPUT}
        """
        response = self.llm_screener.invoke(prompt)
        
        try:
            final_json = parse_json_from_response(response.content)
            validated = ValidatedResumeReport(**final_json)
            return {"final_report": validated.model_dump()}
        except Exception as e:
            logger.error(f"Error in Resume Screener Node: {e}")
            raise

def build_resume_graph():
    nodes = ResumeGraphNodes()
    workflow = StateGraph(ResumeState)

    workflow.add_node("screener", nodes.screener_node)
    
    workflow.set_entry_point("screener")
    workflow.add_edge("screener", END)

    return workflow.compile()

class ResumeGraphOrchestrator:
    def __init__(self, document_text: str):
        self.document_text = document_text
        self.app = build_resume_graph()

    def run(self) -> Dict[str, Any]:
        initial_state = ResumeState(
            document_text=self.document_text,
            final_report=None
        )
        
        result_state = self.app.invoke(initial_state)
        report_data = result_state.get("final_report")
        if not report_data:
            raise ValueError("Resume Graph failed to produce final report.")
            
        # Replicate Usage Metrics
        from app.crew.crew_utils import _GLOBAL_TOKEN_USAGE
        usage_metrics = {
            "total_tokens": _GLOBAL_TOKEN_USAGE["total_tokens"],
            "prompt_tokens": _GLOBAL_TOKEN_USAGE["prompt_tokens"],
            "completion_tokens": _GLOBAL_TOKEN_USAGE["completion_tokens"],
            "successful_requests": _GLOBAL_TOKEN_USAGE["successful_requests"],
            "total_cost_usd": _GLOBAL_TOKEN_USAGE["total_cost_usd"],
            "source": "LangGraph",
            "note": "Single node"
        }
        return {
            **report_data,
            "usage_metrics": usage_metrics
        }
