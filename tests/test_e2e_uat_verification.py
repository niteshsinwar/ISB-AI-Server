"""
End-to-End UAT Verification Test Suite
Covers scenarios from 'AI verification fixes.xlsx':
- Employment: company name, salary, payslip type, currency
- Education: college name, CGPA, field of study, scale verification
- Recommender: submission status, email type, name matching, family detection
- Task creation: only when record + doc exist with mismatch
"""
import asyncio
import sys
import os
import json
import logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.salesforce_service import SalesforceService
from app.config import SALESFORCE_ORGS, RECOMMENDER_DETAIL_OBJECT_API_NAME

# Pytest guard: script-style E2E suite (functions take params, orchestrated by
# main()); under pytest the params are misread as fixtures. Run via python directly.
import sys as _sys, os as _os
if "pytest" in _sys.modules and not _os.getenv("RUN_UAT_TESTS"):
    import pytest as _pytest
    _pytest.skip("Live UAT E2E script; run with python tests/test_e2e_uat_verification.py", allow_module_level=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger("E2E_UAT_TEST")


def get_uat_service() -> SalesforceService:
    """Connect to UAT Salesforce."""
    org_config = SALESFORCE_ORGS["uat"]
    if not all(org_config.values()):
        raise RuntimeError("UAT Salesforce credentials not configured in .env")
    return SalesforceService(
        client_id=org_config['client_id'],
        client_secret=org_config['client_secret'],
        token_url=org_config['token_url'],
        org_alias="uat"
    )


async def find_test_applications(sf: SalesforceService) -> dict:
    """Find applications with various record types for comprehensive testing."""
    logger.info("=" * 80)
    logger.info("PHASE 1: FINDING TEST APPLICATIONS IN UAT")
    logger.info("=" * 80)

    # Find applications with recommender details (submitted)
    recommender_apps = await asyncio.to_thread(
        sf.sf.query,
        """
        SELECT Application__c, Id, First_Name__c, Last_Name__c, Email__c, Status__c
        FROM ISB_Recommender_Details__c
        WHERE Status__c = 'Submitted'
        ORDER BY CreatedDate DESC
        LIMIT 5
        """
    )
    logger.info(f"Found {len(recommender_apps.get('records', []))} submitted recommender details")

    # Find applications with employment records
    employment_apps = await asyncio.to_thread(
        sf.sf.query,
        """
        SELECT Application__c, Id
        FROM ISB_Employment_Log__c
        WHERE Type_of_Employment__c = 'Full-Time'
        ORDER BY CreatedDate DESC
        LIMIT 5
        """
    )
    logger.info(f"Found {len(employment_apps.get('records', []))} employment records")

    # Find applications with education records
    education_apps = await asyncio.to_thread(
        sf.sf.query,
        """
        SELECT Application__c, Id
        FROM ISB_Education_Log__c
        WHERE Education_History__r.Degree_Level__c IN ('Bachelors', 'Master')
        ORDER BY CreatedDate DESC
        LIMIT 5
        """
    )
    logger.info(f"Found {len(education_apps.get('records', []))} education records")

    return {
        "recommender": recommender_apps.get('records', []),
        "employment": employment_apps.get('records', []),
        "education": education_apps.get('records', [])
    }


async def test_recommender_verification(sf: SalesforceService, recommender_records: list):
    """
    Test recommender verification pipeline:
    - Submission status check
    - Email type classification (personal vs corporate)
    - Name matching (first/last name vs applicant)
    - Personal email reason analysis (LLM)
    - Family relationship detection (LLM)
    """
    logger.info("\n" + "=" * 80)
    logger.info("PHASE 2: RECOMMENDER VERIFICATION TESTING")
    logger.info("=" * 80)

    if not recommender_records:
        logger.warning("No recommender records found for testing")
        return

    from app.processors.recommender_processor import process_single_recommender_detail
    from app.langgraph.llm_utils import reset_global_usage

    results = []
    for i, rec in enumerate(recommender_records[:3]):  # Test up to 3
        recommender_id = rec['Id']
        application_id = rec['Application__c']
        logger.info(f"\n--- Test {i+1}: Recommender {recommender_id} ---")
        logger.info(f"  Name: {rec.get('First_Name__c')} {rec.get('Last_Name__c')}")
        logger.info(f"  Email: {rec.get('Email__c')}")
        logger.info(f"  Status: {rec.get('Status__c')}")
        logger.info(f"  Application: {application_id}")

        try:
            result = await process_single_recommender_detail(
                sf_service=sf,
                recommender_detail_id=recommender_id,
                application_id=application_id,
                item_index=i + 1
            )
            logger.info(f"  Result: {result}")
            results.append({"id": recommender_id, "status": "success", "result": result})
        except Exception as e:
            logger.error(f"  ERROR: {e}")
            results.append({"id": recommender_id, "status": "error", "error": str(e)})

    # Verify AVS records were created
    logger.info("\n--- Verifying AVS Records ---")
    for rec in recommender_records[:3]:
        app_id = rec['Application__c']
        avs_check = await asyncio.to_thread(
            sf.sf.query,
            f"""
            SELECT Id, Name, Overall_Feedback__c, Percentage_Confidence__c,
                   Mismatched_Field_List__c, Verification_Analysis_Report__c
            FROM Application_Verification_Summary__c
            WHERE Application__c = '{app_id}'
              AND Name LIKE 'Recommender%'
            ORDER BY CreatedDate DESC LIMIT 1
            """
        )
        if avs_check.get('records'):
            avs = avs_check['records'][0]
            logger.info(f"  AVS Found: {avs['Name']}")
            logger.info(f"    Confidence: {avs.get('Percentage_Confidence__c')}%")
            logger.info(f"    Feedback: {(avs.get('Overall_Feedback__c') or '')[:100]}")
            logger.info(f"    Mismatched: {avs.get('Mismatched_Field_List__c')}")
        else:
            logger.warning(f"  NO AVS record found for application {app_id}")

    return results


async def test_employment_verification(sf: SalesforceService, employment_records: list):
    """
    Test employment verification pipeline:
    - Company name verification
    - Salary verification
    - Payslip type detection (bank statement vs payslip)
    - Task creation for mismatches
    """
    logger.info("\n" + "=" * 80)
    logger.info("PHASE 3: EMPLOYMENT VERIFICATION TESTING")
    logger.info("=" * 80)

    if not employment_records:
        logger.warning("No employment records found for testing")
        return

    from app.processors.employment_processor import process_single_employment_detail

    results = []
    for i, rec in enumerate(employment_records[:2]):  # Test 2
        employment_id = rec['Id']
        application_id = rec['Application__c']
        logger.info(f"\n--- Test {i+1}: Employment {employment_id} ---")
        logger.info(f"  Application: {application_id}")

        try:
            result = await process_single_employment_detail(
                sf_service=sf,
                employment_log_id=employment_id,
                parent_application_id=application_id,
                item_index=i + 1
            )
            logger.info(f"  Result: {result}")
            results.append({"id": employment_id, "status": "success", "result": result})
        except Exception as e:
            logger.error(f"  ERROR: {e}")
            results.append({"id": employment_id, "status": "error", "error": str(e)})

    # Check if tasks were created
    logger.info("\n--- Checking Tasks Created ---")
    for rec in employment_records[:2]:
        task_check = await asyncio.to_thread(
            sf.sf.query,
            f"""
            SELECT Id, Subject, Status, Priority, CreatedDate
            FROM Task
            WHERE WhatId = '{rec['Id']}'
              AND Subject LIKE 'Review%Mismatch%'
            ORDER BY CreatedDate DESC LIMIT 5
            """
        )
        tasks = task_check.get('records', [])
        if tasks:
            logger.info(f"  Tasks for {rec['Id']}: {len(tasks)} found")
            for t in tasks:
                logger.info(f"    - {t['Subject']} [{t['Status']}] ({t['Priority']})")
        else:
            logger.info(f"  No mismatch tasks for {rec['Id']} (fields matched or no document)")

    return results


async def test_education_verification(sf: SalesforceService, education_records: list):
    """
    Test education verification pipeline:
    - College name verification
    - CGPA/percentage verification
    - Task creation for mismatches
    """
    logger.info("\n" + "=" * 80)
    logger.info("PHASE 4: EDUCATION VERIFICATION TESTING")
    logger.info("=" * 80)

    if not education_records:
        logger.warning("No education records found for testing")
        return

    from app.processors.education_processor import process_single_education_history_detail

    results = []
    for i, rec in enumerate(education_records[:2]):  # Test 2
        education_id = rec['Id']
        application_id = rec['Application__c']
        logger.info(f"\n--- Test {i+1}: Education {education_id} ---")
        logger.info(f"  Application: {application_id}")

        try:
            result = await process_single_education_history_detail(
                sf_service=sf,
                education_log_id=education_id,
                parent_application_id=application_id,
                item_index=i + 1
            )
            logger.info(f"  Result: {result}")
            results.append({"id": education_id, "status": "success", "result": result})
        except Exception as e:
            logger.error(f"  ERROR: {e}")
            results.append({"id": education_id, "status": "error", "error": str(e)})

    # Check tasks created
    logger.info("\n--- Checking Tasks Created ---")
    for rec in education_records[:2]:
        task_check = await asyncio.to_thread(
            sf.sf.query,
            f"""
            SELECT Id, Subject, Status, Priority, CreatedDate
            FROM Task
            WHERE WhatId = '{rec['Id']}'
              AND Subject LIKE 'Review%Mismatch%'
            ORDER BY CreatedDate DESC LIMIT 5
            """
        )
        tasks = task_check.get('records', [])
        if tasks:
            logger.info(f"  Tasks for {rec['Id']}: {len(tasks)} found")
            for t in tasks:
                logger.info(f"    - {t['Subject']} [{t['Status']}] ({t['Priority']})")
        else:
            logger.info(f"  No mismatch tasks for {rec['Id']} (fields matched or no document)")

    return results


async def test_full_application_trigger(sf: SalesforceService, application_id: str):
    """
    Test complete application trigger (like SF would call it).
    Processes all record types for one application.
    """
    logger.info("\n" + "=" * 80)
    logger.info(f"PHASE 5: FULL APPLICATION TRIGGER - {application_id}")
    logger.info("=" * 80)

    from app.config import RELATED_RECORD_PROCESSING_CONFIG
    import importlib

    for config in RELATED_RECORD_PROCESSING_CONFIG:
        target_type = config["target_record_type"]
        retrieval_method = config["retrieval_method"]
        logger.info(f"\n--- Processing: {target_type} (priority {config['priority']}) ---")

        try:
            if retrieval_method == "self":
                record_ids = [application_id]
            else:
                record_ids = await asyncio.to_thread(
                    sf.get_directly_related_record_ids,
                    parent_record_id=application_id,
                    child_object_api_name=target_type,
                    lookup_field_on_child_to_parent=config["lookup_on_child_to_parent"],
                    filtering_criteria=config.get("filtering_criteria"),
                    order_by=config.get("order_by"),
                    limit=config.get("limit")
                )

            logger.info(f"  Found {len(record_ids)} records")

            if not record_ids:
                logger.info(f"  Skipping - no records found")
                continue

            # Dynamically import and call processor
            module = importlib.import_module(config["processor_module"])
            processor_fn = getattr(module, config["processor_function_name"])

            for idx, record_id in enumerate(record_ids[:3]):
                logger.info(f"  Processing record {idx+1}/{len(record_ids)}: {record_id}")
                try:
                    result = await processor_fn(
                        sf_service=sf,
                        **_build_processor_kwargs(config, record_id, application_id, idx + 1)
                    )
                    logger.info(f"    Result: {result}")
                except Exception as e:
                    logger.error(f"    ERROR: {e}")

        except Exception as e:
            logger.error(f"  Fetch error: {e}")


def _build_processor_kwargs(config: dict, record_id: str, application_id: str, item_index: int) -> dict:
    """Build kwargs for processor function based on config."""
    target = config["target_record_type"]

    if target == "hed__Application__c":
        return {
            "application_id": record_id,
            "parent_application_id": application_id,
            "application_object_api_name": "hed__Application__c",
            "item_index": item_index,
        }
    elif target == "ISB_Education_Log__c":
        return {"education_log_id": record_id, "parent_application_id": application_id, "item_index": item_index}
    elif target == "ISB_Employment_Log__c":
        return {"employment_log_id": record_id, "parent_application_id": application_id, "item_index": item_index}
    elif target == "hed__Test__c":
        return {"test_score_id": record_id, "parent_application_id": application_id, "item_index": item_index}
    elif target == "DocumentChecklistItem":
        return {"resume_dci_id": record_id, "parent_application_id": application_id, "item_index": item_index}
    elif target == "ISB_Recommender_Details__c":
        return {"recommender_detail_id": record_id, "application_id": application_id, "item_index": item_index}
    else:
        return {"record_id": record_id, "application_id": application_id}


async def verify_avs_results(sf: SalesforceService, application_id: str):
    """Check all AVS records for a given application after processing."""
    logger.info("\n" + "=" * 80)
    logger.info(f"PHASE 6: VERIFY AVS RESULTS FOR {application_id}")
    logger.info("=" * 80)

    avs_records = await asyncio.to_thread(
        sf.sf.query,
        f"""
        SELECT Id, Name, Overall_Feedback__c, Percentage_Confidence__c,
               Mismatched_Field_List__c, CreatedDate
        FROM Application_Verification_Summary__c
        WHERE Application__c = '{application_id}'
        ORDER BY CreatedDate DESC
        """
    )

    records = avs_records.get('records', [])
    logger.info(f"Total AVS records: {len(records)}")

    for rec in records:
        confidence = rec.get('Percentage_Confidence__c') or 'N/A'
        logger.info(f"\n  [{rec['Name']}]")
        logger.info(f"    Confidence: {confidence}%")
        logger.info(f"    Feedback: {(rec.get('Overall_Feedback__c') or 'None')[:120]}")
        logger.info(f"    Mismatched Fields: {rec.get('Mismatched_Field_List__c') or 'None'}")
        logger.info(f"    Created: {rec.get('CreatedDate')}")

    # Check tasks created
    tasks = await asyncio.to_thread(
        sf.sf.query,
        f"""
        SELECT Id, Subject, Status, Priority, WhatId, CreatedDate
        FROM Task
        WHERE Subject LIKE 'Review%Mismatch%'
        ORDER BY CreatedDate DESC
        LIMIT 20
        """
    )
    task_records = tasks.get('records', [])
    logger.info(f"\n  Total Mismatch Tasks (recent): {len(task_records)}")
    for t in task_records[:10]:
        logger.info(f"    - {t['Subject']} | WhatId={t['WhatId']} | {t['Status']} | {t['Priority']}")


async def main():
    """Run comprehensive E2E UAT verification."""
    logger.info("=" * 80)
    logger.info("ISB AI SERVER - COMPREHENSIVE E2E UAT VERIFICATION")
    logger.info(f"Started at: {datetime.now().isoformat()}")
    logger.info("=" * 80)

    # Connect to UAT
    try:
        sf = get_uat_service()
        logger.info(f"Connected to UAT: {sf.instance_url}")
    except Exception as e:
        logger.error(f"Failed to connect to UAT: {e}")
        return

    # Find test data
    test_data = await find_test_applications(sf)

    # Run recommender tests
    recommender_results = await test_recommender_verification(sf, test_data["recommender"])

    # Run employment tests
    employment_results = await test_employment_verification(sf, test_data["employment"])

    # Run education tests
    education_results = await test_education_verification(sf, test_data["education"])

    # Full application trigger test (use first recommender's application)
    if test_data["recommender"]:
        app_id = test_data["recommender"][0]["Application__c"]
        await test_full_application_trigger(sf, app_id)
        await verify_avs_results(sf, app_id)

    # Summary
    logger.info("\n" + "=" * 80)
    logger.info("SUMMARY")
    logger.info("=" * 80)
    logger.info(f"Recommender tests: {len(recommender_results or [])} executed")
    logger.info(f"Employment tests: {len(employment_results or [])} executed")
    logger.info(f"Education tests: {len(education_results or [])} executed")
    logger.info(f"Completed at: {datetime.now().isoformat()}")


if __name__ == "__main__":
    asyncio.run(main())
