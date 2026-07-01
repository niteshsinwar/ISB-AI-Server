"""Tests for structured education comparison and deterministic reporting."""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.langgraph.education_graph import (
    EducationGraphNodes,
    _ensure_required_education_comparisons,
)
from app.langgraph.report_builder import (
    build_verification_report,
    parse_comparison_json,
)


def test_parse_comparison_json_accepts_fenced_array():
    parsed = parse_comparison_json(
        '```json\n[{"field_name":"Degree/Qualification","status":"MATCH"}]\n```'
    )
    assert parsed[0]["field_name"] == "Degree/Qualification"


def test_parse_comparison_json_accepts_verification_analysis_wrapper():
    parsed = parse_comparison_json(json.dumps({
        "verification_analysis_report": [
            {"field_name": "SF Field of Study", "status": "MATCH"}
        ]
    }))
    assert parsed[0]["field_name"] == "SF Field of Study"


@pytest.mark.parametrize("value", ["{}", "", "not json", "[]"])
def test_parse_comparison_json_rejects_non_comparison_arrays(value):
    with pytest.raises(ValueError):
        parse_comparison_json(value)


def test_required_education_rows_remain_separate():
    comparisons = [
        {
            "field_name": "degree_name",
            "record_value": "Bachelor of Engineering",
            "document_value": "Bachelor of Engineering",
            "status": "MATCH",
            "confidence": 100,
            "notes": "Degree explicitly printed.",
        },
        {
            "field_name": "field_of_study",
            "record_value": "Engineering",
            "document_value": "Engineering",
            "status": "MATCH",
            "confidence": 90,
            "notes": "Inferred from Bachelor of Engineering in Information Technology.",
        },
        {
            "field_name": "specialization",
            "record_value": "Computer Science",
            "document_value": "Information Technology",
            "status": "MATCH",
            "confidence": 90,
            "notes": "Closely related specializations.",
        },
    ]
    record = {
        "Degree/Qualification": "Bachelor of Engineering",
        "SF Field of Study": "Engineering",
        "Major/Specialization": "Computer Science",
    }

    reconciled = _ensure_required_education_comparisons(comparisons, record)

    assert [item["field_name"] for item in reconciled] == [
        "Degree/Qualification",
        "SF Field of Study",
        "Major/Specialization",
    ]
    assert all(item["_is_critical"] for item in reconciled)
    assert reconciled[1]["document_value"] == "Engineering"
    assert reconciled[1]["confidence"] == 90
    assert reconciled[2]["document_value"] == "Information Technology"


def test_specialization_substitution_cannot_pass_as_field_of_study():
    comparisons = [
        {
            "field_name": "field_of_study",
            "record_value": "Computer Science",
            "document_value": "Information Technology",
            "status": "MATCH",
            "confidence": 100,
            "notes": "Related subjects.",
        }
    ]
    record = {
        "SF Field of Study": "Engineering",
        "Major/Specialization": "Computer Science",
    }

    reconciled = _ensure_required_education_comparisons(comparisons, record)

    field_row = next(item for item in reconciled if item["field_name"] == "SF Field of Study")
    specialization_row = next(
        item for item in reconciled if item["field_name"] == "Major/Specialization"
    )
    assert field_row["record_value"] == "Engineering"
    assert field_row["document_value"] is None
    assert field_row["status"] == "NOT_FOUND"
    assert specialization_row["status"] == "NOT_FOUND"


