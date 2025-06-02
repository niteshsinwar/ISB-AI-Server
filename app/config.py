import os
from dotenv import load_dotenv
from typing import List, Dict, Deque # For rate limit type hints
from collections import deque, defaultdict # For rate limit data structures
from datetime import datetime, timezone # For rate limit timestamps

# Load environment variables from .env file
load_dotenv()

# --- Application Metadata ---
APP_TITLE: str = "Salesforce Document Text Extraction and Application Analysis API"
APP_DESCRIPTION: str = "API for extracting information from document text and analyzing Salesforce application records."
APP_VERSION: str = "9.2.0" # Versioning for Application_Verification_Summary__c update

# --- Logging Configuration ---
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# --- Salesforce Configuration ---
SALESFORCE_USERNAME: str | None = os.getenv("SALESFORCE_USERNAME")
SALESFORCE_PASSWORD: str | None = os.getenv("SALESFORCE_PASSWORD")
SALESFORCE_SECURITY_TOKEN: str | None = os.getenv("SALESFORCE_SECURITY_TOKEN")
SALESFORCE_DOMAIN: str | None = os.getenv("SALESFORCE_DOMAIN", "test")
SALESFORCE_INSTANCE_URL: str | None = os.getenv("SALESFORCE_INSTANCE_URL")
SALESFORCE_AUTH_MODE: str = os.getenv("SALESFORCE_AUTH_MODE", "password").lower()

SALESFORCE_CLIENT_ID: str | None = os.getenv("SALESFORCE_CLIENT_ID")
SALESFORCE_CLIENT_SECRET: str | None = os.getenv("SALESFORCE_CLIENT_SECRET")
SALESFORCE_TOKEN_URL: str | None = os.getenv("SALESFORCE_TOKEN_URL")

# --- Salesforce Object and Field API Names ---

# Application Verification Summary Object (NEW CENTRALIZED REPORTING OBJECT)
APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME: str = "Application_Verification_Summary__c"
AVS_APPLICATION_LOOKUP_FIELD: str = "Application__c" # Lookup to hed__Application__c
AVS_CONTACT_LOOKUP_FIELD: str = "Contact__c" # Lookup to Contact
AVS_EDUCATION_HISTORY_LOOKUP_FIELD: str = "Education_History__c" # Lookup to hed__Education_History__c
AVS_TEST_LOOKUP_FIELD: str = "Test__c" # Lookup to hed__Test__c
AVS_AFFILIATION_LOOKUP_FIELD: str = "Affiliation__c" # Lookup to hed__Affiliation__c
AVS_REPORT_FIELD: str = "Verification_Analysis_Report__c" # Long Text Area on AVS object
AVS_NAME_FIELD: str = "Name" # Text field on AVS object (e.g., "Personal Detail Analysis")

# Main Application Object
APPLICATION_OBJECT_API_NAME: str = "hed__Application__c"
# Field on hed__Application__c that looks up to Contact (Applicant)
# Ensure your Apex 'documentVerification/application' returns this ID in recordData
# Example: "Applicant__c", "Contact__c", "Primary_Contact__c" - VERIFY THIS
APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP: str = os.getenv("APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP", "Applicant__c")


# Education History Object
EDUCATION_HISTORY_OBJECT_API_NAME: str = "hed__Education_History__c"
EDUCATION_HISTORY_JUNCTION_OBJECT: str = "ISB_Education_Log__c"
EDUCATION_HISTORY_JUNCTION_FIELD_TO_PARENT: str = "Application__c"
EDUCATION_HISTORY_JUNCTION_FIELD_TO_TARGET: str = "Education_History__c"

# Employment Log Object (Triggers employment verification)
ISB_EMPLOYMENT_LOG_OBJECT_API_NAME: str = "ISB_Employment_Log__c"
# Note: The employment processor will get Affiliation__c ID from the Apex call triggered by ISB_Employment_Log__c ID.
# The report is stored on Application_Verification_Summary__c linked to this Affiliation__c.

# Test Score Object
TEST_SCORE_OBJECT_API_NAME: str = "hed__Test__c"
TEST_SCORE_LOOKUP_TO_PARENT_APP: str = "Application__c"


# Apex REST Endpoint paths (segments after /services/apexrest/)
# Keys here MUST match the 'sobject_api_name_key' used in processors when calling sf_service.get_record_detail_from_apex
APEX_ENDPOINT_PATHS: Dict[str, str] = {
    EDUCATION_HISTORY_OBJECT_API_NAME: "documentVerification/education",        # Key is hed__Education_History__c
    APPLICATION_OBJECT_API_NAME: "documentVerification/application",            # Key is hed__Application__c
    TEST_SCORE_OBJECT_API_NAME: "documentVerification/testscore",               # Key is hed__Test__c
    ISB_EMPLOYMENT_LOG_OBJECT_API_NAME: "documentVerification/employment",      # Key is ISB_Employment_Log__c
}

