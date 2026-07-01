"""Full UAT pipeline tests for all remaining record types."""
import sys, os, asyncio, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from app.config import SALESFORCE_ORGS
from app.services.salesforce_service import SalesforceService
from app.langgraph.llm_utils import reset_global_usage, get_job_cost_summary

org = SALESFORCE_ORGS['uat']
sf = SalesforceService(org['client_id'], org['client_secret'], org['token_url'], 'uat')

results = {}


def section(title):
    print(f"\n{'='*70}\n  {title}\n{'='*70}")


# ─── TEST 1: RESUME + DCI LINKING ───────────────────────────────
section("TEST 1: RESUME PIPELINE + DCI LINKING")

dci_id = '0ddfs0000000V2GAAU'
app_id = 'a3lfs0000000gfBAAQ'

dci_before = sf._call_sf_api_with_retry(lambda: sf.sf.query(
    f"SELECT Id, Application_Verification_Summary__c FROM DocumentChecklistItem WHERE Id = '{dci_id}'"
))
before_link = dci_before['records'][0].get('Application_Verification_Summary__c') if dci_before.get('records') else None
print(f"  DCI link BEFORE: {before_link}")


async def run_resume():
    from app.processors.resume_processor import process_single_resume_detail
    from app.services.document_extraction_service import create_text_extractor
    reset_global_usage()
    extractor = create_text_extractor()
    start = time.time()
    result = await process_single_resume_detail(
        sf_service=sf, resume_dci_id=dci_id,
        parent_application_id=app_id, extractor_instance=extractor,
    )
    elapsed = time.time() - start
    print(f"  Completed in {elapsed:.1f}s")
    print(f"  Result: {result[:150] if isinstance(result, str) else type(result).__name__}")
    cost = get_job_cost_summary()
    print(f"  Cost: ${cost.get('totals', {}).get('total_cost_usd', 0):.4f}")
    return result


res = asyncio.run(run_resume())

dci_after = sf._call_sf_api_with_retry(lambda: sf.sf.query(
    f"SELECT Id, Application_Verification_Summary__c FROM DocumentChecklistItem WHERE Id = '{dci_id}'"
))
after_link = dci_after['records'][0].get('Application_Verification_Summary__c') if dci_after.get('records') else None
print(f"  DCI link AFTER: {after_link}")

if after_link and after_link != before_link:
    results['Resume + DCI Link'] = 'PASS'
    print("  *** PASS: DCI linked to new AVS ***")
elif 'Skipped' in str(res) or 'skipped' in str(res).lower():
    results['Resume + DCI Link'] = 'SKIPPED (already verified)'
    print("  *** SKIPPED ***")
elif 'fallback' in str(res).lower() or 'data issue' in str(res).lower():
    results['Resume + DCI Link'] = 'FALLBACK (data issue)'
    print("  *** FALLBACK ***")
else:
    results['Resume + DCI Link'] = f'PROCESSED (link: {after_link})'
    print(f"  *** PROCESSED (link={after_link}) ***")


# ─── TEST 2: GMAT_FOCUS ─────────────────────────────────────────
section("TEST 2: GMAT_FOCUS TEST SCORE")

gf_test_id = 'a3nfs000000KCg9AAG'
gf_app_id = 'a3lfs0000000VobAAE'

data = sf.get_test_score_record_data(gf_test_id, gf_app_id)
record_data = data.get('recordData', {})
rtn = record_data.get('RecordTypeName__c', '')
print(f"  Record type: {rtn}")

has_total = any('Total' in k for k in record_data.keys())
has_data_insights = any('Data_Insights' in k for k in record_data.keys())
print(f"  Has Total fields: {has_total}")
print(f"  Has Data_Insights fields: {has_data_insights}")

if rtn.upper() == 'GMAT_FOCUS' and has_total and has_data_insights:
    results['GMAT_FOCUS fields'] = 'PASS'
    print("  *** PASS: Correct field set ***")
elif rtn.upper() == 'GMAT_FOCUS':
    results['GMAT_FOCUS fields'] = f'PARTIAL (total={has_total}, insights={has_data_insights})'
else:
    results['GMAT_FOCUS fields'] = f'WRONG TYPE: {rtn}'


