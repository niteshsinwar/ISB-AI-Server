# project_root/app/services/admission_sf_service.py
"""
Admission Track Salesforce operations.
All methods here are mixed into SalesforceService via AdmissionSFMixin.
They rely on self.sf, self._ensure_connected(), self._call_sf_api_with_retry(),
self._download_content_version(), and self._get_field_value_case_insensitive()
being available on the instance (provided by the base class).
"""
import logging
import asyncio
import json
import base64
from typing import Dict, Any, Optional, List

from app.config import (
    APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME,
    AVS_APPLICATION_LOOKUP_FIELD, AVS_CONTACT_LOOKUP_FIELD,
    AVS_EDUCATION_HISTORY_LOOKUP_FIELD, AVS_TEST_LOOKUP_FIELD,
    AVS_AFFILIATION_LOOKUP_FIELD, AVS_REPORT_FIELD, AVS_NAME_FIELD,
    AVS_OVERALL_FEEDBACK_FIELD, AVS_MISMATCHED_LIST_FIELD, AVS_CONFIDENCE_FIELD,
    AVS_TASK_DCI_LOOKUP_FIELD,
    AI_SERVER_JOB_OBJECT_API_NAME, AIJ_APPLICATION_LOOKUP_FIELD, AIJ_JOB_ID_FIELD,
    AIJ_STATUS_FIELD, AIJ_MESSAGE_FIELD, AIJ_PROGRESS_FIELD, AIJ_CLIENT_FP_FIELD, AIJ_LOGS_FIELD,
    APPLICATION_OBJECT_API_NAME, APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP,
)

logger = logging.getLogger(__name__)


