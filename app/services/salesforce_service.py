import os
import logging
import asyncio
from simple_salesforce import (
    Salesforce,
    SalesforceAuthenticationFailed,
    SalesforceMalformedRequest,
    SalesforceResourceNotFound,
    SalesforceExpiredSession # Ensure this is imported if specifically caught
)
import requests
from typing import Tuple, List, Dict, Any, Optional, Callable

from fastapi import HTTPException, Depends # Though Depends is for FastAPI app code, not service typically

# Import configurations from app.config
from app.config import (
    SALESFORCE_USERNAME, SALESFORCE_PASSWORD, SALESFORCE_SECURITY_TOKEN,
    SALESFORCE_DOMAIN, SALESFORCE_INSTANCE_URL, SALESFORCE_AUTH_MODE,
    SALESFORCE_CLIENT_ID, SALESFORCE_CLIENT_SECRET, SALESFORCE_TOKEN_URL,
    APEX_ENDPOINT_PATHS,
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
                    raise SalesforceAuthenticationFailed(msg, response_content=token_data) # Pass response_content

                self.sf = Salesforce(instance_url=instance_url_from_token.rstrip('/'), session_id=access_token)
                self.instance_url = instance_url_from_token.replace("https://", "").split('/')[0]
                logger.info(f"Successfully connected to Salesforce via Client Credentials (Host: {self.instance_url})")

            elif self.auth_mode == "password":
                logger.info("Attempting Salesforce connection using Username-Password Flow.")
                if not all([SALESFORCE_USERNAME, SALESFORCE_PASSWORD, SALESFORCE_SECURITY_TOKEN, (SALESFORCE_DOMAIN or SALESFORCE_INSTANCE_URL)]):
                    msg = "Salesforce credentials (USERNAME, PASSWORD, TOKEN, and DOMAIN/INSTANCE_URL) missing."
                    logger.error(msg)
                    raise ValueError(msg)
                
                # Store original values before potentially overwriting sf_instance
                instance_url_to_use = SALESFORCE_INSTANCE_URL
                domain_to_use = SALESFORCE_DOMAIN

                if instance_url_to_use:
                    self.sf = Salesforce(
                        instance_url=instance_url_to_use.rstrip('/'), # type: ignore
                        username=SALESFORCE_USERNAME, password=SALESFORCE_PASSWORD, security_token=SALESFORCE_SECURITY_TOKEN
                    )
                    self.instance_url = instance_url_to_use.replace("https://", "").split('/')[0] # type: ignore
                else: 
                    self.sf = Salesforce(
                        username=SALESFORCE_USERNAME, password=SALESFORCE_PASSWORD, security_token=SALESFORCE_SECURITY_TOKEN,
                        domain=domain_to_use # type: ignore
                    )
                    if hasattr(self.sf, 'sf_instance') and self.sf.sf_instance:
                        self.instance_url = self.sf.sf_instance
                    elif hasattr(self.sf, 'base_url') and self.sf.base_url: # simple_salesforce uses base_url
                        self.instance_url = self.sf.base_url.replace("https://", "").split('/')[0]
                    elif domain_to_use:
                        self.instance_url = f"{domain_to_use}.my.salesforce.com" 
                    else:
                        logger.critical("Could not determine Salesforce instance URL hostname for password flow.")
                        raise ValueError("Could not determine Salesforce instance URL hostname.")
                logger.info(f"Successfully connected to Salesforce via Password Flow (Host: {self.instance_url})")
            else:
                msg = f"Unsupported SALESFORCE_AUTH_MODE: '{self.auth_mode}'. Must be 'client_credentials' or 'password'."
                logger.error(msg)
                raise ValueError(msg)

        except requests.exceptions.HTTPError as e:
            error_text = e.response.text if e.response is not None else str(e)
            logger.error(f"HTTP error during client credentials token request: {error_text}", exc_info=True)
            raise SalesforceAuthenticationFailed(f"Client credentials token request failed: {error_text}", response_content=e.response.content if e.response is not None else None)
        except SalesforceAuthenticationFailed as e:
            logger.error(f"Salesforce authentication failed: {e.message if hasattr(e, 'message') else e}")
            raise 
        except Exception as e:
            logger.error(f"Error connecting to Salesforce: {e}", exc_info=True)
            raise ValueError(f"Error connecting to Salesforce: {e}")

    def _ensure_connected(self):
        """Ensures that self.sf is initialized, attempting to connect if not."""
        if not self.sf or not self.instance_url:
            logger.warning("Salesforce connection not established or invalid. Attempting to connect/reconnect.")
            self._connect() 
            if not self.sf or not self.instance_url: # Check again after attempting connection
                 logger.error("Salesforce service reconnection failed or instance_url still not set.")
                 raise RuntimeError("Salesforce service not properly initialized or reconnection failed.")

    def _call_sf_api_with_retry(self, api_call_func: Callable, *args, **kwargs) -> Any:
        """
        Wrapper to execute a Salesforce API call with a single retry on session expiration.
        'api_call_func' is a callable that performs the actual simple_salesforce operation.
        """
        try:
            self._ensure_connected() # Ensures self.sf is not None and tries to connect if it is
            return api_call_func(*args, **kwargs)
        except (SalesforceAuthenticationFailed, SalesforceExpiredSession) as e: # SalesforceExpiredSession is often a subclass
            logger.warning(f"Salesforce operation failed due to session issue ({type(e).__name__}). "
                           "Attempting to reconnect and retry once.")
            try:
                self._connect()  # Force re-authentication, this will update self.sf
                # Retry the original operation with the new self.sf instance
                return api_call_func(*args, **kwargs)
            except Exception as retry_exception:
                logger.error(f"Salesforce operation failed on retry after re-authentication: {retry_exception}", exc_info=True)
                raise retry_exception 
        # Non-authentication errors are allowed to propagate directly
        # Specific errors like SalesforceMalformedRequest or SalesforceResourceNotFound
        # will be caught by the calling methods if needed.

    def get_record_detail_from_apex(self, record_id: str, sobject_api_name_key: str) -> Optional[Dict[str, Any]]:
        if not (isinstance(record_id, str) and (len(record_id) == 15 or len(record_id) == 18)):
            logger.error(f"Invalid Salesforce ID format for record_id: {record_id}")
            return None # Or raise ValueError for caller to handle

        endpoint_path_segment = self.apex_endpoint_path_map.get(sobject_api_name_key)
        if not endpoint_path_segment:
            logger.error(f"No Apex endpoint path configured for SObject API key: {sobject_api_name_key}")
            return None

        # Ensure instance_url is valid before forming full_url
        self._ensure_connected() 
        base_instance_url = self.instance_url.strip('/') # type: ignore
        apex_rest_path = f"/services/apexrest/{endpoint_path_segment.strip('/')}/{record_id}"
        full_url = f"https://{base_instance_url}{apex_rest_path}"

        logger.info(f"Calling Apex REST: POST {full_url} for SObject key '{sobject_api_name_key}' with ID {record_id}")

        def do_apex_post():
            if not self.sf or not self.sf.session: # self.sf should be valid after _ensure_connected
                raise RuntimeError("Salesforce client (self.sf.session) not properly initialized for Apex call.")
            # Headers should be fresh if _connect was called by _call_sf_api_with_retry
            return self.sf.session.post(full_url, headers=self.sf.headers, json={}, timeout=60)

        try:
            response = self._call_sf_api_with_retry(do_apex_post)

            if 400 <= response.status_code < 600:
                error_content = response.text
                # If it's an auth error status code even after retry, log it critically.
                if response.status_code in [401, 403]:
                    logger.critical(f"Persistent Authentication Failure ({response.status_code}) for Apex call to {full_url} "
                                    f"for ID {record_id} even after retry attempt. Response: {error_content[:500]}")
                else:
                    logger.error(f"Apex REST call failed for {full_url} (SObject key '{sobject_api_name_key}' ID {record_id}). "
                                 f"Status: {response.status_code}. Response: {error_content[:500]}")
                return None

            if response.content:
                try:
                    details = response.json()
                    logger.info(f"Successfully received details from Apex for SObject key '{sobject_api_name_key}' ID {record_id}.")
                    return details
                except requests.exceptions.JSONDecodeError:
                    logger.error(f"Failed to decode JSON from Apex for SObject key '{sobject_api_name_key}' ID {record_id}. Response: {response.text[:200]}")
                    return None
            else: # No content but success status
                logger.info(f"Apex endpoint for SObject key '{sobject_api_name_key}' ID {record_id} returned status {response.status_code} with empty body.")
                return {} if 200 <= response.status_code < 300 else None

        except requests.exceptions.RequestException as e: # Network errors
            logger.error(f"Network error calling Apex for SObject key '{sobject_api_name_key}' ID {record_id} to {full_url}: {e}", exc_info=True)
            return None
        except (SalesforceMalformedRequest, SalesforceResourceNotFound) as e_sf: # Specific SF errors not retried
             logger.error(f"Salesforce API error (not retried) for Apex call to {full_url} (ID {record_id}): {e_sf}", exc_info=True)
             return None
        except Exception as e: # Catches errors from retry logic or other unexpected issues
            logger.error(f"Unexpected error calling Apex for SObject key '{sobject_api_name_key}' ID {record_id} to {full_url}: {e}", exc_info=True)
            return None

    def upsert_verification_summary(
        self, application_id: str, report_content: str, name_value: str,
        contact_id: Optional[str] = None, education_history_id: Optional[str] = None,
        test_id: Optional[str] = None, affiliation_id: Optional[str] = None
    ) -> bool:
        summary_object_name = APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME
        soql_query_parts = [f"SELECT Id FROM {summary_object_name} WHERE {AVS_APPLICATION_LOOKUP_FIELD} = '{application_id}'"]
        
        payload_updates: Dict[str, Any] = {
            AVS_REPORT_FIELD: report_content,
            AVS_NAME_FIELD: name_value,
            AVS_APPLICATION_LOOKUP_FIELD: application_id # Ensure application_id is in payload for create
        }
        secondary_lookup_field: Optional[str] = None
        secondary_lookup_id_value: Optional[str] = None

        if contact_id:
            secondary_lookup_field = AVS_CONTACT_LOOKUP_FIELD
            secondary_lookup_id_value = contact_id
        elif education_history_id:
            secondary_lookup_field = AVS_EDUCATION_HISTORY_LOOKUP_FIELD
            secondary_lookup_id_value = education_history_id
        elif test_id:
            secondary_lookup_field = AVS_TEST_LOOKUP_FIELD
            secondary_lookup_id_value = test_id
        elif affiliation_id:
            secondary_lookup_field = AVS_AFFILIATION_LOOKUP_FIELD
            secondary_lookup_id_value = affiliation_id
        else:
            logger.error(f"No secondary ID provided for App {application_id} to find/create {summary_object_name}.")
            return False

        if secondary_lookup_field and secondary_lookup_id_value:
            soql_query_parts.append(f" AND {secondary_lookup_field} = '{secondary_lookup_id_value}'")
            payload_updates[secondary_lookup_field] = secondary_lookup_id_value # For create
        
        soql_query = "".join(soql_query_parts) + " LIMIT 1"
        logger.info(f"Querying for existing {summary_object_name}: {soql_query}")

        try:
            # Ensure self.sf is ready and sobject_handler can be obtained
            self._ensure_connected()
            if not self.sf: # Should have been handled by _ensure_connected
                raise RuntimeError("Salesforce client (self.sf) is not available for upsert.")
                
            sobject_handler = getattr(self.sf, summary_object_name, None)
            if sobject_handler is None:
                logger.error(f"SObject type '{summary_object_name}' not found in Salesforce instance.")
                return False

            def do_sf_query(q: str):
                if not self.sf: raise RuntimeError("SF client lost before query")
                return self.sf.query(q)
            result = self._call_sf_api_with_retry(do_sf_query, soql_query)

            if result.get('totalSize', 0) > 0 and len(result.get('records', [])) > 0:
                existing_summary_id = result['records'][0]['Id']
                logger.info(f"Found existing {summary_object_name} ID {existing_summary_id}. Updating.")
                
                # Payload for update doesn't need lookup fields usually, only fields being changed
                update_payload = {
                    AVS_REPORT_FIELD: report_content,
                    AVS_NAME_FIELD: name_value 
                }

                def do_sf_update(handler: Any, rec_id: str, load: Dict[str, Any]):
                    return handler.update(rec_id, load) # type: ignore
                update_status_code = self._call_sf_api_with_retry(do_sf_update, sobject_handler, existing_summary_id, update_payload)
                
                if update_status_code == 204:
                    logger.info(f"Successfully updated {summary_object_name} ID {existing_summary_id}.")
                    return True
                else:
                    logger.error(f"Failed to update {summary_object_name} ID {existing_summary_id}. Status code: {update_status_code}")
                    return False
            else:
                logger.info(f"No existing {summary_object_name} found. Creating new record with payload: {payload_updates}")
                def do_sf_create(handler: Any, load: Dict[str, Any]):
                    return handler.create(load) # type: ignore
                create_response = self._call_sf_api_with_retry(do_sf_create, sobject_handler, payload_updates)
                
                if create_response.get('success'):
                    logger.info(f"Successfully created new {summary_object_name} record with ID {create_response.get('id')}.")
                    return True
                else:
                    logger.error(f"Failed to create new {summary_object_name}. Errors: {create_response.get('errors')}")
                    return False

        except (SalesforceMalformedRequest, SalesforceResourceNotFound) as e_sf_specific:
            logger.error(f"Salesforce API error during {summary_object_name} upsert for App {application_id}: {e_sf_specific}", exc_info=True)
            return False
        except Exception as e: # Catches errors from retry logic or other unexpected issues
            logger.error(f"Unexpected error during {summary_object_name} upsert for App {application_id}: {e}", exc_info=True)
            return False

    def _fetch_related_ids_generic(self, soql_query: str, context_log_message: str) -> List[str]:
        """Generic helper to run a SOQL query for IDs and handle retries."""
        logger.info(f"Executing SOQL: {soql_query} ({context_log_message})")
        
        def do_sf_query_all(q: str):
            if not self.sf: raise RuntimeError("SF client lost before query_all")
            return self.sf.query_all(q) # type: ignore

        try:
            result = self._call_sf_api_with_retry(do_sf_query_all, soql_query)
            record_ids = [record['Id'] for record in result['records']] # Assumes 'Id' is always the field
            logger.info(f"Found {len(record_ids)} records for: {context_log_message}.")
            return record_ids
        except SalesforceMalformedRequest as e:
            err_content = e.content if hasattr(e, 'content') and e.content else str(e)
            logger.error(f"Malformed SOQL ({context_log_message}): {soql_query} - Error: {err_content}")
            raise ValueError(f"SOQL query error for {context_log_message}. Check object/field names and permissions.")
        except Exception as e: # Includes errors from retry mechanism
            logger.error(f"Error fetching records ({context_log_message}): {e}", exc_info=True)
            raise RuntimeError(f"Failed to fetch records for {context_log_message} after potential retries.")

    def get_directly_related_record_ids(
        self, parent_record_id: str, parent_object_api_name: str,
        child_object_api_name: str, lookup_field_on_child_to_parent: str
    ) -> List[str]:
        if not (isinstance(parent_record_id, str) and (len(parent_record_id) == 15 or len(parent_record_id) == 18)):
            raise ValueError(f"Invalid parent_record_id format: {parent_record_id}")
        if not child_object_api_name or not lookup_field_on_child_to_parent:
            raise ValueError("child_object_api_name and lookup_field_on_child_to_parent are required.")

        soql_query = (
            f"SELECT Id FROM {child_object_api_name} "
            f"WHERE {lookup_field_on_child_to_parent} = '{parent_record_id}'"
        )
        context = f"direct relation {child_object_api_name} for {parent_object_api_name} ID {parent_record_id}"
        return self._fetch_related_ids_generic(soql_query, context)

    def get_target_ids_via_junction(
        self, parent_record_id: str, parent_object_api_name: str,
        junction_object_api_name: str,
        junction_field_to_parent: str, junction_field_to_target: str
    ) -> List[str]:
        if not (isinstance(parent_record_id, str) and (len(parent_record_id) == 15 or len(parent_record_id) == 18)):
            raise ValueError(f"Invalid parent_record_id format: {parent_record_id}")
        if not all([junction_object_api_name, junction_field_to_parent, junction_field_to_target]):
            raise ValueError("Junction object details (name, field to parent, field to target) are required.")

        soql_query = (
            f"SELECT {junction_field_to_target} FROM {junction_object_api_name} "
            f"WHERE {junction_field_to_parent} = '{parent_record_id}' AND {junction_field_to_target} != NULL"
        )
        context = f"target IDs via junction {junction_object_api_name} for {parent_object_api_name} ID {parent_record_id}"
        logger.info(f"Executing SOQL: {soql_query} ({context})")
        
        def do_sf_query_all_junction(q: str):
            if not self.sf: raise RuntimeError("SF client lost before query_all (junction)")
            return self.sf.query_all(q) # type: ignore

        try:
            result = self._call_sf_api_with_retry(do_sf_query_all_junction, soql_query)
            # Important: The target field is junction_field_to_target, not always 'Id'
            record_ids = [record[junction_field_to_target] for record in result['records'] if record.get(junction_field_to_target) is not None]
            logger.info(f"Found {len(record_ids)} records for: {context}.")
            return record_ids
        except SalesforceMalformedRequest as e:
            err_content = e.content if hasattr(e, 'content') and e.content else str(e)
            logger.error(f"Malformed SOQL ({context}): {soql_query} - Error: {err_content}")
            raise ValueError(f"SOQL query error for {context}. Check object/field names and permissions.")
        except KeyError as e_key: 
            logger.error(f"Field '{junction_field_to_target}' not in query result from {junction_object_api_name}. Query: {soql_query}. Error: {e_key}")
            raise ValueError(f"Configuration error: Field '{junction_field_to_target}' missing in junction query result for {context}.")
        except Exception as e: # Includes errors from retry mechanism
            logger.error(f"Error fetching records ({context}): {e}", exc_info=True)
            raise RuntimeError(f"Failed to fetch records for {context} after potential retries.")

    def update_record_analysis_report(self, record_id: str, sobject_api_name: str, report_content: str) -> bool:
        """DEPRECATED. Use upsert_verification_summary."""
        self._ensure_connected() # Keep for safety if ever called, though deprecated
        logger.warning(f"DEPRECATED: update_record_analysis_report called for {sobject_api_name} ID {record_id}.")
        # Original logic can remain or be fully removed if desired.
        # For now, just ensuring it won't break if accidentally called but won't do anything useful.
        return False

# --- FastAPI Dependency (Typically lives in the FastAPI app's modules, e.g., dependencies.py or main.py) ---
# Kept here for completeness based on previous context, but ideally refactored.
_sf_service_instance: Optional[SalesforceService] = None
_sf_service_lock = asyncio.Lock() # type: ignore

async def get_sf_service_dependency() -> SalesforceService:
    global _sf_service_instance
    if _sf_service_instance is None:
        async with _sf_service_lock:
            if _sf_service_instance is None:
                try:
                    logger.info("Initializing SalesforceService singleton for dependency...")
                    _sf_service_instance = SalesforceService()
                    # _ensure_connected is called implicitly by _connect in __init__
                    # and explicitly by _call_sf_api_with_retry.
                    # A check after init can be good.
                    if not _sf_service_instance.sf or not _sf_service_instance.instance_url:
                         logger.error("SalesforceService initialized but sf client or instance_url is missing. Connection likely failed.")
                         _sf_service_instance = None # Reset to allow re-initialization attempt
                         raise HTTPException(status_code=503, detail="Salesforce Service unavailable: Connection failed during init.")
                    logger.info(f"SalesforceService singleton created. Instance URL: {_sf_service_instance.instance_url}") 
                except (ValueError, SalesforceAuthenticationFailed) as e:
                    logger.error(f"Failed to initialize SalesforceService for dependency: {e}", exc_info=True)
                    _sf_service_instance = None # Reset on failure
                    raise HTTPException(status_code=503, detail=f"Salesforce Service unavailable: Init failed: {str(e)}")
                except Exception as e: 
                    logger.error(f"Unexpected error initializing SalesforceService for dependency: {e}", exc_info=True)
                    _sf_service_instance = None # Reset on failure
                    raise HTTPException(status_code=500, detail=f"Unexpected error initializing Salesforce Service: {str(e)}")
    
    # For every request using the dependency, ensure the connection is still good.
    # The _call_sf_api_with_retry method now handles _ensure_connected internally before each call.
    # However, a quick check here can't hurt, especially if some methods don't use the retry wrapper (though they should).
    try:
        # This explicit call to _ensure_connected here might be redundant if all service methods
        # correctly use _call_sf_api_with_retry, which itself calls _ensure_connected.
        # If a method is called that doesn't go through the retry wrapper, this is a fallback.
        if _sf_service_instance: # Check if it's not None from a previous init failure
            _sf_service_instance._ensure_connected()
        else: # Should have been caught by the init block
             raise HTTPException(status_code=503, detail="Salesforce Service unavailable: instance is None.")

    except (RuntimeError, ValueError, SalesforceAuthenticationFailed) as e:
        logger.error(f"SalesforceService connection check/reconnect failed for dependency: {e}", exc_info=True)
        # Consider resetting _sf_service_instance to None here to allow re-init on next request
        # _sf_service_instance = None 
        raise HTTPException(status_code=503, detail=f"Salesforce Service connection issue: {str(e)}")

    return _sf_service_instance