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


class RecommenderState(TypedDict):
    """State for recommender verification graph."""
    # Submission check
    submission_status: Optional[str]
    is_submitted: Optional[bool]

    # Email classification
    email: Optional[str]
    email_type: Optional[str]

    # Name matching
    recommender_first_name: Optional[str]
    recommender_last_name: Optional[str]
    applicant_first_name: Optional[str]
    applicant_last_name: Optional[str]
    first_name_match: Optional[bool]
    last_name_match: Optional[bool]
    potential_family_flag: Optional[bool]

    # Cross-contact fraud checks (recommender vs applicant)
    email_match: Optional[bool]
    mobile_match: Optional[bool]

    # LLM analysis results
    personal_email_reason: Optional[str]
    family_relationship_probability: Optional[str]
    family_relationship_analysis: Optional[str]

    # Report output
    field_comparison_summary: Optional[str]
    overall_feedback: Optional[str]
    confidence_range: Optional[str]
    mismatched_field_list: Optional[str]
    verification_analysis_report: Optional[List[Dict[str, Any]]]

    # Findings accumulator
    findings: List[Dict[str, Any]]
