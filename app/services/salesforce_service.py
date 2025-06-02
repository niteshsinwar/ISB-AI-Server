import os
import logging
import asyncio
from simple_salesforce import Salesforce, SalesforceAuthenticationFailed, SalesforceMalformedRequest, SalesforceResourceNotFound
import requests
from typing import Tuple, List, Dict, Any, Optional

from fastapi import HTTPException, Depends

# Import configurations from app.config
from app.config import (
    SALESFORCE_USERNAME, SALESFORCE_PASSWORD, SALESFORCE_SECURITY_TOKEN,
    SALESFORCE_DOMAIN, SALESFORCE_INSTANCE_URL, SALESFORCE_AUTH_MODE,
    SALESFORCE_CLIENT_ID, SALESFORCE_CLIENT_SECRET, SALESFORCE_TOKEN_URL,
    APEX_ENDPOINT_PATHS,
    # Import new AVS object and field names
    APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME,
    AVS_APPLICATION_LOOKUP_FIELD, AVS_CONTACT_LOOKUP_FIELD,
    AVS_EDUCATION_HISTORY_LOOKUP_FIELD, AVS_TEST_LOOKUP_FIELD,
    AVS_AFFILIATION_LOOKUP_FIELD, AVS_REPORT_FIELD, AVS_NAME_FIELD
)

logger = logging.getLogger(__name__)

