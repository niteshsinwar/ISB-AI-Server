"""Unit tests for app/services/salesforce_service.py (base class and connection manager)."""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


class TestSalesforceBase:
    @patch("app.services.salesforce_service.requests.post")
    def test_connect_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "access_token": "fake_token_123",
            "instance_url": "https://test.salesforce.com"
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        from app.services.salesforce_service import _SalesforceBase
        with patch("app.services.salesforce_service.Salesforce") as mock_sf:
            mock_sf_instance = MagicMock()
            mock_sf_instance.sf_instance = "test.salesforce.com"
            mock_sf.return_value = mock_sf_instance

            svc = _SalesforceBase(
                client_id="cid", client_secret="csec",
                token_url="https://test.salesforce.com/services/oauth2/token",
                org_alias="dev"
            )
            assert svc.instance_url == "test.salesforce.com"
            assert svc.sf is not None

    @patch("app.services.salesforce_service.requests.post")
    def test_connect_missing_credentials_raises(self, mock_post):
        from app.services.salesforce_service import _SalesforceBase
        with pytest.raises(ValueError):
            _SalesforceBase(client_id="", client_secret="csec", token_url="https://x.com/token")

    @patch("app.services.salesforce_service.requests.post")
    def test_connect_http_error(self, mock_post):
        import requests
        from simple_salesforce import SalesforceAuthenticationFailed
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=MagicMock(status_code=401, text="Unauthorized"))
        mock_post.return_value = mock_resp

        from app.services.salesforce_service import _SalesforceBase
        with pytest.raises(SalesforceAuthenticationFailed):
            _SalesforceBase(client_id="cid", client_secret="csec", token_url="https://x.com/token")

    @patch("app.services.salesforce_service.requests.post")
    def test_call_sf_api_with_retry_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"access_token": "tok", "instance_url": "https://t.sf.com"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        from app.services.salesforce_service import _SalesforceBase
        with patch("app.services.salesforce_service.Salesforce") as mock_sf:
            mock_sf.return_value = MagicMock(sf_instance="t.sf.com")
            svc = _SalesforceBase(client_id="c", client_secret="s", token_url="https://t.sf.com/token")
            result = svc._call_sf_api_with_retry(lambda: {"totalSize": 1})
            assert result == {"totalSize": 1}

    @patch("app.services.salesforce_service.requests.post")
    def test_call_sf_api_with_retry_expired_session(self, mock_post):
        from simple_salesforce import SalesforceExpiredSession
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"access_token": "tok", "instance_url": "https://t.sf.com"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        from app.services.salesforce_service import _SalesforceBase
        with patch("app.services.salesforce_service.Salesforce") as mock_sf:
            mock_sf.return_value = MagicMock(sf_instance="t.sf.com")
            svc = _SalesforceBase(client_id="c", client_secret="s", token_url="https://t.sf.com/token")

            call_count = [0]
            def api_call():
                call_count[0] += 1
                if call_count[0] == 1:
                    raise SalesforceExpiredSession("https://t.sf.com", 401, "SF", "expired")
                return "success"

            result = svc._call_sf_api_with_retry(api_call)
            assert result == "success"
            assert call_count[0] == 2

    @patch("app.services.salesforce_service.requests.post")
    def test_get_field_value_case_insensitive(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"access_token": "tok", "instance_url": "https://t.sf.com"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        from app.services.salesforce_service import _SalesforceBase
        with patch("app.services.salesforce_service.Salesforce") as mock_sf:
            mock_sf.return_value = MagicMock(sf_instance="t.sf.com")
            svc = _SalesforceBase(client_id="c", client_secret="s", token_url="https://t.sf.com/token")

            record = {"Name": "Test Record", "email__c": "x@y.com"}
            assert svc._get_field_value_case_insensitive(record, "Name") == "Test Record"
            assert svc._get_field_value_case_insensitive(record, "name") == "Test Record"
            assert svc._get_field_value_case_insensitive(record, "Email__c") == "x@y.com"
            assert svc._get_field_value_case_insensitive(record, "NonExistent") is None


class TestSalesforceServiceMRO:
    def test_mixin_composition(self):
        from app.services.salesforce_service import SalesforceService
        from app.services.admission_sf_service import AdmissionSFMixin
        from app.services.eedl_sf_service import EedlSFMixin

        mro_names = [c.__name__ for c in SalesforceService.__mro__]
        assert "AdmissionSFMixin" in mro_names
        assert "EedlSFMixin" in mro_names
        assert "_SalesforceBase" in mro_names

    def test_has_admission_methods(self):
        from app.services.salesforce_service import SalesforceService
        assert hasattr(SalesforceService, "get_record_detail_from_apex")
        assert hasattr(SalesforceService, "upsert_ai_server_job")
        assert hasattr(SalesforceService, "get_latest_ai_server_job")
        assert hasattr(SalesforceService, "upsert_verification_summary")
        assert hasattr(SalesforceService, "get_test_score_record_data")

    def test_has_eedl_methods(self):
        from app.services.salesforce_service import SalesforceService
        assert hasattr(SalesforceService, "get_eedl_id_document_data")
        assert hasattr(SalesforceService, "get_eedl_education_record_data")
        assert hasattr(SalesforceService, "upsert_eedl_ai_server_job")
        assert hasattr(SalesforceService, "get_latest_eedl_ai_server_job")


class TestConnectionManager:
    @pytest.mark.asyncio
    async def test_get_service_unknown_org(self):
        from app.services.salesforce_service import SalesforceConnectionManager
        mgr = SalesforceConnectionManager()
        with pytest.raises(ValueError, match="Unknown Salesforce org"):
            await mgr.get_service("nonexistent_org")

    @pytest.mark.asyncio
    @patch("app.services.salesforce_service.SalesforceService")
    async def test_get_service_caches(self, mock_svc_class):
        from app.services.salesforce_service import SalesforceConnectionManager
        mock_svc_class.return_value = MagicMock(instance_url="test.sf.com")
        mgr = SalesforceConnectionManager()

        svc1 = await mgr.get_service("dev")
        svc2 = await mgr.get_service("dev")
        assert svc1 is svc2
        assert mock_svc_class.call_count == 1
