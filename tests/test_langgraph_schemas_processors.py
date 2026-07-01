"""Unit tests for LangGraph, schemas, state, and processors."""
import pytest
from unittest.mock import patch, MagicMock


# ============================================================================
# LangGraph State & Schemas
# ============================================================================
class TestLangGraphState:
    def test_state_has_expected_fields(self):
        from app.langgraph.state import VerificationState
        annotations = VerificationState.__annotations__
        assert "record_data" in annotations
        assert "document_text" in annotations
        assert "document_type" in annotations
        assert "final_report" in annotations
        assert "usage_metrics" in annotations
        assert "document_type_reasoning" in annotations

class TestLangGraphSchemas:
    def test_validated_crew_report(self):
        from app.langgraph.schemas import ValidatedCrewReport
        assert ValidatedCrewReport is not None

    def test_validated_resume_report(self):
        from app.langgraph.schemas import ValidatedResumeReport
        assert ValidatedResumeReport is not None

    def test_validated_citizenship_report(self):
        from app.langgraph.schemas import ValidatedCitizenshipReport
        assert ValidatedCitizenshipReport is not None


# ============================================================================
# Graph Builds
# ============================================================================
class TestGraphBuilds:
    def test_build_application_graph(self):
        from app.langgraph.application_graph import build_application_graph
        graph = build_application_graph()
        assert graph is not None and hasattr(graph, "invoke")

    def test_build_education_graph(self):
        from app.langgraph.education_graph import build_education_graph
        graph = build_education_graph()
        assert graph is not None and hasattr(graph, "invoke")

    def test_build_employment_graph(self):
        from app.langgraph.employment_graph import build_employment_graph
        graph = build_employment_graph()
        assert graph is not None and hasattr(graph, "invoke")

    def test_build_test_score_graph(self):
        from app.langgraph.test_score_graph import build_test_score_graph
        graph = build_test_score_graph()
        assert graph is not None and hasattr(graph, "invoke")

    def test_build_eedl_citizenship_graph(self):
        from app.langgraph.eedl_citizenship_graph import build_citizenship_graph
        graph = build_citizenship_graph()
        assert graph is not None and hasattr(graph, "invoke")

    def test_build_eedl_education_graph(self):
        from app.langgraph.eedl_education_graph import build_eedl_education_graph
        graph = build_eedl_education_graph()
        assert graph is not None and hasattr(graph, "invoke")


# ============================================================================
# Graph Utils
# ============================================================================
class TestGraphUtils:
    def test_get_llm(self):
        from app.langgraph.graph_utils import get_llm
        with patch("app.langgraph.graph_utils.initialize_llm") as mock_init:
            mock_init.return_value = MagicMock()
            get_llm("gemini-2.5-flash", 0.1)
            mock_init.assert_called_once()

    def test_clean_and_extract_json_returns_string(self):
        from app.langgraph.graph_utils import clean_and_extract_json
        result = clean_and_extract_json('{"key": "value"}')
        assert isinstance(result, str)

    def test_clean_and_extract_json_strips_markdown_fences(self):
        from app.langgraph.graph_utils import clean_and_extract_json
        raw = '```json\n{"a": 1}\n```'
        result = clean_and_extract_json(raw)
        assert "```" not in result
        assert '"a"' in result


# ============================================================================
# Graph Prompts
# ============================================================================
class TestGraphPrompts:
    def test_application_prompts(self):
        from app.langgraph.graph_prompts import APPLICATION_DATA_COMPARATOR_AGENT_GOAL
        assert isinstance(APPLICATION_DATA_COMPARATOR_AGENT_GOAL, str)
        assert len(APPLICATION_DATA_COMPARATOR_AGENT_GOAL) > 50

    def test_employment_classifier_prompt(self):
        from app.langgraph.graph_prompts import EMPLOYMENT_DOC_CLASSIFICATION_TASK, EMPLOYMENT_DOC_CLASSIFIER_GOAL
        assert "bank" in EMPLOYMENT_DOC_CLASSIFICATION_TASK.lower()
        assert isinstance(EMPLOYMENT_DOC_CLASSIFIER_GOAL, str)

    def test_eedl_prompts(self):
        from app.langgraph.eedl_graph_prompts import EEDL_CITIZENSHIP_COMPARATOR_GOAL
        assert isinstance(EEDL_CITIZENSHIP_COMPARATOR_GOAL, str)
        assert len(EEDL_CITIZENSHIP_COMPARATOR_GOAL) > 20


# ============================================================================
# Employment Graph Routing
# ============================================================================
class TestEmploymentGraphRouting:
    def test_route_bank_statement(self):
        from app.langgraph.employment_graph import _route_after_classification
        assert _route_after_classification({"document_type": "BANK_STATEMENT"}) == "bank_statement_reporter"

    def test_route_non_bank_statement(self):
        from app.langgraph.employment_graph import _route_after_classification
        assert _route_after_classification({"document_type": "EMPLOYMENT_DOCUMENT"}) == "comparator"

    def test_route_none(self):
        from app.langgraph.employment_graph import _route_after_classification
        assert _route_after_classification({"document_type": None}) == "comparator"


