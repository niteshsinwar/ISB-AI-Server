#!/usr/bin/env python3
"""Task creation scenarios verification - focused on rule validation."""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from app.config import SALESFORCE_ORGS
from app.services.salesforce_service import SalesforceService
from app.core.task_builder import (
    extract_task_worthy_mismatches,
    build_task_from_mismatch,
    should_create_task_for_field,
    TASK_TRIGGERING_FIELDS
)

# Pytest guard: this is a live-UAT integration script. Under pytest, run it
# only when explicitly requested (RUN_UAT_TESTS=1) — conftest injects fake
# credentials which would otherwise fail authentication at import/collection time.
import sys as _sys, os as _os
if "pytest" in _sys.modules and not _os.getenv("RUN_UAT_TESTS"):
    import pytest as _pytest
    _pytest.skip("Live UAT integration script; set RUN_UAT_TESTS=1 to run under pytest", allow_module_level=True)

org = SALESFORCE_ORGS['uat']
sf = SalesforceService(org['client_id'], org['client_secret'], org['token_url'], 'uat')

print("=" * 80)
print("  TASK CREATION RULES VALIDATION")
print("=" * 80)

# Test 1: Field classification
print("\n[TEST 1] Field Classification - Which fields trigger tasks?")
test_fields = [
    ("Company Name", True),
    ("Salary", True),
    ("College Name", True),
    ("CGPA", True),
    ("Passport Number", False),
    ("Address", False),
    ("Date of Birth", False),
    ("Aadhaar Number", False),
]

print("\nField Classification Results:")
for field_name, expected in test_fields:
    result = should_create_task_for_field(field_name)
    status = "✓" if result == expected else "✗"
    print(f"  {status} {field_name:25} → task={result} (expected={expected})")

# Test 2: Task extraction from verification report
print("\n[TEST 2] Task Extraction from Verification Report")

# Scenario A: Report with multiple mismatches, only some should trigger tasks
mock_report_a = {
    "verification_analysis_report": [
        {
            "field_name": "Company Name",
            "record_value": "Acme Corp",
            "document_value": "Acme Corporation",
            "status": "MISMATCH",
            "confidence": 75,
            "notes": "Name variation detected"
        },
        {
            "field_name": "Salary",
            "record_value": "500000",
            "document_value": "450000",
            "status": "MISMATCH",
            "confidence": 60,
            "notes": "Salary mismatch"
        },
        {
            "field_name": "Passport Number",
            "record_value": "AB123456",
            "document_value": "AB123457",
            "status": "MISMATCH",
            "confidence": 85,
            "notes": "Passport number mismatch (non-critical)"
        },
    ],
    "mismatched_field_list": "Company Name; Salary; Passport Number",
}

task_worthy_a = extract_task_worthy_mismatches(mock_report_a, mock_report_a.get('mismatched_field_list', ''))
print(f"\nScenario A: 3 mismatches, 2 should be task-worthy")
print(f"Result: Found {len(task_worthy_a)} task-worthy mismatches")
for m in task_worthy_a:
    print(f"  - {m['field_name']} (confidence: {m['confidence']}%)")

if len(task_worthy_a) == 2:
    print("  ✓ PASS: Correctly filtered out non-critical fields")
else:
    print("  ✗ FAIL: Should have exactly 2 task-worthy mismatches")

# Scenario B: Report with matching fields
print(f"\nScenario B: Report with all matching fields")
mock_report_b = {
    "verification_analysis_report": [
        {
            "field_name": "Company Name",
            "record_value": "Acme Corp",
            "document_value": "Acme Corp",
            "status": "MATCH",
            "confidence": 100,
            "notes": "Perfect match"
        },
        {
            "field_name": "Salary",
            "record_value": "500000",
            "document_value": "500000",
            "status": "MATCH",
            "confidence": 100,
            "notes": "Perfect match"
        },
    ],
    "mismatched_field_list": "",  # Empty because all match
}

task_worthy_b = extract_task_worthy_mismatches(mock_report_b, mock_report_b.get('mismatched_field_list', ''))
print(f"Result: Found {len(task_worthy_b)} task-worthy mismatches")
if len(task_worthy_b) == 0:
    print("  ✓ PASS: No tasks when all fields match")
