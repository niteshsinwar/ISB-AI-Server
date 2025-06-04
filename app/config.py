import os
from dotenv import load_dotenv
from typing import List, Dict, Deque
from collections import deque, defaultdict
from datetime import datetime, timezone

# Load environment variables from .env file
load_dotenv()

# --- Application Metadata ---
APP_TITLE: str = "Salesforce Document Text Extraction and Application Analysis API"
APP_DESCRIPTION: str = "API for extracting information from document text and analyzing Salesforce application records."
APP_VERSION: str = "1.0.0"

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

# Application Verification Summary Object
APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME: str = "Application_Verification_Summary__c"
AVS_APPLICATION_LOOKUP_FIELD: str = "Application__c"
AVS_CONTACT_LOOKUP_FIELD: str = "Contact__c"
AVS_EDUCATION_HISTORY_LOOKUP_FIELD: str = "Education_History__c" # Links to hed__Education_History__c
AVS_TEST_LOOKUP_FIELD: str = "Test__c"
AVS_AFFILIATION_LOOKUP_FIELD: str = "Affiliation__c" # Links to hed__Affiliation__c
AVS_REPORT_FIELD: str = "Verification_Analysis_Report__c"
AVS_NAME_FIELD: str = "Name"

# Main Application Object
APPLICATION_OBJECT_API_NAME: str = "hed__Application__c"
APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP: str = os.getenv("APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP", "Applicant__c")

# --- Education Configuration ---
EDUCATION_DETAIL_OBJECT_API_NAME: str = "hed__Education_History__c" # The actual detail record
EDUCATION_LOG_OBJECT_API_NAME: str = "ISB_Education_Log__c"       # The log record (entry point)
EDUCATION_LOG_FIELD_TO_PARENT_APP: str = "Application__c"        # On ISB_Education_Log__c -> Application
EDUCATION_LOG_FIELD_TO_DETAIL: str = "Education_History__c"      # On ISB_Education_Log__c -> hed__Education_History__c

# --- Employment Configuration ---
EMPLOYMENT_DETAIL_OBJECT_API_NAME: str = "hed__Affiliation__c"       # The actual detail record
EMPLOYMENT_LOG_OBJECT_API_NAME: str = "ISB_Employment_Log__c"    # The log record (entry point)
EMPLOYMENT_LOG_FIELD_TO_PARENT_APP: str = "Application__c"       # On ISB_Employment_Log__c -> Application
EMPLOYMENT_LOG_FIELD_TO_DETAIL: str = "Affiliation__c"           # On ISB_Employment_Log__c -> hed__Affiliation__c

# --- Test Score Configuration (Direct Processing) ---
TEST_SCORE_OBJECT_API_NAME: str = "hed__Test__c"
TEST_SCORE_LOOKUP_TO_PARENT_APP: str = "Application__c"           # On hed__Test__c -> Application


# Apex REST Endpoint paths
# Keys indicate the type of ID the Python processor sends to the Apex endpoint's path segment.
APEX_ENDPOINT_PATHS: Dict[str, str] = {
    EDUCATION_LOG_OBJECT_API_NAME: "documentVerification/education",    # Path now expects ISB_Education_Log__c ID
    EMPLOYMENT_LOG_OBJECT_API_NAME: "documentVerification/employment", # Path expects ISB_Employment_Log__c ID
    APPLICATION_OBJECT_API_NAME: "documentVerification/application",    # Path expects hed__Application__c ID
    TEST_SCORE_OBJECT_API_NAME: "documentVerification/testscore",       # Path expects hed__Test__c ID
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
MAX_GLOBAL_REQUESTS_PER_WINDOW: int = int(os.getenv("MAX_GLOBAL_REQUESTS_PER_WINDOW", "30"))
GLOBAL_RATE_LIMIT_WINDOW_SECONDS: int = int(os.getenv("GLOBAL_RATE_LIMIT_WINDOW_SECONDS", "60"))
MAX_CLIENT_REQUESTS_PER_WINDOW: int = int(os.getenv("MAX_CLIENT_REQUESTS_PER_WINDOW", "10"))
CLIENT_RATE_LIMIT_WINDOW_SECONDS: int = int(os.getenv("CLIENT_RATE_LIMIT_WINDOW_SECONDS", "60"))
MIN_SECONDS_BETWEEN_SAME_APP_REQUESTS: int = int(os.getenv("MIN_SECONDS_BETWEEN_SAME_APP_REQUESTS", "5"))
SUSPICIOUS_THRESHOLD_REQUESTS: int = int(os.getenv("SUSPICIOUS_THRESHOLD_REQUESTS", "35"))
SUSPICIOUS_WINDOW_SECONDS: int = int(os.getenv("SUSPICIOUS_WINDOW_SECONDS", "60"))
SUSPICIOUS_BLOCK_DURATION_SECONDS: int = int(os.getenv("SUSPICIOUS_BLOCK_DURATION_SECONDS", "300"))
MAX_CONCURRENT_PROCESSING_SLOTS: int = int(os.getenv("MAX_CONCURRENT_PROCESSING_SLOTS", "10"))
RECENTLY_PROCESSED_TTL_SECONDS: int = int(os.getenv("RECENTLY_PROCESSED_TTL_SECONDS", "300"))
ACTIVE_PROCESSING_TIMEOUT_SECONDS: int = int(os.getenv("ACTIVE_PROCESSING_TIMEOUT_SECONDS", "900"))


# --- Endpoint Configuration ---
# Defines how related records are found and which processor to use.
# For "log-first" patterns, target_record_type is the Log Object.
RELATED_RECORD_PROCESSING_CONFIG: List[Dict[str, any]] = [
    {
        "target_record_type": EDUCATION_LOG_OBJECT_API_NAME, # Process ISB_Education_Log__c records
        "retrieval_method": "direct",
        "lookup_on_child_to_parent": EDUCATION_LOG_FIELD_TO_PARENT_APP,
        "processor_module": "app.processors.education_processor",
        "processor_function_name": "process_single_education_history_detail"
    },
    {
        "target_record_type": EMPLOYMENT_LOG_OBJECT_API_NAME, # Process ISB_Employment_Log__c records
        "retrieval_method": "direct",
        "lookup_on_child_to_parent": EMPLOYMENT_LOG_FIELD_TO_PARENT_APP,
        "processor_module": "app.processors.employment_processor",
        "processor_function_name": "process_single_employment_detail"
    },
    {
        "target_record_type": TEST_SCORE_OBJECT_API_NAME, # Process hed__Test__c records directly
        "retrieval_method": "direct",
        "lookup_on_child_to_parent": TEST_SCORE_LOOKUP_TO_PARENT_APP,
        "processor_module": "app.processors.test_score_processor",
        "processor_function_name": "process_single_test_score_detail"
    }
]

# Maximum length for the analysis report stored in Salesforce
MAX_SALESFORCE_REPORT_LENGTH: int = int(os.getenv("MAX_SALESFORCE_REPORT_LENGTH", "32000"))