"""Unit tests for app/core/ modules."""
import asyncio
import time
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


# ============================================================================
# Tests for app/core/processing_utils.py
# ============================================================================
class TestProcessingUtils:
    def test_parse_sf_datetime_valid(self):
        from app.core.processing_utils import parse_sf_datetime
        dt = parse_sf_datetime("2025-01-15T10:30:00.000+0000")
        assert dt is not None
        assert dt.year == 2025
        assert dt.month == 1
        assert dt.day == 15
        assert dt.hour == 10
        assert dt.minute == 30

    def test_parse_sf_datetime_with_z(self):
        from app.core.processing_utils import parse_sf_datetime
        dt = parse_sf_datetime("2025-06-01T12:00:00.000Z")
        assert dt is not None
        assert dt.year == 2025
        assert dt.month == 6

    def test_parse_sf_datetime_none(self):
        from app.core.processing_utils import parse_sf_datetime
        assert parse_sf_datetime(None) is None
        assert parse_sf_datetime("") is None

    def test_parse_sf_datetime_invalid(self):
        from app.core.processing_utils import parse_sf_datetime
        assert parse_sf_datetime("not-a-date") is None

    def test_should_skip_no_existing_avs(self):
        from app.core.processing_utils import should_skip_processing
        skip, reason = should_skip_processing(None, None, None)
        assert skip is False
        assert reason == "no_existing_avs"

    def test_should_skip_confidence_100_int(self):
        # Post-fix semantics: skip is based purely on AVS being newer than both
        # the record and the document — confidence no longer short-circuits.
        from app.core.processing_utils import should_skip_processing
        avs = {"Percentage_Confidence__c": 100, "LastModifiedDate": "2025-01-01T00:00:00.000+0000"}
        skip, reason = should_skip_processing(avs, None, None)
        assert skip is True
        assert reason == "avs_newer_than_record_and_doc"

    def test_should_skip_confidence_100_str(self):
        from app.core.processing_utils import should_skip_processing
        avs = {"Percentage_Confidence__c": "100", "LastModifiedDate": "2025-01-01T00:00:00.000+0000"}
        skip, reason = should_skip_processing(avs, None, None)
        assert skip is True

    def test_should_skip_avs_newer(self):
        from app.core.processing_utils import should_skip_processing
        avs = {"Percentage_Confidence__c": 80, "LastModifiedDate": "2025-06-01T00:00:00.000+0000"}
        skip, reason = should_skip_processing(avs, "2025-01-01T00:00:00.000+0000", "2025-01-01T00:00:00.000+0000")
        assert skip is True
        assert "avs_newer" in reason

    def test_should_not_skip_record_modified_after(self):
        from app.core.processing_utils import should_skip_processing
        avs = {"Percentage_Confidence__c": 80, "LastModifiedDate": "2025-01-01T00:00:00.000+0000"}
        skip, reason = should_skip_processing(avs, "2025-06-01T00:00:00.000+0000", "2024-12-01T00:00:00.000+0000")
        assert skip is False
        assert "record_modified" in reason

    def test_should_not_skip_doc_modified_after(self):
        from app.core.processing_utils import should_skip_processing
        avs = {"Percentage_Confidence__c": 80, "LastModifiedDate": "2025-01-01T00:00:00.000+0000"}
        skip, reason = should_skip_processing(avs, "2024-12-01T00:00:00.000+0000", "2025-06-01T00:00:00.000+0000")
        assert skip is False
        assert "doc_modified" in reason

    def test_should_not_skip_no_avs_date(self):
        from app.core.processing_utils import should_skip_processing
        avs = {"Percentage_Confidence__c": 80, "LastModifiedDate": None}
        skip, reason = should_skip_processing(avs, "2025-01-01T00:00:00.000+0000", None)
        assert skip is False
        assert "avs_date_missing" in reason


