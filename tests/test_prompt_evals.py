"""Golden-document prompt evaluation harness.

Purpose: the deterministic layer is regression-locked, but a reworded prompt can
still shift LLM extraction behavior. Each test here feeds a synthetic document
with KNOWN ground truth through the real graph (real Gemini call) and asserts
the business contract — row statuses, mandatory rows, verification status —
not exact wording, so normal LLM variance passes but behavioral drift fails.

Run: RUN_LLM_EVALS=1 pytest tests/test_prompt_evals.py -v
(Skipped by default: requires a real CREW_GOOGLE_API_KEY and spends ~$0.01.)

Field names intentionally mirror the REAL Apex payload keys (verified against
UAT 2026-07-02) so these evals exercise the same contract as production.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if not os.getenv("RUN_LLM_EVALS"):
    pytest.skip(
        "LLM prompt evals: set RUN_LLM_EVALS=1 (uses real Gemini API)",
        allow_module_level=True,
    )


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", str(name or "").casefold())


def _row(report, field_name):
    """Find a report row by normalized field name."""
    for row in report.get("verification_analysis_report", []):
        if _norm(row.get("field_name")) == _norm(field_name):
            return row
    return None


def _status(row):
    return str((row or {}).get("status") or "").upper()


# ---------------------------------------------------------------------------
# EMPLOYMENT
# ---------------------------------------------------------------------------

EMPLOYMENT_RECORD = {
    "applicantName": "Rahul Kumar Sharma",
    "employerName": "Infosys Limited",
    "jobTitle": "Senior Software Engineer",
    "compensation": "1200000",
    "currency": "INR",
    "startDate": "2021-04-01",
    "endDate": None,
}

CLEAN_PAYSLIP = """
## Page 1

INFOSYS LIMITED
Payslip for the month of May 2025
Employee Name: Rahul Sharma          Employee ID: INF48291
Designation: Software Engineer       Date of Joining: 01-Apr-2021
| Earnings        | Amount (INR) |
| Basic           | 50,000       |
| HRA             | 25,000       |
| Special Allow.  | 25,000       |
| **Gross Pay**   | **1,00,000** |
| Deductions (PF, TDS) | 12,000  |
| Net Pay         | 88,000       |
"""


def test_employment_clean_payslip_passes():
    from app.langgraph.employment_graph import EmploymentGraphOrchestrator
    report = EmploymentGraphOrchestrator(
        dict(EMPLOYMENT_RECORD), CLEAN_PAYSLIP, "2025-06-15"
    ).run()

    # Name with missing middle name must still MATCH (excel rule)
    assert _status(_row(report, "applicantName")) == "MATCH", report
    assert _status(_row(report, "employerName")) == "MATCH", report
    # Gross 1,00,000 x 12 = 12,00,000 == record compensation → MATCH (gross-salary rule)
    assert _status(_row(report, "compensation")) == "MATCH", report
    assert report["verification_status"] == "Passed", report["overall_feedback"]


def test_employment_wrong_company_flags_mismatch():
    from app.langgraph.employment_graph import EmploymentGraphOrchestrator
    from app.core.task_builder import extract_task_worthy_mismatches

    doc = CLEAN_PAYSLIP.replace("INFOSYS LIMITED", "WIPRO TECHNOLOGIES")
    record = dict(EMPLOYMENT_RECORD)
    report = EmploymentGraphOrchestrator(record, doc, "2025-06-15").run()

    assert _status(_row(report, "employerName")) == "MISMATCH", report
    assert report["verification_status"] != "Passed"
    # The mismatch must be task-worthy end to end
    worthy = extract_task_worthy_mismatches(report, report["mismatched_field_list"])
    assert any(_norm(m["field_name"]) == "employername" for m in worthy), worthy


def test_employment_bank_statement_rejected_at_zero():
    from app.langgraph.employment_graph import EmploymentGraphOrchestrator
    doc = """
## Page 1
HDFC BANK - Account Statement
Account No: XXXXXX4521   IFSC: HDFC0001234
Statement period: 01-May-2025 to 31-May-2025
Opening Balance: 2,45,000.00
| Date | Description | Debit | Credit | Balance |
| 02-05 | UPI/GROCERY | 1,200 | | 2,43,800 |
| 05-05 | SALARY CREDIT INFOSYS | | 88,000 | 3,31,800 |
| 12-05 | ATM WITHDRAWAL | 10,000 | | 3,21,800 |
Closing Balance: 3,21,800.00
"""
    report = EmploymentGraphOrchestrator(dict(EMPLOYMENT_RECORD), doc, "2025-06-15").run()
    assert report["verification_status"] == "Failed", report
    assert report["confidence_range"] == 0
    assert "payslip" in report["mismatched_field_list"].lower()


def test_employment_usd_salary_matching_after_conversion_not_penalized():
    from app.langgraph.employment_graph import EmploymentGraphOrchestrator
    record = dict(EMPLOYMENT_RECORD)
    record["employerName"] = "Google LLC"
    record["compensation"] = "10400000"  # ~ $10,000/mo x 12 x 86.38 ≈ 1.036 Cr INR
    doc = """