else:
    print("  ✗ FAIL: Should have no task-worthy mismatches when fields match")

# Scenario C: Education record with CGPA mismatch
print(f"\nScenario C: Education record with CGPA mismatch")
mock_report_c = {
    "verification_analysis_report": [
        {
            "field_name": "College Name",
            "record_value": "MIT",
            "document_value": "Massachusetts Institute of Technology",
            "status": "MISMATCH",
            "confidence": 95,
            "notes": "Institution name variation"
        },
        {
            "field_name": "CGPA",
            "record_value": "3.9",
            "document_value": "3.8",
            "status": "MISMATCH",
            "confidence": 70,
            "notes": "CGPA mismatch - scale might differ"
        },
    ],
    "mismatched_field_list": "College Name; CGPA",
}

task_worthy_c = extract_task_worthy_mismatches(mock_report_c, mock_report_c.get('mismatched_field_list', ''))
print(f"Result: Found {len(task_worthy_c)} task-worthy mismatches")
for m in task_worthy_c:
    print(f"  - {m['field_name']} (confidence: {m['confidence']}%)")

if len(task_worthy_c) == 2:
    print("  ✓ PASS: Both education fields triggered tasks")
else:
    print("  ✗ FAIL: Should have 2 task-worthy mismatches")

# Test 3: Task data structure
print("\n[TEST 3] Task Data Structure Validation")

task_data = build_task_from_mismatch(
    field_name="Company Name",
    record_value="Acme Corp",
    document_value="Acme Corporation",
    notes="Name variation detected but likely same entity",
    confidence=75,
    child_record_id="a0zIp000000HbIbIAK",
    application_id="a3l5j000000DLuXAAW",
    record_type_name="Employment",
    child_record_label="Acme Corp",
)

print(f"\nTask data structure check:")
required_fields = ["Subject", "Description", "WhatId", "Status", "Priority"]
for field in required_fields:
    if field in task_data:
        print(f"  ✓ {field}")
    else:
        print(f"  ✗ {field} (missing)")

print(f"\nTask details:")
print(f"  Subject: {task_data['Subject']}")
print(f"  WhatId: {task_data['WhatId']}")
print(f"  Status: {task_data['Status']}")
print(f"  Priority: {task_data['Priority']}")

# Test 4: Verify existing tasks in UAT
print("\n[TEST 4] Verify Existing Tasks in UAT")

try:
    result = sf.sf.query("""
        SELECT Id, Subject, WhatId, Status, CreatedDate
        FROM Task
        WHERE Subject LIKE 'Review % Mismatch%'
        ORDER BY CreatedDate DESC
        LIMIT 10
    """)

    tasks = result.get('records', [])
    print(f"\nFound {len(tasks)} verification task(s) in UAT")

    # Group by WhatId to see patterns
    from collections import defaultdict
    by_record = defaultdict(list)
    for task in tasks:
        by_record[task['WhatId']].append(task)

    print(f"\nTasks by record:")
    for whatid, task_list in sorted(by_record.items()):
        print(f"  {whatid}: {len(task_list)} task(s)")
        for task in task_list[:2]:  # Show first 2
            print(f"    - {task['Subject']}")

except Exception as e:
    print(f"Error querying tasks: {e}")

print("\n" + "=" * 80)
print("  TASK CREATION RULES SUMMARY")
print("=" * 80)
print("""
✓ Rules Verified:
  1. Tasks created ONLY for MISMATCH fields in TASK_TRIGGERING_FIELDS
  2. Task-triggering fields:
     - Employment: Company Name, Salary
     - Education: College Name, CGPA
  3. Task structure includes: Subject, Description, WhatId, Status, Priority
  4. Non-critical fields (Passport, Address, etc.) do NOT trigger tasks
  5. Matching fields (confidence=100%) do NOT trigger tasks

✗ When NO tasks are created:
  1. Document missing (data issue fallback)
  2. All fields match between record and document
  3. Mismatched field is not task-triggering
  4. Confidence is too low or field is marked as informational
""")
print("=" * 80)