class SalesforceService:
    def __init__(self):
        self.sf: Optional[Salesforce] = None
        self.instance_url: Optional[str] = None
        self.auth_mode: str = SALESFORCE_AUTH_MODE
        
        self.apex_endpoint_path_map: Dict[str, str] = APEX_ENDPOINT_PATHS

        self._connect()

    def _connect(self):
        """Internal method to establish Salesforce connection."""
        try:
            if self.auth_mode == "client_credentials":
                logger.info("Attempting Salesforce connection using Client Credentials Flow.")
                if not all([SALESFORCE_CLIENT_ID, SALESFORCE_CLIENT_SECRET, SALESFORCE_TOKEN_URL]):
                    msg = "SALESFORCE_CLIENT_ID, SECRET, and TOKEN_URL must be set for client_credentials flow."
                    logger.error(msg)
                    raise ValueError(msg)

                payload = {
                    'grant_type': 'client_credentials',
                    'client_id': SALESFORCE_CLIENT_ID,
                    'client_secret': SALESFORCE_CLIENT_SECRET
                }
                headers = {'Content-Type': 'application/x-www-form-urlencoded'}
                
                response = requests.post(SALESFORCE_TOKEN_URL, headers=headers, data=payload, timeout=30)
                response.raise_for_status()
                
                token_data = response.json()
                access_token = token_data.get("access_token")
                instance_url_from_token = token_data.get("instance_url")

                if not access_token or not instance_url_from_token:
                    msg = "Failed to retrieve access_token or instance_url from client credentials response."
                    logger.error(f"{msg} Response: {token_data}")
                    raise SalesforceAuthenticationFailed(msg)

                self.sf = Salesforce(instance_url=instance_url_from_token.rstrip('/'), session_id=access_token)
                self.instance_url = instance_url_from_token.replace("https://", "").split('/')[0]
                logger.info(f"Successfully connected to Salesforce via Client Credentials (Host: {self.instance_url})")

            elif self.auth_mode == "password":
                logger.info("Attempting Salesforce connection using Username-Password Flow.")
                if not all([SALESFORCE_USERNAME, SALESFORCE_PASSWORD, SALESFORCE_SECURITY_TOKEN, (SALESFORCE_DOMAIN or SALESFORCE_INSTANCE_URL)]):
                    msg = "Salesforce credentials (USERNAME, PASSWORD, TOKEN, and DOMAIN/INSTANCE_URL) missing."
                    logger.error(msg)
                    raise ValueError(msg)
                
                if SALESFORCE_INSTANCE_URL:
                    self.sf = Salesforce(
                        instance_url=SALESFORCE_INSTANCE_URL.rstrip('/'),
                        username=SALESFORCE_USERNAME, password=SALESFORCE_PASSWORD, security_token=SALESFORCE_SECURITY_TOKEN
                    )
                    self.instance_url = SALESFORCE_INSTANCE_URL.replace("https://", "").split('/')[0]
                else: 
                    self.sf = Salesforce(
                        username=SALESFORCE_USERNAME, password=SALESFORCE_PASSWORD, security_token=SALESFORCE_SECURITY_TOKEN,
                        domain=SALESFORCE_DOMAIN
                    )
                    if hasattr(self.sf, 'sf_instance') and self.sf.sf_instance:
                        self.instance_url = self.sf.sf_instance
                    elif hasattr(self.sf, 'base_url') and self.sf.base_url:
                        self.instance_url = self.sf.base_url.replace("https://", "").split('/')[0]
                    elif SALESFORCE_DOMAIN:
                        self.instance_url = f"{SALESFORCE_DOMAIN}.my.salesforce.com" 
                    else:
                        logger.critical("Could not determine Salesforce instance URL hostname for password flow with domain.")
                        raise ValueError("Could not determine Salesforce instance URL hostname.")
                logger.info(f"Successfully connected to Salesforce via Password Flow (Host: {self.instance_url})")
            else:
                msg = f"Unsupported SALESFORCE_AUTH_MODE: '{self.auth_mode}'. Must be 'client_credentials' or 'password'."
                logger.error(msg)
                raise ValueError(msg)

        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP error during client credentials token request: {e.response.text if e.response else str(e)}", exc_info=True)
            raise SalesforceAuthenticationFailed(f"Client credentials token request failed: {e.response.text if e.response else str(e)}")
        except SalesforceAuthenticationFailed as e:
            logger.error(f"Salesforce authentication failed: {e}")
            raise 
        except Exception as e:
            logger.error(f"Error connecting to Salesforce: {e}", exc_info=True)
            raise ValueError(f"Error connecting to Salesforce: {e}")

    def _ensure_connected(self):
        if not self.sf or not self.instance_url:
            logger.warning("Salesforce connection not established. Attempting to reconnect.")
            self._connect() 
            if not self.sf or not self.instance_url:
                 raise RuntimeError("Salesforce service not properly initialized or reconnection failed.")

    def get_record_detail_from_apex(self, record_id: str, sobject_api_name_key: str) -> Optional[Dict[str, Any]]:
        """
        Calls the Apex REST endpoint to get record details.
        """
        self._ensure_connected()
        if not (isinstance(record_id, str) and (len(record_id) == 15 or len(record_id) == 18)):
            logger.error(f"Invalid Salesforce ID format for record_id: {record_id}")
            return None

        endpoint_path_segment = self.apex_endpoint_path_map.get(sobject_api_name_key)
        if not endpoint_path_segment:
            logger.error(f"No Apex endpoint path configured for SObject API key: {sobject_api_name_key}")
            return None

        apex_rest_path = f"/services/apexrest/{endpoint_path_segment.strip('/')}/{record_id}"
        base_instance_url = self.instance_url.strip('/') if self.instance_url else '' # type: ignore
        full_url = f"https://{base_instance_url}{apex_rest_path}"


        logger.info(f"Calling Apex REST: POST {full_url} for SObject key '{sobject_api_name_key}' with ID {record_id}")
        try:
            response = self.sf.session.post(full_url, headers=self.sf.headers, json={}, timeout=60) # type: ignore

            if 400 <= response.status_code < 600:
                error_content = response.text
                logger.error(f"Apex REST call failed for SObject key '{sobject_api_name_key}' ID {record_id}. Status: {response.status_code}. Response: {error_content[:500]}")
                return None

            if response.content:
                try:
                    details = response.json()
                    logger.info(f"Successfully received details from Apex for SObject key '{sobject_api_name_key}' ID {record_id}.")
                    return details
                except requests.exceptions.JSONDecodeError:
                    logger.error(f"Failed to decode JSON from Apex for SObject key '{sobject_api_name_key}' ID {record_id}. Response: {response.text[:200]}")
                    return None
            else:
                logger.info(f"Apex endpoint for SObject key '{sobject_api_name_key}' ID {record_id} returned status {response.status_code} with empty body.")
                return {} if 200 <= response.status_code < 300 else None

        except requests.exceptions.RequestException as e:
            logger.error(f"Network error calling Apex for SObject key '{sobject_api_name_key}' ID {record_id}: {e}", exc_info=True)
            return None
        except Exception as e: 
            logger.error(f"Unexpected error calling Apex for SObject key '{sobject_api_name_key}' ID {record_id}: {e}", exc_info=True)
            return None

    def update_record_analysis_report(self, record_id: str, sobject_api_name: str, report_content: str) -> bool:
        """
        DEPRECATED in favor of upsert_verification_summary.
        """
        self._ensure_connected()
        logger.warning(f"DEPRECATED: update_record_analysis_report called for {sobject_api_name} ID {record_id}. Please use upsert_verification_summary.")
        logger.error(f"Direct update on {sobject_api_name} via update_record_analysis_report is disabled. Use upsert_verification_summary to update {APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME}.")
        return False


    def upsert_verification_summary(
        self,
        application_id: str,
        report_content: str,
        name_value: str,
        contact_id: Optional[str] = None,
        education_history_id: Optional[str] = None,
        test_id: Optional[str] = None,
        affiliation_id: Optional[str] = None
    ) -> bool:
        """
        Creates or updates an Application_Verification_Summary__c record.
        """
        self._ensure_connected()
        if not self.sf:
            logger.error("Salesforce connection not available for upsert_verification_summary.")
            return False

        summary_object_name = APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME
        
        application_id_soql = f"'{application_id}'"
        
        soql_query = f"SELECT Id FROM {summary_object_name} WHERE {AVS_APPLICATION_LOOKUP_FIELD} = {application_id_soql}"
        
        secondary_lookup_field: Optional[str] = None
        secondary_lookup_id_soql: Optional[str] = None
        secondary_lookup_id_value: Optional[str] = None


        if contact_id:
            secondary_lookup_id_soql = f"'{contact_id}'"
            soql_query += f" AND {AVS_CONTACT_LOOKUP_FIELD} = {secondary_lookup_id_soql}"
            secondary_lookup_field = AVS_CONTACT_LOOKUP_FIELD
            secondary_lookup_id_value = contact_id
        elif education_history_id:
            secondary_lookup_id_soql = f"'{education_history_id}'"
            soql_query += f" AND {AVS_EDUCATION_HISTORY_LOOKUP_FIELD} = {secondary_lookup_id_soql}"
            secondary_lookup_field = AVS_EDUCATION_HISTORY_LOOKUP_FIELD
            secondary_lookup_id_value = education_history_id
        elif test_id:
            secondary_lookup_id_soql = f"'{test_id}'"
            soql_query += f" AND {AVS_TEST_LOOKUP_FIELD} = {secondary_lookup_id_soql}"
            secondary_lookup_field = AVS_TEST_LOOKUP_FIELD
            secondary_lookup_id_value = test_id
        elif affiliation_id:
            secondary_lookup_id_soql = f"'{affiliation_id}'"
            soql_query += f" AND {AVS_AFFILIATION_LOOKUP_FIELD} = {secondary_lookup_id_soql}"
            secondary_lookup_field = AVS_AFFILIATION_LOOKUP_FIELD
            secondary_lookup_id_value = affiliation_id
        else:
            logger.error(f"No secondary ID (Contact, Education, Test, Affiliation) provided for Application {application_id} to find/create {summary_object_name}.")
            return False
        
        soql_query += " LIMIT 1"
        logger.info(f"Querying for existing {summary_object_name}: {soql_query}")

        try:
            # Get the SObject type instance dynamically
            sobject_handler = getattr(self.sf, summary_object_name, None)
            if sobject_handler is None:
                logger.error(f"SObject type '{summary_object_name}' not found in Salesforce instance. Cannot upsert.")
                return False

            result = self.sf.query(soql_query) # type: ignore
            payload = {
                AVS_REPORT_FIELD: report_content,
                AVS_NAME_FIELD: name_value 
            }

            if result.get('totalSize', 0) > 0 and len(result.get('records', [])) > 0:
                existing_summary_id = result['records'][0]['Id']
                logger.info(f"Found existing {summary_object_name} ID {existing_summary_id}. Updating report and name.")
                # Use the dynamic sobject_handler for update
                update_status_code = sobject_handler.update(existing_summary_id, payload)
                if update_status_code == 204: # HTTP 204 No Content indicates success for update
                    logger.info(f"Successfully updated {summary_object_name} ID {existing_summary_id}.")
                else:
                    logger.error(f"Failed to update {summary_object_name} ID {existing_summary_id}. Status code: {update_status_code}")
                    # You might want to inspect self.sf.session.last_response here for more details if available
                    return False
            else:
                logger.info(f"No existing {summary_object_name} found for Application {application_id} and secondary ID. Creating new record.")
                payload[AVS_APPLICATION_LOOKUP_FIELD] = application_id
                if secondary_lookup_field and secondary_lookup_id_value:
                    payload[secondary_lookup_field] = secondary_lookup_id_value
                
                # Use the dynamic sobject_handler for create
                create_response = sobject_handler.create(payload)
                if create_response.get('success'):
                    logger.info(f"Successfully created new {summary_object_name} record with ID {create_response.get('id')}.")
                else:
                    logger.error(f"Failed to create new {summary_object_name}. Errors: {create_response.get('errors')}")
                    return False
            return True

        except SalesforceMalformedRequest as e:
            err_content = e.content if hasattr(e, 'content') and e.content else str(e)
            logger.error(f"Malformed request during {summary_object_name} upsert. Query: {soql_query}. Error: {err_content}", exc_info=True)
            return False
        except SalesforceResourceNotFound as e:
            logger.error(f"{summary_object_name} or related field not found. Error: {e}", exc_info=True)
            return False
        except AttributeError as ae: # Could happen if getattr fails or sf is None
            logger.error(f"Attribute error during {summary_object_name} upsert, possibly SObject type not found or sf client issue: {ae}", exc_info=True)
            return False
        except Exception as e:
            logger.error(f"Error during {summary_object_name} upsert: {e}", exc_info=True)
            return False

    def get_directly_related_record_ids(
        self, parent_record_id: str, parent_object_api_name: str,
        child_object_api_name: str, lookup_field_on_child_to_parent: str
    ) -> List[str]:
        self._ensure_connected()
        if not (isinstance(parent_record_id, str) and (len(parent_record_id) == 15 or len(parent_record_id) == 18)):
            raise ValueError(f"Invalid parent_record_id format: {parent_record_id}")
        if not child_object_api_name or not lookup_field_on_child_to_parent:
            raise ValueError("child_object_api_name and lookup_field_on_child_to_parent are required.")

        soql_query = (
            f"SELECT Id FROM {child_object_api_name} "
            f"WHERE {lookup_field_on_child_to_parent} = '{parent_record_id}'"
        )
        logger.info(f"Executing SOQL for direct relation: {soql_query}")
        try:
            result = self.sf.query_all(soql_query) # type: ignore
            record_ids = [record['Id'] for record in result['records']]
            logger.info(f"Found {len(record_ids)} '{child_object_api_name}' records related to '{parent_object_api_name}' ID {parent_record_id}.")
            return record_ids
        except SalesforceMalformedRequest as e:
            err_content = e.content if hasattr(e, 'content') and e.content else str(e)
            logger.error(f"Malformed SOQL (direct relation): {soql_query} - Error: {err_content}")
            raise ValueError(f"SOQL query error. Check object/field names and permissions for {child_object_api_name} and {lookup_field_on_child_to_parent}.")
        except Exception as e:
            logger.error(f"Error fetching directly related '{child_object_api_name}' for Parent {parent_record_id} ({parent_object_api_name}): {e}", exc_info=True)
            raise RuntimeError(f"Failed to fetch directly related records.")

    def get_target_ids_via_junction(
        self, parent_record_id: str, parent_object_api_name: str,
        junction_object_api_name: str,
        junction_field_to_parent: str, junction_field_to_target: str
    ) -> List[str]:
        self._ensure_connected()
        if not (isinstance(parent_record_id, str) and (len(parent_record_id) == 15 or len(parent_record_id) == 18)):
            raise ValueError(f"Invalid parent_record_id format: {parent_record_id}")
        if not all([junction_object_api_name, junction_field_to_parent, junction_field_to_target]):
            raise ValueError("Junction object details (name, field to parent, field to target) are required.")

        soql_query = (
            f"SELECT {junction_field_to_target} FROM {junction_object_api_name} "
            f"WHERE {junction_field_to_parent} = '{parent_record_id}' AND {junction_field_to_target} != NULL"
        )
        logger.info(f"Executing SOQL via junction: {soql_query}")
        try:
            result = self.sf.query_all(soql_query) # type: ignore
            record_ids = [record[junction_field_to_target] for record in result['records'] if record[junction_field_to_target] is not None]
            logger.info(f"Found {len(record_ids)} target IDs via '{junction_object_api_name}' for '{parent_object_api_name}' ID {parent_record_id}.")
            return record_ids
        except SalesforceMalformedRequest as e:
            err_content = e.content if hasattr(e, 'content') and e.content else str(e)
            logger.error(f"Malformed SOQL (junction): {soql_query} - Error: {err_content}")
            raise ValueError(f"SOQL query error. Check object/field names and permissions for {junction_object_api_name}, {junction_field_to_parent}, {junction_field_to_target}.")
        except KeyError as e_key: 
            logger.error(f"Field '{junction_field_to_target}' not in query result from {junction_object_api_name}. Query: {soql_query}. Error: {e_key}")
            raise ValueError(f"Configuration error: Field '{junction_field_to_target}' missing in junction query result.")
        except Exception as e:
            logger.error(f"Error fetching target IDs via '{junction_object_api_name}' for Parent {parent_record_id} ({parent_object_api_name}): {e}", exc_info=True)
            raise RuntimeError(f"Failed to fetch target records via junction.")

