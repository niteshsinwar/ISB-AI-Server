"""Deterministic regression tests for bugs found in the July 2026 reliability audit.

Every test here runs offline (no Salesforce, no Gemini). Each one pins a bug
that previously shipped silently:

1. RecommenderState dropped email_match/mobile_match between LangGraph nodes,
   so fraud alerts never reached the final report (confidence stayed 100).
2. task_builder only matched prettified field names, never the real Apex
   payload keys (employerName, School/Institute/Campus, SF CGPA/Percentage).
3. report_builder's allowed_fields filter silently deleted prompt-mandated
   synthetic rows ("Payslip Recency", "CGPA Scale") and any row whose casing
   differed from the record key.
4. Family-relationship probability was extracted by naive substring search,
   so "highly unlikely" was classified as High.
5. Phone comparison was exact-string, so "+91 98765 43210" != "9876543210".
"""
import pytest

from app.langgraph.recommender_graph import (
    RecommenderGraphOrchestrator,
    _extract_probability,
    _normalize_phone,
)
from app.core.task_builder import (
    should_create_task_for_field,
    extract_task_worthy_mismatches,
)
from app.langgraph.report_builder import build_verification_report


# ---------------------------------------------------------------------------
# 1. Recommender fraud detection must survive the LangGraph state schema
# ---------------------------------------------------------------------------

def test_recommender_fraud_flags_reach_final_report():
    """Matching email+mobile between recommender and applicant must zero confidence."""
    recommender = {
        "First_Name__c": "John",
        "Last_Name__c": "Smith",  # different last name -> no LLM family node
        "Email__c": "shared@corpmail.com",  # corporate -> no LLM email node
        "MobilePhone__c": "+91 98765-43210",
        "Status__c": "Submitted",
    }
    applicant = {
        "First_Name__c": "Jane",
        "Last_Name__c": "Doe",
        "Email": "shared@corpmail.com",
        "MobilePhone": "9876543210",
    }
    report = RecommenderGraphOrchestrator(recommender, [], applicant).run()

    assert "email_cross_match_fraud" in report["mismatched_field_list"]
    assert "mobile_cross_match_fraud" in report["mismatched_field_list"]
    assert "FRAUD ALERT" in report["overall_feedback"]
    assert int(report["confidence_range"]) == 0
    # Report must render as an HTML table (consistent with other AVS reports)
    assert "<table" in report["field_comparison_summary"]


_SUFFICIENT_RESPONSE = [{
    "Question__c": "How do you know the applicant?",
    "Answer__c": "I have supervised this applicant directly for four years.",
}]


def test_recommender_clean_path_full_confidence():
    recommender = {
        "First_Name__c": "Amit",
        "Last_Name__c": "Verma",
        "Email__c": "amit@company.com",
        "MobilePhone__c": "+91 11111 11111",
        "Status__c": "Submitted",
    }
    applicant = {
        "First_Name__c": "Jane",
        "Last_Name__c": "Doe",
        "Email": "jane@example.com",
        "MobilePhone": "9876543210",
    }
    report = RecommenderGraphOrchestrator(recommender, _SUFFICIENT_RESPONSE, applicant).run()
    assert int(report["confidence_range"]) == 100
    assert report["mismatched_field_list"] == ""


def test_recommender_not_submitted_penalized():
    recommender = {
        "First_Name__c": "Amit",
        "Last_Name__c": "Verma",
        "Email__c": "amit@company.com",
        "MobilePhone__c": "1",
        "Status__c": "Sent to Core Engine",
    }
    applicant = {
        "First_Name__c": "Jane",
        "Last_Name__c": "Doe",
        "Email": "jane@example.com",
        "MobilePhone": "2",
    }
    report = RecommenderGraphOrchestrator(recommender, _SUFFICIENT_RESPONSE, applicant).run()
    assert int(report["confidence_range"]) == 80
    assert "not been submitted" in report["overall_feedback"]
    assert "not_submitted" in report["mismatched_field_list"]


def test_recommender_insufficient_response_content_penalized():
    """Apex parity: no substantive answer (>= 4 words) must be flagged."""
    recommender = {
        "First_Name__c": "Amit",
        "Last_Name__c": "Verma",
        "Email__c": "amit@company.com",
        "MobilePhone__c": "1",
        "Status__c": "Submitted",
    }
    applicant = {"First_Name__c": "Jane", "Last_Name__c": "Doe",
                 "Email": "jane@example.com", "MobilePhone": "2"}
    # No responses at all
    report = RecommenderGraphOrchestrator(recommender, [], applicant).run()
    assert int(report["confidence_range"]) == 80
    assert "insufficient_response_content" in report["mismatched_field_list"]
    # A 3-word answer is still insufficient
    short = [{"Question__c": "Q", "Answer__c": "Very good candidate"}]
    report2 = RecommenderGraphOrchestrator(recommender, short, applicant).run()
    assert "insufficient_response_content" in report2["mismatched_field_list"]


