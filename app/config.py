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
APP_VERSION: str = "9.1.0" # Versioning for employment update

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
# Main Application Object
APPLICATION_OBJECT_API_NAME: str = "hed__Application__c"
APPLICATION_ANALYSIS_REPORT_FIELD: str = "Background_Verification_Details__c"

# Education History Object
EDUCATION_HISTORY_OBJECT_API_NAME: str = "hed__Education_History__c"
EDUCATION_HISTORY_ANALYSIS_REPORT_FIELD: str = "Verification_Analysis_Report__c"
EDUCATION_HISTORY_JUNCTION_OBJECT: str = "ISB_Education_Log__c" # Junction linking Application to Education History
EDUCATION_HISTORY_JUNCTION_FIELD_TO_PARENT: str = "Application__c" # On ISB_Education_Log__c to Application__c
EDUCATION_HISTORY_JUNCTION_FIELD_TO_TARGET: str = "Education_History__c" # On ISB_Education_Log__c to hed__Education_History__c

# Employment Log Object (NEW - This is the object whose ID triggers employment verification)
ISB_EMPLOYMENT_LOG_OBJECT_API_NAME: str = "ISB_Employment_Log__c" # Verify this API name
# This field will store the analysis report for an employment verification.
# Ensure this field exists on the ISB_Employment_Log__c object in Salesforce.
EMPLOYMENT_LOG_ANALYSIS_REPORT_FIELD: str = "Verification_Analysis_Report__c" # Verify/Create this field

# Test Score Object
TEST_SCORE_OBJECT_API_NAME: str = "hed__Test__c"
TEST_SCORE_ANALYSIS_REPORT_FIELD: str = "Verification_Analysis_Report__c"
TEST_SCORE_LOOKUP_TO_PARENT_APP: str = "Application__c" # Field on hed__Test__c linking to hed__Application__c

# Apex REST Endpoint paths (segments after /services/apexrest/)
# Keys here MUST match the 'sobject_api_name_key' used in processors when calling sf_service.get_record_detail_from_apex
APEX_ENDPOINT_PATHS: Dict[str, str] = {
    EDUCATION_HISTORY_OBJECT_API_NAME: "documentVerification/education",        # Key is hed__Education_History__c
    APPLICATION_OBJECT_API_NAME: "documentVerification/application",            # Key is hed__Application__c
    TEST_SCORE_OBJECT_API_NAME: "documentVerification/testscore",               # Key is hed__Test__c
    ISB_EMPLOYMENT_LOG_OBJECT_API_NAME: "documentVerification/employment",      # NEW - Key is ISB_Employment_Log__c
}

# --- Google Gemini Configuration ---
GOOGLE_API_KEY: str | None = os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL_NAME: str = os.getenv("GEMINI_MODEL_NAME", "gemini-1.5-flash")
TEXT_EXTRACTION_OCR_PROMPT: str = (
    "Extract all textual content from this document page accurately. "
    "Preserve line breaks, formatting, and structure where possible. "
    "If no text is present, respond with an empty string or 'NO_TEXT_FOUND'."
)
MAX_CONCURRENT_OCR_PAGES: int = int(os.getenv("MAX_CONCURRENT_OCR_PAGES", "3"))


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
ACTIVE_PROCESSING_TIMEOUT_SECONDS: int = int(os.getenv("ACTIVE_PROCESSING_TIMEOUT_SECONDS", "900"))


# --- Endpoint Configuration ---
RELATED_RECORD_PROCESSING_CONFIG: List[Dict[str, any]] = [
    {
        "target_record_type": EDUCATION_HISTORY_OBJECT_API_NAME, # This is hed__Education_History__c
        "retrieval_method": "via_junction", 
        "junction_object": EDUCATION_HISTORY_JUNCTION_OBJECT, # ISB_Education_Log__c
        "junction_field_to_parent": EDUCATION_HISTORY_JUNCTION_FIELD_TO_PARENT, # Application__c on ISB_Education_Log__c
        "junction_field_to_target": EDUCATION_HISTORY_JUNCTION_FIELD_TO_TARGET, # Education_History__c on ISB_Education_Log__c
        "processor_module": "app.processors.education_processor",
        "processor_function_name": "process_single_education_history_detail"
    },
    {
        "target_record_type": TEST_SCORE_OBJECT_API_NAME, # This is hed__Test__c
        "retrieval_method": "direct", 
        "lookup_on_child_to_parent": TEST_SCORE_LOOKUP_TO_PARENT_APP, # Application__c on hed__Test__c
        "processor_module": "app.processors.test_score_processor",
        "processor_function_name": "process_single_test_score_detail"
    },
    {
        # For employment, we iterate over ISB_Employment_Log__c records that are children of the Application.
        # The processor will then handle fetching Affiliation details based on the ISB_Employment_Log__c record.
        "target_record_type": ISB_EMPLOYMENT_LOG_OBJECT_API_NAME, # The records to iterate over
        "retrieval_method": "direct", # ISB_Employment_Log__c has a direct lookup to Application__c
        "lookup_on_child_to_parent": "Application__c", # Field on ISB_Employment_Log__c that points to hed__Application__c. VERIFY THIS FIELD NAME.
        "processor_module": "app.processors.employment_processor",
        "processor_function_name": "process_single_employment_detail"
    }
]