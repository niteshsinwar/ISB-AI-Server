import logging
import json
from typing import Any, Dict, Optional
from langchain_google_genai import ChatGoogleGenerativeAI


import requests

from app.config import GEMINI_PRICING, GEMINI_DEFAULT_PRICING, LONG_CONTEXT_THRESHOLD

logger = logging.getLogger(__name__)

# Global usage accumulator with cost tracking
_GLOBAL_TOKEN_USAGE = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "successful_requests": 0,
    "total_cost_usd": 0.0,
    "cost_breakdown": []  # List of per-request costs
}

def get_model_pricing(model_name: str) -> Dict[str, float]:
    """Get pricing for a specific model, with fallback to default."""
    model_key = model_name.lower()
    for key in GEMINI_PRICING:
        if key in model_key or model_key in key:
            return GEMINI_PRICING[key]
    return GEMINI_DEFAULT_PRICING

def calculate_cost(prompt_tokens: int, completion_tokens: int, model_name: str) -> Dict[str, float]:
    """
    Calculate cost for a single API call.

    Args:
        prompt_tokens: Number of input/prompt tokens
        completion_tokens: Number of output/completion tokens (includes thinking tokens)
        model_name: Name of the model used

    Returns:
        Dict with input_cost, output_cost, and total_cost in USD
    """
    pricing = get_model_pricing(model_name)
    long_context = prompt_tokens > LONG_CONTEXT_THRESHOLD

    input_rate = pricing["input_long_per_1m"] if long_context else pricing["input_per_1m"]
    output_rate = pricing["output_per_1m"]

    input_cost = (prompt_tokens / 1_000_000) * input_rate
    output_cost = (completion_tokens / 1_000_000) * output_rate
    total_cost = input_cost + output_cost

    return {
        "input_cost": round(input_cost, 6),
        "output_cost": round(output_cost, 6),
        "total_cost": round(total_cost, 6),
        "model": model_name,
        "pricing_used": {
            "input_rate_per_1m": input_rate,
            "output_rate_per_1m": output_rate
        }
    }

# Store original request method
_original_request = requests.Session.request

def _intercept_google_usage(self, method, url, *args, **kwargs):
    """
    Intercept Google Gemini API calls to track token usage globally.
    Handles both streaming and non-streaming responses.
    """
    # Execute the request first
    response = _original_request(self, method, url, *args, **kwargs)
    
    try:
        # Check if it's a Gemini API call
        is_google = "generativelanguage.googleapis.com" in url
        
        if is_google and method.upper() == "POST":
            try:
                data = response.json()
                usage = None
                # Extract model name from URL - supports all Gemini model versions
                # Order matters: check more specific patterns first
                # STRICT POLICY: Only support Gemini 2.5 Flash
                # Default to 2.5 flash, verify if URL matches (it should)
                model_name = "gemini-2.5-flash"
                if "gemini-2.5-flash" not in url:
                    # Log warning but proceed as 2.5 flash (or could be 2.5-flash-lite)
                    pass

                # Handle streaming response (returns a list of chunks)
                if isinstance(data, list):
                    # For streaming, usage metadata is typically in the last chunk
                    for chunk in reversed(data):
                        if isinstance(chunk, dict) and "usageMetadata" in chunk:
                            usage = chunk.get("usageMetadata", {})
                            break
                # Handle non-streaming response (returns a dict)
                elif isinstance(data, dict):
                    usage = data.get("usageMetadata", {})

                if usage:
                    # DEBUG: Log FULL raw usageMetadata from Gemini for validation
                    logger.info(f"[GEMINI_RAW_USAGE] Full usageMetadata: {json.dumps(usage, indent=2)}")

                    # Known fields per ACTUAL Gemini API response (validated 2026-02-06):
                    # Total = promptTokenCount + candidatesTokenCount + thoughtsTokenCount
                    # candidatesTokenCount does NOT include thinking - they are separate
                    known_fields = {
                        'promptTokenCount',           # Input tokens (text + images combined)
                        'candidatesTokenCount',       # Output tokens (NOT including thinking)
                        'totalTokenCount',            # prompt + candidates + thoughts
                        'thoughtsTokenCount',         # Thinking/reasoning tokens (SEPARATE from candidates)
                        'cachedContentTokenCount',    # Cached content tokens
                        'toolUsePromptTokenCount',    # Tool use tokens
                        'promptTokensDetails',        # Breakdown by modality [{modality:1=text,2=image, tokenCount}]
                        'cacheTokensDetails',         # Cached breakdown by modality
                    }
                    actual_fields = set(usage.keys())
                    unknown_fields = actual_fields - known_fields
                    if unknown_fields:
                        logger.warning(f"[GEMINI_RAW_USAGE] UNKNOWN FIELDS DETECTED: {unknown_fields}")

                    p_tokens = usage.get("promptTokenCount", 0)
                    c_tokens = usage.get("candidatesTokenCount", 0)  # Output only, NOT including thinking
                    total = usage.get("totalTokenCount", 0)
                    thinking_tokens = usage.get("thoughtsTokenCount", 0)  # Thinking tokens (SEPARATE)
                    cached_tokens = usage.get("cachedContentTokenCount", 0)
                    tool_tokens = usage.get("toolUsePromptTokenCount", 0)

                    # Extract modality breakdown for detailed logging
                    prompt_details = usage.get("promptTokensDetails", [])
                    text_input = sum(d.get("tokenCount", 0) for d in prompt_details if d.get("modality") == 1)
                    image_input = sum(d.get("tokenCount", 0) for d in prompt_details if d.get("modality") == 2)

                    # Validate: total = prompt + candidates + thoughts (verified against actual API)
                    expected_total = p_tokens + c_tokens + thinking_tokens
                    if total != expected_total and total > 0:
                        logger.warning(f"[GEMINI_RAW_USAGE] TOTAL MISMATCH: reported={total}, calculated={expected_total} "
                                      f"(prompt={p_tokens}, candidates={c_tokens}, thoughts={thinking_tokens})")

                    # For cost: prompt (input) + candidates + thinking (output)
                    # Thinking tokens are billed at output rate per Google pricing
                    total_output = c_tokens + thinking_tokens
                    cost_info = calculate_cost(p_tokens, total_output, model_name)

                    # Detailed logging with modality breakdown
                    modality_info = f"[text={text_input}, image={image_input}]" if prompt_details else ""
                    logger.info(f"[Network Interceptor] Captured [{model_name}]: "
                               f"prompt={p_tokens}{modality_info}, candidates={c_tokens}, thoughts={thinking_tokens}, cached={cached_tokens} "
                               f"| Total={total} | Cost: ${cost_info['total_cost']:.6f}")

                    # Use global variable directly
                    _GLOBAL_TOKEN_USAGE["prompt_tokens"] += p_tokens
                    _GLOBAL_TOKEN_USAGE["completion_tokens"] += total_output  # candidates + thinking
                    _GLOBAL_TOKEN_USAGE["total_tokens"] += total
                    _GLOBAL_TOKEN_USAGE["successful_requests"] += 1
                    _GLOBAL_TOKEN_USAGE["total_cost_usd"] += cost_info["total_cost"]
                    _GLOBAL_TOKEN_USAGE["cost_breakdown"].append({
                        "model": model_name,
                        "prompt_tokens": p_tokens,
                        "prompt_text_tokens": text_input,      # Text portion of input
                        "prompt_image_tokens": image_input,    # Image portion of input (258 per tile)
                        "candidates_tokens": c_tokens,         # Output (NOT including thinking)
                        "thinking_tokens": thinking_tokens,    # Thinking/reasoning tokens
                        "cached_tokens": cached_tokens,
                        "total_output": total_output,          # candidates + thinking (billable output)
                        "total_cost": cost_info["total_cost"],
                        "gemini_total": total                  # Gemini's reported total for validation
                    })
            except Exception:
                pass # Silent fail on parsing if not JSON
    except Exception as e:
        logger.warning(f"Interceptor error: {e}")

    return response

