import logging
import asyncio
import json
from simple_salesforce import (
    Salesforce,
    SalesforceAuthenticationFailed,
    SalesforceMalformedRequest,
    SalesforceResourceNotFound,
    SalesforceExpiredSession
)
import requests
from typing import Dict, Any, Optional, Callable, List

from fastapi import HTTPException, Depends, Path

from app.config import (
    SALESFORCE_ORGS, APEX_ENDPOINT_PATHS,
    APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME,
    AVS_APPLICATION_LOOKUP_FIELD, AVS_CONTACT_LOOKUP_FIELD,
    AVS_EDUCATION_HISTORY_LOOKUP_FIELD, AVS_TEST_LOOKUP_FIELD,
    AVS_AFFILIATION_LOOKUP_FIELD, AVS_REPORT_FIELD, AVS_NAME_FIELD,
    AVS_OVERALL_FEEDBACK_FIELD, AVS_CONFIDENCE_FIELD, AVS_TASK_DCI_LOOKUP_FIELD,
    AI_SERVER_JOB_OBJECT_API_NAME, AIJ_APPLICATION_LOOKUP_FIELD, AIJ_JOB_ID_FIELD,
    AIJ_STATUS_FIELD, AIJ_MESSAGE_FIELD, AIJ_PROGRESS_FIELD, AIJ_CLIENT_FP_FIELD
)

logger = logging.getLogger(__name__)