# --- Google Gemini Configuration ---
GOOGLE_API_KEY: str | None = os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL_NAME: str = os.getenv("GEMINI_MODEL_NAME", "gemini-1.5-flash")
TEXT_EXTRACTION_OCR_PROMPT: str = (
    "Extract all textual content from this document page accurately. "
    "Preserve line breaks, formatting, and structure where possible. "
    "If no text is present, respond with an empty string or 'NO_TEXT_FOUND'."
)
MAX_CONCURRENT_OCR_PAGES: int = int(os.getenv("MAX_CONCURRENT_OCR_PAGES", "10"))


# --- API Rate Limiting and Processing Configuration ---
MAX_GLOBAL_REQUESTS_PER_WINDOW: int = int(os.getenv("MAX_GLOBAL_REQUESTS_PER_WINDOW", "10"))
GLOBAL_RATE_LIMIT_WINDOW_SECONDS: int = int(os.getenv("GLOBAL_RATE_LIMIT_WINDOW_SECONDS", "60"))
MAX_CLIENT_REQUESTS_PER_WINDOW: int = int(os.getenv("MAX_CLIENT_REQUESTS_PER_WINDOW", "5"))
CLIENT_RATE_LIMIT_WINDOW_SECONDS: int = int(os.getenv("CLIENT_RATE_LIMIT_WINDOW_SECONDS", "60"))
MIN_SECONDS_BETWEEN_SAME_APP_REQUESTS: int = int(os.getenv("MIN_SECONDS_BETWEEN_SAME_APP_REQUESTS", "5"))
SUSPICIOUS_THRESHOLD_REQUESTS: int = int(os.getenv("SUSPICIOUS_THRESHOLD_REQUESTS", "20"))
SUSPICIOUS_WINDOW_SECONDS: int = int(os.getenv("SUSPICIOUS_WINDOW_SECONDS", "60"))
SUSPICIOUS_BLOCK_DURATION_SECONDS: int = int(os.getenv("SUSPICIOUS_BLOCK_DURATION_SECONDS", "300"))
MAX_CONCURRENT_PROCESSING_SLOTS: int = int(os.getenv("MAX_CONCURRENT_PROCESSING_SLOTS", "4"))
RECENTLY_PROCESSED_TTL_SECONDS: int = int(os.getenv("RECENTLY_PROCESSED_TTL_SECONDS", "300"))
ACTIVE_PROCESSING_TIMEOUT_SECONDS: int = int(os.getenv("ACTIVE_PROCESSING_TIMEOUT_SECONDS", "900")) # 15 minutes


# --- Endpoint Configuration ---
RELATED_RECORD_PROCESSING_CONFIG: List[Dict[str, any]] = [
    {
        "target_record_type": EDUCATION_HISTORY_OBJECT_API_NAME,
        "retrieval_method": "via_junction",
        "junction_object": EDUCATION_HISTORY_JUNCTION_OBJECT,
        "junction_field_to_parent": EDUCATION_HISTORY_JUNCTION_FIELD_TO_PARENT,
        "junction_field_to_target": EDUCATION_HISTORY_JUNCTION_FIELD_TO_TARGET,
        "processor_module": "app.processors.education_processor",
        "processor_function_name": "process_single_education_history_detail"
    },
    {
        "target_record_type": TEST_SCORE_OBJECT_API_NAME,
        "retrieval_method": "direct",
        "lookup_on_child_to_parent": TEST_SCORE_LOOKUP_TO_PARENT_APP,
        "processor_module": "app.processors.test_score_processor",
        "processor_function_name": "process_single_test_score_detail"
    },
    {
        "target_record_type": ISB_EMPLOYMENT_LOG_OBJECT_API_NAME, # Iterate over these log records
        "retrieval_method": "direct",
        # Field on ISB_Employment_Log__c that points to hed__Application__c. VERIFY THIS FIELD NAME.
        "lookup_on_child_to_parent": "Application__c",
        "processor_module": "app.processors.employment_processor",
        "processor_function_name": "process_single_employment_detail"
    }
]

# Maximum length for the analysis report stored in Salesforce
MAX_SALESFORCE_REPORT_LENGTH: int = int(os.getenv("MAX_SALESFORCE_REPORT_LENGTH", "32000"))