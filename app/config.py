# project_root/app/config.py
import os
from dotenv import load_dotenv
from typing import List, Dict

# Load environment variables from .env file
load_dotenv()

# --- Application Metadata ---
APP_TITLE: str = "Salesforce Document Text Extraction and Application Analysis API"
APP_DESCRIPTION: str = "API for extracting information from document text and analyzing Salesforce application records."
APP_VERSION: str = "1.0.0"

# --- Logging Configuration ---
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# --- Salesforce Configuration ---
SALESFORCE_DOMAIN: str | None = os.getenv("SALESFORCE_DOMAIN", "test")
SALESFORCE_INSTANCE_URL: str | None = os.getenv("SALESFORCE_INSTANCE_URL")
SALESFORCE_CLIENT_ID: str | None = os.getenv("SALESFORCE_CLIENT_ID")
SALESFORCE_CLIENT_SECRET: str | None = os.getenv("SALESFORCE_CLIENT_SECRET")
SALESFORCE_TOKEN_URL: str | None = os.getenv("SALESFORCE_TOKEN_URL")

# --- Salesforce Object and Field API Names ---
APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME: str = "Application_Verification_Summary__c"
AVS_APPLICATION_LOOKUP_FIELD: str = "Application__c"
AVS_CONTACT_LOOKUP_FIELD: str = "Contact__c"
AVS_EDUCATION_HISTORY_LOOKUP_FIELD: str = "Education_History__c"
AVS_TEST_LOOKUP_FIELD: str = "Test__c"
AVS_AFFILIATION_LOOKUP_FIELD: str = "Affiliation__c"
AVS_REPORT_FIELD: str = "Verification_Analysis_Report__c"
AVS_NAME_FIELD: str = "Name"
AVS_OVERALL_FEEDBACK_FIELD: str = "Overall_feedback__c"
AVS_CONFIDENCE_FIELD: str = "Percentage_Confidence__c"
AVS_TASK_DCI_LOOKUP_FIELD: str = "Application_Verification_Summary__c"
APPLICATION_OBJECT_API_NAME: str = "hed__Application__c"
APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP: str = os.getenv("APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP", "Applicant__c")
EDUCATION_LOG_OBJECT_API_NAME: str = "ISB_Education_Log__c"
EDUCATION_LOG_FIELD_TO_PARENT_APP: str = "Application__c"
EDUCATION_LOG_FIELD_TO_DETAIL: str = "Education_History__c"
EMPLOYMENT_LOG_OBJECT_API_NAME: str = "ISB_Employment_Log__c"
EMPLOYMENT_LOG_FIELD_TO_PARENT_APP: str = "Application__c"
EMPLOYMENT_LOG_FIELD_TO_DETAIL: str = "Affiliation__c"
TEST_SCORE_OBJECT_API_NAME: str = "hed__Test__c"
TEST_SCORE_LOOKUP_TO_PARENT_APP: str = "Application__c"

# Apex REST Endpoint paths
APEX_ENDPOINT_PATHS: Dict[str, str] = {
    EDUCATION_LOG_OBJECT_API_NAME: "documentVerification/education",
    EMPLOYMENT_LOG_OBJECT_API_NAME: "documentVerification/employment",
    APPLICATION_OBJECT_API_NAME: "documentVerification/application",
    TEST_SCORE_OBJECT_API_NAME: "documentVerification/testscore",
}

# --- Google Gemini Configuration ---
GOOGLE_API_KEY: str | None = os.getenv("GOOGLE_API_KEY")

# --- Model for general purpose text extraction (OCR) ---
MODEL_TEXT_EXTRACTION: str = os.getenv("MODEL_TEXT_EXTRACTION", "gemini-2.5-pro-preview-06-05")
MODEL_DATA_ANALYSIS: str = os.getenv("MODEL_DATA_ANALYSIS", "gemini-2.5-pro-preview-06-05") # Using a powerful model for analysis
# --- Tiered Model Configuration for Crews ---
# Tier 1: For the most complex reasoning (e.g., Marksheet Calculation)
MODEL_COMPLEX_REASONING: str = os.getenv("MODEL_COMPLEX_REASONING", "gemini-2.5-pro-preview-06-05")
TEMP_COMPLEX_REASONING: float = float(os.getenv("TEMP_COMPLEX_REASONING", "0.2"))

