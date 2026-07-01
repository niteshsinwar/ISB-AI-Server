import os
from dotenv import load_dotenv
from typing import List, Dict, Any, Optional

# Load environment variables from .env file
load_dotenv()

# --- Application Metadata ---
APP_TITLE: str = "Salesforce Document Text Extraction and Application Analysis API"
APP_DESCRIPTION: str = "API for extracting information from document text and analyzing Salesforce application records."
APP_VERSION: str = "2.0.0" # Production-grade architecture with process isolation, best-in-class observability, and fault tolerance

# --- Salesforce Configuration ---
SALESFORCE_ORGS: Dict[str, Dict[str, Any]] = {
    "dev": {
        "client_id": os.getenv("DEV_SALESFORCE_CLIENT_ID"),
        "client_secret": os.getenv("DEV_SALESFORCE_CLIENT_SECRET"),
        "token_url": os.getenv("DEV_SALESFORCE_TOKEN_URL"),
    },
    "uat": {
        "client_id": os.getenv("UAT_SALESFORCE_CLIENT_ID"),
        "client_secret": os.getenv("UAT_SALESFORCE_CLIENT_SECRET"),
        "token_url": os.getenv("UAT_SALESFORCE_TOKEN_URL"),
    },
    "prod": {
        "client_id": os.getenv("PROD_SALESFORCE_CLIENT_ID"),
        "client_secret": os.getenv("PROD_SALESFORCE_CLIENT_SECRET"),
        "token_url": os.getenv("PROD_SALESFORCE_TOKEN_URL"),
    },
    "cee_dev": {
        "client_id": os.getenv("CEE_DEV_SALESFORCE_CLIENT_ID"),
        "client_secret": os.getenv("CEE_DEV_SALESFORCE_CLIENT_SECRET"),
        "token_url": os.getenv("CEE_DEV_SALESFORCE_TOKEN_URL"),
    },
}

# --- Salesforce Object and Field API Names ---
AI_SERVER_JOB_OBJECT_API_NAME: str = os.getenv("AI_SERVER_JOB_OBJECT_API_NAME", "AI_Server_Job__c")
AIJ_APPLICATION_LOOKUP_FIELD: str = os.getenv("AIJ_APPLICATION_LOOKUP_FIELD", "Application__c")
AIJ_JOB_ID_FIELD: str = os.getenv("AIJ_JOB_ID_FIELD", "Job_ID__c")
AIJ_STATUS_FIELD: str = os.getenv("AIJ_STATUS_FIELD", "Status__c")
AIJ_MESSAGE_FIELD: str = os.getenv("AIJ_MESSAGE_FIELD", "Message__c")
AIJ_PROGRESS_FIELD: str = os.getenv("AIJ_PROGRESS_FIELD", "Progress_Details__c")
AIJ_CLIENT_FP_FIELD: str = os.getenv("AIJ_CLIENT_FP_FIELD", "Client_Fingerprint__c")
AIJ_LOGS_FIELD: str = os.getenv("AIJ_LOGS_FIELD", "logs__c")

APPLICATION_VERIFICATION_SUMMARY_OBJECT_API_NAME: str = "Application_Verification_Summary__c"
AVS_APPLICATION_LOOKUP_FIELD: str = "Application__c"
AVS_CONTACT_LOOKUP_FIELD: str = "Contact__c"
AVS_EDUCATION_HISTORY_LOOKUP_FIELD: str = "Education_History__c"
AVS_TEST_LOOKUP_FIELD: str = "Test__c"
AVS_AFFILIATION_LOOKUP_FIELD: str = "Affiliation__c"
AVS_REPORT_FIELD: str = "Verification_Analysis_Report__c"
AVS_NAME_FIELD: str = "Name"
AVS_OVERALL_FEEDBACK_FIELD: str = "Overall_Feedback__c"
AVS_MISMATCHED_LIST_FIELD: str = "Mismatched_Field_List__c"
AVS_CONFIDENCE_FIELD: str = "Percentage_Confidence__c"
AVS_TASK_DCI_LOOKUP_FIELD: str = "Application_Verification_Summary__c"
APPLICATION_OBJECT_API_NAME: str = "hed__Application__c"
APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP: str = os.getenv("APPLICATION_CONTACT_LOOKUP_FIELD_ON_APP", "Applicant__c")
EDUCATION_LOG_OBJECT_API_NAME: str = "ISB_Education_Log__c"
EDUCATION_LOG_FIELD_TO_PARENT_APP: str = "Application__c"