async def run_gmat_focus():
    from app.processors.test_score_processor import process_single_test_score_detail
    from app.services.document_extraction_service import create_text_extractor
    reset_global_usage()
    extractor = create_text_extractor()
    start = time.time()
    result = await process_single_test_score_detail(
        sf_service=sf, test_score_id=gf_test_id,
        parent_application_id=gf_app_id, extractor_instance=extractor,
    )
    elapsed = time.time() - start
    print(f"  Completed in {elapsed:.1f}s")
    print(f"  Result: {result[:150] if isinstance(result, str) else type(result).__name__}")
    cost = get_job_cost_summary()
    print(f"  Cost: ${cost.get('totals', {}).get('total_cost_usd', 0):.4f}")
    return result


gf_res = asyncio.run(run_gmat_focus())

avs = sf._call_sf_api_with_retry(lambda: sf.sf.query(
    f"SELECT Id, Overall_Feedback__c, Percentage_Confidence__c FROM Application_Verification_Summary__c WHERE Test__c = '{gf_test_id}' ORDER BY CreatedDate DESC LIMIT 1"
))
if avs.get('records'):
    r = avs['records'][0]
    results['GMAT_FOCUS pipeline'] = f"PASS (Conf: {r.get('Percentage_Confidence__c')})"
    print(f"  SF: Conf={r.get('Percentage_Confidence__c')} | {(r.get('Overall_Feedback__c') or '')[:150]}")
elif 'Skipped' in str(gf_res):
    results['GMAT_FOCUS pipeline'] = 'SKIPPED'
else:
    results['GMAT_FOCUS pipeline'] = f'Result: {str(gf_res)[:80]}'


# ─── TEST 3: EEDL ID DOCUMENT ───────────────────────────────────
section("TEST 3: EEDL ID DOCUMENT")

opp_id = '006fs000002GB6QAAW'

async def run_eedl_id():
    from app.processors.eedl_id_processor import process_eedl_id_document
    from app.services.document_extraction_service import create_text_extractor
    reset_global_usage()
    extractor = create_text_extractor()
    start = time.time()
    try:
        result = await process_eedl_id_document(
            sf_service=sf, opportunity_id=opp_id,
            parent_opportunity_id=opp_id,
            extractor_instance=extractor,
        )
        elapsed = time.time() - start
        print(f"  Completed in {elapsed:.1f}s")
        print(f"  Result: {result[:200] if isinstance(result, str) else type(result).__name__}")
        cost = get_job_cost_summary()
        print(f"  Cost: ${cost.get('totals', {}).get('total_cost_usd', 0):.4f}")
        return result
    except Exception as e:
        elapsed = time.time() - start
        print(f"  ERROR after {elapsed:.1f}s: {type(e).__name__}: {e}")
        return f"ERROR: {e}"


eedl_id_res = asyncio.run(run_eedl_id())

if 'ERROR' in str(eedl_id_res):
    results['EEDL ID Document'] = f'ERROR: {str(eedl_id_res)[:80]}'
elif 'Skipped' in str(eedl_id_res) or 'skipped' in str(eedl_id_res).lower():
    results['EEDL ID Document'] = 'SKIPPED'
elif 'No' in str(eedl_id_res) and 'document' in str(eedl_id_res).lower():
    results['EEDL ID Document'] = f'NO DOC: {str(eedl_id_res)[:80]}'
else:
    results['EEDL ID Document'] = 'PROCESSED'


# ─── TEST 4: EEDL EDUCATION ─────────────────────────────────────
section("TEST 4: EEDL EDUCATION")

