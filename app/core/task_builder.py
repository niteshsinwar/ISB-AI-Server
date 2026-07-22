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
    child_record_id: str,
    application_id: str,
    record_type_name: str = "Verification",
    child_record_label: str = None,
) -> Dict[str, Any]:
    """
    Build a Salesforce Task record for a verification mismatch.

    The Task's WhatId ("Related To") points at the parent **Application** so
    that every mismatch task for an applicant rolls up under one record. Because
    the specific child record (Employment/Education log) is therefore NOT the
    WhatId, its type + Id (+ a human-readable label when available) are baked
    into the Subject and Description so a reviewer can still identify exactly
    which child record and which check the task is about.

    Args:
        field_name: Name of the mismatched field
        record_value: Value in Salesforce record
        document_value: Value extracted from document
        notes: LLM reasoning/notes
        confidence: Confidence percentage
        child_record_id: Employment/Education log record ID (for identification only)
        application_id: Application ID — becomes the Task WhatId
        record_type_name: Type of record (Employment, Education, etc.)
        child_record_label: Human-readable child name (e.g. employer/college), optional

    Returns:
        Dict with Task fields ready for Salesforce
    """
    label_part = f" - {child_record_label}" if child_record_label else ""
    subject = f"Review {field_name} Mismatch - {record_type_name}{label_part}"

    child_line = child_record_label or "N/A"
    description = f"""
VERIFICATION MISMATCH DETECTED

Field: {field_name}
Record Type: {record_type_name}
Application: {application_id}
Related {record_type_name} Record: {child_line} (Id: {child_record_id})

Salesforce Value: {record_value or 'NOT PROVIDED'}
Document Value: {document_value or 'NOT FOUND'}
LLM Confidence: {confidence}%

LLM Notes: {notes}

ACTION REQUIRED:
- Open the {record_type_name} record above (Id: {child_record_id})
- Verify the correct value from the document
- Update the Salesforce record if needed
- Close this task once resolved
""".strip()

    return {
        "Subject": subject,
        "Description": description,
        "WhatId": application_id,  # parent Application — one rollup point per applicant
        # OwnerId is set by the caller when an assignee resolves; if none does,
        # the task is still created and Salesforce defaults it to the integration user.
        "Status": "Open",
        "Priority": "High",
        "ActivityDate": None,  # Today's date
    }


_MATCH_STATUSES = {"MATCH", "MATCHED", "PASS", "PASSED", "VERIFIED"}


def extract_task_worthy_mismatches(
    verification_report: Dict[str, Any],
    mismatched_field_list: str = "",
) -> List[Dict[str, Any]]:
    """
    Extract task-worthy mismatches from the verification report.

    Reads each row's own `status` field directly — it does NOT re-parse
    `mismatched_field_list` for gating. That string is free-form text meant
    for human/UI display; historical reports have embedded "field:reason"
    pairs in it (confirmed in prod data), which silently defeated an exact
    `field in field_names` string match here and meant task creation never
    fired for those records even though the field names themselves would
    have matched `should_create_task_for_field`. `mismatched_field_list` is
    now unused for logic and kept only for backward-compatible call sites.

    Returns list of {field_name, record_value, document_value, notes, confidence}
    for fields that should trigger task creation.
    """
    report_rows = verification_report.get("verification_analysis_report", [])
    if not report_rows:
        return []

    task_worthy = []
    for row in report_rows:
        field = row.get("field_name", "")
        status = str(row.get("status") or "").strip().upper()
        if status in _MATCH_STATUSES:
            continue
        if should_create_task_for_field(field):
            task_worthy.append({
                "field_name": field,
                "record_value": row.get("record_value"),
                "document_value": row.get("document_value"),
                "notes": row.get("notes", ""),
                "confidence": row.get("confidence", 0),
            })

    logger.info(f"Found {len(task_worthy)} task-worthy mismatches: {[m['field_name'] for m in task_worthy]}")
    return task_worthy