# --- FastAPI Dependency ---
_sf_service_instance: Optional[SalesforceService] = None
_sf_service_lock = asyncio.Lock()

async def get_sf_service_dependency() -> SalesforceService:
    global _sf_service_instance
    if _sf_service_instance is None:
        async with _sf_service_lock:
            if _sf_service_instance is None:
                try:
                    logger.info("Initializing SalesforceService singleton for dependency...")
                    _sf_service_instance = SalesforceService()
                    if not _sf_service_instance.instance_url: 
                         logger.error("SalesforceService initialized but instance_url is missing. Connection likely failed.")
                         _sf_service_instance = None 
                         raise HTTPException(status_code=503, detail="Salesforce Service unavailable: Connection failed, instance URL not set.")
                    logger.info(f"SalesforceService singleton created. Instance URL: {_sf_service_instance.instance_url}") 
                except (ValueError, SalesforceAuthenticationFailed) as e:
                    logger.error(f"Failed to initialize SalesforceService for dependency: {e}", exc_info=True)
                    _sf_service_instance = None
                    raise HTTPException(status_code=503, detail=f"Salesforce Service unavailable: {str(e)}")
                except Exception as e: 
                    logger.error(f"Unexpected error initializing SalesforceService for dependency: {e}", exc_info=True)
                    _sf_service_instance = None
                    raise HTTPException(status_code=500, detail=f"Unexpected error initializing Salesforce Service: {str(e)}")
    try:
        _sf_service_instance._ensure_connected() 
    except (RuntimeError, ValueError, SalesforceAuthenticationFailed) as e:
        logger.error(f"SalesforceService connection check/reconnect failed for dependency: {e}", exc_info=True)
        _sf_service_instance = None 
        raise HTTPException(status_code=503, detail=f"Salesforce Service connection issue: {str(e)}")

    return _sf_service_instance