def test_report_builder_constructs_safe_html_and_metadata():
    comparisons = [
        {
            "field_name": "SF Field of Study",
            "record_value": "Engineering",
            "document_value": "Engineering <script>alert(1)</script>",
            "status": "MATCH",
            "confidence": 90,
            "notes": "Reliably inferred from degree context.",
            "_is_critical": True,
        },
        {
            "field_name": "GPA",
            "record_value": "8.5",
            "document_value": "7.0",
            "status": "MISMATCH",
            "confidence": 60,
            "notes": "Final GPA differs from applicant record.",
            "_is_critical": True,
        },
    ]

    report = build_verification_report(comparisons)

    assert report["confidence_range"] == 75
    assert report["verification_status"] == "Needs Review"
    assert report["mismatched_field_list"] == "GPA"
    assert "<script>" not in report["field_comparison_summary"]
    assert "&lt;script&gt;" in report["field_comparison_summary"]
    assert "Is Critical" not in report["field_comparison_summary"]
    assert report["overall_percentage_confidence"] == 75
    assert "verification_analysis_report" in report
    assert "is_critical" not in report["verification_analysis_report"][0]
    assert "_is_critical" not in report["verification_analysis_report"][0]


def test_critical_issue_cannot_receive_passed_status_at_score_80():
    report = build_verification_report([
        {
            "field_name": "SF Field of Study",
            "record_value": "Engineering",
            "document_value": None,
            "status": "NOT_FOUND",
            "confidence": 60,
            "notes": "Field could not be established.",
            "_is_critical": True,
        }
    ])
    assert report["confidence_range"] == 80
    assert report["verification_status"] == "Needs Review"


def test_education_reporter_does_not_invoke_an_llm():
    with patch("app.langgraph.education_graph.get_llm") as get_llm:
        get_llm.return_value = MagicMock()
        nodes = EducationGraphNodes()

    comparison_output = json.dumps([
        {
            "field_name": "Student Name",
            "record_value": "Jane Doe",
            "document_value": "Jane Doe",
            "status": "MATCH",
            "confidence": 100,
            "notes": "Exact match.",
        }
    ])
    result = nodes.reporter_node({"comparison_task_output": comparison_output})

    assert result["final_report"]["verification_status"] == "Passed"
    assert result["final_report"]["verification_analysis_report"][0]["field_name"] == "Student Name"
    assert "is_critical" not in result["final_report"]["verification_analysis_report"][0]
    nodes.llm_comparator.invoke.assert_not_called()


def test_education_comparator_retries_one_malformed_response():
    malformed = MagicMock(content="not json")
    valid = MagicMock(content=json.dumps({
        "verification_analysis_report": [
            {
                "field_name": "Student Name",
                "record_value": "Jane Doe",
                "document_value": "Jane Doe",
                "status": "MATCH",
                "confidence": 100,
                "notes": "Exact match.",
            }
        ]
    }))
    with patch("app.langgraph.education_graph.get_llm") as get_llm:
        comparator = MagicMock()
        comparator.invoke.side_effect = [malformed, valid]
        get_llm.return_value = comparator
        nodes = EducationGraphNodes()

    result = nodes.comparator_node({
        "verifiable_fields": ["SF Full Name"],
        "record_data": {"SF Full Name": "Jane Doe"},
        "document_text": "Jane Doe",
    })

    parsed = json.loads(result["comparison_task_output"])
    assert parsed["verification_analysis_report"][0]["status"] == "MATCH"
    assert comparator.invoke.call_count == 2


def test_education_criticality_is_not_left_to_the_llm():
    reconciled = _ensure_required_education_comparisons([
        {
            "field_name": "School/Institute/Campus",
            "record_value": "Example College",
            "document_value": "Example University",
            "status": "MISMATCH",
            "confidence": 60,
            "notes": "Institution differs.",
        },
        {
            "field_name": "SF CGPA/Percentage",
            "record_value": "80",
            "document_value": "70",
            "status": "MISMATCH",
            "confidence": 60,
            "notes": "Percentage differs.",
        },
    ], {})

    assert all(item["_is_critical"] for item in reconciled)


def test_critical_mismatch_cannot_claim_high_confidence():
    reconciled = _ensure_required_education_comparisons([
        {
            "field_name": "Major/Specialization",
            "record_value": "Engineering",
            "document_value": "Communication and Signal Processing",
            "status": "MISMATCH",
            "confidence": 95,
            "notes": "Specialization differs.",
        }
    ], {"Major/Specialization": "Engineering"})

    assert reconciled[0]["confidence"] == 70
