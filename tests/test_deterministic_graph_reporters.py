"""Regression tests for deterministic graph report generation across pipelines."""

import json
from unittest.mock import MagicMock, patch

from app.langgraph.application_graph import ApplicationGraphNodes
from app.langgraph.eedl_citizenship_graph import CitizenshipGraphNodes
from app.langgraph.eedl_education_graph import EEDLEducationGraphNodes
from app.langgraph.employment_graph import EmploymentGraphNodes
from app.langgraph.test_score_graph import TestScoreGraphNodes as ScoreGraphNodes
from app.processors.eedl_id_processor import _parse_mismatched_fields


def _comparison_output(*rows):
    return json.dumps({"verification_analysis_report": list(rows)})


def test_application_reporter_is_deterministic_and_hides_critical_column():
    with patch("app.langgraph.application_graph.get_llm") as get_llm:
        get_llm.return_value = MagicMock()
        nodes = ApplicationGraphNodes()

    result = nodes.reporter_node({
        "comparison_task_output": _comparison_output({
            "field_name": "Full Name",
            "record_value": "Jane Doe",
            "document_value": "Jane Doe",
            "status": "MATCH",
            "confidence": 100,
            "notes": "Exact match.",
        })
    })

    report = result["final_report"]
    assert report["verification_status"] == "Passed"
    assert report["overall_percentage_confidence"] == 100
    assert report["verification_analysis_report"][0]["field_name"] == "Full Name"
    assert "Is Critical" not in report["field_comparison_summary"]
    assert "is_critical" not in report["verification_analysis_report"][0]
    nodes.llm_comparator.invoke.assert_not_called()


def test_employment_bank_statement_reporter_forces_failed_document_type_mismatch():
    with patch("app.langgraph.employment_graph.get_llm") as get_llm:
        get_llm.return_value = MagicMock()
        nodes = EmploymentGraphNodes()

    result = nodes.bank_statement_reporter_node({
        "document_type_reasoning": "Bank account transaction rows were detected.",
    })

    report = result["final_report"]
    assert report["verification_status"] == "Failed"
    assert report["confidence_range"] == 20
    assert report["overall_percentage_confidence"] == 20
    assert report["mismatched_field_list"] == "Document_Type"
    assert report["verification_analysis_report"][0]["document_value"] == "BANK_STATEMENT"
    assert "Is Critical" not in report["field_comparison_summary"]
    nodes.llm_comparator.invoke.assert_not_called()


def test_employment_reporter_uses_name_criticality_without_exposing_it():
    with patch("app.langgraph.employment_graph.get_llm") as get_llm:
        get_llm.return_value = MagicMock()
        nodes = EmploymentGraphNodes()

    result = nodes.reporter_node({
        "comparison_task_output": _comparison_output({
            "field_name": "Employee Name",
            "record_value": "Jane Doe",
            "document_value": "Janet Doe",
            "status": "MISMATCH",
            "confidence": 60,
            "notes": "Name differs.",
        })
    })

    report = result["final_report"]
    assert report["verification_status"] == "Needs Review"
    assert report["mismatched_field_list"] == "Employee Name"
    assert "is_critical" not in report["verification_analysis_report"][0]


def test_test_score_reporter_renders_three_way_source_values():
    with patch("app.langgraph.test_score_graph.get_llm") as get_llm:
        get_llm.return_value = MagicMock()
        nodes = ScoreGraphNodes()

    result = nodes.reporter_node({
        "record_data": {"API_VerbalScore": "164"},
        "comparison_task_output": _comparison_output({
            "field_name": "API_VerbalScore",
            "api_value": "164",
            "applicant_value": "160",
            "document_value": "164",
            "status": "MISMATCH",
            "confidence": 60,
            "notes": "Applicant entered score differs from API and scorecard.",
        }),
    })

    html = result["final_report"]["field_comparison_summary"]
    assert "API Value" in html
    assert "Applicant Value" in html
    assert "Document Value" in html
    assert result["final_report"]["mismatched_field_list"] == "API_VerbalScore"
    assert "Is Critical" not in html


def test_eedl_education_reporter_is_deterministic():
    with patch("app.langgraph.eedl_education_graph.get_llm") as get_llm:
        get_llm.return_value = MagicMock()
        nodes = EEDLEducationGraphNodes()

    result = nodes.reporter_node({
        "comparison_task_output": _comparison_output({
            "field_name": "Degree_Type__c",
            "record_value": "MBA",
            "document_value": "MBA",
            "status": "MATCH",
            "confidence": 100,
            "notes": "Exact match.",
        })
    })

    assert result["final_report"]["verification_status"] == "Passed"
    assert result["final_report"]["verification_analysis_report"][0]["field_name"] == "Degree_Type__c"


def test_eedl_citizenship_reporter_preserves_analysis_and_derives_suggestion():
    with patch("app.langgraph.eedl_citizenship_graph.get_llm") as get_llm:
        get_llm.return_value = MagicMock()
        nodes = CitizenshipGraphNodes()

    result = nodes.reporter_node({
        "document_text": "Government of India Aadhaar UIDAI",
        "comparison_task_output": _comparison_output({
            "field_name": "Nationality",
            "record_value": "India",
            "document_value": "India",
            "status": "MATCH",
            "confidence": 100,
            "notes": "Nationality is present on document.",
        }),
    })

    report = result["final_report"]
    assert report["verification_status"] == "Passed"
    assert report["suggested_citizenship_value"] == "Indian"
    assert report["verification_analysis_report"][0]["field_name"] == "Nationality"


def test_eedl_id_mismatch_parser_supports_field_only_and_legacy_formats():
    assert _parse_mismatched_fields("name; citizenship") == {
        "name": "MISMATCH",
        "citizenship": "MISMATCH",
    }
    assert _parse_mismatched_fields("name: NOT_FOUND; citizenship: MISMATCH") == {
        "name": "NOT_FOUND",
        "citizenship": "MISMATCH",
    }
