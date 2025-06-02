import os
import logging
from simple_salesforce import Salesforce, SalesforceAuthenticationFailed, SalesforceMalformedRequest, SalesforceResourceNotFound
import requests
from dotenv import load_dotenv # Legacy service will load its own .env
from typing import Tuple, List, Dict, Any, Optional

# Load .env file for this legacy service specifically
# This assumes a .env file is present in the project root or where this script is run from.
# If your main app already loads .env, this might be redundant or could cause issues
# if different .env files are intended. For a true legacy encapsulation, it might manage its own.
# For now, let's assume it loads the same project .env.
load_dotenv()

# Configure logging specifically for this legacy service
# This ensures it doesn't interfere with or rely on global logging config from the main app,
# though in practice, if the root logger is already configured, this might not reconfigure it.
legacy_logger = logging.getLogger(__name__ + ".legacy") # Differentiate logger name
if not legacy_logger.hasHandlers(): # Configure only if not already configured
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    # Ensure log_level_str is a valid level name
    numeric_level = getattr(logging, log_level_str, None)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO # Default to INFO if invalid
    
    legacy_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    legacy_handler = logging.StreamHandler()
    legacy_handler.setFormatter(legacy_formatter)
    legacy_logger.addHandler(legacy_handler)
    legacy_logger.setLevel(numeric_level)
    legacy_logger.propagate = False # Prevent messages from also going to the root logger if it's configured

# Define SObject API names as constants for clarity within this legacy service
LEGACY_APPLICATION_OBJECT_API_NAME = "hed__Application__c"
LEGACY_EDUCATION_HISTORY_OBJECT_API_NAME = "hed__Education_History__c"
LEGACY_TEST_SCORE_OBJECT_API_NAME = "hed__Test__c"
# Add other SObject API names if this legacy service used them


