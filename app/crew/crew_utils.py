# project_root/app/crew/crew_utils.py
import logging
import json
from typing import Any, Dict, Optional
from langchain_google_genai import ChatGoogleGenerativeAI

logger = logging.getLogger(__name__)

def initialize_llm(model: str, temperature: float, api_key: str, resource_manager=None) -> Optional[ChatGoogleGenerativeAI]:
    """Initialize the LLM with the given model and temperature, with optional resource tracking."""
    try:
        llm = ChatGoogleGenerativeAI(
            model=model,
            temperature=temperature,
            google_api_key=api_key,
            model_kwargs={"response_mime_type": "application/json"}
        )

        # Track LLM with resource manager for guaranteed cleanup
        if resource_manager:
            resource_manager.track_llm(llm)

        logger.info(f"LLM initialized with model: {model} (resource tracking: {resource_manager is not None})")
        return llm
    except Exception as e:
        logger.error(f"Failed to initialize LLM: {e}", exc_info=True)
        return None

def clean_and_extract_json(response_string: str) -> str:
    """Extract a valid JSON string from the response, handling formatting issues."""
    if not isinstance(response_string, str):
        return ""
    start_brace_index = response_string.find('{')
    if start_brace_index == -1:
        start_brace_index = response_string.find('[')
        if start_brace_index == -1:
            return ""
    end_brace_index = response_string.rfind('}')
    if end_brace_index == -1:
        end_brace_index = response_string.rfind(']')
    if end_brace_index == -1 or end_brace_index < start_brace_index:
        return ""
    json_str = response_string[start_brace_index:end_brace_index + 1].strip()
    for i in range(len(json_str), 0, -1):
        try:
            json.loads(json_str[:i])
            return json_str[:i]
        except json.JSONDecodeError:
            continue
    return ""

def log_error(message: str, exc_info: bool = True):
    """Log an error message with optional exception information."""
    logger.error(message, exc_info=exc_info)

# FIX: The decorator is now a simple class-based decorator that takes no arguments.
# This aligns with the new "fail-fast" strategy where we raise an exception
# instead of returning a default value.
class CrewErrorHandler:
    """Decorator to handle errors in crew execution, raising an exception on failure."""
    def __call__(self, func):
        def wrapper(*args, **kwargs):
            try:
                result = func(*args, **kwargs)
                if isinstance(result, dict) and "error" in result:
                    # If the crew's internal logic returns an error dict, raise it as an exception
                    raise ValueError(f"Crew task failed: {result['error']}")
                return result
            except Exception as e:
                # Catch any other unexpected exception during the crew's run
                log_error(f"Critical error in crew execution: {e}")
                # Re-raise the exception to be caught by the main processor, ensuring the job fails
                raise ValueError(f"Crew execution failed: {e}")
        return wrapper