def test_recommender_declared_family_relationship_flagged():
    """Apex parity: Other_Relationship__c containing a family keyword."""
    recommender = {
        "First_Name__c": "Amit",
        "Last_Name__c": "Verma",
        "Email__c": "amit@company.com",
        "MobilePhone__c": "1",
        "Status__c": "Submitted",
        "Other_Relationship__c": "He is my Uncle",
    }
    applicant = {"First_Name__c": "Jane", "Last_Name__c": "Doe",
                 "Email": "jane@example.com", "MobilePhone": "2"}
    report = RecommenderGraphOrchestrator(recommender, _SUFFICIENT_RESPONSE, applicant).run()
    assert "family_relationship_declared" in report["mismatched_field_list"]
    assert int(report["confidence_range"]) == 70  # 100 - 30


def test_recommender_handles_null_fields():
    """Null/missing values must not crash the deterministic nodes."""
    report = RecommenderGraphOrchestrator(
        {"Status__c": None, "Email__c": None},
        [],
        None,  # applicant details unavailable
    ).run()
    assert report["confidence_range"] is not None


# ---------------------------------------------------------------------------
# 2. Task creation must fire on the real Apex payload field names
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field_name", [
    "employerName",                # employment Apex payload key
    "compensation",                # employment Apex payload key
    "School/Institute/Campus",     # education Apex payload key
    "SF CGPA/Percentage",          # education Apex payload key
    "Company Name",                # prettified variants must keep working
    "Salary",
    "College Name",
    "CGPA",
])
def test_task_triggers_on_real_and_pretty_names(field_name):
    assert should_create_task_for_field(field_name), field_name


@pytest.mark.parametrize("field_name", [
    "Passport Number", "Birthdate", "Gender", "jobTitle", "startDate",
    "endDate", "applicantName", "SF Field of Study", "Payslip Recency",
])
def test_task_not_triggered_on_non_financial_fields(field_name):
    assert not should_create_task_for_field(field_name), field_name


def test_extract_task_worthy_mismatches_with_real_education_report():
    report = {
        "verification_analysis_report": [
            {"field_name": "School/Institute/Campus", "record_value": "ABC College",
             "document_value": "XYZ University", "status": "MISMATCH",
             "confidence": 60, "notes": "type mismatch"},
            {"field_name": "SF CGPA/Percentage", "record_value": "8.1",
             "document_value": "7.4", "status": "MISMATCH",
             "confidence": 60, "notes": "cgpa differs"},
            {"field_name": "SF Full Name", "record_value": "A", "document_value": "A",
             "status": "MATCH", "confidence": 100, "notes": ""},
        ]
    }
    worthy = extract_task_worthy_mismatches(
        report, "School/Institute/Campus;SF CGPA/Percentage"
    )
    assert {m["field_name"] for m in worthy} == {"School/Institute/Campus", "SF CGPA/Percentage"}


def test_extract_task_worthy_handles_na_and_empty():
    assert extract_task_worthy_mismatches({"verification_analysis_report": []}, "N/A") == []
    assert extract_task_worthy_mismatches({}, "") == []
    assert extract_task_worthy_mismatches({}, None) == []


# ---------------------------------------------------------------------------
# 3. report_builder must keep synthetic rows and tolerate case differences
# ---------------------------------------------------------------------------

def _row(field, status="MISMATCH", confidence=0):
    return {
        "field_name": field,
        "record_value": "x",
        "document_value": "y",
        "status": status,
        "confidence": confidence,
        "notes": "n",
    }


def test_payslip_recency_row_survives_allowed_fields_filter():
    report = build_verification_report(
        [_row("employerName", "MATCH", 100), _row("Payslip Recency", "MISMATCH", 0)],
        critical_field_names={"employerName", "Payslip Recency"},
        allowed_fields=["employerName", "compensation"],
        extra_allowed_fields={"Payslip Recency", "Payslip"},
    )
    assert "Payslip Recency" in report["mismatched_field_list"]
    # critical row at 0 confidence -> deduction of 50
    assert report["confidence_range"] == 50


def test_cgpa_scale_and_semester_rows_survive_education_filter():
    report = build_verification_report(
        [
            _row("SF CGPA/Percentage", "MATCH", 100),
            _row("Number of Semesters", "MISMATCH", 70),
            _row("CGPA Scale", "MISMATCH", 60),
        ],
        allowed_fields=["SF CGPA/Percentage"],
        extra_allowed_fields={"Number of Semesters", "CGPA Scale"},
    )
    names = report["mismatched_field_list"]
    assert "Number of Semesters" in names
    assert "CGPA Scale" in names


def test_allowed_fields_filter_is_case_insensitive():
    """LLM echoing 'Employer Name' for record key 'employerName' must not be dropped."""
    report = build_verification_report(
        [_row("Employer Name", "MISMATCH", 60)],
        allowed_fields=["employerName"],
    )
    assert "Employer Name" in report["mismatched_field_list"]


