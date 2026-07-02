"""Task creation logic for verification mismatches requiring manual review."""
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# Fields that trigger automatic task creation when mismatched
TASK_TRIGGERING_FIELDS = {
    "company_name", "employer_name", "organization",
    "salary", "compensation", "ctc",
    "college_name", "institute", "institution",
    "cgpa", "gpa", "percentage",
}

# Normalize field names to task-triggering names
FIELD_NAME_NORMALIZATION = {
    "Company Name": "company_name",
    "Employer Name": "company_name",
    "Organization": "company_name",
    "Compensation": "salary",
    "Salary": "salary",
    "CTC": "salary",
    "College Name": "college_name",
    "Institute": "college_name",
    "Institution": "college_name",
    "Institution Name": "college_name",
    "Institution_Name__c": "college_name",
    "CGPA": "cgpa",
    "GPA": "cgpa",
    "Percentage": "cgpa",
    "hed__Compensation__c": "salary",
    "hed__College_Name__c": "college_name",
    "hed__CGPA__c": "cgpa",
}


def should_create_task_for_field(field_name: str) -> bool:
    """Check if a field mismatch should trigger a task."""
    normalized = FIELD_NAME_NORMALIZATION.get(field_name, field_name.lower())
    return normalized in TASK_TRIGGERING_FIELDS


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