## Page 1
GOOGLE LLC
Payslip - May 2025
Employee: Rahul Sharma    Employee ID: GGL-2211
Title: Senior Software Engineer   Start Date: Apr 2021
Gross Monthly Salary: USD 10,000.00
Deductions: USD 2,100.00
Net Pay: USD 7,900.00
"""
    report = EmploymentGraphOrchestrator(record, doc, "2025-06-15").run()
    comp = _row(report, "compensation")
    # Amounts align after conversion → currency difference alone must not penalize
    assert _status(comp) == "MATCH", comp
    assert int(comp["confidence"]) >= 90, comp


def test_employment_stale_payslip_gets_recency_row():
    from app.langgraph.employment_graph import EmploymentGraphOrchestrator
    doc = CLEAN_PAYSLIP.replace("May 2025", "August 2024")
    report = EmploymentGraphOrchestrator(dict(EMPLOYMENT_RECORD), doc, "2025-06-15").run()
    recency = _row(report, "Payslip Recency")
    assert recency is not None, "Payslip Recency row missing for a 10-month-old payslip"
    assert _status(recency) == "MISMATCH", recency


# ---------------------------------------------------------------------------
# EDUCATION
# ---------------------------------------------------------------------------

EDUCATION_RECORD = {
    "SF Full Name": "Priya Venkatesh Iyer",
    "School/Institute/Campus": "RV College of Engineering",
    "Degree/Qualification": "Bachelors",
    "degreeLevel": "Bachelors",
    "SF Field of Study": "Engineering",
    "Major/Specialization": "Computer Science",
    "SF CGPA/Percentage": "8.42",
    "SF Passing Year": "2019",
    "From": "2015-08-01",
    "To": "2019-06-30",
}

CLEAN_MARKSHEET = """
## Page 1

VISVESVARAYA TECHNOLOGICAL UNIVERSITY
RV COLLEGE OF ENGINEERING, BANGALORE
Consolidated Grade Card - Bachelor of Engineering
Branch: Computer Science and Engineering
Name of Student: Priya Iyer         USN: 1RV15CS089
Duration: 2015 - 2019 (8 Semesters)
| Semester | SGPA |
| 1 | 8.1 | | 2 | 8.3 | | 3 | 8.5 | | 4 | 8.4 |
| 5 | 8.6 | | 6 | 8.4 | | 7 | 8.5 | | 8 | 8.5 |
Final CGPA: 8.42
Class Awarded: First Class with Distinction     Year of Passing: 2019
"""


def test_education_clean_marksheet_passes():
    from app.langgraph.education_graph import EducationGraphOrchestrator
    report = EducationGraphOrchestrator(dict(EDUCATION_RECORD), CLEAN_MARKSHEET).run()

    # Middle-name difference (record has "Venkatesh") must not penalize
    assert _status(_row(report, "SF Full Name")) == "MATCH", report
    assert _status(_row(report, "School/Institute/Campus")) == "MATCH", report
    assert _status(_row(report, "SF CGPA/Percentage")) == "MATCH", report
    # Mandatory independent rows must all exist
    for required in ("Degree/Qualification", "SF Field of Study",
                     "Major/Specialization", "Number of Semesters"):
        assert _row(report, required) is not None, f"missing row: {required}"
    assert _status(_row(report, "Number of Semesters")) == "MATCH"
    assert report["verification_status"] == "Passed", report["overall_feedback"]


def test_education_wrong_cgpa_flags_critical_mismatch():
    from app.langgraph.education_graph import EducationGraphOrchestrator
    from app.core.task_builder import extract_task_worthy_mismatches

    record = dict(EDUCATION_RECORD)
    record["SF CGPA/Percentage"] = "9.10"  # doc explicitly says 8.42
    report = EducationGraphOrchestrator(record, CLEAN_MARKSHEET).run()

    assert _status(_row(report, "SF CGPA/Percentage")) == "MISMATCH", report
    assert report["verification_status"] != "Passed"
    worthy = extract_task_worthy_mismatches(report, report["mismatched_field_list"])
    assert any(_norm(m["field_name"]) == "sfcgpapercentage" for m in worthy), worthy


def test_education_odd_semesters_flagged():
    from app.langgraph.education_graph import EducationGraphOrchestrator
    doc = """
