# project_root/app/crew/test_score_crew.py
import os
import logging
import json
from typing import Dict, Any, List
from crewai import Agent, Task, Crew, Process
from langchain_google_genai import ChatGoogleGenerativeAI

# Import shared configurations
# Create an app/config.py file with your actual GOOGLE_API_KEY and GEMINI_MODEL_NAME
# Example app/config.py:
# GOOGLE_API_KEY = "your_google_api_key_here"
# GEMINI_MODEL_NAME = "gemini-pro" # Or your preferred model
try:
    from app.config import GOOGLE_API_KEY, GEMINI_MODEL_NAME
except ImportError:
    GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY") # Fallback to environment variable
    GEMINI_MODEL_NAME = os.environ.get("GEMINI_MODEL_NAME", "gemini-pro") # Fallback
    if not GOOGLE_API_KEY:
        print("Warning: GOOGLE_API_KEY not found in app.config or environment variables.")


logger = logging.getLogger(__name__)
# Basic logging configuration for demonstration if not configured elsewhere
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


# LLM Initialization
gemini_llm_test_score = None
if GOOGLE_API_KEY:
    try:
        gemini_llm_test_score = ChatGoogleGenerativeAI(
            model=GEMINI_MODEL_NAME,
            temperature=0.2,
            google_api_key=GOOGLE_API_KEY,
        )
        logger.info(f"TestScoreVerificationCrew LLM initialized with model: {GEMINI_MODEL_NAME}")
    except Exception as e:
        logger.error(f"Failed to initialize LLM for TestScoreVerificationCrew ({GEMINI_MODEL_NAME}): {e}", exc_info=True)
        gemini_llm_test_score = None
else:
    logger.critical("TEST_SCORE_CREW: GOOGLE_API_KEY not set. LLM will not be available.")


# Comprehensive list of fields the agent uses as its master reference.
# The agent will determine relevance based on RecordTypeName__c from the record.
TEST_SCORE_VERIFICATION_FIELDS = [
    "RecordTypeName__c", "Test_Date__c", "Email__c", "Test_ID__c",         # Common identifiers/metadata
    "Verbal_Score__c", "Verbal_Percentile__c",                             # Common sections (scoring/scales may differ)
    "Quantitative_Score__c", "Quantitative_Percentile__c",                 # Common sections (scoring/scales may differ)
    "Total_Score__c", "Total_Percentile__c",                             # Common concept (derivation/composition differs significantly)
    "Data_Insights_Score__c", "Data_Insights_Percentile__c",             # Primarily GMAT specific for main total score
    "Analytical_Writing_Score__c", "Analytical_Writing_Percentile__c"     # GRE: distinct scored section. GMAT: separate AWA score (if this field is used for it)
]

# Define a mapping from canonical field names (used by the agent and TEST_SCORE_VERIFICATION_FIELDS)
# to a list of potential Salesforce field names.
# The first Salesforce name in the list found in record_data_dict will be used.
SALESFORCE_TO_CANONICAL_MAP = {
    "RecordTypeName__c": ["RecordTypeName__c"],
    "Test_Date__c": ["hed__Test_Date__c", "Test_Date__c"],
    "Email__c": ["Email__c"],
    "Test_ID__c": ["Test_ID__c"],
    "Verbal_Score__c": ["VerbalScore__c", "Verbal_Score__c"],
    "Verbal_Percentile__c": ["VerbalPercentile__c", "Verbal_Percentile__c"],
    "Quantitative_Score__c": ["QuantScore__c", "Quantitative_Score__c"],
    "Quantitative_Percentile__c": ["QuantPercentile__c", "Quantitative_Percentile__c"],
    "Total_Score__c": ["Total_Score__c"],
    "Total_Percentile__c": ["Total_Percentile__c"],
    "Data_Insights_Score__c": ["Data_Insights_Score__c"],
    "Data_Insights_Percentile__c": ["Data_Insights_Percentile__c"],
    "Analytical_Writing_Score__c": ["Analytical_Score__c", "Analytical_Writing_Score__c"],
    "Analytical_Writing_Percentile__c": ["Analytical_Percentile__c", "Analytical_Writing_Percentile__c"]
}