class AdmissionSFMixin:
    """
    Mixin containing all Admission-track Salesforce methods.
    Must be used alongside the base SalesforceService which provides:
      - self.sf, self._ensure_connected(), self._call_sf_api_with_retry()
      - self._download_content_version(), self._get_field_value_case_insensitive()
      - self.apex_endpoint_path_map
    """

    # -----------------------------------------------------------------------
    # Apex REST call (legacy — to be phased out per-record-type)
    # -----------------------------------------------------------------------

    def get_record_detail_from_apex(self, record_id: str, sobject_api_name_key: str) -> Dict[str, Any]:
        from app.services.salesforce_service import SalesforceAPIError
        import requests as _requests

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
        except _requests.exceptions.HTTPError as e:
            error_details = e.response.text if e.response else str(e)
            logger.error(f"HTTP error calling Apex endpoint {full_url}. Status: {e.response.status_code}. Response: {error_details}", exc_info=True)
            raise SalesforceAPIError(f"Salesforce API returned an error: {error_details}")
        except Exception as e:
            logger.error(f"An unexpected error occurred calling Apex endpoint {full_url}: {e}", exc_info=True)
            raise SalesforceAPIError(f"An unexpected error occurred during Apex call: {e}")

    # -----------------------------------------------------------------------
    # AI Server Job (Admission)
    # -----------------------------------------------------------------------

    async def upsert_ai_server_job(self, job_id: str, application_id: str, status: str, **kwargs) -> str:
        from app.services.salesforce_service import SalesforceAPIError
        self._ensure_connected()
        soql = f"SELECT Id FROM {AI_SERVER_JOB_OBJECT_API_NAME} WHERE {AIJ_APPLICATION_LOOKUP_FIELD} = '{application_id}' ORDER BY CreatedDate DESC LIMIT 1"

        def do_query():
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
        logs = kwargs.get('logs')
        if logs is not None and logs:
            payload[AIJ_LOGS_FIELD] = logs[:131072]

        handler = getattr(self.sf, AI_SERVER_JOB_OBJECT_API_NAME)
        try:
            if result['totalSize'] > 0:
                existing_id = self._get_field_value_case_insensitive(result['records'][0], 'Id')
                update_payload = {k: v for k, v in payload.items() if k != AIJ_APPLICATION_LOOKUP_FIELD}

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
        from app.services.salesforce_service import SalesforceAPIError
        self._ensure_connected()
        soql = (
            f"SELECT Id, {AIJ_JOB_ID_FIELD}, {AIJ_STATUS_FIELD}, {AIJ_MESSAGE_FIELD}, "
            f"CreatedDate, LastModifiedDate, {AIJ_PROGRESS_FIELD}, {AIJ_CLIENT_FP_FIELD}, {AIJ_LOGS_FIELD} "
            f"FROM {AI_SERVER_JOB_OBJECT_API_NAME} WHERE {AIJ_APPLICATION_LOOKUP_FIELD} = '{application_id}' "
            f"ORDER BY CreatedDate DESC LIMIT 1"
        )

        def do_query():
            return self._call_sf_api_with_retry(lambda: self.sf.query(soql))

        try:
            result = await asyncio.get_event_loop().run_in_executor(None, do_query)
            if result['totalSize'] > 0:
                rec = result['records'][0]
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
                    "logs": self._get_field_value_case_insensitive(rec, AIJ_LOGS_FIELD),
                }
            return None
        except Exception as e:
            logger.error(f"Error getting latest job for App {application_id}: {e}", exc_info=True)
            raise SalesforceAPIError(f"Failed to query for latest job from Salesforce: {e}")

    # -----------------------------------------------------------------------
    # Application Verification Summary (AVS) CRUD
    # -----------------------------------------------------------------------

    def upsert_verification_summary(self, application_id: str, report_content: str, name_value: str, **kwargs) -> str:
        from app.services.salesforce_service import SalesforceAPIError
        self._ensure_connected()
        summary_obj_name = APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME
        payload = {AVS_APPLICATION_LOOKUP_FIELD: application_id}
        if report_content is not None:
            payload[AVS_REPORT_FIELD] = report_content
        if name_value:
            payload[AVS_NAME_FIELD] = name_value[:80]
        if overall_feedback := kwargs.get('overall_feedback'):
            payload[AVS_OVERALL_FEEDBACK_FIELD] = overall_feedback
        if mismatched := kwargs.get('mismatched_field_list'):
            payload[AVS_MISMATCHED_LIST_FIELD] = mismatched
        confidence_range = kwargs.get('confidence_range')
        if confidence_range is not None:
            payload[AVS_CONFIDENCE_FIELD] = confidence_range

        # Optional lookup fields
        contact_id = kwargs.get('contact_id')
        if contact_id:
            payload[AVS_CONTACT_LOOKUP_FIELD] = contact_id
        education_history_id = kwargs.get('education_history_id')
        if education_history_id:
            payload[AVS_EDUCATION_HISTORY_LOOKUP_FIELD] = education_history_id
        test_id = kwargs.get('test_id')
        if test_id:
            payload[AVS_TEST_LOOKUP_FIELD] = test_id
        affiliation_id = kwargs.get('affiliation_id')
        if affiliation_id:
            payload[AVS_AFFILIATION_LOOKUP_FIELD] = affiliation_id

        # Build query to find existing record
        soql = f"SELECT Id FROM {summary_obj_name} WHERE {AVS_APPLICATION_LOOKUP_FIELD} = '{application_id}' AND {AVS_NAME_FIELD} = '{name_value}'"
        if education_history_id:
            soql += f" AND {AVS_EDUCATION_HISTORY_LOOKUP_FIELD} = '{education_history_id}'"
        if test_id:
            soql += f" AND {AVS_TEST_LOOKUP_FIELD} = '{test_id}'"
        if affiliation_id:
            soql += f" AND {AVS_AFFILIATION_LOOKUP_FIELD} = '{affiliation_id}'"
        soql += " LIMIT 1"

        try:
            handler = getattr(self.sf, summary_obj_name)
            result = self._call_sf_api_with_retry(lambda: self.sf.query(soql))
            if result.get('totalSize', 0) > 0:
                existing_id = result['records'][0]['Id']
                update_payload = {k: v for k, v in payload.items() if k != AVS_APPLICATION_LOOKUP_FIELD}
                status = self._call_sf_api_with_retry(lambda: handler.update(existing_id, update_payload))
                if status != 204:
                    raise SalesforceAPIError(f"Failed to update AVS. Status: {status}")
                return existing_id
            else:
                res = self._call_sf_api_with_retry(lambda: handler.create(payload))
                if not (isinstance(res, dict) and res.get('success')):
                    raise SalesforceAPIError(f"Failed to create AVS. Response: {res}")
                return res.get('id')
        except Exception as e:
            if not isinstance(e, SalesforceAPIError):
                logger.error(f"Error upserting AVS: {e}", exc_info=True)
                raise SalesforceAPIError(f"Upsert for AVS failed: {e}")
            raise

    def link_summary_to_related_items(self, summary_id: str, task_id: Optional[str], dci_id: Optional[str], overall_feedback: Optional[str] = None):
        from app.services.salesforce_service import SalesforceAPIError
        if not summary_id:
            return
        self._ensure_connected()
        if dci_id:
            try:
                handler = getattr(self.sf, 'DocumentChecklistItem')
                self._call_sf_api_with_retry(lambda: handler.update(dci_id, {AVS_TASK_DCI_LOOKUP_FIELD: summary_id}))
                logger.info(f"Successfully linked Summary {summary_id} to DCI {dci_id}.")
            except Exception as e:
                logger.error(f"Failed to update DocumentChecklistItem {dci_id}: {e}")
                raise SalesforceAPIError(f"Failed to link summary {summary_id} to DCI {dci_id}: {e}")

    def _find_open_task_id(self, what_id: str, subject: str) -> Optional[str]:
        """
        Return the Id of the most recent OPEN (not-closed) Task with this exact
        Subject hanging off `what_id`, or None. Uses Task.IsClosed so it honours
        whatever statuses the org marks as closed, not just 'Completed'.
        """
        if not what_id or not subject:
            return None
        # Escape backslashes then single quotes for the SOQL string literal —
        # Subject is composed from field names / labels and can contain quotes.
        safe_subject = subject.replace("\\", "\\\\").replace("'", "\\'")
        soql = (
            "SELECT Id FROM Task "
            f"WHERE WhatId = '{what_id}' AND Subject = '{safe_subject}' "
            "AND IsClosed = false ORDER BY CreatedDate DESC LIMIT 1"
        )
        try:
            result = self._call_sf_api_with_retry(lambda: self.sf.query(soql))
            if result.get('totalSize', 0) > 0:
                return result['records'][0]['Id']
        except Exception as e:
            logger.warning(f"Could not check for existing open task on {what_id}: {e}")
        return None

    def create_verification_task(
        self,
        what_id: str,
        task_data: Dict[str, Any],
        owner_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Create (or refresh) a Salesforce Task for a verification mismatch.

        Dedup rule: if an OPEN task with the same Subject already hangs off
        `what_id`, its details are refreshed in place instead of inserting a
        duplicate. CLOSED tasks are left untouched — a recurrence of the same
        mismatch after resolution gets a brand-new task.

        Args:
            what_id: Record to attach as the Task's WhatId ("Related To").
                     This is the parent Application, so all mismatch tasks for an
                     applicant roll up under one record.
            task_data: Dict with Subject, Description, Status, Priority
            owner_id: Optional User/Queue ID for OwnerId. When None, a NEW task is
                      still created; Salesforce defaults OwnerId to the integration
                      (running) user so the mismatch is never silently dropped.

        Returns:
            Task ID of the created or updated task.
        """
        from app.services.salesforce_service import SalesforceAPIError

        self._ensure_connected()

        try:
            task_handler = getattr(self.sf, 'Task')
            subject = task_data.get("Subject")

            # Refresh an existing OPEN task rather than creating a duplicate.
            # Owner and Status are intentionally left alone so we don't yank a
            # task away from whoever is already working it.
            existing_open_id = self._find_open_task_id(what_id, subject)
            if existing_open_id:
                update_fields = {
                    "Description": task_data.get("Description"),
                    "Priority": task_data.get("Priority", "High"),
                }
                self._call_sf_api_with_retry(
                    lambda: task_handler.update(existing_open_id, update_fields)
                )
                logger.info(
                    f"Refreshed existing OPEN verification task {existing_open_id} "
                    f"on {what_id} (dedup on Subject); no duplicate created."
                )
                return existing_open_id

            task_record = {
                **task_data,
                "WhatId": what_id,
            }
            if owner_id:
                task_record["OwnerId"] = owner_id
            else:
                # No resolvable assignee: omit OwnerId so SF defaults it to the
                # integration user rather than skipping the task entirely.
                task_record.pop("OwnerId", None)
                logger.warning(
                    f"Creating task on {what_id} with no explicit owner; "
                    "OwnerId will default to the integration user."
                )
            task_id = self._call_sf_api_with_retry(lambda: task_handler.create(task_record))
            logger.info(
                f"Created verification task {task_id} on {what_id}, "
                f"owner={owner_id or 'default(integration user)'}"
            )
            return task_id
        except Exception as e:
            logger.error(f"Failed to create task on {what_id}: {e}")
            raise SalesforceAPIError(f"Failed to create verification task: {e}")

    # -----------------------------------------------------------------------
    # Existing AVS metadata (for skip-logic)
    # -----------------------------------------------------------------------

    def get_existing_avs_metadata(
        self,
        application_id: str,
        contact_id: Optional[str] = None,
        education_history_id: Optional[str] = None,
        test_id: Optional[str] = None,
        affiliation_id: Optional[str] = None,
        name_value: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        self._ensure_connected()
        soql = (
            f"SELECT Id, LastModifiedDate, {AVS_CONFIDENCE_FIELD} "
            f"FROM {APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME} "
            f"WHERE {AVS_APPLICATION_LOOKUP_FIELD} = '{application_id}'"
        )
        if contact_id:
            soql += f" AND {AVS_CONTACT_LOOKUP_FIELD} = '{contact_id}'"
        if education_history_id:
            soql += f" AND {AVS_EDUCATION_HISTORY_LOOKUP_FIELD} = '{education_history_id}'"
        if test_id:
            soql += f" AND {AVS_TEST_LOOKUP_FIELD} = '{test_id}'"
        if affiliation_id:
            soql += f" AND {AVS_AFFILIATION_LOOKUP_FIELD} = '{affiliation_id}'"
        if name_value:
            soql += f" AND {AVS_NAME_FIELD} = '{name_value}'"
        soql += " ORDER BY LastModifiedDate DESC LIMIT 1"
        try:
            result = self._call_sf_api_with_retry(lambda: self.sf.query(soql))
            if result.get('totalSize', 0) > 0:
                rec = result['records'][0]
                return {
                    'Id': rec.get('Id'),
                    'LastModifiedDate': rec.get('LastModifiedDate'),
                    'Percentage_Confidence__c': rec.get(AVS_CONFIDENCE_FIELD),
                }
            return None
        except Exception as e:
            logger.warning(f"Could not fetch existing AVS metadata for app {application_id}: {e}")
            return None

    # -----------------------------------------------------------------------
    # Related record discovery (used by job_worker)
    # -----------------------------------------------------------------------

    def get_directly_related_record_ids(
        self,
        parent_record_id: str,
        child_object_api_name: str,
        lookup_field_on_child_to_parent: str,
        filtering_criteria: Optional[Dict[str, Any]] = None,
        order_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[str]:
        from app.services.salesforce_service import SalesforceAPIError
        self._ensure_connected()
        if not all([child_object_api_name, lookup_field_on_child_to_parent]):
            raise ValueError("Missing required arguments.")

        soql = f"SELECT Id FROM {child_object_api_name} WHERE {lookup_field_on_child_to_parent} = '{parent_record_id}'"

        if filtering_criteria:
            filters = filtering_criteria if isinstance(filtering_criteria, list) else [filtering_criteria]
            for f in filters:
                if not isinstance(f, dict):
                    continue
                if "subquery_filter" in f:
                    sqf = f.get("subquery_filter", {})
                    subquery = sqf.get("subquery", {})
                    if all([sqf.get("field"), sqf.get("operator"), subquery.get("object"), subquery.get("select_field"), subquery.get("where_clause")]):
                        soql += (
                            f" AND {sqf['field']} {sqf['operator']} "
                            f"(SELECT {subquery['select_field']} FROM {subquery['object']} "
                            f"WHERE {subquery['where_clause']})"
                        )
                    continue
                field_name = f.get("field_api_name")
                if not field_name:
                    continue
                if "allowed_values" in f:
                    allowed_values = f.get("allowed_values")
                    if isinstance(allowed_values, list) and allowed_values:
                        formatted_values = ', '.join(f"'{val}'" for val in allowed_values)
                        soql += f" AND {field_name} IN ({formatted_values})"
                elif "operator" in f and "value" in f:
                    soql += f" AND {field_name} {f['operator']} '{f['value']}'"

        if order_by:
            soql += f" ORDER BY {order_by}"
        if limit is not None and isinstance(limit, int):
            soql += f" LIMIT {limit}"

        logger.info(f"Executing filtered query: {soql}")
        try:
            result = self._call_sf_api_with_retry(lambda: self.sf.query_all(soql))
            return [rec['Id'] for rec in result['records']]
        except Exception as e:
            logger.error(f"Error getting related record IDs: {e}", exc_info=True)
            raise SalesforceAPIError(f"Failed to get related record IDs: {e}")

    async def get_contact_id_for_application(self, application_id: str) -> str:
        from app.services.salesforce_service import SalesforceAPIError
        if not application_id:
            raise SalesforceAPIError("Application ID is required.")
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
                raise SalesforceAPIError(f"Failed to query for Contact ID: {e}")
            raise

    async def get_task_assignee_for_application(
        self,
        application_id: str,
        dci_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Retrieves the User ID to assign mismatch-review tasks to.

        Business rule: the task goes to whoever OWNS the DocumentChecklistItem
        (the checklist record the applicant's document hangs off). Falls back
        to the Application's OwnerId only when no checklist owner can be
        resolved, so a task always reaches a responsible human.
        """
        from app.core.processing_utils import is_valid_salesforce_id
        self._ensure_connected()

        # Primary: DocumentChecklistItem owner
        if dci_id and is_valid_salesforce_id(dci_id):
            try:
                soql_dci = f"SELECT OwnerId FROM DocumentChecklistItem WHERE Id = '{dci_id}' LIMIT 1"
                def do_dci_query():
                    return self._call_sf_api_with_retry(lambda: self.sf.query(soql_dci))
                dci_result = await asyncio.get_event_loop().run_in_executor(None, do_dci_query)
                if dci_result.get('totalSize'):
                    owner_id = dci_result['records'][0].get('OwnerId')
                    if owner_id:
                        return owner_id
                logger.warning(f"DocumentChecklistItem {dci_id} has no resolvable owner; falling back to Application owner.")
            except Exception as e:
                logger.warning(f"Failed to fetch DCI owner for {dci_id}: {e}; falling back to Application owner.")

        # Fallback: Application owner
        try:
            soql_app = f"SELECT OwnerId FROM {APPLICATION_OBJECT_API_NAME} WHERE Id = '{application_id}' LIMIT 1"
            def do_app_query():
                return self._call_sf_api_with_retry(lambda: self.sf.query(soql_app))
            app_result = await asyncio.get_event_loop().run_in_executor(None, do_app_query)
            if app_result.get('totalSize'):
                return app_result['records'][0].get('OwnerId')
            return None
        except Exception as e:
            logger.warning(f"Failed to fetch task assignee for application {application_id}: {e}")
            return None

    # -----------------------------------------------------------------------
    # Test Score: Python-side data assembly (replaces Apex REST)
    # -----------------------------------------------------------------------

    _TEST_SCORE_DCI_MAP = {
        "GRE": "GRE Score Card",
        "GMAT": "GMAT Score Card",
        "GMAT_FOCUS": "GMAT Score Card",
    }

    _TEST_FIELDS = (
        "Id, LastModifiedDate, RecordTypeName__c, Application__c, "
        "VerbalPercentile__c, VerbalScore__c, QuantPercentile__c, QuantScore__c, "
        "Data_Insights_Percentile__c, Data_Insights_score__c, "
        "Total_Percentile__c, Total_Score__c, "
        "Analytical_Percentile__c, Analytical_Score__c, "
        "IR_Percentile__c, IR_Score__c, "
        "hed__Test_Date__c, Test_ID__c, Registration_No__c, Email__c"
    )

    _TEST_SCORE_FIELDS = (
        "Id, Application__c, Email__c, "
        "VerbalPercentile__c, VerbalScore__c, QuantPercentile__c, QuantScore__c, "
        "Data_Insights_Percentile__c, Data_Insights_Score__c, "
        "Total_Percentile__c, Total_Score__c, "
        "Analytical_Percentile__c, Analytical_Score__c, "
        "IR_Percentile__c, IR_Score__c, "
        "Test_Date__c, Test_ID__c, Registration_No__c, Birthdate__c, Mode_of_Test_Taken__c"
    )

    def get_test_score_record_data(self, test_id: str, parent_application_id: str) -> Dict[str, Any]:
        from app.services.salesforce_service import SalesforceAPIError
        self._ensure_connected()

        def _fallback(record_data: Dict[str, Any], message: str) -> Dict[str, Any]:
            record_data["Salesforce_data_issue_Summary"] = message
            return {"recordData": record_data, "documentPayload": None, "Salesforce_data_issue_Summary": message}

        try:
            soql_test = f"SELECT {self._TEST_FIELDS} FROM hed__Test__c WHERE Id = '{test_id}' LIMIT 1"
            test_result = self._call_sf_api_with_retry(lambda: self.sf.query(soql_test))
            if not test_result.get('totalSize'):
                return _fallback({}, f"Test record {test_id} not found in Salesforce.")
            test_rec = test_result['records'][0]

            application_id = test_rec.get('Application__c') or parent_application_id
            record_type_name = test_rec.get('RecordTypeName__c') or ''
            if not record_type_name:
                return _fallback({"LastModifiedDate": test_rec.get("LastModifiedDate")},
                                 "RecordTypeName__c is missing on Test record.")

            soql_ts = f"SELECT {self._TEST_SCORE_FIELDS} FROM hed__Test_Score__c WHERE hed__Test__c = '{test_id}' LIMIT 1"
            ts_result = self._call_sf_api_with_retry(lambda: self.sf.query(soql_ts))
            ts_rec = ts_result['records'][0] if ts_result.get('totalSize') else {}

            applicant_name = None
            applicant_birthdate = None
            try:
                soql_app = (
                    f"SELECT hed__Applicant__r.Name, hed__Applicant__r.Birthdate "
                    f"FROM {APPLICATION_OBJECT_API_NAME} WHERE Id = '{application_id}' LIMIT 1"
                )
                app_result = self._call_sf_api_with_retry(lambda: self.sf.query(soql_app))
                if app_result.get('totalSize'):
                    applicant_rel = app_result['records'][0].get('hed__Applicant__r') or {}
                    applicant_name = applicant_rel.get('Name')
                    applicant_birthdate = applicant_rel.get('Birthdate')
            except Exception as e:
                logger.warning(f"Could not fetch applicant info for application {application_id}: {e}")

            record_data = self._build_test_score_payload(
                test_rec, ts_rec, record_type_name, applicant_name, applicant_birthdate
            )

            dci_name_criteria = self._TEST_SCORE_DCI_MAP.get(record_type_name.upper())
            if not dci_name_criteria:
                return _fallback(record_data, f"Unsupported Test RecordTypeName__c: {record_type_name}.")

            soql_dci = (
                f"SELECT Id FROM DocumentChecklistItem "
                f"WHERE ParentRecordId = '{application_id}' AND Name LIKE '%{dci_name_criteria}%' "
                f"ORDER BY CreatedDate DESC LIMIT 1"
            )
            dci_result = self._call_sf_api_with_retry(lambda: self.sf.query(soql_dci))
            if not dci_result.get('totalSize'):
                return _fallback(record_data, f"Required document '{dci_name_criteria}' not found for Application {application_id}.")

            dci_id = dci_result['records'][0]['Id']
            soql_link = (
                f"SELECT ContentDocument.LatestPublishedVersionId "
                f"FROM ContentDocumentLink WHERE LinkedEntityId = '{dci_id}' "
                f"ORDER BY SystemModstamp DESC LIMIT 1"
            )
            link_result = self._call_sf_api_with_retry(lambda: self.sf.query(soql_link))
            if not link_result.get('totalSize'):
                return _fallback(record_data, f"No file attached to DocumentChecklistItem for '{dci_name_criteria}'.")

            content_version_id = link_result['records'][0]['ContentDocument']['LatestPublishedVersionId']
            document_payload = self._download_content_version(content_version_id)
            return {"recordData": record_data, "documentPayload": document_payload, "Salesforce_data_issue_Summary": None}

        except SalesforceAPIError:
            raise
        except Exception as e:
            logger.error(f"Error in get_test_score_record_data for {test_id}: {e}", exc_info=True)
            raise SalesforceAPIError(f"Failed to fetch Test Score record data: {e}")

    def _build_test_score_payload(
        self,
        test_rec: Dict[str, Any],
        ts_rec: Dict[str, Any],
        record_type_name: str,
        applicant_name: Optional[str],
        applicant_birthdate: Optional[str],
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "RecordTypeName__c": record_type_name,
            "LastModifiedDate": test_rec.get("LastModifiedDate"),
            "Test_Mode": ts_rec.get("Mode_of_Test_Taken__c"),
            "Applicant_Name": applicant_name,
            "Applicant_Birthdate": applicant_birthdate,
            "Applicant_Test_Date": test_rec.get("hed__Test_Date__c"),
            "Applicant_Email": test_rec.get("Email__c"),
            "Applicant_Registration_No": test_rec.get("Registration_No__c"),
            "Applicant_VerbalPercentile": test_rec.get("VerbalPercentile__c"),
            "Applicant_VerbalScore": test_rec.get("VerbalScore__c"),
            "Applicant_QuantPercentile": test_rec.get("QuantPercentile__c"),
            "Applicant_QuantScore": test_rec.get("QuantScore__c"),
            "API_Name": None,
            "API_Birthdate": ts_rec.get("Birthdate__c"),
            "API_Test_Date": ts_rec.get("Test_Date__c"),
            "API_Email": ts_rec.get("Email__c"),
            "API_Registration_No": ts_rec.get("Registration_No__c"),
            "API_VerbalPercentile": ts_rec.get("VerbalPercentile__c"),
            "API_VerbalScore": ts_rec.get("VerbalScore__c"),
            "API_QuantPercentile": ts_rec.get("QuantPercentile__c"),
            "API_QuantScore": ts_rec.get("QuantScore__c"),
        }

        rtn_upper = record_type_name.upper()
        if rtn_upper == "GRE":
            payload["Applicant_Analytical_Percentile"] = test_rec.get("Analytical_Percentile__c")
            payload["Applicant_Analytical_Score"] = test_rec.get("Analytical_Score__c")
            payload["API_Analytical_Percentile"] = ts_rec.get("Analytical_Percentile__c")
            payload["API_Analytical_Score"] = ts_rec.get("Analytical_Score__c")
        elif rtn_upper == "GMAT":
            payload["Applicant_Total_Score"] = test_rec.get("Total_Score__c")
            payload["Applicant_Total_Percentile"] = test_rec.get("Total_Percentile__c")
            payload["API_Total_Score"] = ts_rec.get("Total_Score__c")
            payload["API_Total_Percentile"] = ts_rec.get("Total_Percentile__c")
            payload["Applicant_Test_ID"] = test_rec.get("Test_ID__c")
            payload["Applicant_Analytical_Percentile"] = test_rec.get("Analytical_Percentile__c")
            payload["Applicant_Analytical_Score"] = test_rec.get("Analytical_Score__c")
            payload["Applicant_IR_Percentile"] = test_rec.get("IR_Percentile__c")
            payload["Applicant_IR_Score"] = test_rec.get("IR_Score__c")
            payload["API_Test_ID"] = ts_rec.get("Test_ID__c")
            payload["API_Analytical_Percentile"] = ts_rec.get("Analytical_Percentile__c")
            payload["API_Analytical_Score"] = ts_rec.get("Analytical_Score__c")
            payload["API_IR_Percentile"] = ts_rec.get("IR_Percentile__c")
            payload["API_IR_Score"] = ts_rec.get("IR_Score__c")
        elif rtn_upper == "GMAT_FOCUS":
            payload["Applicant_Total_Score"] = test_rec.get("Total_Score__c")
            payload["Applicant_Total_Percentile"] = test_rec.get("Total_Percentile__c")
            payload["API_Total_Score"] = ts_rec.get("Total_Score__c")
            payload["API_Total_Percentile"] = ts_rec.get("Total_Percentile__c")
            payload["Applicant_Test_ID"] = test_rec.get("Test_ID__c")
            payload["Applicant_Data_Insights_Percentile"] = test_rec.get("Data_Insights_Percentile__c")
            payload["Applicant_Data_Insights_Score"] = test_rec.get("Data_Insights_score__c")
            payload["API_Test_ID"] = ts_rec.get("Test_ID__c")
            payload["API_Data_Insights_Percentile"] = ts_rec.get("Data_Insights_Percentile__c")
            payload["API_Data_Insights_Score"] = ts_rec.get("Data_Insights_Score__c")

        return payload

    # -----------------------------------------------------------------------
    # DCI Document Data (for Resume)
    # -----------------------------------------------------------------------

    def get_dci_document_data(self, dci_id: str) -> Dict[str, Any]:
        import time
        from app.services.salesforce_service import SalesforceAPIError
        if not dci_id:
            raise SalesforceAPIError("DCI ID is required to fetch document data.")
        self._ensure_connected()

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
                    break
                logger.warning(f"Attempt {attempt + 1}/{max_retries}: ContentDocumentLink not found for DCI {dci_id}.")
                time.sleep(retry_delay_seconds)
            except Exception as e:
                logger.error(f"Exception during SOQL for DCI {dci_id} on attempt {attempt+1}: {e}", exc_info=True)
                time.sleep(retry_delay_seconds)

        if not content_version_id:
            raise SalesforceAPIError(f"Could not find any ContentDocumentLink for DCI {dci_id} after {max_retries} attempts.")

        try:
            handler = getattr(self.sf, 'ContentVersion')
            version_record = self._call_sf_api_with_retry(lambda: handler.get(content_version_id))
            version_data_url = version_record.get('VersionData')
            if not version_data_url:
                raise SalesforceAPIError(f"ContentVersion {content_version_id} has no VersionData URL.")
            full_download_url = f"https://{self.sf.sf_instance}{version_data_url}"

            def do_download():
                resp = self.sf.session.get(full_download_url, headers=self.sf.headers, timeout=60)
                resp.raise_for_status()
                return resp.content

            file_bytes = self._call_sf_api_with_retry(do_download)
            base64_data = base64.b64encode(file_bytes).decode('utf-8')
            return {
                "LastModifiedDate": dci_last_modified,
                "documentPayload": {
                    "fileName": version_record.get('Title'),
                    "fileExtension": version_record.get('FileExtension'),
                    "base64Data": base64_data,
                    "LastModifiedDate": version_record.get('LastModifiedDate'),
                },
            }
        except Exception as e:
            logger.error(f"File download failed for CV ID {content_version_id}: {e}", exc_info=True)
            raise SalesforceAPIError(f"File download failed for ContentVersion {content_version_id}: {e}")