class LegacySalesforceService:
    def __init__(self):
        self.auth_mode = os.getenv("SALESFORCE_AUTH_MODE", "password").lower()
        self.sf: Optional[Salesforce] = None
        self.instance_url: Optional[str] = None # This will be just the hostname, e.g., "yourdomain.my.salesforce.com"
        self.base_url: Optional[str] = None # This will be the full base URL, e.g., "https://yourdomain.my.salesforce.com"

        legacy_logger.info(f"Initializing LegacySalesforceService with auth_mode: {self.auth_mode}")

        try:
            if self.auth_mode == "client_credentials":
                legacy_logger.info("Attempting Salesforce connection using Client Credentials Flow (Legacy).")
                client_id = os.getenv("SALESFORCE_CLIENT_ID")
                client_secret = os.getenv("SALESFORCE_CLIENT_SECRET")
                token_url = os.getenv("SALESFORCE_TOKEN_URL")

                if not all([client_id, client_secret, token_url]):
                    msg = "LEGACY_SF_SERVICE: SALESFORCE_CLIENT_ID, SALESFORCE_CLIENT_SECRET, and SALESFORCE_TOKEN_URL must be set for client_credentials flow."
                    legacy_logger.error(msg)
                    raise ValueError(msg)

                payload = {
                    'grant_type': 'client_credentials',
                    'client_id': client_id,
                    'client_secret': client_secret
                }
                headers = {'Content-Type': 'application/x-www-form-urlencoded'}
                
                response = requests.post(token_url, headers=headers, data=payload, timeout=30)
                response.raise_for_status() 
                
                token_data = response.json()
                access_token = token_data.get("access_token")
                instance_url_from_token = token_data.get("instance_url") # This is the full base URL

                if not access_token or not instance_url_from_token:
                    msg = "LEGACY_SF_SERVICE: Failed to retrieve access_token or instance_url from client credentials response."
                    legacy_logger.error(f"{msg} Response: {token_data}")
                    raise SalesforceAuthenticationFailed(msg)

                self.base_url = instance_url_from_token.rstrip('/')
                self.sf = Salesforce(instance_url=self.base_url, session_id=access_token)
                self.instance_url = self.base_url.replace("https://", "").split('/')[0] # Extract hostname
                legacy_logger.info(f"LEGACY_SF_SERVICE: Successfully connected via Client Credentials (Instance Host: {self.instance_url})")

            elif self.auth_mode == "password":
                legacy_logger.info("Attempting Salesforce connection using Username-Password Flow (Legacy).")
                username = os.getenv("SALESFORCE_USERNAME")
                password = os.getenv("SALESFORCE_PASSWORD")
                security_token = os.getenv("SALESFORCE_SECURITY_TOKEN")
                domain = os.getenv("SALESFORCE_DOMAIN") # e.g., "test" for sandbox, "login" for prod
                instance_url_env = os.getenv("SALESFORCE_INSTANCE_URL") # Full URL like https://yourdomain.my.salesforce.com

                if not all([username, password, security_token, (domain or instance_url_env)]):
                    msg = "LEGACY_SF_SERVICE: Salesforce credentials (USERNAME, PASSWORD, SECURITY_TOKEN, and DOMAIN/INSTANCE_URL) missing for password flow."
                    legacy_logger.error(msg)
                    raise ValueError(msg)
                
                if instance_url_env:
                    self.sf = Salesforce(
                        instance_url=instance_url_env.rstrip('/'),
                        username=username,
                        password=password,
                        security_token=security_token,
                    )
                else: # Use domain
                    self.sf = Salesforce(
                        username=username,
                        password=password,
                        security_token=security_token,
                        domain=domain 
                    )
                
                # After connection, simple-salesforce sets sf.base_url
                if hasattr(self.sf, 'base_url') and self.sf.base_url:
                    self.base_url = self.sf.base_url.rstrip('/')
                    self.instance_url = self.base_url.replace("https://", "").split('/')[0]
                elif instance_url_env: # Fallback if base_url not set but instance_url_env was
                    self.base_url = instance_url_env.rstrip('/')
                    self.instance_url = self.base_url.replace("https://", "").split('/')[0]
                elif domain : # Fallback for password flow with domain if base_url isn't set
                    # This is a guess, simple-salesforce should ideally provide base_url
                    self.base_url = f"https://{domain}.my.salesforce.com" 
                    self.instance_url = f"{domain}.my.salesforce.com"
                else:
                    legacy_logger.critical("LEGACY_SF_SERVICE: Could not determine Salesforce base_url or instance_url hostname.")
                    raise ValueError("Could not determine Salesforce base_url or instance_url hostname.")
                legacy_logger.info(f"LEGACY_SF_SERVICE: Successfully connected via Password Flow (Instance Host: {self.instance_url}, Base URL: {self.base_url})")

            else:
                msg = f"LEGACY_SF_SERVICE: Unsupported SALESFORCE_AUTH_MODE: '{self.auth_mode}'. Must be 'client_credentials' or 'password'."
                legacy_logger.error(msg)
                raise ValueError(msg)
            
            # Internal maps as per the old service structure
            self.apex_endpoint_path_map = {
                LEGACY_EDUCATION_HISTORY_OBJECT_API_NAME: "documentVerification/education",
                LEGACY_APPLICATION_OBJECT_API_NAME: "documentVerification/application",
                LEGACY_TEST_SCORE_OBJECT_API_NAME: "documentVerification/testscore",
                # Add ISB_Employment_Log__c if this legacy service was supposed to handle it
                "ISB_Employment_Log__c": "documentVerification/employment", # Assuming it was handled
            }
            self.analysis_report_field_map = {
                LEGACY_EDUCATION_HISTORY_OBJECT_API_NAME: "Verification_Analysis_Report__c",
                LEGACY_APPLICATION_OBJECT_API_NAME: "Background_Verification_Details__c", 
                LEGACY_TEST_SCORE_OBJECT_API_NAME: "Verification_Analysis_Report__c", 
                "ISB_Employment_Log__c": "Verification_Analysis_Report__c", # Assuming
            }

        except requests.exceptions.HTTPError as e:
            legacy_logger.error(f"LEGACY_SF_SERVICE: HTTP error during client credentials token request: {e.response.text if e.response else str(e)}", exc_info=True)
            raise SalesforceAuthenticationFailed(f"Client credentials token request failed: {e.response.text if e.response else str(e)}")
        except SalesforceAuthenticationFailed as e:
            legacy_logger.error(f"LEGACY_SF_SERVICE: Salesforce authentication failed: {e}")
            raise ValueError(f"Salesforce authentication failed: {e}") # Keep as ValueError for consistency with old code
        except Exception as e:
            legacy_logger.error(f"LEGACY_SF_SERVICE: Error connecting to Salesforce: {e}", exc_info=True)
            raise ValueError(f"Error connecting to Salesforce: {e}") # Keep as ValueError

    def _ensure_connected(self):
        """Ensures there's an active Salesforce session."""
        if not self.sf or not self.base_url: # Check base_url as it's used for constructing URLs
            legacy_logger.error("LEGACY_SF_SERVICE: Salesforce service not properly initialized or connection lost.")
            # Unlike the modern service, this old version doesn't have a robust _connect() to recall here.
            # It relies on successful __init__.
            raise RuntimeError("LegacySalesforceService not properly initialized. Connection might have failed during __init__.")


    def _get_file_extension_from_metadata(self, version_metadata: dict) -> str:
        """
        Extracts file extension from ContentVersion metadata.
        Input: version_metadata (dict): Metadata of a ContentVersion record.
        Output: file_extension (str): The determined file extension (lowercase).
        """
        title = version_metadata.get('Title', "")
        file_type_sf = version_metadata.get('FileType')
        path_on_client = version_metadata.get('PathOnClient')
        sf_file_extension_field = version_metadata.get('FileExtension')

        if path_on_client and '.' in path_on_client:
            ext = path_on_client.split('.')[-1].lower()
            if ext: return ext
        
        if sf_file_extension_field and isinstance(sf_file_extension_field, str) and '.' not in sf_file_extension_field:
            ext = sf_file_extension_field.lower()
            if ext: return ext

        if file_type_sf:
            ext_map = {
                'PDF': 'pdf', 'JPEG': 'jpg', 'JPG': 'jpg', 'PNG': 'png',
                'WORD': 'docx', 'WORD_X': 'docx', 'DOC': 'doc', 
                'EXCEL': 'xlsx', 'EXCEL_X': 'xlsx', 'XLS': 'xls',
                'TEXT': 'txt', 'WEBP': 'webp', 'TIFF': 'tiff', 'TIF': 'tif',
                'POWER_POINT_X': 'pptx', 'POWER_POINT': 'ppt',
            }
            ext = ext_map.get(file_type_sf.upper())
            if ext: return ext

        if title and '.' in title:
            ext = title.split('.')[-1].lower()
            if ext: return ext
        
        legacy_logger.warning(f"LEGACY_SF_SERVICE: Could not determine file extension for CV ID: {version_metadata.get('Id')}. Defaulting to 'bin'.")
        return "bin"

    def download_file_in_memory(self, content_version_id: str) -> Tuple[bytes, str, str]:
        """
        Downloads a Salesforce ContentVersion file into memory.
        Input: content_version_id (str): The ID of the ContentVersion.
        Output: Tuple containing (file_bytes, file_extension, original_filename).
        """
        self._ensure_connected()
        try:
            legacy_logger.info(f"LEGACY_SF_SERVICE: Fetching ContentVersion metadata for ID: {content_version_id}")
            query = (
                f"SELECT Id, Title, FileType, PathOnClient, VersionData, FileExtension "
                f"FROM ContentVersion WHERE Id = '{content_version_id}'"
            )
            version_metadata_results = self.sf.query(query)

            if not version_metadata_results or version_metadata_results['totalSize'] == 0:
                raise FileNotFoundError(f"LEGACY_SF_SERVICE: ContentVersion with ID '{content_version_id}' not found.")

            version_metadata = version_metadata_results['records'][0]
            version_data_url_path = version_metadata.get('VersionData') # This is a relative path
            original_filename = version_metadata.get('PathOnClient') or version_metadata.get('Title', content_version_id)

            if not version_data_url_path:
                raise RuntimeError(f"LEGACY_SF_SERVICE: VersionData URL not found for CV ID '{content_version_id}'.")

            file_extension = self._get_file_extension_from_metadata(version_metadata)
            
            # self.base_url is the full instance URL, e.g., https://yourdomain.my.salesforce.com
            # version_data_url_path is relative, e.g., /services/data/vXX.X/...
            # simple-salesforce session.get handles joining these correctly if base_url is set on sf.session
            if not self.base_url.endswith('/') and not version_data_url_path.startswith('/'):
                full_download_url = self.base_url + '/' + version_data_url_path
            elif self.base_url.endswith('/') and version_data_url_path.startswith('/'):
                 full_download_url = self.base_url + version_data_url_path[1:]
            else:
                full_download_url = self.base_url + version_data_url_path


            legacy_logger.info(f"LEGACY_SF_SERVICE: Downloading file content from: {full_download_url}")
            response = self.sf.session.get(full_download_url, headers=self.sf.headers, stream=True, timeout=60)
            response.raise_for_status()

            file_bytes = response.content
            legacy_logger.info(f"LEGACY_SF_SERVICE: File '{original_filename}' (ID: {content_version_id}, Ext: {file_extension}) downloaded ({len(file_bytes)} bytes).")
            return file_bytes, file_extension, original_filename

        except requests.exceptions.RequestException as e:
            legacy_logger.error(f"LEGACY_SF_SERVICE: HTTP error downloading file {content_version_id}: {e}", exc_info=True)
            raise RuntimeError(f"Network error downloading file from Salesforce: {e}")
        except FileNotFoundError:
            raise
        except SalesforceResourceNotFound:
             raise FileNotFoundError(f"LEGACY_SF_SERVICE: ContentVersion ID '{content_version_id}' not found (SalesforceResourceNotFound).")
        except Exception as e:
            legacy_logger.error(f"LEGACY_SF_SERVICE: Error downloading file {content_version_id}: {e}", exc_info=True)
            raise RuntimeError(f"Unexpected error downloading file: {e}")

    # Add other methods from your old salesforce_service.py if they were used by legacy_endpoints
    # For example, if get_record_detail_from_apex or update_record_analysis_report were used by the old /extract.
    # For now, I'm assuming the legacy /extract only needed download_file_in_memory.
    # If it needs get_record_detail_from_apex, it would look like this:

    def get_record_detail_from_apex(self, record_id: str, record_type_key: str) -> Optional[Dict[str, Any]]:
        """
        Fetches record details from a configured Apex REST endpoint (Legacy version).
        record_type_key: The key used in self.apex_endpoint_path_map.
        """
        self._ensure_connected()
        if not (isinstance(record_id, str) and (len(record_id) == 15 or len(record_id) == 18)):
            legacy_logger.error(f"LEGACY_SF_SERVICE: Invalid Salesforce ID format for record_id: {record_id}")
            return None

        endpoint_path_segment = self.apex_endpoint_path_map.get(record_type_key)
        if not endpoint_path_segment:
            legacy_logger.error(f"LEGACY_SF_SERVICE: No Apex endpoint path configured for record_type_key: {record_type_key}")
            return None

        apex_rest_path = f"/services/apexrest/{endpoint_path_segment.strip('/')}/{record_id}"
        full_url = f"{self.base_url.strip('/')}{apex_rest_path}" # Use self.base_url

        legacy_logger.info(f"LEGACY_SF_SERVICE: Calling Apex REST for {record_type_key} ID {record_id}: POST {full_url}")
        try:
            response = self.sf.session.post(full_url, headers=self.sf.headers, json={}, timeout=60) 

            if 400 <= response.status_code < 600 :
                error_content = response.text
                legacy_logger.error(f"LEGACY_SF_SERVICE: Apex REST call failed for {record_type_key} ID {record_id}. Status: {response.status_code}. Response: {error_content[:500]}")
                return None

            if response.content:
                try:
                    details = response.json()
                    legacy_logger.info(f"LEGACY_SF_SERVICE: Successfully received details from Apex for {record_type_key} ID {record_id}.")
                    return details
                except requests.exceptions.JSONDecodeError:
                    legacy_logger.error(f"LEGACY_SF_SERVICE: Failed to decode JSON from Apex for {record_type_key} ID {record_id}. Response: {response.text[:200]}")
                    return None
            else:
                legacy_logger.info(f"LEGACY_SF_SERVICE: Apex endpoint for {record_type_key} ID {record_id} returned empty response with status {response.status_code}.")
                return {} if 200 <= response.status_code < 300 else None

        except requests.exceptions.RequestException as e:
            legacy_logger.error(f"LEGACY_SF_SERVICE: Network error calling Apex for {record_type_key} ID {record_id}: {e}", exc_info=True)
            return None
        except Exception as e:
            legacy_logger.error(f"LEGACY_SF_SERVICE: Unexpected error calling Apex for {record_type_key} ID {record_id}: {e}", exc_info=True)
            return None