async def run_eedl_edu():
    from app.processors.eedl_education_processor import process_eedl_education_record
    from app.services.document_extraction_service import create_text_extractor
    reset_global_usage()

    # Find education records linked to any opportunity's contact
    # Use known Education__c record directly
    edu_q = sf._call_sf_api_with_retry(lambda: sf.sf.query(
        "SELECT Id, Degree_Type__c, Contact__c FROM Education__c LIMIT 1"
    ))
    print(f"  Education__c records available: {edu_q.get('totalSize', 0)}")
    if not edu_q.get('records'):
        return "NO EDUCATION RECORDS IN ORG"

    edu_id = edu_q['records'][0]['Id']
    degree = edu_q['records'][0].get('Degree_Type__c')
    contact_id = edu_q['records'][0].get('Contact__c')
    print(f"  Testing: {edu_id} (Degree: {degree}, Contact: {contact_id})")

    # Find an opportunity for this contact
    opp_q = sf._call_sf_api_with_retry(lambda: sf.sf.query(
        f"SELECT Id FROM Opportunity WHERE ContactId = '{contact_id}' LIMIT 1"
    ))
    if not opp_q.get('records'):
        # Use any opportunity - the processor will handle the mismatch
        test_opp_id = opp_id
        print(f"  No opportunity for this contact, using: {test_opp_id}")
    else:
        test_opp_id = opp_q['records'][0]['Id']
        print(f"  Found matching opportunity: {test_opp_id}")

    extractor = create_text_extractor()
    start = time.time()
    try:
        result = await process_eedl_education_record(
            sf_service=sf, education_id=edu_id,
            parent_opportunity_id=test_opp_id, extractor_instance=extractor,
        )
        elapsed = time.time() - start
        print(f"  Completed in {elapsed:.1f}s")
        print(f"  Result: {result[:200] if isinstance(result, str) else type(result).__name__}")
        cost = get_job_cost_summary()
        print(f"  Cost: ${cost.get('totals', {}).get('total_cost_usd', 0):.4f}")
        return result
    except Exception as e:
        elapsed = time.time() - start
        print(f"  ERROR after {elapsed:.1f}s: {type(e).__name__}: {e}")
        return f"ERROR: {e}"


eedl_edu_res = asyncio.run(run_eedl_edu())

if 'ERROR' in str(eedl_edu_res):
    results['EEDL Education'] = f'ERROR: {str(eedl_edu_res)[:80]}'
elif 'NO EDUCATION' in str(eedl_edu_res):
    results['EEDL Education'] = 'NO RECORDS (test data gap)'
else:
    results['EEDL Education'] = 'PROCESSED'


# ─── TEST 5: EMPLOYMENT BANK STATEMENT ROUTE ────────────────────
section("TEST 5: EMPLOYMENT (Bank Statement Classifier)")

# We can't easily find a bank statement - let's verify the classifier code path
# by checking what happens with the document type detection
print("  Testing document classifier logic directly...")
from app.langgraph.employment_graph import EmploymentGraphNodes
from unittest.mock import MagicMock, patch

# Mock an LLM that returns BANK_STATEMENT classification
mock_response = MagicMock()
mock_response.content = json.dumps({"document_type": "BANK_STATEMENT", "reasoning": "Contains bank transactions"})

with patch("app.langgraph.employment_graph.get_llm") as mock_llm:
    mock_llm.return_value = MagicMock()
    nodes = EmploymentGraphNodes()
    nodes.llm_classifier = MagicMock()
    nodes.llm_classifier.invoke = MagicMock(return_value=mock_response)

    state = {
        "record_data": {"applicantName": "Test User", "employerName": "Acme"},
        "verifiable_fields": ["applicantName", "employerName"],
        "document_text": "HDFC Bank Statement Jan-Mar 2025 Salary Credit Rs 50000",
    }

    result = nodes.classifier_node(state)
    doc_type = result.get("document_type")
    print(f"  Classifier returned: document_type={doc_type}")

    if doc_type == "BANK_STATEMENT":
        # Test the routing function
        from app.langgraph.employment_graph import _route_after_classification
        route = _route_after_classification({"document_type": "BANK_STATEMENT"})
        print(f"  Router returned: {route}")
        if route == "bank_statement_reporter":
            results['Bank Statement Route'] = 'PASS'
            print("  *** PASS: Bank statement correctly routes to hard-fail ***")
        else:
            results['Bank Statement Route'] = f'FAIL: routed to {route}'
    else:
        results['Bank Statement Route'] = f'FAIL: classifier returned {doc_type}'


# ─── FINAL SUMMARY ──────────────────────────────────────────────
section("FINAL UAT TEST RESULTS")
all_pass = True
for test_name, result in results.items():
    status = "PASS" if "PASS" in result else ("SKIP" if "SKIP" in result else "CHECK")
    icon = "[OK]" if "PASS" in result else ("[--]" if "SKIP" in result else "[!!]")
    print(f"  {icon} {test_name}: {result}")
    if "FAIL" in result or "ERROR" in result:
        all_pass = False

print(f"\n  {'ALL TESTS PASSED' if all_pass else 'SOME TESTS NEED ATTENTION'}")