class TestScoreVerificationAgents:
    def data_comparator_agent(self) -> Agent:
        if not gemini_llm_test_score:
            raise RuntimeError("LLM for TestScoreVerificationAgents not initialized. Cannot create agent.")
        
        field_list_str = ", ".join([f"'{field}'" for field in TEST_SCORE_VERIFICATION_FIELDS])

        return Agent(
            role="Meticulous Test Score Verification Analyst for GMAT and GRE", # Role updated
            goal=(
                "You are provided with: "
                "1. Structured Salesforce record data for a Test Score (as a JSON string), which includes **'RecordTypeName__c'** (either 'GRE' or 'GMAT'). "
                "2. Raw text extracted from a supporting official test score document. "
                "Your multi-step goal is to: "
                "  a. Based on the **'RecordTypeName__c'** in the Salesforce record (either 'GRE' or 'GMAT'), determine the **specific list of fields that are relevant** for that particular test type's primary assessment and score reporting. "
                "     This list will be a dynamically selected subset of all known test score fields "
                f"     (master list for context: `{field_list_str}`). \n"
                "     - If 'RecordTypeName__c' is **'GRE'**: \n" # GRE Relevance
                "       - Relevant fields include Verbal, Quantitative, Analytical Writing (as a scored section from 0-6), and their respective scores/percentiles, plus the overall Total Score (typically Verbal Score + Quantitative Score for the main score range like 260-340).\n"
                "       - Fields like 'Data_Insights_Score__c' are NOT relevant for GRE.\n"
                "     - If 'RecordTypeName__c' is **'GMAT'**: \n" # GMAT Relevance
                "       - Relevant fields for the main GMAT score (typically 205-805) include Verbal, Quantitative, Data Insights, and their respective scores/percentiles, plus the overall Total Score. \n"
                "       - 'Analytical_Writing_Score__c' for GMAT refers to the AWA (Analytical Writing Assessment), which is scored separately (e.g., 0-6) and does NOT contribute to the main GMAT Total Score (205-805). You should still process an AWA score if present and listed as 'Analytical_Writing_Score__c', but recognize its separate nature for GMAT when considering the main Total Score. \n"
                "     Your output JSON array should only contain objects for fields you determine are relevant for comparison based on these distinctions for the given test type.\n"
                "  b. For **each field in the determined relevant list from step a**: "
                "     i. **Value Extraction and Determination from Document (Field-Specific Rules):**\n"
                "        - For most fields: Meticulously extract its value from the raw document text. If a value for this relevant field is not found in the document, this field's 'document_value' will be 'Not Found in Document'.\n"
                "        - **Special Handling for 'Total_Score__c' (only if 'Total_Score__c' is determined to be relevant in step a):**\n"
                "          1. First, attempt to find an explicitly stated Total Score in the document text. If a numeric Total Score is explicitly found, use this as the 'document_value' for 'Total_Score__c'. The 'notes' should indicate 'Explicitly found in document'.\n"
                "          2. If an explicit Total Score is NOT found in the document text, AND the 'RecordTypeName__c' is **'GRE'**: \n" # Total Score for GRE
                "             - Check the values you previously extracted *from the document text* for 'Verbal_Score__c' and 'Quantitative_Score__c' during this current analysis of the document.\n"
                "             - If both 'Verbal_Score__c' and 'Quantitative_Score__c' were successfully extracted from the document text as numeric values, then calculate the 'document_value' for 'Total_Score__c' by summing these two document-extracted section scores (Document Verbal + Document Quantitative).\n"
                "             - The 'notes' for 'Total_Score__c' must then clearly state 'Calculated from document section scores (Verbal + Quant)'. The confidence for this calculated score should be 'High' if the section scores were extracted with high confidence.\n"
                "             - If these required section scores were not found in the document text or were not numeric, then the 'document_value' for 'Total_Score__c' remains 'Not Found in Document'. The 'notes' should explain why calculation was not possible (e.g., 'Explicit Total Score not found, and section scores for calculation were missing or non-numeric in document').\n"
                "          3. If the 'RecordTypeName__c' is **'GMAT'**, and an explicit Total Score is NOT found in the document text: \n" # Total Score for GMAT
                "             - The 'document_value' for 'Total_Score__c' is 'Not Found in Document'. \n"
                "             - The 'notes' should state 'Explicit GMAT Total Score not found in document; GMAT Total Score is a scaled score derived from Verbal, Quantitative, and Data Insights sections and is not calculated by simple summation here. Report individual GMAT section scores as extracted.'\n"
                "          4. If, after the above steps, no explicit or calculated value is determined for 'Total_Score__c' from the document, its 'document_value' is 'Not Found in Document'.\n"
                "     ii. Apply specific verification rules (as detailed below) to this field, using the 'document_value' determined in step b.i. "
                "     iii. Compare the 'document_value' (which might be an extracted value, a calculated value for Total_Score__c for GRE, or 'Not Found in Document') against the Salesforce 'record_value' for this field. "
                "     iv. Create a JSON object detailing: 'field_name' (for this specific relevant field), 'record_value', 'document_value', "
                "         'status' ('Matched', 'Mismatched', 'Partially Matched (Detail Variance)', 'Found in Record Only', 'Found in Document Only', 'Not Found in Either', 'Potentially Update Record'), "
                "         'confidence' (High, Medium, Low - assess based on clarity of extraction or calculation basis), "
                "         'notes' (Crucial for explaining status and value origins for this field, especially if calculated or related to GMAT AWA). "
                "  c. **Specific Verification Rules (to be applied to the relevant fields identified in step a)**: "
                "     - **'RecordTypeName__c' / Test Type**: Document should confirm the test type ('GRE' or 'GMAT') stated in the record. Minor variations are acceptable. "
                "     - **Key Identifiers** (e.g., `Test_ID__c`, `Email__c` - if relevant for the test type): Must match exactly between record and document. "
                "     - **'Test_Date__c'**: Aim for exact match (YYYY-MM-DD). Handle partial matches as specified. "
                "     - **Scores & Percentiles** (Verbal, Quantitative, Analytical Writing, Data Insights, Total - *only those relevant to the specific test type's primary scoring structure being processed*): Must match precisely. Allow minor numeric formatting differences (e.g., 99 vs 99.00). The 'Total_Score__c' comparison will use its document value as determined in step b.i. "
                "  d. **'Official Score' Indication & Handling Discrepancies (applied to the processed relevant fields)**: "
                "     - If all critical scores, percentiles, and key identifiers *that are relevant to this test type's primary assessment* match exactly, the notes for `RecordTypeName__c` (or a summary note) should indicate: 'Document strongly supports the record as official for the identified relevant fields.' "
                "     - If a score/percentile *in the relevant set* is blank or different from the document, BUT other key identifiers (if relevant) AND a majority of other *relevant* scores/percentiles match the document, set status for the differing field to 'Potentially Update Record'. Notes must clearly state record value, document value, and why an update might be considered. "
                "     - If critical identifiers *relevant to this test type* (e.g., Test_ID__c, Email__c) do not match, this is a major discrepancy, status 'Mismatched' for those identifiers, and overall confidence in the match should be low, even if some scores appear similar. "
                "**Output a single, valid JSON array string where each element is an object representing the comparison *only for one of the fields identified as relevant in step a*.**"
            ),
            backstory=(
                "You are an expert AI system designed for precise and rule-based analysis of standardized test score reports, specifically GMAT and GRE. " # Backstory updated
                "You meticulously extract data, understand the nuances of GMAT and GRE including their specific scoring structures (e.g., how GRE Total Scores are commonly derived, how GMAT Total Scores are scaled, and how GMAT AWA is separate), focus only on relevant fields based on the test's Record Type, apply strict verification rules, and clearly report findings."
            ),
            llm=gemini_llm_test_score,
            verbose=True,
            allow_delegation=False,
        )

    def final_report_generator_agent(self) -> Agent:
        if not gemini_llm_test_score:
            raise RuntimeError("LLM for TestScoreVerificationAgents not initialized. Cannot create agent.")
        return Agent(
            role="Insightful Test Score Verification Report Synthesizer",
            goal=(
                "You will receive a JSON array string detailing field-by-field comparisons for test scores (GMAT or GRE). This array will *only* contain fields relevant to the specific test type being verified. "
                "Format this into a single, human-readable string report starting with 'Test Score Verification Details:'. "
                "List each field's comparison clearly. "
                "After the field-by-field breakdown, provide a concise 1-2 line 'Overall Feedback'. "
                "This feedback must intelligently summarize the verification outcome, focusing on CRITICAL discrepancies "
                "(e.g., 'Mismatched' relevant scores, `Test_ID__c`, `Email__c`, or significantly different `Test_Date__c` year). "
                "Also highlight if the comparison suggests the document strongly supports the record as 'official' or if there are fields with status 'Potentially Update Record'. "
                "Downplay minor date detail variances if core components align and it's noted. "
                "The feedback should reflect a human-like assessment of whether the document substantially supports and validates the recorded test score information."
            ),
            backstory=(
                "You are a skilled report writer who synthesizes complex GMAT and GRE test score verification data into insightful summaries. " # Backstory updated
                "You can distinguish between critical errors, minor data variations, and situations suggesting record updates based on official documents. You expect to receive data only for fields relevant to the test type."
            ),
            llm=gemini_llm_test_score,
            verbose=True,
            allow_delegation=False,
        )

