from typing import TypedDict, Dict, Any, List, Optional

class VerificationState(TypedDict):
    """
    Shared state for verification graphs.
    """
    # Inputs
    record_data: Dict[str, Any]
    document_text: str
    verifiable_fields: List[str]
    
    # Intermediate Outputs (Raw LLM responses)
    comparison_task_output: Optional[str]
    
    # Final Outputs
    final_report: Optional[Dict[str, Any]]
    
    # Traceability & Cost
    usage_metrics: Dict[str, Any]
    model_config: Dict[str, str]
