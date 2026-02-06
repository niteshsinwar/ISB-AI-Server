import logging
import json
import re
from typing import Any, Dict, List, Optional
from langchain_google_genai import ChatGoogleGenerativeAI
from app.config import (
    CREW_GOOGLE_API_KEY, 
    MODEL_STANDARD_VERIFICATION, 
    TEMP_STANDARD_VERIFICATION,
    MODEL_HTML_SYNTHESIS,
    TEMP_HTML_SYNTHESIS
)
# Reuse the existing interceptor-friendly LLM init from llm_utils
from app.langgraph.llm_utils import initialize_llm, clean_and_extract_json

logger = logging.getLogger(__name__)

def get_llm(model_name: str, temperature: float) -> ChatGoogleGenerativeAI:
    """Wrapper to get LLM instance using the centralized utility."""
    return initialize_llm(model_name, temperature, CREW_GOOGLE_API_KEY)

def parse_json_from_response(response_content: str) -> Dict[str, Any]:
    """
    Parses JSON from an LLM response string.
    """
    json_str = clean_and_extract_json(response_content)
    if not json_str:
        raise ValueError("Could not extract JSON from LLM response")
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to decode JSON: {e}")

def merge_usage_metrics(current_metrics: Dict[str, Any], new_metrics: Dict[str, Any]) -> Dict[str, Any]:
    """
    Helper to merge usage metrics if we were manually tracking them.
    Note: The global interceptor in crew_utils handles the actual cost tracking,
    but we might want to pass step-level info if needed.
    """
    # For now, we rely on the global accumulator, but this placeholder 
    # ensures we have a spot to aggregate if LangGraph specific logic is added.
    return current_metrics