class SalesforceService:
    """A class to represent a connection to a single Salesforce org."""
    def __init__(self, client_id: str, client_secret: str, token_url: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
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
                raise SalesforceAuthenticationFailed(400, "Failed to retrieve access_token or instance_url from Salesforce response.")
            
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
        try:
            self._ensure_connected()
            return api_call_func(*args, **kwargs)
        except (SalesforceAuthenticationFailed, SalesforceExpiredSession, requests.exceptions.ConnectionError):
            logger.warning("Salesforce session issue detected. Reconnecting and retrying.")
            self._connect()
            return api_call_func(*args, **kwargs)

    def get_record_detail_from_apex(self, record_id: str, sobject_api_name_key: str) -> Optional[Dict[str, Any]]:
        if not (isinstance(record_id, str) and (len(record_id) == 15 or len(record_id) == 18)):
            logger.error(f"Invalid Salesforce ID format provided: {record_id}")
            return None
        endpoint_path_segment = self.apex_endpoint_path_map.get(sobject_api_name_key)
        if not endpoint_path_segment:
            logger.error(f"No Apex endpoint path configured for key: {sobject_api_name_key}")
            return None
        
        full_url = f"https://{self.sf.sf_instance}/services/apexrest/{endpoint_path_segment.strip('/')}/{record_id}"
        logger.info(f"Calling Apex REST: POST {full_url}")
        def do_apex_post():
            if not self.sf or not self.sf.session:
                raise RuntimeError("Salesforce client session not properly initialized for Apex call.")
            return self.sf.session.post(full_url, headers=self.sf.headers, json={}, timeout=60)
        try:
            response = self._call_sf_api_with_retry(do_apex_post)
            response.raise_for_status()
            return response.json() if response.content else {}
        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP error calling Apex endpoint {full_url}. Status: {e.response.status_code}. Response: {e.response.text}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"An unexpected error occurred calling Apex endpoint {full_url}: {e}", exc_info=True)
            return None

    async def upsert_ai_server_job(self, job_id: str, application_id: str, status: str, **kwargs) -> Optional[str]:
        soql = f"SELECT Id FROM {AI_SERVER_JOB_OBJECT_API_NAME} WHERE {AIJ_APPLICATION_LOOKUP_FIELD} = '{application_id}' LIMIT 1"
        def do_query():
            return self._call_sf_api_with_retry(self.sf.query, soql)
        try:
            result = await asyncio.get_event_loop().run_in_executor(None, do_query)
        except Exception as e:
            logger.error(f"Failed to query for existing job for app {application_id}: {e}", exc_info=True)
            return None
        payload = {
            AIJ_APPLICATION_LOOKUP_FIELD: application_id,
            AIJ_JOB_ID_FIELD: job_id,
            AIJ_STATUS_FIELD: status,
        }
        if message := kwargs.get('message'): payload[AIJ_MESSAGE_FIELD] = message[:131072]
        if progress_details := kwargs.get('progress_details'): payload[AIJ_PROGRESS_FIELD] = progress_details
        if client_fp := kwargs.get('client_fingerprint'): payload[AIJ_CLIENT_FP_FIELD] = client_fp
        handler = getattr(self.sf, AI_SERVER_JOB_OBJECT_API_NAME)
        try:
            if result['totalSize'] > 0:
                existing_id = result['records'][0]['Id']
                update_payload = payload.copy()
                del update_payload[AIJ_APPLICATION_LOOKUP_FIELD]
                def do_update():
                    return self._call_sf_api_with_retry(handler.update, existing_id, update_payload)
                response = await asyncio.get_event_loop().run_in_executor(None, do_update)
                return existing_id if response == 204 else None
            else:
                def do_create():
                    return self._call_sf_api_with_retry(handler.create, payload)
                response = await asyncio.get_event_loop().run_in_executor(None, do_create)
                return response.get('id') if isinstance(response, dict) and response.get('success') else None
        except Exception as e:
            logger.error(f"Exception during upsert for job {job_id}: {e}", exc_info=True)
            return None

    async def get_latest_ai_server_job(self, application_id: str) -> Optional[Dict[str, Any]]:
        soql = f"SELECT Id, {AIJ_JOB_ID_FIELD}, {AIJ_STATUS_FIELD}, {AIJ_MESSAGE_FIELD}, CreatedDate, LastModifiedDate, {AIJ_PROGRESS_FIELD}, {AIJ_CLIENT_FP_FIELD} FROM {AI_SERVER_JOB_OBJECT_API_NAME} WHERE {AIJ_APPLICATION_LOOKUP_FIELD} = '{application_id}' ORDER BY CreatedDate DESC LIMIT 1"
        def do_query(): return self._call_sf_api_with_retry(self.sf.query, soql)
        try:
            result = await asyncio.get_event_loop().run_in_executor(None, do_query)
            if result['totalSize'] > 0:
                rec = result['records'][0]
                progress = json.loads(rec.get(AIJ_PROGRESS_FIELD) or '{}')
                return {
                    "job_id": rec.get(AIJ_JOB_ID_FIELD), "application_id": application_id,
                    "status": rec.get(AIJ_STATUS_FIELD), "message": rec.get(AIJ_MESSAGE_FIELD),
                    "created_at": rec.get("CreatedDate"), "last_updated_at": rec.get("LastModifiedDate"),
                    "progress": progress, "salesforce_job_record_id": rec.get("Id"),
                    "client_fingerprint": rec.get(AIJ_CLIENT_FP_FIELD)
                }
            return None
        except Exception as e:
            logger.error(f"Error getting latest job for App {application_id}: {e}", exc_info=True)
            return None
            
    def upsert_verification_summary( self, application_id: str, report_content: str, name_value: str, **kwargs) -> Optional[str]:
        summary_obj_name = APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME
        payload = {AVS_REPORT_FIELD: report_content, AVS_NAME_FIELD: name_value, AVS_APPLICATION_LOOKUP_FIELD: application_id}
        if val := kwargs.get('overall_feedback'): payload[AVS_OVERALL_FEEDBACK_FIELD] = val
        if val := kwargs.get('confidence_range'): payload[AVS_CONFIDENCE_FIELD] = val
        secondary_field, secondary_id = None, None
        if val := kwargs.get('contact_id'): secondary_field, secondary_id = AVS_CONTACT_LOOKUP_FIELD, val
        elif val := kwargs.get('education_history_id'): secondary_field, secondary_id = AVS_EDUCATION_HISTORY_LOOKUP_FIELD, val
        elif val := kwargs.get('test_id'): secondary_field, secondary_id = AVS_TEST_LOOKUP_FIELD, val
        elif val := kwargs.get('affiliation_id'): secondary_field, secondary_id = AVS_AFFILIATION_LOOKUP_FIELD, val
        else: return None
        soql = f"SELECT Id FROM {summary_obj_name} WHERE {AVS_APPLICATION_LOOKUP_FIELD} = '{application_id}' AND {secondary_field} = '{secondary_id}' LIMIT 1"
        try:
            handler = getattr(self.sf, summary_obj_name)
            result = self._call_sf_api_with_retry(self.sf.query, soql)
            if result.get('totalSize', 0) > 0:
                existing_id = result['records'][0]['Id']
                update_payload = payload.copy()
                update_payload.pop(AVS_APPLICATION_LOOKUP_FIELD, None)
                status = self._call_sf_api_with_retry(handler.update, existing_id, update_payload)
                return existing_id if status == 204 else None
            else:
                payload[secondary_field] = secondary_id
                res = self._call_sf_api_with_retry(handler.create, payload)
                return res.get('id') if res.get('success') else None
        except Exception as e:
            logger.error(f"Error upserting verification summary: {e}", exc_info=True)
            return None
            
    def link_summary_to_related_items(self, summary_id: str, task_id: Optional[str], dci_id: Optional[str], overall_feedback: Optional[str] = None) -> None:
        if not summary_id: return
        if dci_id:
            try:
                handler = getattr(self.sf, 'DocumentChecklistItem')
                self._call_sf_api_with_retry(handler.update, dci_id, {AVS_TASK_DCI_LOOKUP_FIELD: summary_id})
                logger.info(f"Successfully linked Summary {summary_id} to DCI {dci_id}.")
            except Exception as e: 
                logger.error(f"Failed to update DocumentChecklistItem {dci_id}: {e}")

    # CRITICAL MODIFICATION: This method now accepts an optional 'filtering_criteria' dictionary
    # to build a more specific SOQL query, preventing unsupported records from being fetched.
    def get_directly_related_record_ids(
        self,
        parent_record_id: str,
        child_object_api_name: str,
        lookup_field_on_child_to_parent: str,
        filtering_criteria: Optional[Dict[str, Any]] = None,
        order_by: Optional[str] = None,
        limit: Optional[int] = None
    ) -> List[str]:
        """
        Fetches IDs of child records related to a parent.
        Can optionally apply filtering, sorting, and limiting criteria.

        Args:
            parent_record_id: The ID of the parent record (e.g., an Application).
            child_object_api_name: The API name of the child object to query.
            lookup_field_on_child_to_parent: The lookup field on the child to the parent.
            filtering_criteria: A dictionary like {"field_api_name": "...", "allowed_values": [...]}.
            order_by: A string for the SOQL ORDER BY clause (e.g., "CreatedDate DESC").
            limit: An integer for the SOQL LIMIT clause.
        """
        if not all([child_object_api_name, lookup_field_on_child_to_parent]):
            raise ValueError("Missing required arguments 'child_object_api_name' or 'lookup_field_on_child_to_parent'.")

        # Start building the base SOQL query
        soql = f"SELECT Id FROM {child_object_api_name} WHERE {lookup_field_on_child_to_parent} = '{parent_record_id}'"

        # Dynamically add the filtering clause if criteria are provided and valid
        if filtering_criteria and isinstance(filtering_criteria, dict):
            field_name = filtering_criteria.get("field_api_name")
            allowed_values = filtering_criteria.get("allowed_values")

            if field_name and isinstance(allowed_values, list) and allowed_values:
                formatted_values = ', '.join(f"'{val}'" for val in allowed_values)
                soql += f" AND {field_name} IN ({formatted_values})"
            else:
                logger.warning(f"Invalid or empty 'filtering_criteria' provided for {child_object_api_name}. Ignoring.")

        # NEW: Dynamically add ORDER BY and LIMIT clauses if provided
        if order_by:
            soql += f" ORDER BY {order_by}"
        if limit is not None and isinstance(limit, int):
            soql += f" LIMIT {limit}"

        logger.info(f"Executing filtered query for related records: {soql}")
        try:
            result = self._call_sf_api_with_retry(self.sf.query_all, soql)
            return [rec['Id'] for rec in result['records']]
        except Exception as e:
            logger.error(f"Error getting related record IDs with query '{soql}': {e}", exc_info=True)
            return []

    def get_target_ids_via_junction(self, parent_record_id: str, **kwargs) -> List[str]:
        junc_obj, junc_parent, junc_target = kwargs.get('junction_object_api_name'), kwargs.get('junction_field_to_parent'), kwargs.get('junction_field_to_target')
        if not all([junc_obj, junc_parent, junc_target]): raise ValueError("Missing required arguments.")
        soql = f"SELECT {junc_target} FROM {junc_obj} WHERE {junc_parent} = '{parent_record_id}' AND {junc_target} != NULL"
        try:
            result = self._call_sf_api_with_retry(self.sf.query_all, soql)
            return [rec[junc_target] for rec in result['records'] if rec.get(junc_target)]
        except Exception as e:
            logger.error(f"Error getting target IDs via junction: {e}", exc_info=True)
            return []

# --- NEW: Salesforce Connection Manager ---
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
                        token_url=org_config['token_url']
                    )
                    self._services[org_alias] = service
                except Exception as e:
                    raise HTTPException(status_code=503, detail=f"SF Service for '{org_alias}' unavailable: {e}")

            return self._services[org_alias]

# --- MODIFIED: Dependency Injection Logic ---
_sf_connection_manager = SalesforceConnectionManager()

async def get_salesforce_service(org_alias: str = Path(..., description="The SF org alias (e.g., 'dev', 'uat')")) -> SalesforceService:
    """FastAPI dependency that provides a SalesforceService instance for a specific org from the request path."""
    if org_alias not in SALESFORCE_ORGS:
        raise HTTPException(status_code=404, detail=f"The SF org '{org_alias}' is not configured.")
    return await _sf_connection_manager.get_service(org_alias)

async def get_default_dev_service() -> SalesforceService:
    """FastAPI dependency that always provides a SalesforceService instance for the 'dev' org."""
    logger.info("No org prefix in URL. Defaulting to 'dev' org.")
    return await _sf_connection_manager.get_service("dev")