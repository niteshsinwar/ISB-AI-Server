"""Shared deterministic report-builder contract tests."""

from app.langgraph.report_builder import build_verification_report


def test_report_builder_supports_three_way_test_score_rows():
    report = build_verification_report(
        [
            {
                "field_name": "Applicant_Name",
                "api_value": "Jane Doe",
                "applicant_value": "Jane Doe",
                "document_value": "Jane Doe",
                "status": "MATCH",
                "confidence": 100,
                "notes": "All sources match.",
            },
            {
                "field_name": "API_VerbalScore",
                "api_value": "164",
                "applicant_value": "160",
                "document_value": "164",
                "status": "MISMATCH",
                "confidence": 60,
                "notes": "Applicant value differs from API/document.",
            },
        ],
        critical_field_names={"Applicant_Name", "API_VerbalScore"},
    )

    html = report["field_comparison_summary"]
    assert "API Value" in html
    assert "Applicant Value" in html
    assert "Document Value" in html
    assert report["mismatched_field_list"] == "API_VerbalScore"
    assert report["verification_status"] == "Needs Review"
    assert report["overall_percentage_confidence"] == report["confidence_range"]
    assert "is_critical" not in report["verification_analysis_report"][0]


def test_report_builder_supports_extra_pipeline_metadata():
    report = build_verification_report(
        [
            {
                "field_name": "Citizenship",
                "record_value": "India",
                "document_value": "India",
                "status": "MATCH",
                "confidence": 100,
                "notes": "Passport country matches.",
            }
        ],
        critical_field_names={"Citizenship"},
        extra_fields={"suggested_citizenship_value": "India"},
    )

    assert report["verification_status"] == "Passed"
    assert report["suggested_citizenship_value"] == "India"
    assert report["mismatched_field_list"] == "N/A"
