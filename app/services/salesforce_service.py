import os
import logging
import asyncio
from simple_salesforce import (
    Salesforce,
    SalesforceAuthenticationFailed,
    SalesforceMalformedRequest,
    SalesforceResourceNotFound,
    SalesforceExpiredSession
)
import requests
from typing import Tuple, List, Dict, Any, Optional, Callable

from fastapi import HTTPException, Depends

# Import configurations from app.config
# Note: The username/password related configs are no longer used but are left here
# for context. They can be safely removed from your config.py and .env files.
from app.config import (
    SALESFORCE_INSTANCE_URL, SALESFORCE_CLIENT_ID, SALESFORCE_CLIENT_SECRET,
    SALESFORCE_TOKEN_URL, APEX_ENDPOINT_PATHS,
    APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME,
    AVS_APPLICATION_LOOKUP_FIELD, AVS_CONTACT_LOOKUP_FIELD,
    AVS_EDUCATION_HISTORY_LOOKUP_FIELD, AVS_TEST_LOOKUP_FIELD,
    AVS_AFFILIATION_LOOKUP_FIELD, AVS_REPORT_FIELD, AVS_NAME_FIELD,
    AVS_OVERALL_FEEDBACK_FIELD, AVS_CONFIDENCE_FIELD, AVS_TASK_DCI_LOOKUP_FIELD
)

logger = logging.getLogger(__name__)