# ============================================================================
# Test Score Internal Fields
# ============================================================================
class TestTestScoreInternalFields:
    def test_is_list_with_entries(self):
        from app.langgraph.test_score_graph import _TEST_SCORE_INTERNAL_FIELDS
        assert isinstance(_TEST_SCORE_INTERNAL_FIELDS, list)
        assert len(_TEST_SCORE_INTERNAL_FIELDS) > 5

    def test_contains_non_verification_fields(self):
        from app.langgraph.test_score_graph import _TEST_SCORE_INTERNAL_FIELDS
        # Should contain fields irrelevant to score verification
        assert any("Cohort" in f or "Program" in f for f in _TEST_SCORE_INTERNAL_FIELDS)


# ============================================================================
# Processor Imports & Signatures
# ============================================================================
class TestProcessorImports:
    def test_application_processor(self):
        from app.processors.application_processor import process_single_application_detail
        assert callable(process_single_application_detail)

    def test_education_processor(self):
        from app.processors.education_processor import process_single_education_history_detail
        assert callable(process_single_education_history_detail)

    def test_employment_processor(self):
        from app.processors.employment_processor import process_single_employment_detail
        assert callable(process_single_employment_detail)

    def test_test_score_processor(self):
        from app.processors.test_score_processor import process_single_test_score_detail
        assert callable(process_single_test_score_detail)

    def test_resume_processor(self):
        from app.processors.resume_processor import process_single_resume_detail
        assert callable(process_single_resume_detail)

    def test_eedl_id_processor(self):
        from app.processors.eedl_id_processor import process_eedl_id_document
        assert callable(process_eedl_id_document)

    def test_eedl_education_processor(self):
        from app.processors.eedl_education_processor import process_eedl_education_record
        assert callable(process_eedl_education_record)


class TestProcessorSignatures:
    def test_application_processor_has_sf_service_param(self):
        import inspect
        from app.processors.application_processor import process_single_application_detail
        params = list(inspect.signature(process_single_application_detail).parameters.keys())
        assert "sf_service" in params

    def test_education_processor_has_sf_service_param(self):
        import inspect
        from app.processors.education_processor import process_single_education_history_detail
        params = list(inspect.signature(process_single_education_history_detail).parameters.keys())
        assert "sf_service" in params

    def test_employment_processor_has_sf_service_param(self):
        import inspect
        from app.processors.employment_processor import process_single_employment_detail
        params = list(inspect.signature(process_single_employment_detail).parameters.keys())
        assert "sf_service" in params

    def test_test_score_has_sf_service_param(self):
        import inspect
        from app.processors.test_score_processor import process_single_test_score_detail
        params = list(inspect.signature(process_single_test_score_detail).parameters.keys())
        assert "sf_service" in params

    def test_eedl_id_has_sf_config_param(self):
        import inspect
        from app.processors.eedl_id_processor import process_eedl_id_document
        params = list(inspect.signature(process_eedl_id_document).parameters.keys())
        assert "sf_service" in params or "sf_config" in params


# ============================================================================
# Response Schemas
# ============================================================================
class TestResponseSchemas:
    def test_analyze_body_request(self):
        from app.schemas.responses import AnalyzeApplicationBodyRequest
        req = AnalyzeApplicationBodyRequest(record_id="a3l000000000001AAA")
        assert req.record_id == "a3l000000000001AAA"

    def test_health_response(self):
        from app.schemas.responses import HealthResponse, DependencyStatus
        from datetime import datetime, timezone
        resp = HealthResponse(
            status="ok", timestamp=datetime.now(timezone.utc),
            application_version="2.0.0",
            checks=[DependencyStatus(name="SF", status="ok")]
        )
        assert resp.status == "ok"

    def test_job_status_response(self):
        from app.schemas.responses import JobStatusResponse
        from datetime import datetime, timezone
        resp = JobStatusResponse(
            job_id="uuid-1", application_id="a3l001", status="completed",
            created_at=datetime.now(timezone.utc), last_updated_at=datetime.now(timezone.utc)
        )
        assert resp.status == "completed"

    def test_eedl_body_request(self):
        from app.schemas.responses import AnalyzeEEDLBodyRequest
        req = AnalyzeEEDLBodyRequest(record_id="006000000000001")
        assert req.record_id == "006000000000001"

    def test_queue_overview(self):
        from app.schemas.responses import QueueOverviewResponse
        resp = QueueOverviewResponse(
            active_jobs=0, tracked_jobs_total=0,
            slot_utilization={"active_slots": 0, "max_slots": 15, "load_percent": 0.0},
            all_jobs=[]
        )
        assert resp.active_jobs == 0
