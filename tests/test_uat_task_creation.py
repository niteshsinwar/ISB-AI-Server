#!/usr/bin/env python3
"""UAT test: Verify task creation for verification mismatches."""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from app.config import SALESFORCE_ORGS
from app.services.salesforce_service import SalesforceService
from app.core.task_builder import extract_task_worthy_mismatches, build_task_from_mismatch

# Pytest guard: this is a live-UAT integration script. Under pytest, run it
# only when explicitly requested (RUN_UAT_TESTS=1) — conftest injects fake
# credentials which would otherwise fail authentication at import/collection time.
import sys as _sys, os as _os
if "pytest" in _sys.modules and not _os.getenv("RUN_UAT_TESTS"):
    import pytest as _pytest
    _pytest.skip("Live UAT integration script; set RUN_UAT_TESTS=1 to run under pytest", allow_module_level=True)

org = SALESFORCE_ORGS['uat']
sf = SalesforceService(org['client_id'], org['client_secret'], org['token_url'], 'uat')

print("=" * 70)
print("  UAT TEST: Task Creation for Verification Mismatches")
print("=" * 70)

# Find an employment record with known mismatches
try:
    result = sf.sf.query("""
        SELECT Id, Name, OwnerId
        FROM AI_Server_Job__c
        LIMIT 5
    """)

    if not result.get('records'):
        print("\n✗ No employment records found in UAT")
        sys.exit(1)

    employment_log = result['records'][0]
    employment_log_id = employment_log['Id']
    owner_id = employment_log.get('OwnerId')

    print(f"\n✓ Found employment record: {employment_log_id}")
    print(f"  Owner: {owner_id}")

except Exception as e:
    print(f"\n✗ Error querying employment records: {e}")
    sys.exit(1)

# Simulate a verification report with mismatches
mock_report = {
    "verification_analysis_report": [
        {
            "field_name": "Company Name",
            "record_value": "Acme Corp",
            "document_value": "Acme Corporation",
            "status": "MISMATCH",
            "confidence": 65,
            "notes": "Name variation detected but likely same entity"
        },
        {
            "field_name": "Salary",
            "record_value": "500000",
            "document_value": "400000",
            "status": "MISMATCH",
            "confidence": 45,
            "notes": "Salary mismatch - record shows higher amount"
        },
    ],
    "mismatched_field_list": "Company Name; Salary",
    "overall_feedback": "Mismatches detected requiring review",
    "confidence_range": 55,
}

print("\n--- Testing Task Creation Logic ---")

# Extract task-worthy mismatches
task_worthy = extract_task_worthy_mismatches(mock_report, mock_report.get('mismatched_field_list'))
print(f"\n✓ Found {len(task_worthy)} task-worthy mismatches:")
for m in task_worthy:
    print(f"  - {m['field_name']} (confidence: {m['confidence']}%)")

# Build and create tasks
if task_worthy and owner_id:
    print(f"\n--- Creating Tasks in UAT ---")
    tasks_created = []

    for mismatch in task_worthy:
        task_data = build_task_from_mismatch(
            field_name=mismatch['field_name'],
            record_value=mismatch['record_value'],
            document_value=mismatch['document_value'],
            notes=mismatch['notes'],
            confidence=mismatch['confidence'],
            dci_id=employment_log_id,
            application_id="test-app",
            record_type_name="Employment",
        )

        print(f"\n  Creating task for: {mismatch['field_name']}")
        print(f"    Subject: {task_data['Subject']}")

        try:
            task_id = sf.create_verification_task(employment_log_id, task_data, owner_id)
            if task_id:
                tasks_created.append(task_id)
                print(f"    ✓ Task created: {task_id}")
            else:
                print(f"    ✗ Failed to create task (no owner_id)")
        except Exception as e:
            print(f"    ✗ Error creating task: {e}")

    # Verify tasks were created
    if tasks_created:
        print(f"\n--- Verifying Tasks in Salesforce ---")
        try:
            # Extract actual task IDs from OrderedDict responses
            actual_task_ids = []
            for task_resp in tasks_created:
                if isinstance(task_resp, dict) and 'id' in task_resp:
                    actual_task_ids.append(task_resp['id'])
                else:
                    actual_task_ids.append(str(task_resp))

            if actual_task_ids:
                task_ids_str = "', '".join(actual_task_ids)
                result = sf.sf.query(f"""
                    SELECT Id, Subject, Description, WhatId, WhoId, Status, Priority
                    FROM Task
                    WHERE Id IN ('{task_ids_str}')
                """)

                print(f"\n✓ Found {len(result.get('records', []))} tasks in Salesforce:")
                for task in result.get('records', []):
                    print(f"\n  Task: {task['Id']}")
                    print(f"    Subject: {task['Subject']}")
                    print(f"    Status: {task['Status']}")
                    print(f"    Priority: {task['Priority']}")
                    print(f"    WhatId: {task['WhatId']}")
                    desc_preview = (task['Description'] or '')[:100].replace('\n', ' ')
                    print(f"    Description: {desc_preview}...")

        except Exception as e:
            print(f"\n✗ Error verifying tasks: {e}")
    else:
        print("\n✗ No tasks were created")
else:
    print(f"\n⚠ Cannot create tasks: task_worthy={bool(task_worthy)}, owner_id={bool(owner_id)}")

print("\n" + "=" * 70)
print("  TEST COMPLETE")
print("=" * 70)
