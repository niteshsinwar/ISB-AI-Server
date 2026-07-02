# project_root/app/services/salesforce_service.py
"""
Base Salesforce service: auth, connection, retry, shared utilities, and
the SalesforceConnectionManager. Track-specific methods live in mixins:
  - AdmissionSFMixin  (admission_sf_service.py)
  - EedlSFMixin       (eedl_sf_service.py)
SalesforceService inherits from both so all existing imports remain unchanged.
"""
import logging
import asyncio
import base64
from simple_salesforce import (
    Salesforce,
    SalesforceAuthenticationFailed,
    SalesforceMalformedRequest,
    SalesforceResourceNotFound,
    SalesforceExpiredSession
)
import requests
from typing import Dict, Any, Optional, Callable

from fastapi import HTTPException, Path

from app.config import (
    SALESFORCE_ORGS, APEX_ENDPOINT_PATHS,
)

logger = logging.getLogger(__name__)


# --- Custom Exception for clear error propagation ---
class SalesforceAPIError(Exception):
    """Custom exception for Salesforce API errors that contains the response text."""


# --- Base class with shared infrastructure ---
class _SalesforceBase:
    """Connection management, retry logic, and shared utilities."""

    def __init__(self, client_id: str, client_secret: str, token_url: str, org_alias: Optional[str] = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
        self.org_alias = org_alias
        self.sf: Optional[Salesforce] = None
        self.instance_url: Optional[str] = None
        self.apex_endpoint_path_map: Dict[str, str] = APEX_ENDPOINT_PATHS
        self._connect()

    def _connect(self):
        logger.info(f"Attempting Salesforce connection for token URL: {self.token_url}")
        if not all([self.client_id, self.client_secret, self.token_url]):
            raise ValueError("Salesforce client credentials must be set for the service.")

        payload = {'grant_type': 'client_credentials', 'client_id': self.client_id, 'client_secret': self.client_secret}
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        try:
            response = requests.post(self.token_url, headers=headers, data=payload, timeout=30)
            response.raise_for_status()
            token_data = response.json()
            access_token, instance_url = token_data.get("access_token"), token_data.get("instance_url")

            if not access_token or not instance_url:
                raise SalesforceAuthenticationFailed(400, "Failed to retrieve access_token or instance_url.")

            self.sf = Salesforce(instance_url=instance_url.rstrip('/'), session_id=access_token)
            self.instance_url = self.sf.sf_instance
            logger.info(f"Successfully connected to Salesforce: {self.instance_url}")

        except requests.exceptions.HTTPError as e:
            error_content = e.response.text if e.response else str(e)
            status_code = e.response.status_code if e.response else 500
            raise SalesforceAuthenticationFailed(status_code, f"Client credentials token request failed: {error_content}")

        except Exception as e:
            raise ValueError(f"An unexpected error occurred during Salesforce connection: {e}")

    def _ensure_connected(self):
        if not self.sf or not self.instance_url:
            self._connect()

    def _call_sf_api_with_retry(self, api_call_func: Callable, *args, **kwargs) -> Any:
        """Calls a Salesforce API function with retry on session expiry."""
        try:
            self._ensure_connected()
            return api_call_func(*args, **kwargs)
        except SalesforceExpiredSession:
            logger.warning(f"Session expired for {self.instance_url}. Reconnecting.")
            self._connect()
            return api_call_func(*args, **kwargs)
        except (SalesforceMalformedRequest, SalesforceResourceNotFound) as e:
            logger.error(f"Salesforce client error: {type(e).__name__}: {e}")
            raise

    def _get_field_value_case_insensitive(self, record: Dict[str, Any], field_name: str) -> Any:
        """Get a field value with case-insensitive key matching."""
        if field_name in record:
            return record[field_name]
        field_lower = field_name.lower()
        for key, value in record.items():
            if key.lower() == field_lower:
                return value
        return None

    def _download_content_version(self, content_version_id: str) -> Dict[str, Any]:
        """Download a ContentVersion file and return base64 payload."""
        handler = getattr(self.sf, 'ContentVersion')
        version_record = self._call_sf_api_with_retry(lambda: handler.get(content_version_id))
        version_data_url = version_record.get('VersionData')
        if not version_data_url:
            raise SalesforceAPIError(f"ContentVersion {content_version_id} has no VersionData URL.")
        full_url = f"https://{self.sf.sf_instance}{version_data_url}"

        def do_download():
            resp = self.sf.session.get(full_url, headers=self.sf.headers, timeout=60)
            resp.raise_for_status()
            return resp.content

        file_bytes = self._call_sf_api_with_retry(do_download)
        return {
            "base64Data": base64.b64encode(file_bytes).decode('utf-8'),
            "fileExtension": version_record.get('FileExtension'),
            "fileName": version_record.get('Title'),
            "lastModifiedDate": version_record.get('LastModifiedDate'),
        }


# --- Import mixins (after base class is defined so they can reference SalesforceAPIError) ---
from app.services.admission_sf_service import AdmissionSFMixin
from app.services.eedl_sf_service import EedlSFMixin


class SalesforceService(AdmissionSFMixin, EedlSFMixin, _SalesforceBase):
    """
    Full Salesforce service combining base infrastructure with track-specific mixins.
    All existing code imports SalesforceService from this module — that continues to work.
    """


# --- Connection Manager & FastAPI Dependencies ---
class SalesforceConnectionManager:
    """Creates, caches, and provides SalesforceService instances for different orgs."""

    def __init__(self):
        self._services: Dict[str, SalesforceService] = {}
        self._lock = asyncio.Lock()

    async def get_service(self, org_alias: str) -> SalesforceService:
        async with self._lock:
            if org_alias not in self._services:
                logger.info(f"No existing service for '{org_alias}'. Creating new connection.")
                if org_alias not in SALESFORCE_ORGS:
                    raise ValueError(f"Unknown Salesforce org alias: '{org_alias}'.")
                org_config = SALESFORCE_ORGS[org_alias]
                if not all(val for val in org_config.values()):
                    raise HTTPException(status_code=404, detail=f"Config for org '{org_alias}' is incomplete.")
                try:
                    service = SalesforceService(
                        client_id=org_config['client_id'],
                        client_secret=org_config['client_secret'],
                        token_url=org_config['token_url'],
                        org_alias=org_alias
                    )
                    self._services[org_alias] = service
                except Exception as e:
                    raise HTTPException(status_code=503, detail=f"SF Service for '{org_alias}' unavailable: {e}")
            service = self._services[org_alias]
            service.org_alias = org_alias
            return service


_sf_connection_manager = SalesforceConnectionManager()


async def get_salesforce_service(
    org_alias: str = Path(..., description="The SF org alias (e.g., 'dev', 'uat')")
) -> SalesforceService:
    """FastAPI dependency that provides a SalesforceService instance for a specific org."""
    if org_alias not in SALESFORCE_ORGS:
        raise HTTPException(status_code=404, detail=f"The SF org '{org_alias}' is not configured.")
    return await _sf_connection_manager.get_service(org_alias)


async def get_default_dev_service() -> SalesforceService:
    """FastAPI dependency that always provides a SalesforceService for the 'dev' org."""
    return await _sf_connection_manager.get_service("dev")