# ============================================================================
# Tests for app/core/rate_limit_state.py
# ============================================================================
class TestRateLimitState:
    def setup_method(self):
        """Reset module state before each test."""
        import app.core.rate_limit_state as rls
        rls._processing_semaphore = None
        rls._active_processing_slots = 0
        rls._client_last_request.clear()

    def test_generate_fingerprint_basic(self):
        from app.core.rate_limit_state import generate_client_fingerprint
        fp = generate_client_fingerprint({"user-agent": "TestAgent"}, "192.168.1.1")
        assert isinstance(fp, str)
        assert len(fp) == 16

    def test_generate_fingerprint_x_forwarded_for(self):
        from app.core.rate_limit_state import generate_client_fingerprint
        fp1 = generate_client_fingerprint(
            {"x-forwarded-for": "10.0.0.1, 10.0.0.2", "user-agent": "Test"},
            "192.168.1.1"
        )
        fp2 = generate_client_fingerprint(
            {"user-agent": "Test"},
            "10.0.0.1"
        )
        assert fp1 == fp2  # x-forwarded-for should override client_host

    def test_generate_fingerprint_x_real_ip(self):
        from app.core.rate_limit_state import generate_client_fingerprint
        fp1 = generate_client_fingerprint(
            {"x-real-ip": "10.0.0.5", "user-agent": "Test"},
            "192.168.1.1"
        )
        fp2 = generate_client_fingerprint({"user-agent": "Test"}, "10.0.0.5")
        assert fp1 == fp2

    def test_generate_fingerprint_deterministic(self):
        from app.core.rate_limit_state import generate_client_fingerprint
        fp1 = generate_client_fingerprint({"user-agent": "X"}, "1.2.3.4")
        fp2 = generate_client_fingerprint({"user-agent": "X"}, "1.2.3.4")
        assert fp1 == fp2

    def test_generate_fingerprint_different_agents(self):
        from app.core.rate_limit_state import generate_client_fingerprint
        fp1 = generate_client_fingerprint({"user-agent": "AgentA"}, "1.2.3.4")
        fp2 = generate_client_fingerprint({"user-agent": "AgentB"}, "1.2.3.4")
        assert fp1 != fp2

    @pytest.mark.asyncio
    async def test_initialize_semaphore(self):
        from app.core.rate_limit_state import initialize_processing_semaphore, _processing_semaphore
        import app.core.rate_limit_state as rls
        initialize_processing_semaphore()
        assert rls._processing_semaphore is not None

    @pytest.mark.asyncio
    async def test_acquire_without_init_raises(self):
        from app.core.rate_limit_state import acquire_processing_slot
        with pytest.raises(RuntimeError, match="not initialized"):
            await acquire_processing_slot()

    @pytest.mark.asyncio
    async def test_acquire_and_release(self):
        from app.core.rate_limit_state import (
            initialize_processing_semaphore, acquire_processing_slot,
            release_processing_slot, get_active_processing_slots_count
        )
        initialize_processing_semaphore()
        await acquire_processing_slot()
        count = await get_active_processing_slots_count()
        assert count == 1
        await release_processing_slot()
        count = await get_active_processing_slots_count()
        assert count == 0

    @pytest.mark.asyncio
    async def test_throttle_allows_first_request(self):
        from app.core.rate_limit_state import check_simple_throttle
        ok, msg = await check_simple_throttle("client_abc")
        assert ok is True
        assert msg == ""

    @pytest.mark.asyncio
    async def test_throttle_blocks_rapid_repeat(self):
        from app.core.rate_limit_state import check_simple_throttle
        await check_simple_throttle("client_xyz")
        ok, msg = await check_simple_throttle("client_xyz")
        assert ok is False
        assert "Too many requests" in msg

    @pytest.mark.asyncio
    async def test_throttle_allows_after_interval(self):
        from app.core.rate_limit_state import check_simple_throttle
        import app.core.rate_limit_state as rls
        # Simulate request 2 seconds ago
        rls._client_last_request["client_old"] = time.time() - 5.0
        ok, msg = await check_simple_throttle("client_old")
        assert ok is True


# ============================================================================
# Tests for app/core/app_instance.py
# ============================================================================
class TestAppInstance:
    def test_set_and_get(self):
        from app.core.app_instance import set_app_instance, get_app_instance
        from fastapi import FastAPI
        app = FastAPI(title="Test", version="1.0.0")
        set_app_instance(app)
        result = get_app_instance()
        assert result is app

    def test_get_without_set_returns_dummy(self):
        from app.core.app_instance import get_app_instance, _app_instance_storage
        _app_instance_storage["app"] = None
        result = get_app_instance()
        assert hasattr(result, "version")
        assert "N/A" in result.version


# ============================================================================
# Tests for app/core/logging_config.py
# ============================================================================
class TestLoggingConfig:
    def test_setup_logging_no_error(self):
        from app.core.logging_config import setup_logging
        # Should not raise
        setup_logging()

    def test_setup_logging_idempotent(self):
        from app.core.logging_config import setup_logging
        import logging
        setup_logging()
        handler_count = len(logging.getLogger().handlers)
        setup_logging()
        # Should not add extra handlers
        assert len(logging.getLogger().handlers) <= handler_count + 1