## Page 1
VISVESVARAYA TECHNOLOGICAL UNIVERSITY
RV COLLEGE OF ENGINEERING, BANGALORE
Semester Grade Cards - Bachelor of Engineering (Computer Science)
Name: Priya Iyer   USN: 1RV15CS089
| Semester | SGPA |
| 1 | 8.1 | | 2 | 8.3 | | 3 | 8.5 | | 4 | 8.4 |
| 5 | 8.6 | | 6 | 8.4 | | 7 | 8.5 |
(Individual semester marksheets, semesters 1 through 7)
"""
    report = EducationGraphOrchestrator(dict(EDUCATION_RECORD), doc).run()
    sem = _row(report, "Number of Semesters")
    assert sem is not None, "Number of Semesters row missing"
    assert _status(sem) == "MISMATCH", sem  # 7 = odd, not consolidated


def test_education_college_claim_vs_university_only_document():
    from app.langgraph.education_graph import EducationGraphOrchestrator
    doc = CLEAN_MARKSHEET.replace("RV COLLEGE OF ENGINEERING, BANGALORE\n", "")
    report = EducationGraphOrchestrator(dict(EDUCATION_RECORD), doc).run()
    # Applicant claimed a college; document only shows the university → fraud rule
    assert _status(_row(report, "School/Institute/Campus")) == "MISMATCH", report
    assert report["verification_status"] != "Passed"


# ---------------------------------------------------------------------------
# APPLICATION / PERSONAL DETAILS
# ---------------------------------------------------------------------------

APPLICATION_RECORD = {
    "Full Name": "Arjun Mehta",
    "Birthdate": "1994-11-23",
    "Gender": "Male",
    "Nationality": "Indian",
    "Passport Number": "M8823941",
    "PassportExpiryDate": "2027-03-15",
    "Aadhar Card Number": "XXXX-XXXX-7731",
    "ID Proof Type": "Aadhaar",
}


def test_application_aadhaar_excludes_passport_fields():
    from app.langgraph.application_graph import ApplicationGraphOrchestrator
    doc = """
## Page 1
भारत सरकार GOVERNMENT OF INDIA
Unique Identification Authority of India (UIDAI)
आधार AADHAAR
Name: Arjun Mehta
DOB: 23/11/1994        Gender: MALE
XXXX XXXX 7731
"""
    report = ApplicationGraphOrchestrator(dict(APPLICATION_RECORD), doc).run()

    assert _status(_row(report, "Full Name")) == "MATCH", report
    assert _status(_row(report, "Birthdate")) == "MATCH", report
    assert _status(_row(report, "Aadhar Card Number")) == "MATCH", report
    # Passport fields must be EXCLUDED when an Aadhaar was submitted (excel rule)
    assert _row(report, "Passport Number") is None, report
    assert _row(report, "PassportExpiryDate") is None, report
    assert report["verification_status"] == "Passed", report["overall_feedback"]


def test_application_passport_includes_expiry_and_drops_aadhaar():
    from app.langgraph.application_graph import ApplicationGraphOrchestrator
    doc = """
## Page 1
REPUBLIC OF INDIA - PASSPORT
Type: P   Country Code: IND   Passport No: M8823941
Surname: MEHTA          Given Names: ARJUN
Nationality: INDIAN     Sex: M
Date of Birth: 23/11/1994
Date of Issue: 16/03/2017    Date of Expiry: 15/03/2027
"""
    report = ApplicationGraphOrchestrator(dict(APPLICATION_RECORD), doc).run()

    assert _status(_row(report, "Passport Number")) == "MATCH", report
    # Regression: PassportExpiryDate used to be filtered out entirely
    expiry = _row(report, "PassportExpiryDate")
    assert expiry is not None, "PassportExpiryDate row missing for a passport document"
    assert _status(expiry) == "MATCH", expiry
    # Aadhaar must not be verified against a passport
    assert _row(report, "Aadhar Card Number") is None, report


# ---------------------------------------------------------------------------
# TEST SCORE
# ---------------------------------------------------------------------------

def test_gre_never_outputs_combined_total_and_ignores_leaked_fields():
    from app.langgraph.test_score_graph import TestScoreGraphOrchestrator
    record = {
        "RecordTypeName__c": "GRE",
        "Applicant_Name": "Sneha Reddy",
        "Applicant_VerbalScore": 158, "Applicant_VerbalPercentile": 78,
        "Applicant_QuantScore": 165, "Applicant_QuantPercentile": 86,
        "Applicant_Analytical_Score": 4.5, "Applicant_Analytical_Percentile": 80,
        "API_VerbalScore": 158, "API_VerbalPercentile": 78,
        "API_QuantScore": 165, "API_QuantPercentile": 86,
        "API_Analytical_Score": 4.5, "API_Analytical_Percentile": 80,
        "API_Registration_No": "GRE7789123",
        "Applicant_Registration_No": "GRE7789123",
        # Deliberately leaked irrelevant fields (excel: leakage must not happen)
        "Passport Number": "M8823941",
        "Job Title": "Analyst",
    }
    doc = """
## Page 1
ETS - GRE Test Taker Score Report
Name: Sneha Reddy         Registration Number: GRE7789123
Test Date: 12 October 2024
Verbal Reasoning: 158 (78th percentile)
Quantitative Reasoning: 165 (86th percentile)
Analytical Writing: 4.5 (80th percentile)
"""
    report = TestScoreGraphOrchestrator(record, doc).run()
    rows = report.get("verification_analysis_report", [])
    names = {_norm(r.get("field_name")) for r in rows}

    # GRE combined-total exclusion (non-negotiable prompt rule)
    assert not any("totalscore" in n or "totalpercentile" in n for n in names), names
    # Leakage exclusion: passport / job title must never appear
    assert not any("passport" in n or "jobtitle" in n for n in names), names
    assert _status(_row(report, "Applicant_VerbalScore") or _row(report, "VerbalScore")) == "MATCH" or \
           any("verbal" in n for n in names), rows
