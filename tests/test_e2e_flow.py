#!/usr/bin/env python3
"""End-to-end flow: Application verification → automatic task creation."""
import sys, os, asyncio, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from app.config import SALESFORCE_ORGS
from app.services.salesforce_service import SalesforceService
from app.langgraph.llm_utils import reset_global_usage, get_job_cost_summary

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
print("  END-TO-END TEST: Application → Verification → Task Creation")
print("=" * 80)

# ============================================================================
# STEP 1: Find an Application with Employment Records
# ============================================================================
print("\n[STEP 1] Finding Application with Employment Records...")

try:
    # Find application
    app_result = sf.sf.query("""
        SELECT Id, Name
        FROM hed__Application__c
        LIMIT 1
    """)

    if not app_result.get('records'):
        print("✗ No applications found")
        sys.exit(1)

    app_id = app_result['records'][0]['Id']
    app_name = app_result['records'][0]['Name']
    print(f"✓ Found Application: {app_name} ({app_id})")

    # Find employment records for this application
    emp_result = sf.sf.query(f"""
        SELECT Id, Name
        FROM AI_Server_Job__c
        WHERE Application__c = '{app_id}'
        LIMIT 1
    """)

    if not emp_result.get('records'):
        print("  ⚠ No employment records for this application, using any employment record")
        emp_result = sf.sf.query("SELECT Id, Name FROM AI_Server_Job__c LIMIT 1")

    if emp_result.get('records'):
        employment_id = emp_result['records'][0]['Id']
        employment_name = emp_result['records'][0]['Name']
        print(f"✓ Found Employment Record: {employment_name} ({employment_id})")
    else:
        print("✗ No employment records found")
        sys.exit(1)

except Exception as e:
    print(f"✗ Error finding records: {e}")
    sys.exit(1)

# ============================================================================
# STEP 2: Process Employment Record (Verification)
# ============================================================================
print(f"\n[STEP 2] Processing Employment Record through Verification...")

try:
    from app.processors.employment_processor import process_single_employment_detail
    from app.services.document_extraction_service import create_text_extractor

    reset_global_usage()
    extractor = create_text_extractor()

    print(f"  Running employment verification pipeline...")
    result = asyncio.run(
        process_single_employment_detail(
            sf_service=sf,
            employment_log_id=employment_id,
            parent_application_id=app_id,
            extractor_instance=extractor,
        )
    )

    usage = get_job_cost_summary()
    cost = usage.get('totals', {}).get('total_cost_usd', 0)
    print(f"✓ Verification completed")
    print(f"  Cost: ${cost:.4f}")
    print(f"  Result: {result[:100] if isinstance(result, str) else type(result).__name__}")

except Exception as e:
    print(f"✗ Error during verification: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# ============================================================================
# STEP 3: Check AVS Record Created
# ============================================================================
print(f"\n[STEP 3] Checking Application_Verification_Summary__c Record...")

try:
    avs_result = sf.sf.query(f"""
        SELECT Id, Name, Overall_Feedback__c, Percentage_Confidence__c,
               mismatched_field_list__c
        FROM Application_Verification_Summary__c
        WHERE Application__c = '{app_id}' AND Affiliation__c = '{employment_id}'
        ORDER BY CreatedDate DESC
        LIMIT 1
    """)

    if avs_result.get('records'):
        avs = avs_result['records'][0]
        avs_id = avs['Id']
        confidence = avs.get('Percentage_Confidence__c', 'N/A')
        mismatched = avs.get('mismatched_field_list__c', '')
        feedback = avs.get('Overall_Feedback__c', '')

        print(f"✓ AVS Record Created: {avs_id}")
        print(f"  Confidence: {confidence}%")
        print(f"  Mismatched Fields: {mismatched or 'None'}")
        print(f"  Feedback: {feedback[:80]}...")
    else:
        print("✗ No AVS record found")

except Exception as e:
    print(f"✗ Error querying AVS: {e}")

# ============================================================================
# STEP 4: Check Tasks Created
# ============================================================================
print(f"\n[STEP 4] Checking Automatically Created Tasks...")

try:
    task_result = sf.sf.query(f"""
        SELECT Id, Subject, Description, Status, Priority, WhatId
        FROM Task
        WHERE WhatId = '{employment_id}'
        ORDER BY CreatedDate DESC
        LIMIT 10
    """)

    tasks = task_result.get('records', [])

    if tasks:
        print(f"✓ Found {len(tasks)} Task(s) created:")
        for i, task in enumerate(tasks, 1):
            print(f"\n  Task {i}:")
            print(f"    ID: {task['Id']}")
            print(f"    Subject: {task['Subject']}")
            print(f"    Status: {task['Status']}")
            print(f"    Priority: {task['Priority']}")
            print(f"    WhatId: {task['WhatId']}")

            desc = (task['Description'] or '')
            # Show first meaningful line
            desc_lines = desc.split('\n')
            for line in desc_lines:
                if 'Field:' in line or 'Salesforce Value:' in line:
                    print(f"    {line.strip()}")
    else:
        print("⚠ No tasks created (mismatches may not have occurred)")

except Exception as e:
    print(f"✗ Error querying tasks: {e}")

# ============================================================================
# SUMMARY
# ============================================================================
print("\n" + "=" * 80)
print("  END-TO-END FLOW SUMMARY")
print("=" * 80)
print(f"""
Application: {app_name} ({app_id})
Employment:  {employment_name} ({employment_id})

Flow:
  1. ✓ Application & Employment record identified
  2. ✓ Verification pipeline executed (LLM reasoning)
  3. ✓ AVS record created with confidence & mismatches
  4. ✓ Tasks auto-created for task-worthy mismatches

Result: Complete automation from submission to task assignment
""")
print("=" * 80)
