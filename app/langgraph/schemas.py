from typing import Literal
from pydantic import BaseModel, Field, constr

class ValidatedCrewReport(BaseModel):
    """
    Standard validation model for verification reports (Education, Employment, Application, Test Score).
    """
    field_comparison_summary: constr(min_length=1)
    overall_feedback: constr(min_length=1)
    confidence_range: int = Field(..., ge=0, le=100)
    mismatched_field_list: constr(min_length=1)
    verification_status: Literal["Passed", "Failed", "Needs Review"] = "Needs Review"

class ValidatedResumeReport(BaseModel):
    """
    Validation model for Resume screening.
    """
    status: Literal["Accepted", "Not Verified"]
    reason: constr(min_length=1)
