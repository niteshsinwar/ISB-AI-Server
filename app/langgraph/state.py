from typing import TypedDict, Dict, Any, List, Optional

class VerificationState(TypedDict):
    """
    Shared state for verification graphs.
    """
    # Inputs
    record_data: Dict[str, Any]
    document_text: str
    verifiable_fields: List[str]

    # Context dates (for recency checks)
    application_submission_date: Optional[str]  # ISO format date string from Application hed__Application_Date__c

    # Document classification (populated by classifier node where applicable)
    document_type: Optional[str]           # e.g. "PAYSLIP" | "BANK_STATEMENT" | "OFFER_LETTER" | "OTHER"
    document_type_reasoning: Optional[str] # One-sentence explanation from the classifier LLM

    # Intermediate Outputs (Raw LLM responses)
    comparison_task_output: Optional[str]

    # Final Outputs
    final_report: Optional[Dict[str, Any]]

    # Traceability & Cost
    usage_metrics: Dict[str, Any]
    model_config: Dict[str, str]
