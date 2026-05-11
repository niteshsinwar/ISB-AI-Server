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
    AIJ_STATUS_FIELD, AIJ_MESSAGE_FIELD, AIJ_PROGRESS_FIELD, AIJ_CLIENT_FP_FIELD, AIJ_LOGS_FIELD,
    DCI_OBJECT_API_NAME, DCI_STATUS_FIELD,
    APPLICATION_OBJECT_API_NAME, APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP,
    # EEDL imports
    AIJ_OPPORTUNITY_LOOKUP_FIELD,
    EEDL_OPPORTUNITY_OBJECT_API_NAME, EEDL_EDUCATION_OBJECT_API_NAME,
    EEDL_OPP_CONTACT_LOOKUP_FIELD, EEDL_OPP_CITIZENSHIP_FIELD,
    EEDL_EDU_CONTACT_LOOKUP_FIELD, EEDL_EDU_DEGREE_FIELD, EEDL_EDU_UNIVERSITY_FIELD,
    EEDL_EDU_GPA_FIELD, EEDL_EDU_START_DATE_FIELD, EEDL_EDU_END_DATE_FIELD,
    EEDL_VS_OBJECT_API_NAME, EEDL_VS_OPPORTUNITY_LOOKUP_FIELD, EEDL_VS_EDUCATION_LOOKUP_FIELD,
    EEDL_VS_RECORD_TYPE_FIELD, EEDL_VS_VERIFICATION_STATUS_FIELD, EEDL_VS_CONFIDENCE_FIELD,
    EEDL_VS_REPORT_FIELD, EEDL_VS_OVERALL_FEEDBACK_FIELD, EEDL_VS_MISMATCHED_FIELDS_FIELD,
    EEDL_VS_NAME_FIELD, EEDL_VS_RECORD_TYPE_ID_DOCUMENT, EEDL_VS_RECORD_TYPE_EDUCATION,
    EEDL_FILE_MATCHING_CONFIG,
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
        Only reconnects on session expiry to avoid wasting OAuth tokens.

        CRITICAL: api_call_func should be a lambda that accesses self.sf at call time,
        not a bound method captured at closure creation time.
        """
        try:
            self._ensure_connected()
            # If api_call_func is a lambda, it will access current self.sf
            return api_call_func(*args, **kwargs)
        except SalesforceExpiredSession as e:
            # Session expired - reconnect is appropriate
            logger.warning(
                f"Session expired for {self.instance_url}. "
                f"Reconnecting and retrying once."
            )

            # Force a new connection session
            self._connect()

            # CRITICAL: Since we reconnected, self.sf is now a NEW object.
            # If api_call_func is a lambda like: lambda: self.sf.query(soql)
            # it will use the NEW self.sf when called here.
            return api_call_func(*args, **kwargs)
        except (SalesforceMalformedRequest, SalesforceResourceNotFound) as e:
            # Client errors - don't retry, these won't be fixed by reconnecting
            logger.error(f"Salesforce client error: {type(e).__name__}: {e}")
            raise
        # Let other exceptions bubble up without retry

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

    def _get_field_value_case_insensitive(self, record: Dict[str, Any], field_name: str) -> Any:
        """
        Get a field value from a Salesforce record with case-insensitive key matching.
        Salesforce SOQL is case-insensitive but returns field names with their exact API name casing.
        """
        # Direct match first
        if field_name in record:
            return record[field_name]
        # Case-insensitive fallback
        field_lower = field_name.lower()
        for key, value in record.items():
            if key.lower() == field_lower:
                return value
        return None

    async def upsert_ai_server_job(self, job_id: str, application_id: str, status: str, **kwargs) -> str:
        """
        Upsert AI Server Job record.

        IMPORTANT: Logs field is ONLY updated when explicitly passed via kwargs['logs'].
        During intermediate status updates (processing, progress), logs should NOT be passed
        to avoid any risk of clearing or overwriting existing logs.
        Logs are ONLY written at job completion (success or failure).
        """
        self._ensure_connected()
        soql = f"SELECT Id FROM {AI_SERVER_JOB_OBJECT_API_NAME} WHERE {AIJ_APPLICATION_LOOKUP_FIELD} = '{application_id}' ORDER BY CreatedDate DESC LIMIT 1"
        def do_query():
            # Use lambda to access self.sf at call time, not closure creation time
            return self._call_sf_api_with_retry(lambda: self.sf.query(soql))

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

        # CRITICAL: Only include logs field when explicitly passed.
        # This ensures logs are ONLY written at job completion, never during intermediate updates.
        # If logs is None or not passed, the field is completely OMITTED from the update payload,
        # which means Salesforce will leave the existing value untouched.
        logs = kwargs.get('logs')
        if logs is not None and logs:  # Only if explicitly passed and non-empty
            payload[AIJ_LOGS_FIELD] = logs[:131072]
            logger.info(f"Upserting job {job_id}: Included logs payload (len={len(payload[AIJ_LOGS_FIELD])})")
        else:
            logger.debug(f"Upserting job {job_id}: Logs field OMITTED (will not modify existing logs)")

        handler = getattr(self.sf, AI_SERVER_JOB_OBJECT_API_NAME)

        try:
            if result['totalSize'] > 0:
                record = result['records'][0]
                existing_id = self._get_field_value_case_insensitive(record, 'Id')
                update_payload = payload.copy()
                del update_payload[AIJ_APPLICATION_LOOKUP_FIELD]

                # NO preservation logic needed anymore.
                # If logs weren't passed, AIJ_LOGS_FIELD is not in update_payload,
                # which means Salesforce will NOT modify the existing logs value.
                # This is the correct behavior - logs are ONLY updated at job completion.

                def do_update():
                    return self._call_sf_api_with_retry(lambda: handler.update(existing_id, update_payload))
                response = await asyncio.get_event_loop().run_in_executor(None, do_update)
                if response != 204:
                    raise SalesforceAPIError(f"Failed to update AI Server Job record. Status: {response}")
                return existing_id
            else:
                def do_create():
                    return self._call_sf_api_with_retry(lambda: handler.create(payload))
                response = await asyncio.get_event_loop().run_in_executor(None, do_create)
                if not (isinstance(response, dict) and response.get('success')):
                    raise SalesforceAPIError(f"Failed to create AI Server Job record. Response: {response}")
                return response.get('id')
        except Exception as e:
            logger.error(f"Exception during upsert for job {job_id}: {e}", exc_info=True)
            raise SalesforceAPIError(f"Upsert for AI Server Job record failed: {e}")

    async def get_latest_ai_server_job(self, application_id: str) -> Optional[Dict[str, Any]]:
        self._ensure_connected()
        soql = f"SELECT Id, {AIJ_JOB_ID_FIELD}, {AIJ_STATUS_FIELD}, {AIJ_MESSAGE_FIELD}, CreatedDate, LastModifiedDate, {AIJ_PROGRESS_FIELD}, {AIJ_CLIENT_FP_FIELD}, {AIJ_LOGS_FIELD} FROM {AI_SERVER_JOB_OBJECT_API_NAME} WHERE {AIJ_APPLICATION_LOOKUP_FIELD} = '{application_id}' ORDER BY CreatedDate DESC LIMIT 1"
        def do_query(): return self._call_sf_api_with_retry(lambda: self.sf.query(soql))
        try:
            result = await asyncio.get_event_loop().run_in_executor(None, do_query)
            if result['totalSize'] > 0:
                rec = result['records'][0]
                # Use case-insensitive field access to handle Salesforce field name casing variations
                progress_raw = self._get_field_value_case_insensitive(rec, AIJ_PROGRESS_FIELD) or '{}'
                progress = json.loads(progress_raw)
                return {
                    "job_id": self._get_field_value_case_insensitive(rec, AIJ_JOB_ID_FIELD),
                    "application_id": application_id,
                    "status": self._get_field_value_case_insensitive(rec, AIJ_STATUS_FIELD),
                    "message": self._get_field_value_case_insensitive(rec, AIJ_MESSAGE_FIELD),
                    "created_at": self._get_field_value_case_insensitive(rec, "CreatedDate"),
                    "last_updated_at": self._get_field_value_case_insensitive(rec, "LastModifiedDate"),
                    "progress": progress,
                    "salesforce_job_record_id": self._get_field_value_case_insensitive(rec, "Id"),
                    "client_fingerprint": self._get_field_value_case_insensitive(rec, AIJ_CLIENT_FP_FIELD),
                    "logs": self._get_field_value_case_insensitive(rec, AIJ_LOGS_FIELD)
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
            result = self._call_sf_api_with_retry(lambda: self.sf.query(soql))

            if result.get('totalSize', 0) > 0:
                existing_id = result['records'][0]['Id']
                update_payload = payload.copy()
                update_payload.pop(AVS_APPLICATION_LOOKUP_FIELD, None)
                status = self._call_sf_api_with_retry(lambda: handler.update(existing_id, update_payload))
                if status != 204:
                    raise SalesforceAPIError(f"Failed to update verification summary. Status: {status}")
                return existing_id
            else:
                if secondary_field and secondary_id: payload[secondary_field] = secondary_id
                res = self._call_sf_api_with_retry(lambda: handler.create(payload))
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
                self._call_sf_api_with_retry(lambda: handler.update(dci_id, {AVS_TASK_DCI_LOOKUP_FIELD: summary_id}))
                logger.info(f"Successfully linked Summary {summary_id} to DCI {dci_id}.")
            except Exception as e:
                logger.error(f"Failed to update DocumentChecklistItem {dci_id}: {e}")
                raise SalesforceAPIError(f"Failed to link summary {summary_id} to DCI {dci_id}: {e}")

    def touch_verification_summary(self, summary_id: str) -> None:
        """
        Touch the AVS record to update its LastModifiedDate.

        This is called AFTER link_summary_to_related_items to ensure the AVS LastModifiedDate
        is more recent than the linked record's LastModifiedDate. This is critical for the
        skip logic in should_skip_processing() - if AVS.LastModifiedDate < Record.LastModifiedDate,
        the skip logic incorrectly thinks the record was modified and needs reprocessing.

        By touching AVS after linking, we ensure AVS.LastModifiedDate > Record.LastModifiedDate,
        so subsequent runs will correctly skip already-processed records.
        """
        if not summary_id:
            return
        self._ensure_connected()
        try:
            summary_obj_name = APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME
            handler = getattr(self.sf, summary_obj_name)
            # Update with empty payload just to refresh LastModifiedDate
            # Salesforce will update LastModifiedDate even if no fields changed
            self._call_sf_api_with_retry(lambda: handler.update(summary_id, {}))
            logger.debug(f"Touched AVS {summary_id} to update LastModifiedDate")
        except Exception as e:
            # Non-critical - log warning but don't fail the operation
            logger.warning(f"Failed to touch AVS {summary_id}: {e}")

    def get_existing_avs_metadata(
        self,
        application_id: str,
        education_history_id: Optional[str] = None,
        test_id: Optional[str] = None,
        affiliation_id: Optional[str] = None,
        contact_id: Optional[str] = None,
        name_value: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Query for existing AVS record and return metadata for skip-check logic.
        Returns dict with 'LastModifiedDate' and 'Percentage_Confidence__c' if found, else None.
        """
        self._ensure_connected()
        summary_obj_name = APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME

        # Determine secondary lookup field
        secondary_field, secondary_id = None, None
        if contact_id:
            secondary_field, secondary_id = AVS_CONTACT_LOOKUP_FIELD, contact_id
        elif education_history_id:
            secondary_field, secondary_id = AVS_EDUCATION_HISTORY_LOOKUP_FIELD, education_history_id
        elif test_id:
            secondary_field, secondary_id = AVS_TEST_LOOKUP_FIELD, test_id
        elif affiliation_id:
            secondary_field, secondary_id = AVS_AFFILIATION_LOOKUP_FIELD, affiliation_id

        soql = f"SELECT Id, LastModifiedDate, {AVS_CONFIDENCE_FIELD} FROM {summary_obj_name} WHERE {AVS_APPLICATION_LOOKUP_FIELD} = '{application_id}'"
        if secondary_field and secondary_id:
            soql += f" AND {secondary_field} = '{secondary_id}'"
        elif name_value:
            soql += f" AND {AVS_NAME_FIELD} = '{name_value}'"
        else:
            return None  # Cannot query without secondary identifier
        soql += " ORDER BY LastModifiedDate DESC LIMIT 1"

        try:
            result = self._call_sf_api_with_retry(lambda: self.sf.query(soql))
            if result.get('totalSize', 0) > 0:
                rec = result['records'][0]
                return {
                    'Id': rec.get('Id'),
                    'LastModifiedDate': rec.get('LastModifiedDate'),
                    'Percentage_Confidence__c': rec.get(AVS_CONFIDENCE_FIELD)
                }
            return None
        except Exception as e:
            logger.warning(f"Could not fetch existing AVS metadata for app {application_id}: {e}")
            return None  # Non-fatal - proceed with processing if lookup fails

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
            # Use lambda to access self.sf at call time
            result = self._call_sf_api_with_retry(lambda: self.sf.query_all(soql))
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
                return self._call_sf_api_with_retry(lambda: self.sf.query(soql))
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

        # Query DCI record for LastModifiedDate
        dci_last_modified = None
        try:
            soql_dci = f"SELECT Id, LastModifiedDate FROM DocumentChecklistItem WHERE Id = '{dci_id}' LIMIT 1"
            dci_result = self._call_sf_api_with_retry(lambda: self.sf.query(soql_dci))
            if dci_result.get('totalSize', 0) > 0:
                dci_last_modified = dci_result['records'][0].get('LastModifiedDate')
        except Exception as e:
            logger.warning(f"Could not fetch DCI LastModifiedDate for {dci_id}: {e}")

        content_version_id = None
        max_retries = 4
        retry_delay_seconds = 3
        for attempt in range(max_retries):
            try:
                soql_link = f"SELECT ContentDocument.LatestPublishedVersionId FROM ContentDocumentLink WHERE LinkedEntityId = '{dci_id}' ORDER BY SystemModstamp DESC LIMIT 1"
                result_link = self._call_sf_api_with_retry(lambda: self.sf.query(soql_link))
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
            version_record = self._call_sf_api_with_retry(lambda: handler.get(content_version_id))
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
            return {
                "LastModifiedDate": dci_last_modified,  # DCI record date for skip logic
                "documentPayload": {
                    "fileName": version_record.get('Title'),
                    "fileExtension": version_record.get('FileExtension'),
                    "base64Data": base64_data,
                    "LastModifiedDate": version_record.get('LastModifiedDate')  # ContentVersion date
                }
            }
        except Exception as e:
            logger.error(f"An unexpected error occurred during file download for CV ID {content_version_id}: {e}", exc_info=True)
            raise SalesforceAPIError(f"File download failed for ContentVersion {content_version_id}: {e}")


    # -----------------------------------------------------------------------
    # --- EEDL Methods -------------------------------------------------------
    # -----------------------------------------------------------------------

    def _match_id_document_file(self, files: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        keywords = [kw.lower() for kw in EEDL_FILE_MATCHING_CONFIG["id_document_keywords"]]
        for f in files:
            title_lower = f.get("title", "").lower()
            if any(kw in title_lower for kw in keywords):
                return f
        return None

    def _match_education_file(self, files: List[Dict[str, Any]], degree_value: str) -> Optional[Dict[str, Any]]:
        degree_lower = degree_value.lower() if degree_value else ""
        for entry in EEDL_FILE_MATCHING_CONFIG["education_keyword_map"]:
            degree_values_lower = [d.lower() for d in entry["degree_values"]]
            if any(dv in degree_lower or degree_lower in dv for dv in degree_values_lower):
                for f in files:
                    title_lower = f.get("title", "").lower()
                    if any(kw in title_lower for kw in entry["file_keywords"]):
                        return f
        return None

    def _download_content_version(self, content_version_id: str) -> Dict[str, Any]:
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

    def _get_opportunity_files(self, opportunity_id: str) -> List[Dict[str, Any]]:
        soql = (
            f"SELECT ContentDocument.LatestPublishedVersionId, ContentDocument.Title, "
            f"ContentDocument.FileExtension, ContentDocument.LastModifiedDate "
            f"FROM ContentDocumentLink WHERE LinkedEntityId = '{opportunity_id}' "
            f"ORDER BY SystemModstamp DESC"
        )
        result = self._call_sf_api_with_retry(lambda: self.sf.query_all(soql))
        files = []
        for rec in result.get('records', []):
            cd = rec.get('ContentDocument', {})
            files.append({
                "version_id": cd.get('LatestPublishedVersionId'),
                "title": cd.get('Title', ''),
                "extension": cd.get('FileExtension', ''),
                "lastModifiedDate": cd.get('LastModifiedDate'),
            })
        return files

    def get_eedl_id_document_data(self, opportunity_id: str) -> Dict[str, Any]:
        self._ensure_connected()
        try:
            opp_fields = f"Id, Name, {EEDL_OPP_CITIZENSHIP_FIELD}, {EEDL_OPP_CONTACT_LOOKUP_FIELD}, LastModifiedDate"
            soql_opp = f"SELECT {opp_fields} FROM {EEDL_OPPORTUNITY_OBJECT_API_NAME} WHERE Id = '{opportunity_id}' LIMIT 1"
            opp_result = self._call_sf_api_with_retry(lambda: self.sf.query(soql_opp))
            if not opp_result.get('totalSize'):
                return {"recordData": {}, "documentPayload": None, "Salesforce_data_issue_Summary": f"Opportunity {opportunity_id} not found."}
            opp_rec = opp_result['records'][0]
            record_data = {
                "Id": opp_rec.get('Id'),
                "Name": opp_rec.get('Name'),
                EEDL_OPP_CITIZENSHIP_FIELD: opp_rec.get(EEDL_OPP_CITIZENSHIP_FIELD),
                EEDL_OPP_CONTACT_LOOKUP_FIELD: opp_rec.get(EEDL_OPP_CONTACT_LOOKUP_FIELD),
                "LastModifiedDate": opp_rec.get('LastModifiedDate'),
            }

            files = self._get_opportunity_files(opportunity_id)
            matched = self._match_id_document_file(files)
            if not matched:
                return {"recordData": record_data, "documentPayload": None, "Salesforce_data_issue_Summary": "No Aadhaar/Passport file found on Opportunity. Check filename contains 'aadhaar', 'aadhar', or 'passport'."}

            document_payload = self._download_content_version(matched['version_id'])
            return {"recordData": record_data, "documentPayload": document_payload, "Salesforce_data_issue_Summary": None}
        except SalesforceAPIError:
            raise
        except Exception as e:
            logger.error(f"Error in get_eedl_id_document_data for {opportunity_id}: {e}", exc_info=True)
            raise SalesforceAPIError(f"Failed to fetch EEDL ID document data: {e}")

    def get_eedl_education_record_data(self, education_id: str, opportunity_id: str) -> Dict[str, Any]:
        self._ensure_connected()
        try:
            edu_fields = (
                f"Id, {EEDL_EDU_DEGREE_FIELD}, {EEDL_EDU_UNIVERSITY_FIELD}, "
                f"{EEDL_EDU_GPA_FIELD}, {EEDL_EDU_START_DATE_FIELD}, {EEDL_EDU_END_DATE_FIELD}, LastModifiedDate"
            )
            soql_edu = f"SELECT {edu_fields} FROM {EEDL_EDUCATION_OBJECT_API_NAME} WHERE Id = '{education_id}' LIMIT 1"
            edu_result = self._call_sf_api_with_retry(lambda: self.sf.query(soql_edu))
            if not edu_result.get('totalSize'):
                return {"recordData": {}, "documentPayload": None, "Salesforce_data_issue_Summary": f"Education record {education_id} not found."}
            edu_rec = edu_result['records'][0]
            record_data = {
                "Id": edu_rec.get('Id'),
                EEDL_EDU_DEGREE_FIELD: edu_rec.get(EEDL_EDU_DEGREE_FIELD),
                EEDL_EDU_UNIVERSITY_FIELD: edu_rec.get(EEDL_EDU_UNIVERSITY_FIELD),
                EEDL_EDU_GPA_FIELD: edu_rec.get(EEDL_EDU_GPA_FIELD),
                EEDL_EDU_START_DATE_FIELD: edu_rec.get(EEDL_EDU_START_DATE_FIELD),
                EEDL_EDU_END_DATE_FIELD: edu_rec.get(EEDL_EDU_END_DATE_FIELD),
                "LastModifiedDate": edu_rec.get('LastModifiedDate'),
            }

            files = self._get_opportunity_files(opportunity_id)
            degree_value = edu_rec.get(EEDL_EDU_DEGREE_FIELD, '')
            matched = self._match_education_file(files, degree_value)
            if not matched:
                return {"recordData": record_data, "documentPayload": None, "Salesforce_data_issue_Summary": f"No document file matched for Education record {education_id} with degree '{degree_value}'. Check filename keywords in EEDL_FILE_MATCHING_CONFIG."}

            document_payload = self._download_content_version(matched['version_id'])
            return {"recordData": record_data, "documentPayload": document_payload, "Salesforce_data_issue_Summary": None}
        except SalesforceAPIError:
            raise
        except Exception as e:
            logger.error(f"Error in get_eedl_education_record_data for {education_id}: {e}", exc_info=True)
            raise SalesforceAPIError(f"Failed to fetch EEDL education record data: {e}")

    def get_eedl_education_ids_for_opportunity(self, opportunity_id: str) -> List[str]:
        self._ensure_connected()
        soql_opp = f"SELECT {EEDL_OPP_CONTACT_LOOKUP_FIELD} FROM {EEDL_OPPORTUNITY_OBJECT_API_NAME} WHERE Id = '{opportunity_id}' LIMIT 1"
        opp_result = self._call_sf_api_with_retry(lambda: self.sf.query(soql_opp))
        if not opp_result.get('totalSize'):
            raise SalesforceAPIError(f"Opportunity {opportunity_id} not found.")
        contact_id = opp_result['records'][0].get(EEDL_OPP_CONTACT_LOOKUP_FIELD)
        if not contact_id:
            raise SalesforceAPIError(f"Opportunity {opportunity_id} has no Contact lookup.")
        soql_edu = f"SELECT Id FROM {EEDL_EDUCATION_OBJECT_API_NAME} WHERE {EEDL_EDU_CONTACT_LOOKUP_FIELD} = '{contact_id}'"
        edu_result = self._call_sf_api_with_retry(lambda: self.sf.query_all(soql_edu))
        return [rec['Id'] for rec in edu_result.get('records', [])]

    def update_opportunity_citizenship(self, opportunity_id: str, citizenship_value: str) -> bool:
        self._ensure_connected()
        try:
            handler = getattr(self.sf, EEDL_OPPORTUNITY_OBJECT_API_NAME)
            status = self._call_sf_api_with_retry(lambda: handler.update(opportunity_id, {EEDL_OPP_CITIZENSHIP_FIELD: citizenship_value}))
            if status != 204:
                raise SalesforceAPIError(f"Failed to update Opportunity citizenship. Status: {status}")
            logger.info(f"Updated Opportunity {opportunity_id} {EEDL_OPP_CITIZENSHIP_FIELD} = '{citizenship_value}'")
            return True
        except Exception as e:
            logger.error(f"Error updating citizenship on Opportunity {opportunity_id}: {e}", exc_info=True)
            raise SalesforceAPIError(f"Failed to update Opportunity citizenship: {e}")

    def upsert_eedl_verification_summary(
        self,
        opportunity_id: str,
        record_type: str,
        name_value: str,
        report_content: Optional[str] = None,
        overall_feedback: Optional[str] = None,
        confidence_range: Optional[int] = None,
        mismatched_field_list: Optional[str] = None,
        verification_status: Optional[str] = None,
        education_id: Optional[str] = None,
    ) -> str:
        self._ensure_connected()
        payload: Dict[str, Any] = {
            EEDL_VS_OPPORTUNITY_LOOKUP_FIELD: opportunity_id,
            EEDL_VS_RECORD_TYPE_FIELD: record_type,
        }
        if report_content is not None:
            payload[EEDL_VS_REPORT_FIELD] = report_content[:131072]
        if overall_feedback is not None:
            payload[EEDL_VS_OVERALL_FEEDBACK_FIELD] = overall_feedback
        if confidence_range is not None:
            payload[EEDL_VS_CONFIDENCE_FIELD] = confidence_range
        if mismatched_field_list is not None:
            payload[EEDL_VS_MISMATCHED_FIELDS_FIELD] = mismatched_field_list
        if verification_status is not None:
            _status_map = {"Passed": "verified", "Failed": "error", "Needs Review": "insufficient_data"}
            payload[EEDL_VS_VERIFICATION_STATUS_FIELD] = _status_map.get(verification_status, verification_status)
        if education_id:
            payload[EEDL_VS_EDUCATION_LOOKUP_FIELD] = education_id

        soql = f"SELECT Id FROM {EEDL_VS_OBJECT_API_NAME} WHERE {EEDL_VS_OPPORTUNITY_LOOKUP_FIELD} = '{opportunity_id}' AND {EEDL_VS_RECORD_TYPE_FIELD} = '{record_type}'"
        if education_id:
            soql += f" AND {EEDL_VS_EDUCATION_LOOKUP_FIELD} = '{education_id}'"
        soql += " LIMIT 1"

        try:
            handler = getattr(self.sf, EEDL_VS_OBJECT_API_NAME)
            result = self._call_sf_api_with_retry(lambda: self.sf.query(soql))
            if result.get('totalSize', 0) > 0:
                existing_id = result['records'][0]['Id']
                update_payload = {k: v for k, v in payload.items() if k not in (EEDL_VS_OPPORTUNITY_LOOKUP_FIELD,)}
                status = self._call_sf_api_with_retry(lambda: handler.update(existing_id, update_payload))
                if status != 204:
                    raise SalesforceAPIError(f"Failed to update EEDL verification summary. Status: {status}")
                return existing_id
            else:
                res = self._call_sf_api_with_retry(lambda: handler.create(payload))
                if not (isinstance(res, dict) and res.get('success')):
                    raise SalesforceAPIError(f"Failed to create EEDL verification summary. Response: {res}")
                return res.get('id')
        except Exception as e:
            logger.error(f"Error upserting EEDL verification summary: {e}", exc_info=True)
            raise SalesforceAPIError(f"Upsert for EEDL verification summary failed: {e}")

    def get_existing_eedl_vs_metadata(
        self,
        opportunity_id: str,
        record_type: str,
        education_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        self._ensure_connected()
        soql = (
            f"SELECT Id, LastModifiedDate, {EEDL_VS_CONFIDENCE_FIELD} "
            f"FROM {EEDL_VS_OBJECT_API_NAME} "
            f"WHERE {EEDL_VS_OPPORTUNITY_LOOKUP_FIELD} = '{opportunity_id}' "
            f"AND {EEDL_VS_RECORD_TYPE_FIELD} = '{record_type}'"
        )
        if education_id:
            soql += f" AND {EEDL_VS_EDUCATION_LOOKUP_FIELD} = '{education_id}'"
        soql += " ORDER BY LastModifiedDate DESC LIMIT 1"
        try:
            result = self._call_sf_api_with_retry(lambda: self.sf.query(soql))
            if result.get('totalSize', 0) > 0:
                rec = result['records'][0]
                return {
                    'Id': rec.get('Id'),
                    'LastModifiedDate': rec.get('LastModifiedDate'),
                    'Percentage_Confidence__c': rec.get(EEDL_VS_CONFIDENCE_FIELD),
                }
            return None
        except Exception as e:
            logger.warning(f"Could not fetch EEDL VS metadata for opp {opportunity_id}: {e}")
            return None

    async def upsert_eedl_ai_server_job(self, job_id: str, opportunity_id: str, status: str, **kwargs) -> str:
        self._ensure_connected()
        soql = f"SELECT Id FROM {AI_SERVER_JOB_OBJECT_API_NAME} WHERE {AIJ_OPPORTUNITY_LOOKUP_FIELD} = '{opportunity_id}' ORDER BY CreatedDate DESC LIMIT 1"
        def do_query():
            return self._call_sf_api_with_retry(lambda: self.sf.query(soql))
        try:
            result = await asyncio.get_event_loop().run_in_executor(None, do_query)
        except Exception as e:
            raise SalesforceAPIError(f"Failed to query EEDL AI Server Job: {e}")

        payload = {
            AIJ_OPPORTUNITY_LOOKUP_FIELD: opportunity_id,
            AIJ_JOB_ID_FIELD: job_id,
            AIJ_STATUS_FIELD: status,
        }
        if message := kwargs.get('message'): payload[AIJ_MESSAGE_FIELD] = message[:131072]
        if progress_details := kwargs.get('progress_details'): payload[AIJ_PROGRESS_FIELD] = progress_details
        if client_fp := kwargs.get('client_fingerprint'): payload[AIJ_CLIENT_FP_FIELD] = client_fp
        logs = kwargs.get('logs')
        if logs is not None and logs:
            payload[AIJ_LOGS_FIELD] = logs[:131072]

        handler = getattr(self.sf, AI_SERVER_JOB_OBJECT_API_NAME)
        try:
            if result['totalSize'] > 0:
                existing_id = result['records'][0].get('Id')
                update_payload = {k: v for k, v in payload.items() if k != AIJ_OPPORTUNITY_LOOKUP_FIELD}
                def do_update(): return self._call_sf_api_with_retry(lambda: handler.update(existing_id, update_payload))
                resp = await asyncio.get_event_loop().run_in_executor(None, do_update)
                if resp != 204:
                    raise SalesforceAPIError(f"Failed to update EEDL AI Server Job. Status: {resp}")
                return existing_id
            else:
                def do_create(): return self._call_sf_api_with_retry(lambda: handler.create(payload))
                resp = await asyncio.get_event_loop().run_in_executor(None, do_create)
                if not (isinstance(resp, dict) and resp.get('success')):
                    raise SalesforceAPIError(f"Failed to create EEDL AI Server Job. Response: {resp}")
                return resp.get('id')
        except Exception as e:
            raise SalesforceAPIError(f"Upsert for EEDL AI Server Job failed: {e}")

    async def get_latest_eedl_ai_server_job(self, opportunity_id: str) -> Optional[Dict[str, Any]]:
        self._ensure_connected()
        soql = (
            f"SELECT Id, {AIJ_JOB_ID_FIELD}, {AIJ_STATUS_FIELD}, {AIJ_MESSAGE_FIELD}, "
            f"CreatedDate, LastModifiedDate, {AIJ_PROGRESS_FIELD}, {AIJ_CLIENT_FP_FIELD}, {AIJ_LOGS_FIELD} "
            f"FROM {AI_SERVER_JOB_OBJECT_API_NAME} "
            f"WHERE {AIJ_OPPORTUNITY_LOOKUP_FIELD} = '{opportunity_id}' "
            f"ORDER BY CreatedDate DESC LIMIT 1"
        )
        def do_query(): return self._call_sf_api_with_retry(lambda: self.sf.query(soql))
        try:
            result = await asyncio.get_event_loop().run_in_executor(None, do_query)
            if result['totalSize'] > 0:
                rec = result['records'][0]
                progress_raw = self._get_field_value_case_insensitive(rec, AIJ_PROGRESS_FIELD) or '{}'
                progress = json.loads(progress_raw)
                return {
                    "job_id": self._get_field_value_case_insensitive(rec, AIJ_JOB_ID_FIELD),
                    "application_id": opportunity_id,
                    "status": self._get_field_value_case_insensitive(rec, AIJ_STATUS_FIELD),
                    "message": self._get_field_value_case_insensitive(rec, AIJ_MESSAGE_FIELD),
                    "created_at": self._get_field_value_case_insensitive(rec, "CreatedDate"),
                    "last_updated_at": self._get_field_value_case_insensitive(rec, "LastModifiedDate"),
                    "progress": progress,
                    "salesforce_job_record_id": self._get_field_value_case_insensitive(rec, "Id"),
                    "client_fingerprint": self._get_field_value_case_insensitive(rec, AIJ_CLIENT_FP_FIELD),
                    "logs": self._get_field_value_case_insensitive(rec, AIJ_LOGS_FIELD),
                }
            return None
        except Exception as e:
            logger.error(f"Error getting latest EEDL job for Opp {opportunity_id}: {e}", exc_info=True)
            raise SalesforceAPIError(f"Failed to query latest EEDL job: {e}")


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
