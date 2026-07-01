import logging
import json
import re
from typing import Any, Dict, List, Optional
from langchain_google_genai import ChatGoogleGenerativeAI
from app.config import CREW_GOOGLE_API_KEY
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


# =====================================================================================
# == LLM-FRIENDLY DATA FORMATTING
# == These functions convert structured data to readable text for LLM-to-LLM communication
# =====================================================================================

def format_record_for_llm(record_data: Dict[str, Any], fields_to_include: List[str] = None) -> str:
    """
    Format Salesforce record data as LLM-friendly plain text.

    Instead of: {'Institution_Name__c': 'ISB', 'hed__Start_Date__c': '2019-06-01'}

    Outputs:
    SALESFORCE RECORD DATA:
    Institution_Name__c: ISB
    hed__Start_Date__c: 2019-06-01
    degreeLevel: Post Graduate
    ...

    This format is more readable for LLMs and uses fewer tokens than JSON.
    """
    if not record_data:
        return "SALESFORCE RECORD DATA:\nNo data available"

    lines = ["SALESFORCE RECORD DATA:"]

    for field, value in record_data.items():
        # Skip if fields_to_include is specified and this field isn't in it
        if fields_to_include and field not in fields_to_include:
            continue

        # Skip nested objects/dicts
        if isinstance(value, dict):
            continue

        # Format value
        if value is None:
            formatted_value = "(not provided)"
        elif value == "":
            formatted_value = "(empty)"
        else:
            formatted_value = str(value)

        lines.append(f"{field}: {formatted_value}")

    return "\n".join(lines)


def format_fields_for_llm(fields: List[str]) -> str:
    """
    Format field list as LLM-friendly plain text.

    Instead of: ['Institution_Name__c', 'hed__Start_Date__c', ...]

    Outputs:
    FIELDS TO VERIFY:
    - Institution_Name__c
    - hed__Start_Date__c
    - degreeLevel
    ...
    """
    if not fields:
        return "FIELDS TO VERIFY:\nNo fields specified"

    lines = ["FIELDS TO VERIFY:"]
    for field in fields:
        lines.append(f"- {field}")

    return "\n".join(lines)


def format_comparison_for_reporter(comparison_output: str) -> str:
    """
    Ensure comparison output is formatted clearly for the reporter LLM.
    The comparator output may already be readable, but we add headers for clarity.
    """
    return f"""COMPARISON ANALYSIS RESULTS:
---
{comparison_output}
---
Please synthesize the above analysis into a final report."""