# Apply the patch
requests.Session.request = _intercept_google_usage
# ---------------------------------------------------

def reset_global_usage():
    """Reset the global token usage accumulator in-place."""
    _GLOBAL_TOKEN_USAGE.clear()
    _GLOBAL_TOKEN_USAGE.update({
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "successful_requests": 0,
        "total_cost_usd": 0.0,
        "cost_breakdown": []
    })

def get_job_cost_summary() -> Dict[str, Any]:
    """Get a summary of token usage and costs for the current job."""
    global _GLOBAL_TOKEN_USAGE

    # Calculate per-model breakdown
    model_costs = {}
    for entry in _GLOBAL_TOKEN_USAGE.get("cost_breakdown", []):
        model = entry.get("model", "unknown")
        if model not in model_costs:
            model_costs[model] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "thinking_tokens": 0,
                "total_cost": 0.0,
                "request_count": 0
            }
        model_costs[model]["prompt_tokens"] += entry.get("prompt_tokens", 0)
        model_costs[model]["completion_tokens"] += entry.get("completion_tokens", 0)
        model_costs[model]["thinking_tokens"] += entry.get("thinking_tokens", 0)
        model_costs[model]["total_cost"] += entry.get("total_cost", 0)
        model_costs[model]["request_count"] += 1

    return {
        "totals": {
            "prompt_tokens": _GLOBAL_TOKEN_USAGE["prompt_tokens"],
            "completion_tokens": _GLOBAL_TOKEN_USAGE["completion_tokens"],
            "total_tokens": _GLOBAL_TOKEN_USAGE["total_tokens"],
            "successful_requests": _GLOBAL_TOKEN_USAGE["successful_requests"],
            "total_cost_usd": round(_GLOBAL_TOKEN_USAGE.get("total_cost_usd", 0), 6)
        },
        "per_model": model_costs,
        "detailed_breakdown": _GLOBAL_TOKEN_USAGE.get("cost_breakdown", [])
    }

def initialize_llm(model: str, temperature: float, api_key: str) -> Optional[ChatGoogleGenerativeAI]:
    """Initialize the LLM with the given model and temperature."""
    try:
        # Note: Token counting is now handled by the global network interceptor
        
        llm = ChatGoogleGenerativeAI(
            model=model,
            temperature=temperature,
            google_api_key=api_key,
            model_kwargs={"response_mime_type": "application/json"},
            transport="rest"
        )

        logger.info(f"LLM initialized with model: {model}")
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
