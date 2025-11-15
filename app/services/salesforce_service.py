# project_root/app/services/salesforce_service.py

import logging
import asyncio
import json
import base64
import time
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
    AVS_OVERALL_FEEDBACK_FIELD, AVS_MISMATCHED_LIST_FIELD, AVS_CONFIDENCE_FIELD, AVS_TASK_DCI_LOOKUP_FIELD,
    AI_SERVER_JOB_OBJECT_API_NAME, AIJ_APPLICATION_LOOKUP_FIELD, AIJ_JOB_ID_FIELD,
    AIJ_STATUS_FIELD, AIJ_MESSAGE_FIELD, AIJ_PROGRESS_FIELD, AIJ_CLIENT_FP_FIELD,
    DCI_OBJECT_API_NAME, DCI_STATUS_FIELD,
    APPLICATION_OBJECT_API_NAME, APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP
)

logger = logging.getLogger(__name__)

# --- Custom Exception for clear error propagation ---
class SalesforceAPIError(Exception):
    """Custom exception for Salesforce API errors that contains the response text."""
    pass


class SalesforceService:
    """A class to represent a connection to a single Salesforce org."""
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
        """
        Calls a Salesforce API function with a robust retry mechanism.
        If any error occurs, it forces a reconnection and retries the call once.
        """
        try:
            self._ensure_connected()
            return api_call_func(*args, **kwargs)
        except Exception as e:
            # Catch any exception on the first attempt.
            logger.warning(
                f"An operation failed with error: {type(e).__name__}. "
                f"Attempting a forced reconnect and retrying the operation once."
            )
            
            # Force a new connection session.
            self._connect()
            
            # Retry the call. If this second attempt fails, the exception will
            # be raised, as requested.
            return api_call_func(*args, **kwargs)

    def get_record_detail_from_apex(self, record_id: str, sobject_api_name_key: str) -> Dict[str, Any]:
        if not (isinstance(record_id, str) and (len(record_id) == 15 or len(record_id) == 18)):
            raise SalesforceAPIError(f"Invalid Salesforce ID format provided: {record_id}")
        
        self._ensure_connected()
        
        endpoint_path_segment = self.apex_endpoint_path_map.get(sobject_api_name_key)
        if not endpoint_path_segment:
            raise SalesforceAPIError(f"No Apex endpoint path configured for key: {sobject_api_name_key}")
        
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
            error_details = e.response.text if e.response else str(e)
            logger.error(f"HTTP error calling Apex endpoint {full_url}. Status: {e.response.status_code}. Response: {error_details}", exc_info=True)
            raise SalesforceAPIError(f"Salesforce API returned an error: {error_details}")
        except Exception as e:
            logger.error(f"An unexpected error occurred calling Apex endpoint {full_url}: {e}", exc_info=True)
            raise SalesforceAPIError(f"An unexpected error occurred during Apex call: {e}")

    async def upsert_ai_server_job(self, job_id: str, application_id: str, status: str, **kwargs) -> str:
        self._ensure_connected()
        soql = f"SELECT Id FROM {AI_SERVER_JOB_OBJECT_API_NAME} WHERE {AIJ_APPLICATION_LOOKUP_FIELD} = '{application_id}' LIMIT 1"
        def do_query():
            return self._call_sf_api_with_retry(self.sf.query, soql)
        
        try:
            result = await asyncio.get_event_loop().run_in_executor(None, do_query)
        except Exception as e:
            logger.error(f"Failed to query for existing job for app {application_id}: {e}", exc_info=True)
            raise SalesforceAPIError(f"Failed to query for existing AI Server Job: {e}")

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
                if response != 204:
                    raise SalesforceAPIError(f"Failed to update AI Server Job record. Status: {response}")
                return existing_id
            else:
                def do_create():
                    return self._call_sf_api_with_retry(handler.create, payload)
                response = await asyncio.get_event_loop().run_in_executor(None, do_create)
                if not (isinstance(response, dict) and response.get('success')):
                    raise SalesforceAPIError(f"Failed to create AI Server Job record. Response: {response}")
                return response.get('id')
        except Exception as e:
            logger.error(f"Exception during upsert for job {job_id}: {e}", exc_info=True)
            raise SalesforceAPIError(f"Upsert for AI Server Job record failed: {e}")

    async def get_latest_ai_server_job(self, application_id: str) -> Optional[Dict[str, Any]]:
        self._ensure_connected()
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
            return None # It's correct to return None if no job is found.
        except Exception as e:
            logger.error(f"Error getting latest job for App {application_id}: {e}", exc_info=True)
            raise SalesforceAPIError(f"Failed to query for latest job from Salesforce: {e}")
            
    def upsert_verification_summary( self, application_id: str, report_content: str, name_value: str, **kwargs) -> str:
        self._ensure_connected()
        summary_obj_name = APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME
        payload = {AVS_REPORT_FIELD: report_content, AVS_NAME_FIELD: name_value, AVS_APPLICATION_LOOKUP_FIELD: application_id}
        if val := kwargs.get('overall_feedback'): payload[AVS_OVERALL_FEEDBACK_FIELD] = val
        if val := kwargs.get('confidence_range'): payload[AVS_CONFIDENCE_FIELD] = val
        if val := kwargs.get('mismatched_field_list'): payload[AVS_MISMATCHED_LIST_FIELD] = val
        
        secondary_field, secondary_id = None, None
        if val := kwargs.get('contact_id'): secondary_field, secondary_id = AVS_CONTACT_LOOKUP_FIELD, val
        elif val := kwargs.get('education_history_id'): secondary_field, secondary_id = AVS_EDUCATION_HISTORY_LOOKUP_FIELD, val
        elif val := kwargs.get('test_id'): secondary_field, secondary_id = AVS_TEST_LOOKUP_FIELD, val
        elif val := kwargs.get('affiliation_id'): secondary_field, secondary_id = AVS_AFFILIATION_LOOKUP_FIELD, val
        
        soql = f"SELECT Id FROM {summary_obj_name} WHERE {AVS_APPLICATION_LOOKUP_FIELD} = '{application_id}'"
        if secondary_field and secondary_id:
            soql += f" AND {secondary_field} = '{secondary_id}'"
        else:
            soql += f" AND {AVS_NAME_FIELD} = '{name_value}'"
        soql += " LIMIT 1"

        try:
            handler = getattr(self.sf, summary_obj_name)
            result = self._call_sf_api_with_retry(self.sf.query, soql)
            
            if result.get('totalSize', 0) > 0:
                existing_id = result['records'][0]['Id']
                update_payload = payload.copy()
                update_payload.pop(AVS_APPLICATION_LOOKUP_FIELD, None)
                status = self._call_sf_api_with_retry(handler.update, existing_id, update_payload)
                if status != 204:
                    raise SalesforceAPIError(f"Failed to update verification summary. Status: {status}")
                return existing_id
            else:
                if secondary_field and secondary_id: payload[secondary_field] = secondary_id
                res = self._call_sf_api_with_retry(handler.create, payload)
                if not (isinstance(res, dict) and res.get('success')):
                    raise SalesforceAPIError(f"Failed to create verification summary. Response: {res}")
                return res.get('id')
        except Exception as e:
            logger.error(f"Error upserting verification summary with SOQL '{soql}': {e}", exc_info=True)
            raise SalesforceAPIError(f"Upsert for verification summary failed: {e}")
            
    def link_summary_to_related_items(self, summary_id: str, task_id: Optional[str], dci_id: Optional[str], overall_feedback: Optional[str] = None):
        if not summary_id: return
        self._ensure_connected()
        if dci_id:
            try:
                handler = getattr(self.sf, 'DocumentChecklistItem')
                self._call_sf_api_with_retry(handler.update, dci_id, {AVS_TASK_DCI_LOOKUP_FIELD: summary_id})
                logger.info(f"Successfully linked Summary {summary_id} to DCI {dci_id}.")
            except Exception as e: 
                logger.error(f"Failed to update DocumentChecklistItem {dci_id}: {e}")
                raise SalesforceAPIError(f"Failed to link summary {summary_id} to DCI {dci_id}: {e}")

    def get_directly_related_record_ids(
        self,
        parent_record_id: str,
        child_object_api_name: str,
        lookup_field_on_child_to_parent: str,
        filtering_criteria: Optional[Dict[str, Any]] = None,
        order_by: Optional[str] = None,
        limit: Optional[int] = None
    ) -> List[str]:
        self._ensure_connected()
        if not all([child_object_api_name, lookup_field_on_child_to_parent]):
            raise ValueError("Missing required arguments 'child_object_api_name' or 'lookup_field_on_child_to_parent'.")

        soql = f"SELECT Id FROM {child_object_api_name} WHERE {lookup_field_on_child_to_parent} = '{parent_record_id}'"

        # MODIFICATION START: Handle more complex filtering logic
        if filtering_criteria:
            # Normalize to a list to handle both a single dict and a list of dicts
            filters = filtering_criteria if isinstance(filtering_criteria, list) else [filtering_criteria]

            for f in filters:
                if not isinstance(f, dict):
                    logging.warning(f"Ignoring invalid filter item for {child_object_api_name}: {f}")
                    continue

                # Handle subquery filter criteria
                if "subquery_filter" in f:
                    sqf = f.get("subquery_filter", {})
                    subquery = sqf.get("subquery", {})
                    if all([sqf.get("field"), sqf.get("operator"), subquery.get("object"), subquery.get("select_field"), subquery.get("where_clause")]):
                        soql += (
                            f" AND {sqf['field']} {sqf['operator']} "
                            f"(SELECT {subquery['select_field']} FROM {subquery['object']} "
                            f"WHERE {subquery['where_clause']})"
                        )
                    else:
                        logging.warning(f"Invalid or incomplete subquery filter for {child_object_api_name}. Ignoring.")
                    continue

                # Handle standard field filters
                field_name = f.get("field_api_name")
                if not field_name:
                    logging.warning(f"Filter for {child_object_api_name} is missing 'field_api_name'. Ignoring.")
                    continue

                if "allowed_values" in f:
                    allowed_values = f.get("allowed_values")
                    if isinstance(allowed_values, list) and allowed_values:
                        formatted_values = ', '.join(f"'{val}'" for val in allowed_values)
                        soql += f" AND {field_name} IN ({formatted_values})"
                    else:
                        logging.warning(f"Filter key 'allowed_values' for {child_object_api_name} is not a valid list. Ignoring.")
                elif "operator" in f and "value" in f:
                    operator = f.get("operator")
                    value = f.get("value")
                    soql += f" AND {field_name} {operator} '{value}'"
        # MODIFICATION END

        if order_by:
            soql += f" ORDER BY {order_by}"
        if limit is not None and isinstance(limit, int):
            soql += f" LIMIT {limit}"

        logger.info(f"Executing filtered query for related records: {soql}")
        try:
            # Assuming _call_sf_api_with_retry and self.sf.query_all are defined
            result = self._call_sf_api_with_retry(self.sf.query_all, soql)
            return [rec['Id'] for rec in result['records']]
        except Exception as e:
            logger.error(f"Error getting related record IDs with query '{soql}': {e}", exc_info=True)
            raise SalesforceAPIError(f"Failed to get related record IDs with query '{soql}': {e}")

    async def get_contact_id_for_application(self, application_id: str) -> str:
        if not application_id:
            raise SalesforceAPIError("Application ID is required to fetch contact ID.")
        self._ensure_connected()
        soql = f"SELECT {APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP} FROM {APPLICATION_OBJECT_API_NAME} WHERE Id = '{application_id}' LIMIT 1"
        try:
            def do_query():
                return self._call_sf_api_with_retry(self.sf.query, soql)
            result = await asyncio.get_event_loop().run_in_executor(None, do_query)
            if result.get('totalSize', 0) > 0:
                contact_id = result['records'][0].get(APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP)
                if contact_id:
                    return contact_id
            raise SalesforceAPIError(f"Could not find a Contact ID on Application {application_id}.")
        except Exception as e:
            if not isinstance(e, SalesforceAPIError):
                logger.error(f"Failed to query for Contact ID on Application {application_id}: {e}", exc_info=True)
                raise SalesforceAPIError(f"Failed to query for Contact ID on Application {application_id}: {e}")
            raise

    def get_dci_document_data(self, dci_id: str) -> Dict[str, Any]:
        if not dci_id:
            raise SalesforceAPIError("DCI ID is required to fetch document data.")
        self._ensure_connected()
        content_version_id = None
        max_retries = 4
        retry_delay_seconds = 3
        for attempt in range(max_retries):
            try:
                soql_link = f"SELECT ContentDocument.LatestPublishedVersionId FROM ContentDocumentLink WHERE LinkedEntityId = '{dci_id}' ORDER BY SystemModstamp DESC LIMIT 1"
                result_link = self._call_sf_api_with_retry(self.sf.query, soql_link)
                if result_link.get('totalSize', 0) > 0:
                    content_version_id = result_link['records'][0]['ContentDocument']['LatestPublishedVersionId']
                    logger.info(f"Found ContentVersion ID '{content_version_id}' for DCI {dci_id} on attempt {attempt + 1}.")
                    break 
                logger.warning(f"Attempt {attempt + 1}/{max_retries}: ContentDocumentLink not found for DCI {dci_id}. Retrying...")
                time.sleep(retry_delay_seconds)
            except Exception as e:
                logger.error(f"Exception during SOQL query for DCI {dci_id} on attempt {attempt+1}: {e}", exc_info=True)
                time.sleep(retry_delay_seconds)
        
        if not content_version_id:
            raise SalesforceAPIError(f"FINAL FAILURE: Could not find any ContentDocumentLink for DCI {dci_id} after {max_retries} attempts.")
        
        try:
            handler = getattr(self.sf, 'ContentVersion')
            version_record = self._call_sf_api_with_retry(handler.get, content_version_id)
            version_data_url = version_record.get('VersionData')
            if not version_data_url:
                raise SalesforceAPIError(f"Found ContentVersion {content_version_id}, but it has no 'VersionData' URL.")
            
            full_download_url = f"https://{self.sf.sf_instance}{version_data_url}"
            def do_download():
                response = self.sf.session.get(full_download_url, headers=self.sf.headers, timeout=60)
                response.raise_for_status()
                return response.content
            
            file_bytes = self._call_sf_api_with_retry(do_download)
            logger.info(f"Successfully downloaded {len(file_bytes)} bytes for ContentVersion {content_version_id}.")
            base64_data = base64.b64encode(file_bytes).decode('utf-8')
            return {"documentPayload": {
                "fileName": version_record.get('Title'),
                "fileExtension": version_record.get('FileExtension'),
                "base64Data": base64_data
            }}
        except Exception as e:
            logger.error(f"An unexpected error occurred during file download for CV ID {content_version_id}: {e}", exc_info=True)
            raise SalesforceAPIError(f"File download failed for ContentVersion {content_version_id}: {e}")


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

async def get_salesforce_service( org_alias: str = Path(..., description="The SF org alias (e.g., 'dev', 'uat')")) -> SalesforceService:
    """FastAPI dependency that provides a SalesforceService instance for a specific org from the request path."""
    if org_alias not in SALESFORCE_ORGS:
        raise HTTPException(status_code=404, detail=f"The SF org '{org_alias}' is not configured.")
    return await _sf_connection_manager.get_service(org_alias)

async def get_default_dev_service() -> SalesforceService:
    """FastAPI dependency that always provides a SalesforceService instance for the 'dev' org."""
    logger.info("No org prefix in URL. Defaulting to 'dev' org.")
    return await _sf_connection_manager.get_service("dev")