EMPLOYMENT_LOG_OBJECT_API_NAME: str = "ISB_Employment_Log__c"
EMPLOYMENT_LOG_FIELD_TO_PARENT_APP: str = "Application__c"

RECOMMENDER_DETAIL_OBJECT_API_NAME: str = "ISB_Recommender_Details__c"
RECOMMENDER_DETAIL_FIELD_TO_PARENT_APP: str = "Application__c"

TEST_SCORE_OBJECT_API_NAME: str = "hed__Test__c"
TEST_SCORE_LOOKUP_TO_PARENT_APP: str = "Application__c"
# NEW: DocumentChecklistItem fields
DCI_OBJECT_API_NAME: str = "DocumentChecklistItem"
DCI_PARENT_LOOKUP_FIELD: str = "ParentRecordId"
DCI_STATUS_FIELD: str = "Status"

READABLE_OBJECT_NAMES: Dict[str, str] = {
    APPLICATION_OBJECT_API_NAME: "Personal Detail",
    EDUCATION_LOG_OBJECT_API_NAME: "Education Records",
    EMPLOYMENT_LOG_OBJECT_API_NAME: "Employment Records",
    RECOMMENDER_DETAIL_OBJECT_API_NAME: "Recommender Detail",
    TEST_SCORE_OBJECT_API_NAME: "Test Score Records",
    DCI_OBJECT_API_NAME: "Resume Detail"
}

# -----------------------------------------------------------------------
# --- LLM Field Exclusions (single source of truth) ----------------------
# -----------------------------------------------------------------------
# Fields in this list are NEVER passed to the LLM for document verification.
# They are still available in record_data for skip-logic, automation, and
# any other server-side operations — exclusion only applies at the LLM boundary.
#
# Two categories merged into one list:
#   1. Internal routing / lookup fields  — Salesforce IDs and trigger metadata
#      that carry no verifiable meaning against a physical document.
#   2. Standard Salesforce system fields — auto-populated by the platform
#      (timestamps, audit IDs) that will never appear on any document.
LLM_FIELD_EXCLUSIONS: List[str] = [
    # --- Internal routing & lookup fields ---
    'Id', 'type', 'recordId', 'Task_Id', 'triggeringLogId',
    'DocumentchecklistItem_Id', 'Applicant__c', 'Contact', 'ContactId',
    # --- Standard Salesforce system / metadata fields ---
    'LastModifiedDate', 'LastModifiedById',
    'CreatedDate', 'CreatedById',
    'SystemModstamp', 'IsDeleted',
    'LastActivityDate', 'LastViewedDate', 'LastReferencedDate',
]

# -----------------------------------------------------------------------
# --- EEDL Module Configuration (Executive Education & Digital Learning) --
# -----------------------------------------------------------------------

# --- EEDL: Salesforce Object API Names ---
EEDL_OPPORTUNITY_OBJECT_API_NAME: str = os.getenv("EEDL_OPPORTUNITY_OBJECT_API_NAME", "Opportunity")
EEDL_EDUCATION_OBJECT_API_NAME: str = os.getenv("EEDL_EDUCATION_OBJECT_API_NAME", "Education__c")

# --- EEDL: Opportunity Field API Names ---
EEDL_OPP_CONTACT_LOOKUP_FIELD: str = os.getenv("EEDL_OPP_CONTACT_LOOKUP_FIELD", "ContactId")
EEDL_OPP_CITIZENSHIP_FIELD: str = os.getenv("EEDL_OPP_CITIZENSHIP_FIELD", "APP_Citizeship__c")