class TestScoreVerificationTasks:
    def compare_data_and_output_json_task(self, agent: Agent, salesforce_record_data_json_str: str, document_text: str) -> Task:
        return Task(
            description=(
                "Perform a rule-based, comprehensive verification of test score details comparing Salesforce record data against text from an official test score document (GMAT or GRE).\n\n"
                f"**Salesforce Test Score Record Data (JSON String, includes 'RecordTypeName__c' to identify test type 'GRE' or 'GMAT'):**\n```json\n{salesforce_record_data_json_str}\n```\n\n"
                f"**Document Text (Raw Official Test Score Report Text):**\n```text\n{document_text}\n```\n\n"
                "**Your Process & Strict Rules (as per your detailed role and goal description for GMAT/GRE):**\n"
                "1.  **Identify Test Type & Relevant Fields**: Use **'RecordTypeName__c'** from the Salesforce record ('GRE' or 'GMAT') to understand the test structure and determine the specific subset of fields relevant for extraction, comparison, and output, paying attention to differences in scoring components (e.g., Data Insights for GMAT, Analytical Writing's role in GRE vs. GMAT AWA).\n"
                "2.  **Extract/Determine from Document**: For all fields identified as *relevant*, determine their values from the 'Document Text' according to your specific field handling rules (including special logic for Total_Score__c calculation for GRE, and acknowledging GMAT's scaled Total Score).\n"
                "3.  **Apply Verification Rules During Comparison**: Adhere to the defined verification rules for all relevant fields.\n"
                "4.  **Structure Output & Handle Discrepancies**: For each *relevant* field, create a JSON object with all required attributes ('field_name', 'record_value', 'document_value', 'status', 'confidence', 'notes').\n\n"
                "**Final Output of this Task**: A single, valid JSON array string of detailed comparison objects, **containing only the fields identified as relevant to the specific test type (GMAT or GRE).** Ensure all instructions in your goal are meticulously followed."
            ),
            agent=agent,
            expected_output=(
                "A valid JSON array string. Each object details a field's comparison and includes: "
                "'field_name', 'record_value', 'document_value', 'status', 'confidence', 'notes'. "
                "The array must ONLY contain objects for fields relevant to the test type identified by 'RecordTypeName__c'."
            )
        )

    def generate_formatted_report_task(self, agent: Agent, context_tasks: list) -> Task:
        # This task definition remains largely the same as it's about formatting the JSON output
        return Task(
            description=(
                "You will receive a JSON array string (from the previous task's context) detailing field-by-field test score comparisons. This JSON array will *only* contain information for fields relevant to the specific test type. \n"
                "Each object in the array contains 'field_name', 'record_value', 'document_value', 'status', 'confidence', and 'notes'.\n\n"
                "Format this into a single, human-readable string report, starting with 'Test Score Verification Details:'.\n"
                "For each field in the provided JSON, list:\n"
                "- Field: [field_name]\n"
                "  Record Value: [record_value]\n"
                "  Document Value: [document_value]\n"
                "  Status: [status] (Confidence: [confidence])\n"
                "  Notes: [notes]\n\n"
                "After listing all fields, provide a concise 1-2 line 'Overall Feedback'. This feedback should intelligently summarize the outcome, "
                "emphasizing critical mismatches (e.g., `Test_ID__c`, `Email__c`, core scores), or if the document strongly supports the record as 'official' (check notes from comparator). "
                "Explicitly mention if any fields have a status of 'Potentially Update Record' and what those fields are. "
                "The goal is to reflect a human-like assessment of the document's support for the recorded test score and highlight actionable insights."
            ),
            agent=agent,
            context=context_tasks,
            expected_output=(
                "A single string: 'Test Score Verification Details:' section with field-by-field breakdown for relevant fields only, followed by 'Overall Feedback:' (1-2 lines) highlighting critical findings, official status implications, and potential record updates."
            )
        )