def test_hallucinated_metadata_rows_still_dropped():
    with pytest.raises(ValueError):
        build_verification_report(
            [_row("Last Modified Date")],
            allowed_fields=["employerName"],
        )


# ---------------------------------------------------------------------------
# 4. Family probability extraction must be anchored, not substring-based
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("analysis,expected", [
    ("Family relationship probability: Low. Tone is highly professional.", "Low"),
    ("1. Family Relationship Probability (Low/Medium/High): **High**", "High"),
    ("It is highly unlikely; probability is low overall", "Low"),
    ("Probability of family relationship is MEDIUM based on evidence", "Medium"),
    ("There is a high probability the recommender is a parent.", "High"),
    ("no signal at all", "Unknown"),
    ("", "Unknown"),
    (None, "Unknown"),
])
def test_probability_extraction(analysis, expected):
    assert _extract_probability(analysis) == expected


# ---------------------------------------------------------------------------
# 5. Phone normalization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("a,b,equal", [
    ("+91 98765-43210", "9876543210", True),
    ("098765 43210", "9876543210", True),
    ("9876543210", "9876543211", False),
    ("", "", True),  # both empty -> equal, but matcher requires non-empty to flag
    (None, "9876543210", False),
])
def test_phone_normalization_pairs(a, b, equal):
    assert (_normalize_phone(a) == _normalize_phone(b)) is equal


# ---------------------------------------------------------------------------
# 6. Extraction-failure sentinels must be reported as failures, never
#    "verified" as if they were document content (SF transparency)
# ---------------------------------------------------------------------------

def test_concurrent_admission_is_atomic():
    """Regression: 18 simultaneous analyze requests were all accepted because
    duplicate/capacity checks were separate check-then-act calls. try_admit
    must admit exactly one job per application and never exceed capacity."""
    import asyncio
    from app.core.job_manager import JobManager

    async def scenario():
        jm = JobManager()

        # 10 simultaneous requests for the SAME application -> exactly 1 admitted
        results = await asyncio.gather(*(jm.try_admit("APP000000000001AAA", 15) for _ in range(10)))
        assert results.count("admitted") == 1, results
        assert results.count("duplicate") == 9, results

        # 25 simultaneous requests for DISTINCT applications, capacity 15
        jm2 = JobManager()
        ids = [f"APP{i:012d}AAA" for i in range(25)]
        results2 = await asyncio.gather(*(jm2.try_admit(i, 15) for i in ids))
        assert results2.count("admitted") == 15, results2
        assert results2.count("queue_full") == 10, results2

        # cancel_admission releases the reservation for reuse
        await jm.cancel_admission("APP000000000001AAA")
        assert await jm.try_admit("APP000000000001AAA", 15) == "admitted"

    asyncio.get_event_loop().run_until_complete(scenario()) if False else asyncio.run(scenario())


def test_salesforce_id_validation_rejects_hostile_and_malformed_input():
    from app.core.processing_utils import is_valid_salesforce_id

    # Valid shapes
    assert is_valid_salesforce_id("a3lIp0000005j3O")          # 15 alnum
    assert is_valid_salesforce_id("a3lIp0000005j3OIAQ")       # 18 alnum
    # Length-correct but hostile content must be rejected (previously reached SOQL)
    assert not is_valid_salesforce_id("aaaaaaaaaaaaaa'")      # 15 chars with quote
    assert not is_valid_salesforce_id("' OR 1=1 --xxxxxxx")   # 18 chars injection
    assert not is_valid_salesforce_id("a3lIp0000005j3O IA")   # embedded space
    # Wrong lengths / types
    assert not is_valid_salesforce_id("abc")
    assert not is_valid_salesforce_id("a3lIp0000005j3OIA")    # 17
    assert not is_valid_salesforce_id("")
    assert not is_valid_salesforce_id(None)
    assert not is_valid_salesforce_id(123456789012345)


def test_extraction_failure_sentinels_detected():
    from app.core.processing_utils import detect_extraction_failure

    # Error prose returned by the extractor is a failure, not document text
    assert detect_extraction_failure(
        "## Document Processing Error\n\nThis PDF document could not be processed."
    ) is not None
    assert detect_extraction_failure("No content could be extracted from this PDF.") is not None
    # Empty / whitespace / None are failures
    assert detect_extraction_failure("") is not None
    assert detect_extraction_failure("   \n ") is not None
    assert detect_extraction_failure(None) is not None
    # Real document text is not a failure
    assert detect_extraction_failure("## Page 1\n\nUniversity of Delhi\nCGPA: 8.2") is None
    # A document merely *mentioning* errors mid-text is not a failure
    assert detect_extraction_failure("Payslip March 2025\nError correction note: ...") is None