class SalesforceService:
    """
    Service for interacting with the Salesforce API using the OAuth 2.0 Client Credentials Flow.
    """
    def __init__(self):
        """
        Initializes the SalesforceService, preparing it for connection.
        """
        self.sf: Optional[Salesforce] = None
        self.instance_url: Optional[str] = None
        self.apex_endpoint_path_map: Dict[str, str] = APEX_ENDPOINT_PATHS
        # The connection is established on the first API call via _ensure_connected()
        # or can be triggered by the FastAPI dependency.
        self._connect()

    def _connect(self):
        """
        Establishes a Salesforce connection using the OAuth 2.0 Client Credentials Flow.
        This is the sole authentication method for the service.
        """
        logger.info("Attempting Salesforce connection using Client Credentials Flow.")
        
        # Critical configuration check for the required credentials
        if not all([SALESFORCE_CLIENT_ID, SALESFORCE_CLIENT_SECRET, SALESFORCE_TOKEN_URL]):
            msg = "SALESFORCE_CLIENT_ID, SALESFORCE_CLIENT_SECRET, and SALESFORCE_TOKEN_URL must be set in the environment."
            logger.error(msg)
            raise ValueError(msg)

        payload = {
            'grant_type': 'client_credentials',
            'client_id': SALESFORCE_CLIENT_ID,
            'client_secret': SALESFORCE_CLIENT_SECRET
        }
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        
        try:
            # Request the access token from Salesforce
            response = requests.post(SALESFORCE_TOKEN_URL, headers=headers, data=payload, timeout=30)
            response.raise_for_status()  # Raises an HTTPError for bad responses (4xx or 5xx)
            
            token_data = response.json()
            access_token = token_data.get("access_token")
            instance_url_from_token = token_data.get("instance_url")

            if not access_token or not instance_url_from_token:
                msg = "Failed to retrieve access_token or instance_url from client credentials response."
                logger.error(f"{msg} Response: {token_data}")
                raise SalesforceAuthenticationFailed(msg, response_content=token_data)

            # Initialize the simple-salesforce client with the obtained session token
            self.sf = Salesforce(instance_url=instance_url_from_token.rstrip('/'), session_id=access_token)
            # Extract the hostname for logging and internal use
            self.instance_url = instance_url_from_token.replace("https://", "").split('/')[0]
            logger.info(f"Successfully connected to Salesforce via Client Credentials (Host: {self.instance_url})")

        except requests.exceptions.HTTPError as e:
            error_text = e.response.text if e.response is not None else str(e)
            logger.error(f"HTTP error during client credentials token request: {error_text}", exc_info=True)
            raise SalesforceAuthenticationFailed(f"Client credentials token request failed: {error_text}", response_content=e.response.content if e.response is not None else None)
        except Exception as e:
            logger.error(f"An unexpected error occurred during Salesforce connection: {e}", exc_info=True)
            raise ValueError(f"An unexpected error occurred during Salesforce connection: {e}")

    def _ensure_connected(self):
        """Ensures that self.sf is initialized, attempting to connect if not."""
        if not self.sf or not self.instance_url:
            logger.warning("Salesforce connection not established or invalid. Attempting to connect/reconnect.")
            self._connect() 
            if not self.sf or not self.instance_url:
                 logger.error("Salesforce service reconnection failed or instance_url still not set.")
                 raise RuntimeError("Salesforce service not properly initialized or reconnection failed.")

    def _call_sf_api_with_retry(self, api_call_func: Callable, *args, **kwargs) -> Any:
        """
        Wrapper to execute a Salesforce API call with a single retry on session expiration.
        """
        try:
            self._ensure_connected()
            return api_call_func(*args, **kwargs)
        except (SalesforceAuthenticationFailed, SalesforceExpiredSession) as e:
            logger.warning(f"Salesforce operation failed due to session issue ({type(e).__name__}). "
                           "Attempting to reconnect and retry once.")
            try:
                self._connect() # Re-authenticate using the Client Credentials flow
                return api_call_func(*args, **kwargs)
            except Exception as retry_exception:
                logger.error(f"Salesforce operation failed on retry after re-authentication: {retry_exception}", exc_info=True)
                raise retry_exception

    def get_record_detail_from_apex(self, record_id: str, sobject_api_name_key: str) -> Optional[Dict[str, Any]]:
        """
        Calls a custom Apex REST endpoint to get detailed information for a specific record.
        """
        if not (isinstance(record_id, str) and (len(record_id) == 15 or len(record_id) == 18)):
            logger.error(f"Invalid Salesforce ID format for record_id: {record_id}")
            return None

        endpoint_path_segment = self.apex_endpoint_path_map.get(sobject_api_name_key)
        if not endpoint_path_segment:
            logger.error(f"No Apex endpoint path configured for SObject API key: {sobject_api_name_key}")
            return None

        self._ensure_connected() 
        base_instance_url = self.instance_url.strip('/')
        apex_rest_path = f"/services/apexrest/{endpoint_path_segment.strip('/')}/{record_id}"
        full_url = f"https://{base_instance_url}{apex_rest_path}"

        logger.info(f"Calling Apex REST: POST {full_url} for SObject key '{sobject_api_name_key}' with ID {record_id}")

        def do_apex_post():
            if not self.sf or not self.sf.session:
                raise RuntimeError("Salesforce client (self.sf.session) not properly initialized for Apex call.")
            return self.sf.session.post(full_url, headers=self.sf.headers, json={}, timeout=60)

        try:
            response = self._call_sf_api_with_retry(do_apex_post)

            if 400 <= response.status_code < 600:
                error_content = response.text
                if response.status_code in [401, 403]:
                    logger.critical(f"Persistent Authentication Failure ({response.status_code}) for Apex call to {full_url} for ID {record_id} even after retry. Response: {error_content[:500]}")
                else:
                    logger.error(f"Apex REST call failed for {full_url} (SObject key '{sobject_api_name_key}' ID {record_id}). Status: {response.status_code}. Response: {error_content[:500]}")
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
            logger.error(f"Network error calling Apex for SObject key '{sobject_api_name_key}' ID {record_id} to {full_url}: {e}", exc_info=True)
            return None
        except (SalesforceMalformedRequest, SalesforceResourceNotFound) as e_sf:
             logger.error(f"Salesforce API error (not retried) for Apex call to {full_url} (ID {record_id}): {e_sf}", exc_info=True)
             return None
        except Exception as e:
            logger.error(f"Unexpected error calling Apex for SObject key '{sobject_api_name_key}' ID {record_id} to {full_url}: {e}", exc_info=True)
            return None

    def upsert_verification_summary(
        self, application_id: str, report_content: str, name_value: str,
        overall_feedback: Optional[str] = None,
        confidence_range: Optional[int] = None,
        contact_id: Optional[str] = None, education_history_id: Optional[str] = None,
        test_id: Optional[str] = None, affiliation_id: Optional[str] = None
    ) -> Optional[str]:
        """
        Creates or updates an Application_Verification_Summary__c record.
        It finds an existing record based on the Application and a secondary related ID.
        """
        summary_object_name = APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME
        soql_query_parts = [f"SELECT Id FROM {summary_object_name} WHERE {AVS_APPLICATION_LOOKUP_FIELD} = '{application_id}'"]
        
        payload_updates: Dict[str, Any] = {
            AVS_REPORT_FIELD: report_content,
            AVS_NAME_FIELD: name_value,
            AVS_APPLICATION_LOOKUP_FIELD: application_id
        }
        if overall_feedback:
            payload_updates[AVS_OVERALL_FEEDBACK_FIELD] = overall_feedback
        if confidence_range:
            payload_updates[AVS_CONFIDENCE_FIELD] = confidence_range

        secondary_lookup_field: Optional[str] = None
        secondary_lookup_id_value: Optional[str] = None

        # Determine which secondary lookup to use for the query and payload
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
            return None

        if secondary_lookup_field and secondary_lookup_id_value:
            soql_query_parts.append(f" AND {secondary_lookup_field} = '{secondary_lookup_id_value}'")
            payload_updates[secondary_lookup_field] = secondary_lookup_id_value
        
        soql_query = "".join(soql_query_parts) + " LIMIT 1"
        logger.info(f"Querying for existing {summary_object_name}: {soql_query}")

        try:
            self._ensure_connected()
            if not self.sf:
                raise RuntimeError("Salesforce client (self.sf) is not available for upsert.")
                
            sobject_handler = getattr(self.sf, summary_object_name, None)
            if sobject_handler is None:
                logger.error(f"SObject type '{summary_object_name}' not found in Salesforce instance.")
                return None

            def do_sf_query(q: str):
                if not self.sf: raise RuntimeError("SF client lost before query")
                return self.sf.query(q)
            result = self._call_sf_api_with_retry(do_sf_query, soql_query)

            # If a record exists, update it
            if result.get('totalSize', 0) > 0 and len(result.get('records', [])) > 0:
                existing_summary_id = result['records'][0]['Id']
                logger.info(f"Found existing {summary_object_name} ID {existing_summary_id}. Updating.")
                
                update_payload = {
                    AVS_REPORT_FIELD: report_content,
                    AVS_NAME_FIELD: name_value
                }
                if overall_feedback: update_payload[AVS_OVERALL_FEEDBACK_FIELD] = overall_feedback
                if confidence_range: update_payload[AVS_CONFIDENCE_FIELD] = confidence_range

                def do_sf_update(handler: Any, rec_id: str, load: Dict[str, Any]):
                    return handler.update(rec_id, load)
                update_status_code = self._call_sf_api_with_retry(do_sf_update, sobject_handler, existing_summary_id, update_payload)
                
                if update_status_code == 204:
                    logger.info(f"Successfully updated {summary_object_name} ID {existing_summary_id}.")
                    return existing_summary_id
                else:
                    logger.error(f"Failed to update {summary_object_name} ID {existing_summary_id}. Status code: {update_status_code}")
                    return None
            
            # If no record exists, create one
            else:
                logger.info(f"No existing {summary_object_name} found. Creating new record with payload: {payload_updates}")
                def do_sf_create(handler: Any, load: Dict[str, Any]):
                    return handler.create(load)
                create_response = self._call_sf_api_with_retry(do_sf_create, sobject_handler, payload_updates)
                
                if create_response.get('success'):
                    new_id = create_response.get('id')
                    logger.info(f"Successfully created new {summary_object_name} record with ID {new_id}.")
                    return new_id
                else:
                    logger.error(f"Failed to create new {summary_object_name}. Errors: {create_response.get('errors')}")
                    return None

        except (SalesforceMalformedRequest, SalesforceResourceNotFound) as e_sf_specific:
            logger.error(f"Salesforce API error during {summary_object_name} upsert for App {application_id}: {e_sf_specific}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"Unexpected error during {summary_object_name} upsert for App {application_id}: {e}", exc_info=True)
            return None
            
    def link_summary_to_related_items(
        self, summary_id: str, task_id: Optional[str], dci_id: Optional[str], overall_feedback: Optional[str] = None
    ) -> None:
        """
        Links the summary record to related Task and DocumentChecklistItem records.
        - For Task, it appends the feedback to the Description field.
        - For DocumentChecklistItem, it populates a direct lookup field.
        """
        if not summary_id:
            logger.error("Cannot link to related items without a valid summary_id.")
            return

        self._ensure_connected()
        if not self.sf:
            raise RuntimeError("Salesforce client (self.sf) is not available for updating links.")

        # --- Task update is now disabled as per the request ---
        # if task_id and overall_feedback:
        #     logger.info(f"Attempting to link Task ID {task_id} to Summary ID {summary_id} by updating Description with feedback.")
        #     try:
        #         task_handler = getattr(self.sf, 'Task')
        #         
        #         def do_task_update():
        #             # First, get the existing description
        #             existing_task = task_handler.get(task_id)
        #             existing_desc = existing_task.get('Description') or ''
        #             
        #             # Create the feedback text and check if it's already there to prevent duplicates
        #             feedback_text = f"\n\nAI Verification Feedback: {overall_feedback}"
        #             if feedback_text in existing_desc:
        #                 logger.info(f"Feedback for Summary {summary_id} already exists in Task {task_id} description. Skipping update.")
        #                 return 204
        #             
        #             # Append and update
        #             new_desc = existing_desc + feedback_text
        #             return task_handler.update(task_id, {'Description': new_desc})
        #
        #         self._call_sf_api_with_retry(do_task_update)
        #         logger.info(f"Successfully updated Task {task_id} description.")
        #     except Exception as e:
        #         logger.error(f"Failed to update Description on Task {task_id}: {e}", exc_info=True)
        
        # --- Update DocumentChecklistItem with a direct lookup ---
        if dci_id:
            logger.info(f"Attempting to update DocumentChecklistItem ID {dci_id} with lookup to Summary ID {summary_id}.")
            try:
                dci_handler = getattr(self.sf, 'DocumentChecklistItem')
                lookup_payload = {AVS_TASK_DCI_LOOKUP_FIELD: summary_id}
                
                def do_dci_update():
                    return dci_handler.update(dci_id, lookup_payload)

                self._call_sf_api_with_retry(do_dci_update)
                logger.info(f"Successfully updated DocumentChecklistItem {dci_id}.")
            except Exception as e:
                logger.error(f"Failed to update lookup on DocumentChecklistItem {dci_id}: {e}", exc_info=True)
                raise e # Re-raise the exception to be caught by the processor

    # --- Other helper methods remain unchanged ---

    def _fetch_related_ids_generic(self, soql_query: str, context_log_message: str) -> List[str]:
        """Generic helper to run a SOQL query for IDs and handle retries."""
        logger.info(f"Executing SOQL: {soql_query} ({context_log_message})")
        
        def do_sf_query_all(q: str):
            if not self.sf: raise RuntimeError("SF client lost before query_all")
            return self.sf.query_all(q)

        try:
            result = self._call_sf_api_with_retry(do_sf_query_all, soql_query)
            record_ids = [record['Id'] for record in result['records']]
            logger.info(f"Found {len(record_ids)} records for: {context_log_message}.")
            return record_ids
        except SalesforceMalformedRequest as e:
            err_content = e.content if hasattr(e, 'content') and e.content else str(e)
            logger.error(f"Malformed SOQL ({context_log_message}): {soql_query} - Error: {err_content}")
            raise ValueError(f"SOQL query error for {context_log_message}. Check object/field names and permissions.")
        except Exception as e:
            logger.error(f"Error fetching records ({context_log_message}): {e}", exc_info=True)
            raise RuntimeError(f"Failed to fetch records for {context_log_message} after potential retries.")

    def get_directly_related_record_ids(
        self, parent_record_id: str, parent_object_api_name: str,
        child_object_api_name: str, lookup_field_on_child_to_parent: str
    ) -> List[str]:
        """Fetches IDs of child records directly related to a parent via a lookup field."""
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
        """Fetches target record IDs from a parent through a junction object."""
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
            return self.sf.query_all(q)

        try:
            result = self._call_sf_api_with_retry(do_sf_query_all_junction, soql_query)
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
        except Exception as e:
            logger.error(f"Error fetching records ({context}): {e}", exc_info=True)
            raise RuntimeError(f"Failed to fetch records for {context} after potential retries.")

# --- FastAPI Dependency ---

_sf_service_instance: Optional[SalesforceService] = None
_sf_service_lock = asyncio.Lock()

async def get_sf_service_dependency() -> SalesforceService:
    """
    FastAPI dependency that provides a singleton instance of the SalesforceService.
    Initializes the service on first request and handles connection state.
    """
    global _sf_service_instance
    if _sf_service_instance is None:
        async with _sf_service_lock:
            if _sf_service_instance is None:
                try:
                    logger.info("Initializing SalesforceService singleton for dependency...")
                    _sf_service_instance = SalesforceService()
                    if not _sf_service_instance.sf or not _sf_service_instance.instance_url:
                         logger.error("SalesforceService initialized but sf client or instance_url is missing. Connection likely failed.")
                         _sf_service_instance = None
                         raise HTTPException(status_code=503, detail="Salesforce Service unavailable: Connection failed during init.")
                    logger.info(f"SalesforceService singleton created. Instance URL: {_sf_service_instance.instance_url}") 
                except (ValueError, SalesforceAuthenticationFailed) as e:
                    logger.error(f"Failed to initialize SalesforceService for dependency: {e}", exc_info=True)
                    _sf_service_instance = None
                    raise HTTPException(status_code=503, detail=f"Salesforce Service unavailable: Init failed: {str(e)}")
                except Exception as e: 
                    logger.error(f"Unexpected error initializing SalesforceService for dependency: {e}", exc_info=True)
                    _sf_service_instance = None
                    raise HTTPException(status_code=500, detail=f"Unexpected error initializing Salesforce Service: {str(e)}")
    
    try:
        # Ensure the connection is still valid before yielding the instance
        if _sf_service_instance:
            _sf_service_instance._ensure_connected()
        else:
             raise HTTPException(status_code=503, detail="Salesforce Service unavailable: instance is None.")
    except (RuntimeError, ValueError, SalesforceAuthenticationFailed) as e:
        logger.error(f"SalesforceService connection check/reconnect failed for dependency: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail=f"Salesforce Service connection issue: {str(e)}")

    return _sf_service_instance
