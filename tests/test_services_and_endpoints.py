"""Unit tests for SF service mixins and endpoints using proper mocking."""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock, PropertyMock
from fastapi.testclient import TestClient


# ============================================================================
# Helpers
# ============================================================================
def _make_sf_service_mock():
    """Create a fully-mocked SalesforceService that behaves like the real thing."""
    with patch("app.services.salesforce_service.requests.post") as mock_post, \
         patch("app.services.salesforce_service.Salesforce") as mock_sf_cls:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"access_token": "tok", "instance_url": "https://test.sf.com"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        mock_sf_inst = MagicMock()
        mock_sf_inst.sf_instance = "test.sf.com"
        mock_sf_inst.session = MagicMock()
        mock_sf_inst.headers = {"Authorization": "Bearer tok"}
        mock_sf_cls.return_value = mock_sf_inst

        from app.services.salesforce_service import SalesforceService
        svc = SalesforceService(
            client_id="test_cid", client_secret="test_csec",
            token_url="https://test.sf.com/services/oauth2/token", org_alias="dev"
        )
        return svc


# ============================================================================
# AdmissionSFMixin Tests
# ============================================================================
class TestAdmissionMixin:
    def test_get_directly_related_record_ids(self):
        svc = _make_sf_service_mock()
        svc.sf.query_all.return_value = {"totalSize": 2, "records": [{"Id": "r1"}, {"Id": "r2"}]}
        result = svc.get_directly_related_record_ids("a3l001", "ISB_Education_Log__c", "Application__c")
        assert result == ["r1", "r2"]

    def test_get_directly_related_empty(self):
        svc = _make_sf_service_mock()
        svc.sf.query_all.return_value = {"totalSize": 0, "records": []}
        result = svc.get_directly_related_record_ids("a3l001", "ISB_Education_Log__c", "Application__c")
        assert result == []

    def test_get_existing_avs_metadata_found(self):
        svc = _make_sf_service_mock()
        svc.sf.query.return_value = {
            "totalSize": 1,
            "records": [{"Id": "avs001", "LastModifiedDate": "2025-01-01T00:00:00.000+0000", "Percentage_Confidence__c": "90"}]
        }
        result = svc.get_existing_avs_metadata("a3l001")
        assert result is not None
        assert result["Id"] == "avs001"

    def test_get_existing_avs_metadata_not_found(self):
        svc = _make_sf_service_mock()
        svc.sf.query.return_value = {"totalSize": 0, "records": []}
        result = svc.get_existing_avs_metadata("a3l001")
        assert result is None

    @pytest.mark.asyncio
    async def test_upsert_ai_server_job_create(self):
        svc = _make_sf_service_mock()
        svc.sf.query.return_value = {"totalSize": 0, "records": []}
        handler = MagicMock()
        handler.create.return_value = {"success": True, "id": "a0z001"}
        setattr(svc.sf, "AI_Server_Job__c", handler)

        result = await svc.upsert_ai_server_job(job_id="j1", application_id="a3l001", status="queued")
        assert result == "a0z001"

    @pytest.mark.asyncio
    async def test_upsert_ai_server_job_update(self):
        svc = _make_sf_service_mock()
        svc.sf.query.return_value = {"totalSize": 1, "records": [{"Id": "a0z_exist"}]}
        handler = MagicMock()
        handler.update.return_value = 204
        setattr(svc.sf, "AI_Server_Job__c", handler)

        result = await svc.upsert_ai_server_job(job_id="j1", application_id="a3l001", status="completed")
        assert result == "a0z_exist"

    @pytest.mark.asyncio
    async def test_get_contact_id_for_application(self):
        svc = _make_sf_service_mock()
        svc.sf.query.return_value = {
            "totalSize": 1,
            "records": [{"Applicant__c": "003001", "Applicant__r": {"Name": "Jane"}}]
        }
        result = await svc.get_contact_id_for_application("a3l001")
        # Returns contact ID (Applicant__c) or name depending on implementation
        assert result in ["Jane", "003001"]

    def test_get_test_score_record_data_unsupported_type(self):
        """Unsupported RecordTypeName should return an issue."""
        svc = _make_sf_service_mock()
        svc.sf.query.return_value = {
            "totalSize": 1,
            "records": [{"Id": "a3n001", "RecordTypeName__c": "CAT", "attributes": {"type": "hed__Test__c"}}]
        }
        result = svc.get_test_score_record_data("a3n001", "a3l001")
        assert "Salesforce_data_issue_Summary" in result
        assert "Unsupported" in result["Salesforce_data_issue_Summary"]


