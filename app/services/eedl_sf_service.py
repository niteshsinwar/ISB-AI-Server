# project_root/app/services/eedl_sf_service.py
"""
EEDL (Executive Education & Digital Learning) Track Salesforce operations.
All methods here are mixed into SalesforceService via EedlSFMixin.
"""
import logging
import asyncio
import json
import base64
from typing import Dict, Any, Optional, List

from app.config import (
    AI_SERVER_JOB_OBJECT_API_NAME, AIJ_JOB_ID_FIELD,
    AIJ_STATUS_FIELD, AIJ_MESSAGE_FIELD, AIJ_PROGRESS_FIELD, AIJ_CLIENT_FP_FIELD, AIJ_LOGS_FIELD,
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


class EedlSFMixin:
    """
    Mixin containing all EEDL-track Salesforce methods.
    Must be used alongside the base SalesforceService which provides:
      - self.sf, self._ensure_connected(), self._call_sf_api_with_retry()
      - self._download_content_version(), self._get_field_value_case_insensitive()
    """

    # -----------------------------------------------------------------------
    # File matching helpers
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

    # -----------------------------------------------------------------------
    # EEDL: ID Document Data
    # -----------------------------------------------------------------------

    def get_eedl_id_document_data(self, opportunity_id: str) -> Dict[str, Any]:
        from app.services.salesforce_service import SalesforceAPIError
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
                return {"recordData": record_data, "documentPayload": None, "Salesforce_data_issue_Summary": "No Aadhaar/Passport file found on Opportunity."}

            document_payload = self._download_content_version(matched['version_id'])
            return {"recordData": record_data, "documentPayload": document_payload, "Salesforce_data_issue_Summary": None}
        except SalesforceAPIError:
            raise
        except Exception as e:
            logger.error(f"Error in get_eedl_id_document_data for {opportunity_id}: {e}", exc_info=True)
            raise SalesforceAPIError(f"Failed to fetch EEDL ID document data: {e}")

    # -----------------------------------------------------------------------
    # EEDL: Education Record Data
    # -----------------------------------------------------------------------

    def get_eedl_education_record_data(self, education_id: str, opportunity_id: str) -> Dict[str, Any]:
        from app.services.salesforce_service import SalesforceAPIError
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
                return {"recordData": record_data, "documentPayload": None, "Salesforce_data_issue_Summary": f"No document file matched for Education record {education_id}."}

            document_payload = self._download_content_version(matched['version_id'])
            return {"recordData": record_data, "documentPayload": document_payload, "Salesforce_data_issue_Summary": None}
        except SalesforceAPIError:
            raise
        except Exception as e:
            logger.error(f"Error in get_eedl_education_record_data for {education_id}: {e}", exc_info=True)
            raise SalesforceAPIError(f"Failed to fetch EEDL education record data: {e}")

    # -----------------------------------------------------------------------
    # EEDL: Education ID discovery
    # -----------------------------------------------------------------------

    def get_eedl_education_ids_for_opportunity(self, opportunity_id: str) -> List[str]:
        from app.services.salesforce_service import SalesforceAPIError
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

    # -----------------------------------------------------------------------
    # EEDL: Opportunity Citizenship Update
    # -----------------------------------------------------------------------

    def update_opportunity_citizenship(self, opportunity_id: str, citizenship_value: str) -> bool:
        from app.services.salesforce_service import SalesforceAPIError
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

    # -----------------------------------------------------------------------
    # EEDL: Verification Summary CRUD
    # -----------------------------------------------------------------------

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
        from app.services.salesforce_service import SalesforceAPIError
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

    # -----------------------------------------------------------------------
    # EEDL: Existing VS metadata (skip-logic)
    # -----------------------------------------------------------------------

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

    # -----------------------------------------------------------------------
    # EEDL: AI Server Job
    # -----------------------------------------------------------------------

    async def upsert_eedl_ai_server_job(self, job_id: str, opportunity_id: str, status: str, **kwargs) -> str:
        from app.services.salesforce_service import SalesforceAPIError
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

                def do_update():
                    return self._call_sf_api_with_retry(lambda: handler.update(existing_id, update_payload))
                resp = await asyncio.get_event_loop().run_in_executor(None, do_update)
                if resp != 204:
                    raise SalesforceAPIError(f"Failed to update EEDL AI Server Job. Status: {resp}")
                return existing_id
            else:
                def do_create():
                    return self._call_sf_api_with_retry(lambda: handler.create(payload))
                resp = await asyncio.get_event_loop().run_in_executor(None, do_create)
                if not (isinstance(resp, dict) and resp.get('success')):
                    raise SalesforceAPIError(f"Failed to create EEDL AI Server Job. Response: {resp}")
                return resp.get('id')
        except Exception as e:
            raise SalesforceAPIError(f"Upsert for EEDL AI Server Job failed: {e}")

    async def get_latest_eedl_ai_server_job(self, opportunity_id: str) -> Optional[Dict[str, Any]]:
        from app.services.salesforce_service import SalesforceAPIError
        self._ensure_connected()
        soql = (
            f"SELECT Id, {AIJ_JOB_ID_FIELD}, {AIJ_STATUS_FIELD}, {AIJ_MESSAGE_FIELD}, "
            f"CreatedDate, LastModifiedDate, {AIJ_PROGRESS_FIELD}, {AIJ_CLIENT_FP_FIELD}, {AIJ_LOGS_FIELD} "
            f"FROM {AI_SERVER_JOB_OBJECT_API_NAME} "
            f"WHERE {AIJ_OPPORTUNITY_LOOKUP_FIELD} = '{opportunity_id}' "
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