# --- EEDL: Education__c Field API Names ---
EEDL_EDU_CONTACT_LOOKUP_FIELD: str = os.getenv("EEDL_EDU_CONTACT_LOOKUP_FIELD", "Contact__c")
EEDL_EDU_DEGREE_FIELD: str = os.getenv("EEDL_EDU_DEGREE_FIELD", "Degree_Type__c")
EEDL_EDU_UNIVERSITY_FIELD: str = os.getenv("EEDL_EDU_UNIVERSITY_FIELD", "University_Name__c")
EEDL_EDU_GPA_FIELD: str = os.getenv("EEDL_EDU_GPA_FIELD", "GPA__c")
EEDL_EDU_START_DATE_FIELD: str = os.getenv("EEDL_EDU_START_DATE_FIELD", "From__c")
EEDL_EDU_END_DATE_FIELD: str = os.getenv("EEDL_EDU_END_DATE_FIELD", "To__c")

# --- EEDL: AI_Server_Job__c — new Opportunity lookup field (to be created in SF) ---
AIJ_OPPORTUNITY_LOOKUP_FIELD: str = os.getenv("AIJ_OPPORTUNITY_LOOKUP_FIELD", "Opportunity__c")

# --- EEDL: EEDL_Verification_Summary__c Object and Field API Names (to be created in SF) ---
EEDL_VS_OBJECT_API_NAME: str = os.getenv("EEDL_VS_OBJECT_API_NAME", "EEDL_Verification_Summary__c")
EEDL_VS_OPPORTUNITY_LOOKUP_FIELD: str = "Opportunity__c"
EEDL_VS_EDUCATION_LOOKUP_FIELD: str = "Education__c"
EEDL_VS_RECORD_TYPE_FIELD: str = "Record_Type__c"
EEDL_VS_VERIFICATION_STATUS_FIELD: str = "Overall_Status__c"
EEDL_VS_CONFIDENCE_FIELD: str = "Confidence_Score__c"
EEDL_VS_REPORT_FIELD: str = "Summary_HTML__c"
EEDL_VS_OVERALL_FEEDBACK_FIELD: str = "Overall_Feedback__c"
EEDL_VS_MISMATCHED_FIELDS_FIELD: str = "Mismatched_Field_List__c"
EEDL_VS_NAME_FIELD: str = "Name"

# --- EEDL: Record Type picklist values for EEDL_Verification_Summary__c ---
EEDL_VS_RECORD_TYPE_ID_DOCUMENT: str = "ID_Document"
EEDL_VS_RECORD_TYPE_EDUCATION: str = "Education"

# --- EEDL: File Name Matching Configuration ---
# All keyword matching is case-insensitive on the ContentVersion Title/FileName.
# Update keyword lists with actual file naming conventions used by the client.
EEDL_FILE_MATCHING_CONFIG: Dict[str, Any] = {
    # If ANY of these keywords appear in the filename → treat as Aadhaar / Passport ID document
    "id_document_keywords": [
        "aadhaar", "aadhar", "adhar", "adhaar",
        "passport"
    ],
    # Each entry maps filename keywords → Education__c Degree__c values to match against.
    # Matching: if filename contains a file_keyword AND an Education__c record's
    # Degree__c value is in degree_values → that record gets this file.
    "education_keyword_map": [
        {
            "file_keywords": ["ug", "undergraduate", "bachelor", "btech", "be", "bsc", "ba"],
            "degree_values": ["Bachelors", "Bachelor", "B.Tech", "BE", "BSc", "BA"]
        },
        {
            "file_keywords": ["pg", "postgraduate", "master", "mtech", "mba", "msc", "ma"],
            "degree_values": ["Masters", "Master", "MBA", "M.Tech", "MSc", "MA"]
        },
        {
            "file_keywords": ["phd", "doctorate", "doctoral"],
            "degree_values": ["Doctorate", "PhD", "Ph.D"]
        },
        {
            "file_keywords": ["xii", "12th", "hsc", "senior secondary", "plus2", "class12"],
            "degree_values": ["XII", "12th", "HSC", "Senior Secondary"]
        },
        {
            "file_keywords": ["x", "10th", "ssc", "matriculation", "class10"],
            "degree_values": ["X", "10th", "SSC", "Matriculation"]
        }
    ]
}