# ============================================================================
# Tests for app/core/job_manager.py
# ============================================================================
class TestJobModel:
    def test_job_default_values(self):
        from app.core.job_manager import Job
        job = Job(application_id="a3l000001", client_fingerprint="fp123")
        assert job.status == "queued"
        assert job.job_id  # UUID auto-generated
        assert job.application_id == "a3l000001"
        assert job.opportunity_id is None
        assert job.is_stale is False
        assert job.created_at is not None

    def test_job_eedl_mode(self):
        from app.core.job_manager import Job
        job = Job(application_id="006000001", opportunity_id="006000001", client_fingerprint="fp456")
        assert job.opportunity_id == "006000001"


class TestJobManager:
    @pytest.mark.asyncio
    async def test_is_job_active_empty(self):
        from app.core.job_manager import JobManager
        jm = JobManager()
        assert await jm.is_job_active("nonexistent") is False

    @pytest.mark.asyncio
    async def test_get_all_active_jobs_empty(self):
        from app.core.job_manager import JobManager
        jm = JobManager()
        jobs = await jm.get_all_active_jobs()
        assert jobs == {}

    @pytest.mark.asyncio
    async def test_queue_overview_empty(self):
        from app.core.job_manager import JobManager
        jm = JobManager()
        with patch("app.core.job_manager.get_active_processing_slots_count", new_callable=AsyncMock, return_value=0):
            overview = await jm.get_queue_overview()
        assert overview["active_jobs"] == 0
        assert overview["tracked_jobs_total"] == 0
        assert overview["all_jobs"] == []

    @pytest.mark.asyncio
    async def test_create_job_admission(self):
        from app.core.job_manager import JobManager
        jm = JobManager()
        sf_service = MagicMock()
        sf_service.instance_url = "test.salesforce.com"
        sf_service.org_alias = "dev"
        sf_service.get_latest_ai_server_job = AsyncMock(return_value=None)
        sf_service.upsert_ai_server_job = AsyncMock(return_value="a0z000001")

        job = await jm.create_job("a3l000001", "fp_test", sf_service)
        assert job.application_id == "a3l000001"
        assert job.salesforce_job_record_id == "a0z000001"
        assert await jm.is_job_active("a3l000001")

    @pytest.mark.asyncio
    async def test_create_job_eedl(self):
        from app.core.job_manager import JobManager
        jm = JobManager()
        sf_service = MagicMock()
        sf_service.instance_url = "test.salesforce.com"
        sf_service.org_alias = "uat"
        sf_service.get_latest_eedl_ai_server_job = AsyncMock(return_value=None)
        sf_service.upsert_eedl_ai_server_job = AsyncMock(return_value="a0z000002")

        job = await jm.create_job("006000001", "fp_eedl", sf_service, opportunity_id="006000001")
        assert job.opportunity_id == "006000001"
        assert job.salesforce_job_record_id == "a0z000002"
        sf_service.upsert_eedl_ai_server_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_release_and_finalize(self):
        from app.core.job_manager import JobManager, Job
        jm = JobManager()
        job = Job(application_id="a3l000005", client_fingerprint="fp_fin")
        jm._active_jobs["a3l000005"] = job

        with patch("app.core.job_manager.release_processing_slot", new_callable=AsyncMock):
            await jm.release_and_finalize(job)

        assert "a3l000005" not in jm._active_jobs

    @pytest.mark.asyncio
    async def test_stale_job_not_active(self):
        from app.core.job_manager import JobManager, Job
        jm = JobManager()
        job = Job(application_id="a3l000006", client_fingerprint="fp_stale", is_stale=True)
        jm._active_jobs["a3l000006"] = job
        assert await jm.is_job_active("a3l000006") is False

    @pytest.mark.asyncio
    async def test_update_status_stale_job_noop(self):
        from app.core.job_manager import JobManager, Job
        jm = JobManager()
        job = Job(application_id="a3l000007", client_fingerprint="fp_upd", is_stale=True)
        jm._active_jobs["a3l000007"] = job

        sf_service = MagicMock()
        sf_service.upsert_ai_server_job = AsyncMock()
        await jm.update_status("a3l000007", job.job_id, "processing", sf_service)
        sf_service.upsert_ai_server_job.assert_not_called()
