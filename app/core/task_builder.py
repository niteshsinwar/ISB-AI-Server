"""Task creation logic for verification mismatches requiring manual review."""
import logging
import re
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


def _normalize_field_name(field_name: str) -> str:
    """Casefold and strip all non-alphanumerics so 'SF CGPA/Percentage',
    'employerName' and 'Employer Name' all compare consistently."""
    return re.sub(r"[^a-z0-9]", "", (field_name or "").casefold())


# Fields that trigger automatic task creation when mismatched.
# Keys are normalized (see _normalize_field_name). Includes both human-readable
# names the LLM may echo AND the exact keys the Apex payloads use:
#   Employment payload: employerName, compensation
#   Education payload:  School/Institute/Campus, SF CGPA/Percentage
TASK_TRIGGERING_FIELDS = {
    # Company name variants
    "companyname", "employername", "organization", "employer",
    # Salary variants
    "salary", "compensation", "ctc", "hedcompensationc", "annualcompensation",
    # College/institution variants
    "collegename", "institute", "institution", "institutionname",
    "institutionnamec", "hedcollegenamec", "schoolinstitutecampus",
    # CGPA/GPA variants
    "cgpa", "gpa", "percentage", "hedcgpac", "sfcgpapercentage", "gpapercentage",
}


def should_create_task_for_field(field_name: str) -> bool:
    """Check if a field mismatch should trigger a task."""
    return _normalize_field_name(field_name) in TASK_TRIGGERING_FIELDS


def build_task_from_mismatch(
    field_name: str,
    record_value: str,
    document_value: str,
    notes: str,
    confidence: int,
    dci_id: str,
    application_id: str,
    record_type_name: str = "Verification",
) -> Dict[str, Any]:
    """
    Build a Salesforce Task record for a verification mismatch.

    Args:
        field_name: Name of the mismatched field
        record_value: Value in Salesforce record
        document_value: Value extracted from document
        notes: LLM reasoning/notes
        confidence: Confidence percentage
        dci_id: DocumentChecklistItem ID (WhatId)
        application_id: Application ID (for context)
        record_type_name: Type of record (Employment, Education, etc.)

    Returns:
        Dict with Task fields ready for Salesforce
    """
    subject = f"Review {field_name} Mismatch - {record_type_name}"

    description = f"""
VERIFICATION MISMATCH DETECTED

Field: {field_name}
Record Type: {record_type_name}
Application: {application_id}

Salesforce Value: {record_value or 'NOT PROVIDED'}
Document Value: {document_value or 'NOT FOUND'}
LLM Confidence: {confidence}%

LLM Notes: {notes}

ACTION REQUIRED:
- Verify the correct value from the document
- Update the Salesforce record if needed
- Close this task once resolved
""".strip()

    return {
        "Subject": subject,
        "Description": description,
        "WhatId": dci_id,
        # WhoId will be set by caller (from assignment field)
        "Status": "Open",
        "Priority": "High",
        "ActivityDate": None,  # Today's date
    }


def extract_task_worthy_mismatches(
    verification_report: Dict[str, Any],
    mismatched_field_list: str,
) -> List[Dict[str, Any]]:
    """
    Extract task-worthy mismatches from the verification report.

    Returns list of {field_name, record_value, document_value, notes, confidence}
    for fields that should trigger task creation.
    """
    if not mismatched_field_list:
        return []

    task_worthy = []
    report_rows = verification_report.get("verification_analysis_report", [])

    # Parse mismatched_field_list (might be "field1; field2" or "field1, field2")
    field_names = [f.strip() for f in mismatched_field_list.replace(";", ",").split(",")]

    for row in report_rows:
        field = row.get("field_name", "")
        if field in field_names and should_create_task_for_field(field):
            task_worthy.append({
                "field_name": field,
                "record_value": row.get("record_value"),
                "document_value": row.get("document_value"),
                "notes": row.get("notes", ""),
                "confidence": row.get("confidence", 0),
            })

    logger.info(f"Found {len(task_worthy)} task-worthy mismatches: {[m['field_name'] for m in task_worthy]}")
    return task_worthy
