import os
import logging
import asyncio
from simple_salesforce import Salesforce, SalesforceAuthenticationFailed, SalesforceMalformedRequest, SalesforceResourceNotFound
import requests
from typing import Tuple, List, Dict, Any, Optional

from fastapi import HTTPException, Depends # For dependency injection

# Import configurations from app.config
from app.config import (
    SALESFORCE_USERNAME, SALESFORCE_PASSWORD, SALESFORCE_SECURITY_TOKEN,
    SALESFORCE_DOMAIN, SALESFORCE_INSTANCE_URL, SALESFORCE_AUTH_MODE,
    SALESFORCE_CLIENT_ID, SALESFORCE_CLIENT_SECRET, SALESFORCE_TOKEN_URL,
    APEX_ENDPOINT_PATHS, # This is now a Dict directly from config
    APPLICATION_OBJECT_API_NAME, APPLICATION_ANALYSIS_REPORT_FIELD,
    EDUCATION_HISTORY_OBJECT_API_NAME, EDUCATION_HISTORY_ANALYSIS_REPORT_FIELD,
    TEST_SCORE_OBJECT_API_NAME, TEST_SCORE_ANALYSIS_REPORT_FIELD,
    ISB_EMPLOYMENT_LOG_OBJECT_API_NAME, EMPLOYMENT_LOG_ANALYSIS_REPORT_FIELD # NEW
)

logger = logging.getLogger(__name__)

class SalesforceService:
    def __init__(self):
        self.sf: Optional[Salesforce] = None
        self.instance_url: Optional[str] = None
        self.auth_mode: str = SALESFORCE_AUTH_MODE

        # Map SObject API names to their respective analysis report field API names.
        # This is used by update_record_analysis_report.
        # The key is the API name of the SObject WHERE THE REPORT FIELD EXISTS.
        self.analysis_report_field_map: Dict[str, str] = {
            APPLICATION_OBJECT_API_NAME: APPLICATION_ANALYSIS_REPORT_FIELD,
            EDUCATION_HISTORY_OBJECT_API_NAME: EDUCATION_HISTORY_ANALYSIS_REPORT_FIELD,
            TEST_SCORE_OBJECT_API_NAME: TEST_SCORE_ANALYSIS_REPORT_FIELD,
            ISB_EMPLOYMENT_LOG_OBJECT_API_NAME: EMPLOYMENT_LOG_ANALYSIS_REPORT_FIELD, # NEW
        }
        
        # APEX_ENDPOINT_PATHS is already a dict from config.py.
        # Keys in this map are used by get_record_detail_from_apex to construct the endpoint URL.
        # The key should match what the processor passes as 'sobject_api_name_key'.
        self.apex_endpoint_path_map: Dict[str, str] = APEX_ENDPOINT_PATHS

        self._connect() # Attempt connection on initialization

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
        The 'sobject_api_name_key' must be a key in self.apex_endpoint_path_map (from config.APEX_ENDPOINT_PATHS).
        For employment, this key will be ISB_EMPLOYMENT_LOG_OBJECT_API_NAME, and the record_id will be the
        ISB_Employment_Log__c ID. The Apex endpoint will handle fetching related Affiliation data.
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
        full_url = f"https://{self.instance_url.strip('/')}{apex_rest_path}"

        logger.info(f"Calling Apex REST: POST {full_url} for SObject key '{sobject_api_name_key}' with ID {record_id}")
        try:
            response = self.sf.session.post(full_url, headers=self.sf.headers, json={}, timeout=60)

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
        Updates the analysis report field on a given Salesforce record.
        'sobject_api_name' is the API name of the SObject where the report field exists
        (e.g., ISB_Employment_Log__c for employment reports).
        """
        self._ensure_connected()
        if not (isinstance(record_id, str) and (len(record_id) == 15 or len(record_id) == 18)):
            logger.error(f"Invalid Salesforce ID format for record_id: {record_id}")
            return False

        report_field_api_name = self.analysis_report_field_map.get(sobject_api_name)
        if not report_field_api_name:
            logger.error(f"No analysis report field configured for SObject API name: {sobject_api_name} in analysis_report_field_map.")
            return False

        sObject_type_instance = getattr(self.sf, sobject_api_name, None)
        if sObject_type_instance is None:
            logger.error(f"SObject type '{sobject_api_name}' not found in Salesforce instance via simple_salesforce. Cannot update.")
            return False

        update_payload = {report_field_api_name: report_content}
        logger.info(f"Attempting to update {sobject_api_name} ID {record_id} field '{report_field_api_name}'.")
        try:
            status_code = sObject_type_instance.update(record_id, update_payload)
            if status_code == 204:
                logger.info(f"Successfully updated {sobject_api_name} ID {record_id}.")
                return True
            else:
                response_content = "No detailed response content."
                if self.sf.session.last_response:
                    try:
                        response_content = self.sf.session.last_response.json()
                    except Exception:
                        response_content = self.sf.session.last_response.text
                logger.warning(f"Update for {sobject_api_name} ID {record_id} returned status {status_code}. Response: {response_content}")
                return False
        except SalesforceResourceNotFound:
            logger.error(f"{sobject_api_name} record with ID '{record_id}' not found for update.")
            return False
        except SalesforceMalformedRequest as e:
            err_content = e.content if hasattr(e, 'content') else str(e)
            logger.error(f"Malformed request updating {sobject_api_name} ID {record_id}. Payload: {update_payload}. Error: {err_content}")
            return False
        except AttributeError as e:
             logger.error(f"Could not perform update for '{sobject_api_name}'. It might not be a standard or correctly mapped SObject. Error: {e}")
             return False
        except Exception as e:
            logger.error(f"Error updating {sobject_api_name} ID {record_id}: {e}", exc_info=True)
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
            result = self.sf.query_all(soql_query)
            record_ids = [record['Id'] for record in result['records']]
            logger.info(f"Found {len(record_ids)} '{child_object_api_name}' records related to '{parent_object_api_name}' ID {parent_record_id}.")
            return record_ids
        except SalesforceMalformedRequest as e:
            logger.error(f"Malformed SOQL (direct relation): {soql_query} - Error: {e.content if hasattr(e, 'content') else e}")
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
            result = self.sf.query_all(soql_query)
            record_ids = [record[junction_field_to_target] for record in result['records'] if record[junction_field_to_target] is not None]
            logger.info(f"Found {len(record_ids)} target IDs via '{junction_object_api_name}' for '{parent_object_api_name}' ID {parent_record_id}.")
            return record_ids
        except SalesforceMalformedRequest as e:
            logger.error(f"Malformed SOQL (junction): {soql_query} - Error: {e.content if hasattr(e, 'content') else e}")
            raise ValueError(f"SOQL query error. Check object/field names and permissions for {junction_object_api_name}, {junction_field_to_parent}, {junction_field_to_target}.")
        except KeyError as e_key: 
            logger.error(f"Field '{junction_field_to_target}' not in query result from {junction_object_api_name}. Query: {soql_query}. Error: {e_key}")
            raise ValueError(f"Configuration error: Field '{junction_field_to_target}' missing in junction query result.")
        except Exception as e:
            logger.error(f"Error fetching target IDs via '{junction_object_api_name}' for Parent {parent_record_id} ({parent_object_api_name}): {e}", exc_info=True)
            raise RuntimeError(f"Failed to fetch target records via junction.")

# --- FastAPI Dependency ---
_sf_service_instance = None
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