# ============================================================================
# EedlSFMixin Tests
# ============================================================================
class TestEedlMixin:
    def test_get_existing_eedl_vs_metadata_found(self):
        svc = _make_sf_service_mock()
        svc.sf.query.return_value = {
            "totalSize": 1,
            "records": [{"Id": "vs001", "LastModifiedDate": "2025-01-01", "Confidence_Score__c": "85"}]
        }
        result = svc.get_existing_eedl_vs_metadata("006001", "ID_Document")
        assert result is not None
        assert result["Id"] == "vs001"

    def test_get_existing_eedl_vs_metadata_not_found(self):
        svc = _make_sf_service_mock()
        svc.sf.query.return_value = {"totalSize": 0, "records": []}
        result = svc.get_existing_eedl_vs_metadata("006001", "Education")
        assert result is None

    @pytest.mark.asyncio
    async def test_upsert_eedl_ai_server_job_create(self):
        svc = _make_sf_service_mock()
        svc.sf.query.return_value = {"totalSize": 0, "records": []}
        handler = MagicMock()
        handler.create.return_value = {"success": True, "id": "a0z_eedl"}
        setattr(svc.sf, "AI_Server_Job__c", handler)

        result = await svc.upsert_eedl_ai_server_job(job_id="j1", opportunity_id="006001", status="queued")
        assert result == "a0z_eedl"

    @pytest.mark.asyncio
    async def test_get_latest_eedl_ai_server_job_found(self):
        svc = _make_sf_service_mock()
        svc.sf.query.return_value = {
            "totalSize": 1,
            "records": [{
                "Id": "a0z001", "Job_Id__c": "uuid-1", "Status__c": "completed",
                "Message__c": "Done", "Application__c": "006001",
                "CreatedDate": "2025-01-01T00:00:00.000+0000",
                "LastModifiedDate": "2025-01-01T01:00:00.000+0000",
                "Progress_Details__c": None, "Client_Fingerprint__c": "fp", "Logs__c": None
            }]
        }
        result = await svc.get_latest_eedl_ai_server_job("006001")
        assert result is not None
        assert result["job_id"] == "uuid-1"
        assert result["salesforce_job_record_id"] == "a0z001"

    @pytest.mark.asyncio
    async def test_get_latest_eedl_ai_server_job_not_found(self):
        svc = _make_sf_service_mock()
        svc.sf.query.return_value = {"totalSize": 0, "records": []}
        result = await svc.get_latest_eedl_ai_server_job("006001")
        assert result is None

    def test_get_eedl_education_ids_no_contact_raises(self):
        from app.services.salesforce_service import SalesforceAPIError
        svc = _make_sf_service_mock()
        svc.sf.query.return_value = {"totalSize": 1, "records": [{"ContactId": None}]}
        with pytest.raises(SalesforceAPIError, match="no Contact"):
            svc.get_eedl_education_ids_for_opportunity("006001")


# ============================================================================
# Endpoint Tests (TestClient)
# ============================================================================
class TestEndpoints:
    @pytest.fixture(autouse=True)
    def setup_client(self):
        """Patch SF connection manager to avoid real SF calls."""
        with patch("app.services.salesforce_service.requests.post") as mock_post, \
             patch("app.services.salesforce_service.Salesforce") as mock_sf_cls:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"access_token": "tok", "instance_url": "https://test.sf.com"}
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp
            mock_sf_cls.return_value = MagicMock(sf_instance="test.sf.com")

            from app.main import app
            self.client = TestClient(app)
            yield

    def test_root(self):
        resp = self.client.get("/")
        assert resp.status_code == 200
        assert "message" in resp.json()

    def test_health(self):
        resp = self.client.get("/api/v1/admin/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ["ok", "degraded"]
        assert "checks" in data

    def test_queue_overview(self):
        resp = self.client.get("/api/v1/application/queue-overview")
        assert resp.status_code == 200
        data = resp.json()
        assert "active_jobs" in data
        assert "slot_utilization" in data

    def test_analyze_invalid_id_400(self):
        resp = self.client.post("/api/v1/application/analyze", json={"record_id": "short"})
        assert resp.status_code == 400

    def test_analyze_missing_body_422(self):
        resp = self.client.post("/api/v1/application/analyze")
        assert resp.status_code == 422

    def test_status_invalid_id_400(self):
        resp = self.client.get("/api/v1/application/status/bad_id")
        assert resp.status_code == 400

    def test_eedl_invalid_id_400(self):
        resp = self.client.post("/api/v1/eedl/analyze", json={"record_id": "x"})
        assert resp.status_code == 400

    def test_eedl_missing_body_422(self):
        resp = self.client.post("/api/v1/eedl/analyze")
        assert resp.status_code == 422


# ============================================================================
# Document Extraction Service
# ============================================================================
class TestDocumentExtraction:
    def test_create_text_extractor(self):
        from app.services.document_extraction_service import create_text_extractor
        extractor = create_text_extractor()
        assert extractor is not None

    def test_smart_extraction_prompts_exist(self):
        from app.services.document_extraction_service import SMART_EXTRACTION_PROMPTS
        assert isinstance(SMART_EXTRACTION_PROMPTS, dict)
        assert len(SMART_EXTRACTION_PROMPTS) >= 1