class TestScoreVerificationCrewOrchestrator:
    def __init__(self, record_data_dict: Dict[str, Any], document_text: str):
        self.document_text = document_text
        self.agents_provider = TestScoreVerificationAgents()
        self.tasks_provider = TestScoreVerificationTasks()

        self.record_type_name = None
        sf_record_type_keys = SALESFORCE_TO_CANONICAL_MAP.get("RecordTypeName__c", ["RecordTypeName__c"])
        for sf_key in sf_record_type_keys:
            if sf_key in record_data_dict:
                self.record_type_name = record_data_dict[sf_key]
                break
        
        if not self.record_type_name:
            logger.warning(
                "'RecordTypeName__c' (or its mapped Salesforce equivalent) is missing or empty in record_data_dict. "
                "The agent will struggle to determine relevant fields for test score verification."
            )
        elif self.record_type_name not in ["GRE", "GMAT"]:
            logger.warning(
                f"RecordTypeName__c is '{self.record_type_name}', which might not be explicitly handled by the agent's detailed GRE/GMAT logic. "
                "Agent will use general relevance determination."
            )


        processed_record_data = {}
        for canonical_field in TEST_SCORE_VERIFICATION_FIELDS:
            if canonical_field == "RecordTypeName__c":
                processed_record_data[canonical_field] = self.record_type_name
                continue

            value_found = False
            salesforce_keys_for_field = SALESFORCE_TO_CANONICAL_MAP.get(canonical_field, [canonical_field])
            
            for sf_key in salesforce_keys_for_field:
                if sf_key in record_data_dict:
                    processed_record_data[canonical_field] = record_data_dict[sf_key]
                    value_found = True
                    break
            
            if not value_found:
                processed_record_data[canonical_field] = None

        self.salesforce_record_data_json_str = json.dumps(processed_record_data, indent=2)
        
        logger.info(
            f"TestScoreVerificationCrewOrchestrator initialized. Record Type: {self.record_type_name if self.record_type_name else 'Unknown'}. "
            f"Input Document Text Length: {len(document_text)}."
        )

    def run(self) -> str:
        if not gemini_llm_test_score:
            logger.error("TestScoreVerificationCrew cannot run: LLM not initialized.")
            return "Error: LLM for test score verification not available. Please check GOOGLE_API_KEY and ensure Gemini model initialization succeeded."
        try:
            comparator_agent = self.agents_provider.data_comparator_agent()
            report_generator_agent = self.agents_provider.final_report_generator_agent()

            task1_compare_and_structure = self.tasks_provider.compare_data_and_output_json_task(
                agent=comparator_agent,
                salesforce_record_data_json_str=self.salesforce_record_data_json_str,
                document_text=self.document_text
            )
            
            task2_generate_report_str = self.tasks_provider.generate_formatted_report_task(
                agent=report_generator_agent,
                context_tasks=[task1_compare_and_structure]
            )

            crew = Crew(
                agents=[comparator_agent, report_generator_agent],
                tasks=[task1_compare_and_structure, task2_generate_report_str],
                process=Process.sequential,
                verbose=1
            )
            
            final_report_string = crew.kickoff()
            
            logger.info(f"TestScoreVerificationCrew execution completed. Report length: {len(final_report_string if final_report_string else '')}")
            
            if not final_report_string or not isinstance(final_report_string, str):
                logger.error(f"TestScoreVerificationCrew produced an invalid or empty report. Type: {type(final_report_string)}. Output: {str(final_report_string)[:500]}")
                return f"Error: Test Score Verification crew produced an invalid or empty report."

            return final_report_string.strip()

        except Exception as e:
            logger.error(f"TestScoreVerificationCrew execution failed: {e}", exc_info=True)
            return f"Error during test score verification crew processing: {str(e)}"