EEDL_READABLE_OBJECT_NAMES: Dict[str, str] = {
    "ID_Document": "ID Document Verification",
    EEDL_EDUCATION_OBJECT_API_NAME: "Education Verification",
}

# EEDL processing config — mirrors RELATED_RECORD_PROCESSING_CONFIG structure
# Used by job_worker for priority sorting and readable name lookup
EEDL_RECORD_PROCESSING_CONFIG: List[Dict[str, any]] = [
    {
        "target_record_type": "ID_Document",
        "processor_module": "app.processors.eedl_id_processor",
        "processor_function_name": "process_eedl_id_document",
        "priority": 1,
    },
    {
        "target_record_type": EEDL_EDUCATION_OBJECT_API_NAME,
        "processor_module": "app.processors.eedl_education_processor",
        "processor_function_name": "process_eedl_education_record",
        "priority": 2,
    },
]

# Apex REST Endpoint paths (migrated: Test Score now uses Python-side SF service)
APEX_ENDPOINT_PATHS: Dict[str, str] = {
    EDUCATION_LOG_OBJECT_API_NAME: "documentVerification/education",
    EMPLOYMENT_LOG_OBJECT_API_NAME: "documentVerification/employment",
    APPLICATION_OBJECT_API_NAME: "documentVerification/application",
}

# --- Google Gemini Configuration ---
DOC_GOOGLE_API_KEY: Optional[str] = os.getenv("DOC_GOOGLE_API_KEY")
CREW_GOOGLE_API_KEY: Optional[str] = os.getenv("CREW_GOOGLE_API_KEY")
MODEL_DATA_ANALYSIS: str = "gemini-2.5-flash"
MODEL_COMPLEX_REASONING: str = "gemini-2.5-flash"
TEMP_COMPLEX_REASONING: float = float(os.getenv("TEMP_COMPLEX_REASONING", "0.0"))
MODEL_TEXT_EXTRACTION: str = "gemini-2.5-flash"
MODEL_STANDARD_VERIFICATION: str = "gemini-2.5-flash"
MODEL_HTML_SYNTHESIS: str = "gemini-2.5-flash"
TEMP_STANDARD_VERIFICATION: float = float(os.getenv("TEMP_STANDARD_VERIFICATION", "0.0"))
TEMP_HTML_SYNTHESIS: float = float(os.getenv("TEMP_HTML_SYNTHESIS", "0.0"))

# --- Gemini API Pricing Configuration (per 1M tokens) ---
# Source: https://ai.google.dev/gemini-api/docs/pricing
# Last Updated: February 2026
# STICKY POLICY: ONLY GEMINI 2.5 FLASH IS ALLOWED
GEMINI_PRICING: Dict[str, Dict[str, float]] = {
    "gemini-2.5-flash": {
        "input_per_1m": 0.30,           # $0.30 per 1M input tokens (text/image/video)
        "input_audio_per_1m": 1.00,     # $1.00 per 1M audio input tokens
        "input_long_per_1m": 0.30,      # Same price for long context
        "output_per_1m": 2.50,          # $2.50 per 1M output tokens (includes thinking)
    },
}

# Default pricing for unknown models
GEMINI_DEFAULT_PRICING: Dict[str, float] = {
    "input_per_1m": 1.00,
    "input_long_per_1m": 2.00,
    "output_per_1m": 5.00,
}

# Long context threshold (tokens) - pricing changes above this for some models
LONG_CONTEXT_THRESHOLD: int = 200000

# --- Multimodal Token Calculation ---
# --- API Rate Limiting and Processing Configuration ---
# SIMPLIFIED: Single source of truth for capacity management
MAX_CONCURRENT_PROCESSING_SLOTS: int = int(os.getenv("MAX_CONCURRENT_PROCESSING_SLOTS", "15"))

# Job execution timeouts
JOB_TIMEOUT_SECONDS: int = int(os.getenv("JOB_TIMEOUT_SECONDS", "6000"))

# Simple throttle: minimum seconds between requests from same client (prevents spam)
MIN_REQUEST_INTERVAL_SECONDS: float = float(os.getenv("MIN_REQUEST_INTERVAL_SECONDS", "1.0"))