# Tier 2: For standard verification tasks (ID, Employment, Test Scores)
MODEL_STANDARD_VERIFICATION: str = os.getenv("MODEL_STANDARD_VERIFICATION", "gemini-2.5-pro-preview-06-05")
TEMP_STANDARD_VERIFICATION: float = float(os.getenv("TEMP_STANDARD_VERIFICATION", "0.25"))

# Tier 3: For final report generation and synthesis
MODEL_HTML_SYNTHESIS: str = os.getenv("MODEL_HTML_SYNTHESIS", "gemini-2.5-pro-preview-06-05")
TEMP_HTML_SYNTHESIS: float = float(os.getenv("TEMP_HTML_SYNTHESIS", "0.4"))


# --- AI Crew Configuration ---
CONFIDENCE_PICKLIST_RANGES: List[str] = [
    '100',
    '90 to 99',
    '80 to 90',
    '40 to 80',
    '0 to 40'
]

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
RELATED_RECORD_PROCESSING_CONFIG: List[Dict[str, any]] = [
    {
        "target_record_type": EDUCATION_LOG_OBJECT_API_NAME,
        "retrieval_method": "direct",
        "lookup_on_child_to_parent": EDUCATION_LOG_FIELD_TO_PARENT_APP,
        "processor_module": "app.processors.education_processor",
        "processor_function_name": "process_single_education_history_detail"
    },
    {
        "target_record_type": EMPLOYMENT_LOG_OBJECT_API_NAME,
        "retrieval_method": "direct",
        "lookup_on_child_to_parent": EMPLOYMENT_LOG_FIELD_TO_PARENT_APP,
        "processor_module": "app.processors.employment_processor",
        "processor_function_name": "process_single_employment_detail"
    },
    {
        "target_record_type": TEST_SCORE_OBJECT_API_NAME,
        "retrieval_method": "direct",
        "lookup_on_child_to_parent": TEST_SCORE_LOOKUP_TO_PARENT_APP,
        "processor_module": "app.processors.test_score_processor",
        "processor_function_name": "process_single_test_score_detail"
    }
]

# Maximum length for the analysis report stored in Salesforce
# --- Text Extraction Prompts ---
MAX_SALESFORCE_REPORT_LENGTH: int = 131072
MAX_CONCURRENT_OCR_PAGES: int = int(os.getenv("MAX_CONCURRENT_OCR_PAGES", "7"))

# Prompt for the first agent: Raw OCR
RAW_OCR_PROMPT = """
You are a high-precision Optical Character Recognition (OCR) engine. Your only task is to transcribe ALL text from the provided image, exactly as it appears. Maintain the original spatial layout as best as possible. Do not interpret, format, or analyze the content. Output only the raw, transcribed text.
"""

# Prompt for the second agent: Data Structuring and Analysis
DATA_STRUCTURING_PROMPT = """
You are an expert data analyst and document structurer. You will receive raw, messy text transcribed from a document, along with the original document image for visual context. Your task is to analyze both and create a perfect, structured Markdown representation of the document.

**CRITICAL INSTRUCTIONS:**

1.  **Analyze and Reconstruct:** Examine the raw text and the original image to understand the document's true layout, especially for tables, lists, and headers. The raw text may be jumbled; use the image to correct the structure.
2.  **Recreate Tables:** This is your most important task. If you identify a table, recreate it perfectly using Markdown table syntax (`| Header | ... |`). Ensure all columns and rows are correctly aligned as they appear in the original document.
3.  **Perform Calculations:** If the table contains numerical data that can be summed (like marks or amounts), calculate the grand total of the primary column and add it as a `**Summary:**` line after the table. For example: `**Summary:** Calculated Grand Total of Marks: 415`.
4.  **Preserve All Other Text:** Transcribe all non-tabular text (headers, footers, paragraphs) exactly as it appears, preserving formatting like bold or italics.
5.  **Handle Illegible Content:** Use `[ILLEGIBLE]` for unreadable text and `[HANDWRITING: ...]` for handwritten notes.

Your final output must be a single, clean, and complete Markdown string that is a high-fidelity digital version of the original document. Do not add any commentary.
"""