# --- Endpoint Configuration ---
# CRITICAL MODIFICATION: Added 'filtering_criteria' to selectively process only supported record subtypes.
# This aligns the Python server with the business logic in the Apex handlers.
RELATED_RECORD_PROCESSING_CONFIG: List[Dict[str, any]] = [
    {
        "target_record_type": APPLICATION_OBJECT_API_NAME,
        "retrieval_method": "self",
        "lookup_on_child_to_parent": None,
        "processor_module": "app.processors.application_processor",
        "processor_function_name": "process_single_application_detail",
        "priority": 1,
        "filtering_criteria": None
    },
    {
        "target_record_type": EDUCATION_LOG_OBJECT_API_NAME,
        "retrieval_method": "direct",
        "lookup_on_child_to_parent": EDUCATION_LOG_FIELD_TO_PARENT_APP,
        "processor_module": "app.processors.education_processor",
        "processor_function_name": "process_single_education_history_detail",
        "priority": 2,
        "filtering_criteria": {
            "field_api_name": "Education_History__r.Degree_Level__c",
            "allowed_values": [
                'XII', 'Bachelors', 'Master', 'Integrated',
                'Professional Education', 'Doctorate'
            ]
        }
    },
    {
        "target_record_type": EMPLOYMENT_LOG_OBJECT_API_NAME,
        "retrieval_method": "direct",
        "lookup_on_child_to_parent": EMPLOYMENT_LOG_FIELD_TO_PARENT_APP,
        "processor_module": "app.processors.employment_processor",
        "processor_function_name": "process_single_employment_detail",
        "priority": 3,
        "order_by": "Affiliation__r.hed__StartDate__c DESC NULLS LAST",
        "limit": 1,
        "filtering_criteria": {
            "field_api_name": "Type_of_Employment__c",
            "allowed_values": [
                'Full-Time'
            ]
        }
    },
    {
        "target_record_type": TEST_SCORE_OBJECT_API_NAME,
        "retrieval_method": "direct",
        "lookup_on_child_to_parent": TEST_SCORE_LOOKUP_TO_PARENT_APP,
        "processor_module": "app.processors.test_score_processor",
        "processor_function_name": "process_single_test_score_detail",
        "priority": 4,
        "filtering_criteria": {
            "field_api_name": "RecordTypeName__c",
            "allowed_values": ["GMAT_FOCUS", "GMAT", "GRE"]
        }
    },
    {
        "target_record_type": DCI_OBJECT_API_NAME,
        "retrieval_method": "direct",
        "lookup_on_child_to_parent": DCI_PARENT_LOOKUP_FIELD,
        "processor_module": "app.processors.resume_processor",
        "processor_function_name": "process_single_resume_detail",
        "priority": 5,
        "filtering_criteria": [
    {
        "field_api_name": "Name",
        "operator": "LIKE",
        "value": "%resume%"
    },
    {
        # Define the anti-join subquery explicitly
        "subquery_filter": {
            "field": "ParentRecordId",
            "operator": "NOT IN",
            "subquery": {
                "object": "hed__Application__c",
                "select_field": "Id",
                "where_clause": "ApplyingTo__c LIKE '%AMP%'"
            }
        }
    },
    {
        # Exclude records where Status is already 'Accepted'
        "field_api_name": "Status",
        "operator": "!=",
        "value": "Accepted"
    }
]
    },
    {
        "target_record_type": RECOMMENDER_DETAIL_OBJECT_API_NAME,
        "retrieval_method": "direct",
        "lookup_on_child_to_parent": RECOMMENDER_DETAIL_FIELD_TO_PARENT_APP,
        "processor_module": "app.processors.recommender_processor",
        "processor_function_name": "process_single_recommender_detail",
        "priority": 6,
        "filtering_criteria": None
    }
]

# --- Text Extraction Prompts ---
MAX_SALESFORCE_REPORT_LENGTH: int = 131072
RAW_OCR_PROMPT = """
You are a high-precision Optical Character Recognition (OCR) engine. Your only task is to transcribe ALL text from the provided image, exactly as it appears. Maintain the original spatial layout as best as possible. Do not interpret, format, or analyze the content. Output only the raw, transcribed text.
"""
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
