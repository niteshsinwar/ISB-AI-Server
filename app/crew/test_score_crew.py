# project_root/app/crew/test_score_crew.py
import os
import logging
import json
from typing import Dict, Any, List
from crewai import Agent, Task, Crew, Process
from langchain_google_genai import ChatGoogleGenerativeAI

# Import shared configurations
try:
    from app.config import GOOGLE_API_KEY, GEMINI_MODEL_NAME
except ImportError:
    GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
    GEMINI_MODEL_NAME = os.environ.get("GEMINI_MODEL_NAME", "gemini-pro")
    if not GOOGLE_API_KEY:
        print("Warning: GOOGLE_API_KEY not found in app.config or environment variables.")

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# --- Configuration for Fields to Exclude from Agent Processing ---
FIELDS_TO_EXCLUDE_FROM_PROCESSING: List[str] = [
    'Applicant__c',
    'type',
    'Contact',
    'recordId',
    # 'RecordTypeName__c' should NOT be in this list as it's critical for logic,
    # it will be handled specially.
]

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
    logger.critical("TEST_SCORE_CREW: GOOGLE_API_KEY environment variable not set. LLM will not be available.")


class TestScoreVerificationAgents:
    def data_comparator_agent(self, verifiable_apex_field_names: List[str]) -> Agent:
        if not gemini_llm_test_score:
            raise RuntimeError("LLM for TestScoreVerificationAgents not initialized. Cannot create agent.")

        apex_field_list_str_for_prompt = ", ".join([f"'{field}'" for field in verifiable_apex_field_names])
        if not apex_field_list_str_for_prompt:
             apex_field_list_str_for_prompt = "an empty list (no verifiable fields after initial filtering and special handling)"


        agent_prompt = f"""
You are a Meticulous Test Score Verification Analyst specializing in GMAT and GRE scores, possessing human-like intuition and intelligence.
Your goal is to verify test score details from an official document against a Salesforce record.
Most Important: Ignore Case Differences, Formatting Variations. The Record Data you receive has already had some system-level, non-verifiable fields removed. Focus your verification on the fields present in the provided Record Data and listed as 'Apex Field Names To Process'. 'RecordTypeName__c' will always be present in the full Record Data you use internally.

**Input You Receive:**
1.  **Record Data (JSON String)**: Data from Apex, accessible via `salesforce_record_data_json_str`. This data contains ALL original Apex fields, including 'RecordTypeName__c'.
2.  **Document Text (Raw Official Test Score Report Text)**.
3.  **List of Apex Field Names To Process**: This is the definitive list of fields from the Record Data (excluding 'RecordTypeName__c' which you handle specially, and other general exclusions) that you should consider for other canonical concepts or custom fields. It is provided in the task description as `verifiable_apex_field_list_str` (e.g., {apex_field_list_str_for_prompt}).

**Core Test Score Concepts & Specific Verification Rules:**
You understand the following canonical test score concepts. Your final JSON output should *only* include objects for those concepts you determine are relevant to the specific test type (GMAT or GRE) being verified, after processing them in the order listed.

1.  **Canonical Concept: 'RecordTypeName__c' (Test Type Identifier)**
    * *Apex Input Field Name*: This will ALWAYS be **'RecordTypeName__c'** in the input Record Data (JSON string).
    * *Verification Rule*: This is CRUCIAL.
        * The record's test type is directly available from the 'RecordTypeName__c' field in the full Record Data.
        * Corroborate this by analyzing the document text for keywords (e.g., 'GRE General Test', 'GMAT Exam', 'Educational Testing Service', 'GMAC').
        * 'document_value' should be the inferred document test type (e.g., "GRE (inferred from document)").
        * 'status' reflects if the record's test type aligns with the document ('Matched'), differs ('Mismatched'), or is unclear.
        * **The determined actual test type from this step (let's call it 'Determined Test Type') governs the relevance and processing of ALL other score fields.**

(Once 'Determined Test Type' is established, process other relevant canonical concepts)

Common Identifying Information (generally relevant for both test types):
2.  **Canonical Concept: 'Test Date'**
    * *Typical Apex Input Field Names*: 'hed__Test_Date__c', 'Test_Date__c', 'ExamDate', 'DateOfTest'.
    * *Verification Rule*: Aim for exact match (YYYY-MM-DD). Handle partial matches (e.g., month/year only) as 'Partially Matched (Detail Variance)'.
3.  **Canonical Concept: 'Email Address'**
    * *Typical Apex Input Field Names*: 'Email__c', 'RegistrantEmail', 'CandidateEmail'.
    * *Verification Rule*: Must match exactly if present in both record and document.
4.  **Canonical Concept: 'Test ID'** (e.g., Registration Number, Appointment Number)
    * *Typical Apex Input Field Names*: 'Test_ID__c', 'RegistrationNumber', 'AppointmentID', 'GMATTestID', 'GRERegID', 'Test_Score__r.Test_ID__c'.
    * *Verification Rule*: Must match exactly if present in both record and document.

Core Score Components (relevance and interpretation depend on 'Determined Test Type'):
5.  **Canonical Concept: 'Verbal Score'**
    * *Typical Apex Input Field Names*: 'VerbalScore__c', 'Verbal_Score__c', 'VerbalReasoningScore'.
    * *Relevance*: Relevant for both GMAT and GRE. *Rule*: Must match precisely.
6.  **Canonical Concept: 'Verbal Percentile'**
    * *Typical Apex Input Field Names*: 'VerbalPercentile__c', 'Verbal_Percentile__c', 'VerbalRank'.
    * *Relevance*: Relevant for both GMAT and GRE. *Rule*: Must match precisely.
7.  **Canonical Concept: 'Quantitative Score'**
    * *Typical Apex Input Field Names*: 'QuantScore__c', 'Quantitative_Score__c', 'QuantitativeReasoningScore'.
    * *Relevance*: Relevant for both GMAT and GRE. *Rule*: Must match precisely.
8.  **Canonical Concept: 'Quantitative Percentile'**
    * *Typical Apex Input Field Names*: 'QuantPercentile__c', 'Quantitative_Percentile__c', 'QuantRank'.
    * *Relevance*: Relevant for both GMAT and GRE. *Rule*: Must match precisely.
9.  **Canonical Concept: 'Data Insights Score'**
    * *Typical Apex Input Field Names*: 'Data_Insights_Score__c', 'DataInsightsScore', 'DI_Score', 'Data_Insights_score__c'.
    * *Relevance*: **GMAT specific.** For GRE, generally 'Not Applicable'. *Rule*: If relevant, must match precisely.
10. **Canonical Concept: 'Data Insights Percentile'**
    * *Typical Apex Input Field Names*: 'Data_Insights_Percentile__c', 'DataInsightsPercentile', 'DI_Percentile'.
    * *Relevance*: **GMAT specific.** For GRE, 'Not Applicable'. *Rule*: If relevant, must match precisely.
11. **Canonical Concept: 'Analytical Writing Score'** (AWA)
    * *Typical Apex Input Field Names*: 'Analytical_Score__c', 'Analytical_Writing_Score__c', 'AWAScore', 'EssayScore'.
    * *Relevance*: GRE (scored 0-6), GMAT (AWA scored 0-6, separate from main total). *Rule*: If relevant, must match precisely.
12. **Canonical Concept: 'Analytical Writing Percentile'**
    * *Typical Apex Input Field Names*: 'Analytical_Percentile__c', 'Analytical_Writing_Percentile__c', 'AWAPercentile'.
    * *Relevance*: Same as 'Analytical Writing Score'. *Rule*: If relevant, must match precisely.
13. **Canonical Concept: 'Total Score'**
    * *Typical Apex Input Field Names*: 'Total_Score__c', 'OverallScore', 'CompositeScore'.
    * *Relevance*: Both GMAT & GRE.
    * *Document Value & Verification*:
        1. Find explicit Total Score in document.
        2. If 'Determined Test Type' is 'GRE' AND explicit Total NOT found: 'document_value' = (Doc Verbal Score + Doc Quant Score). Note 'Calculated...'. Else 'Not Found'.
        3. If 'Determined Test Type' is 'GMAT' AND explicit Total NOT found: 'document_value' is 'Not Found'. Note 'GMAT Total Score is scaled...'.
        4. Compare determined 'document_value' with record's 'Total Score'.
14. **Canonical Concept: 'Total Percentile'**
    * *Typical Apex Input Field Names*: 'Total_Percentile__c', 'OverallPercentile', 'CompositeRank'.
    * *Relevance*: Both GMAT & GRE. *Rule*: Must match precisely.

**Your Verification Process & Output Structure:**
Let `processed_apex_fields` be a set to track mapped Apex fields from the **List of Apex Field Names To Process**.
Let `results_list` be an empty list for output objects.
Let `determined_test_type` be a variable to store the type from Part 1.

**Part 1: Determine 'RecordTypeName__c' (Test Type Identifier) and 'Determined Test Type'**
   - Process the **'RecordTypeName__c'** Canonical Concept:
     A.  **Find Record Value**:
         * The `record_value` is the value of the **'RecordTypeName__c'** field from the input Record Data (JSON string). You must parse the JSON to get this value.
         * Set `original_apex_field_name` to 'RecordTypeName__c'.
         * If 'RecordTypeName__c' is not present in the Record Data or its value is null/empty, this is a critical error. Set `record_value` to 'MISSING CRITICAL RecordTypeName__c', status 'Error', and notes explaining this. `determined_test_type` would be 'Unknown'.
     B.  **Apply Verification Rule**: Follow rule to analyze document, determine `document_value` for test type, and set `determined_test_type` based on the record's 'RecordTypeName__c' value and document corroboration.
     C.  **Construct Output Object**: Create JSON object for 'RecordTypeName__c'. Add to `results_list`.
     D.  Add 'RecordTypeName__c' to `processed_apex_fields` if it happened to be in the **List of Apex Field Names To Process** (it generally won't be if that list is pre-filtered specifically to exclude it for iterative purposes, but this is a safeguard).

**Part 2: Process Other Canonical Concepts (In Order)**
   - For EACH of the *other* Canonical Concepts (from 'Test Date' onwards), in order:
     A.  **Find Record Value**:
         * Examine its 'Typical Apex Input Field Names'. Check if any are in the **List of Apex Field Names To Process** AND NOT in `processed_apex_fields`.
         * If match (first unprocessed): `record_value` = value from Record Data using the matched Apex field name. `original_apex_field_name` = matched Apex field. Add to `processed_apex_fields`.
         * Else: `record_value` = 'Not Provided in Record'. `original_apex_field_name` = 'N/A'.
     B.  **Check Relevance**: Based on `determined_test_type` (from Part 1), is this Canonical Concept relevant? (e.g., 'Data Insights Score' for GMAT). Key identifiers ('Test Date', 'Email Address', 'Test ID') are generally always relevant if data exists or is expected.
     C.  **Extract/Determine Document Value & Verify (If Relevant)**:
         * If relevant: Determine `document_value` (extract or calculate per rule). If not found, 'Not Found in Document'. Apply Specific Verification Rule. Determine `status`, `confidence`.
         * If not relevant: `document_value` = 'Not Applicable (Test Type is [determined_test_type])', `status` = 'Not Applicable', `confidence` = 'High'.
     D.  **Construct & Conditionally Add Output Object**: Create JSON object. **Add to `results_list` ONLY IF Step B determined it was RELEVANT for `determined_test_type`** (or if it's a key identifier like Test Date, Email, Test ID where presence/absence itself is a finding).

**Part 3: Handle Unmapped Custom Apex Fields (Fields from the 'List of Apex Field Names To Process' that weren't mapped)**
    - Iterate through each `apex_field_name` in the **List of Apex Field Names To Process**.
    - If `apex_field_name` NOT in `processed_apex_fields`:
        * This is custom. Construct object:
            * `'field_name'`: `apex_field_name`. `'original_apex_field_name'`: same.
            * `'record_value'`: from Record Data.
            * `'document_value'`: 'Not Applicable (Custom Field)' or generic extraction.
            * `'status'`: 'Info Extracted (Custom Field)' or 'Needs Manual Review (Custom Field)'.
            * `'notes'`: "Processed as a custom field."
        * Add to `results_list`.

**Part 4: 'Official Score' Indication (Update notes of 'RecordTypeName__c' or add a summary object if easier)**
    - Based on the matching status of *relevant* critical scores and identifiers in `results_list`, update the notes for the 'RecordTypeName__c' concept (or add a specific summary object to `results_list` if that's easier for you to structure) to indicate if the document strongly supports the record, or if there are 'Potentially Update Record' statuses for relevant fields.

**Final Output**: Return `results_list` as a single, valid JSON array. Output ONLY includes RELEVANT Canonical Concepts for the Determined Test Type, plus any custom fields.
"""
        return Agent(
            role="Advanced Test Score Verification Analyst for GMAT/GRE with Prioritized Mapping",
            goal=agent_prompt,
            backstory=(
                "You are an AI expert at verifying GMAT and GRE test scores. "
                "You map varied field names (from a pre-filtered set) to known concepts using typical names first, apply test-type specific logic for relevance and score calculation (knowing 'RecordTypeName__c' is fixed and in the full JSON), and filter output to only relevant fields."
            ),
            llm=gemini_llm_test_score,
            verbose=True,
            allow_delegation=False,
        )

    def final_report_generator_agent(self) -> Agent:
        if not gemini_llm_test_score:
            raise RuntimeError("LLM for TestScoreVerificationAgents not initialized. Cannot create agent.")
        return Agent(
            role="Insightful Test Score Verification Report Synthesizer for GMAT/GRE",
            goal=(
                "You will receive a JSON array string detailing field-by-field comparisons for GMAT or GRE test scores. This array *only* contains fields relevant to the specific test type verified, plus any custom fields found. "
                "Most Important: Ignore Case Differences, Formatting Variations, and DO NOT Include Field Like Record Id\n"
                "Each object includes 'field_name' (a canonical concept or original Apex name), 'original_apex_field_name', and other verification details.\n"
                "Format this into a single, human-readable string report starting with 'Test Score Verification Details:'.\n"
                "The first item should clearly state the determined test type (e.g., from the 'RecordTypeName__c' field's notes or value).\n"
                "Then, for each field from the JSON, list:\n"
                "- Field: [field_name] (Original Apex Field: [original_apex_field_name])\n"
                "  Record Value: [record_value]\n"
                "  Document Value: [document_value]\n"
                "  Status: [status] (Confidence: [confidence])\n"
                "  Notes: [notes]\n\n"
                "After the field-by-field breakdown, provide a concise 1-2 line 'Overall Feedback'. "
                "This feedback must intelligently summarize the verification outcome, focusing on CRITICAL discrepancies for the *relevant* sections of the identified test type (e.g., 'Mismatched' relevant scores, `Test ID`, `Email Address`, or significantly different `Test Date` year). "
                "Also highlight if the comparison suggests the document strongly supports the record as 'official' or if there are fields with status 'Potentially Update Record'. "
                "Acknowledge the test type (GMAT/GRE) in your summary."
            ),
            backstory=(
                "You are a skilled report writer, synthesizing GMAT/GRE test score verification data into clear summaries. "
                "You focus on materiality, distinguishing critical errors from minor variations, and understand that only test-type relevant fields (plus custom) are presented to you. You highlight actionable insights."
            ),
            llm=gemini_llm_test_score,
            verbose=True,
            allow_delegation=False,
        )

class TestScoreVerificationTasks:
    def compare_data_and_output_json_task(self, agent: Agent, salesforce_record_data_json_str: str, document_text: str, verifiable_apex_field_list_str: str) -> Task:
        return Task(
            description=(
                "Perform rule-based verification of GMAT/GRE test scores. The test type is identified directly from the 'RecordTypeName__c' field in the full Salesforce record data. Then, map other received Apex fields (from the 'List of Verifiable Apex Field Names') to Canonical Concepts, apply test-type specific logic for relevance and score calculation. Handle unmapped fields as custom.\n\n"
                f"**Salesforce Record Data (JSON String from Apex - this is the FULL original data, ensuring 'RecordTypeName__c' is available):**\n```json\n{salesforce_record_data_json_str}\n```\n\n"
                f"**Document Text (Raw Official Test Score Report Text):**\n```text\n{document_text}\n```\n\n"
                f"**List of Verifiable Apex Field Names To Process (this list has been pre-filtered, e.g., excludes RecordTypeName__c for *this specific listing*, but agent knows to use RecordTypeName__c from full data):** {verifiable_apex_field_list_str}\n\n"
                "**Your Process & Strict Rules (as per your agent goal for GMAT/GRE analysis):**\n"
                "1.  **Determine Test Type**: Directly use 'RecordTypeName__c' from the full Record Data (JSON string). This dictates further logic.\n"
                "2.  **Process Canonical Concepts**: For each, map known Apex fields from the provided 'List of Verifiable Apex Field Names To Process'. Determine relevance by test type. If relevant, verify. If not, mark 'Not Applicable'.\n"
                "3.  **Handle Unmapped Fields**: Process remaining fields from the 'List of Verifiable Apex Field Names To Process' as custom.\n"
                "4.  **Structure Output**: For each concept/field processed, create JSON object. The final array should ONLY contain objects for RELEVANT canonical concepts for the determined test type, plus any custom fields.\n\n"
                "**Final Output of this Task**: A single, valid JSON array string of comparison objects, **containing ONLY the fields identified as relevant to the specific test type (GMAT or GRE), plus any custom fields processed.**"
            ),
            agent=agent,
            expected_output=(
                "A valid JSON array string. Each object details a field's comparison: "
                "'field_name' (canonical concept name or original Apex name for custom), 'original_apex_field_name', "
                "'record_value', 'document_value', 'status', 'confidence', 'notes'. "
                "The array must ONLY contain objects for fields relevant to the test type determined from 'RecordTypeName__c', plus any processed custom fields."
            )
        )

    def generate_formatted_report_task(self, agent: Agent, context_tasks: list) -> Task:
        return Task(
            description=(
                "You will receive a JSON array string (from context) detailing field-by-field GMAT/GRE test score comparisons. This JSON *only* contains fields relevant to the specific test type verified, plus any custom fields.\n"
                "Transform this JSON into a single, human-readable string report as per your agent's goal. "
                "Ensure your report displays both 'field_name' (canonical concept/custom) and 'original_apex_field_name' for each item. "
                "Your 'Overall Feedback' should be insightful, reflecting the GMAT/GRE context, critical findings, and any 'Potentially Update Record' suggestions."
            ),
            agent=agent,
            context=context_tasks,
            expected_output=(
                "A single string: 'Test Score Verification Details:' section (clearly stating GMAT/GRE type) with field-by-field breakdown for relevant/custom fields only (showing canonical/custom and original Apex names), followed by 'Overall Feedback:'."
            )
        )

class TestScoreVerificationCrewOrchestrator:
    def __init__(self, record_data_dict: Dict[str, Any], document_text: str):
        # Store the original dictionary to ensure 'RecordTypeName__c' is always available if present
        self.original_record_data_dict = record_data_dict.copy()

        # Create a working copy for general processing and filtering for the iterative parts of the agent's logic
        processed_dict_for_iteration = self.original_record_data_dict.copy()
        for field_to_remove in FIELDS_TO_EXCLUDE_FROM_PROCESSING:
            if field_to_remove in processed_dict_for_iteration:
                del processed_dict_for_iteration[field_to_remove]

        # Specifically remove RecordTypeName__c from this iterative list because it's handled uniquely by the agent.
        # The agent will access RecordTypeName__c directly from the full JSON string.
        if 'RecordTypeName__c' in processed_dict_for_iteration:
            del processed_dict_for_iteration['RecordTypeName__c']

        # This list is for the agent to iterate over for Parts 2 & 3 of its logic
        self.verifiable_apex_field_names_for_iteration: List[str] = list(processed_dict_for_iteration.keys())

        # The JSON string passed to the agent should be from the original_record_data_dict
        # so it has access to 'RecordTypeName__c' and all other original fields.
        self.salesforce_record_data_json_str = json.dumps(self.original_record_data_dict, indent=2)
        self.document_text = document_text
        self.agents_provider = TestScoreVerificationAgents()
        self.tasks_provider = TestScoreVerificationTasks()


        record_type_name_from_payload = self.original_record_data_dict.get('RecordTypeName__c', "Unknown (Field 'RecordTypeName__c' not in original payload)")

        logger.info(
            f"TestScoreVerificationCrewOrchestrator initialized. "
            f"Original Apex fields received: {list(self.original_record_data_dict.keys())}. "
            f"Record Type from payload ('RecordTypeName__c'): {record_type_name_from_payload}. Agent will use this directly. "
            f"Fields for iterative processing by agent (after exclusions and special handling of RecordTypeName__c): {self.verifiable_apex_field_names_for_iteration}. "
            f"Input Document Text Length: {len(document_text)}."
        )
        logger.debug(f"Salesforce Record (FULL ORIGINAL DATA passed as JSON) for Agent: {self.salesforce_record_data_json_str}")


    def run(self) -> str:
        if not gemini_llm_test_score:
            logger.error("TestScoreVerificationCrew cannot run: LLM not initialized.")
            return "Error: LLM for test score verification not available. Please check GOOGLE_API_KEY."

        # Check for RecordTypeName__c in the original data
        if 'RecordTypeName__c' not in self.original_record_data_dict or not self.original_record_data_dict.get('RecordTypeName__c'):
             logger.error("TestScoreVerificationCrew: Critical field 'RecordTypeName__c' is missing or empty in the Apex payload. Cannot determine test type.")
             return "Error: Test type identifier ('RecordTypeName__c') missing from input data. Verification cannot proceed."

        # Check if the original payload itself was empty (even before filtering for iteration)
        if not self.original_record_data_dict: # Check if the original dictionary was empty
            logger.warning("TestScoreVerificationCrew: No fields received in Apex payload (original_record_data_dict is empty). Returning empty report.")
            return "Test Score Verification Details:\n\nNo data provided from Salesforce record for verification.\n\nOverall Feedback: No data to verify."
        # The case where only RecordTypeName__c is present (and verifiable_apex_field_names_for_iteration is empty) is handled by the agent
        # as it will process RecordTypeName__c and then find no other fields for Parts 2 & 3.

        try:
            comparator_agent = self.agents_provider.data_comparator_agent(
                verifiable_apex_field_names=self.verifiable_apex_field_names_for_iteration
            )
            report_generator_agent = self.agents_provider.final_report_generator_agent()

            verifiable_apex_field_list_str_for_task = ", ".join([f"'{f}'" for f in self.verifiable_apex_field_names_for_iteration])
            if not verifiable_apex_field_list_str_for_task:
                verifiable_apex_field_list_str_for_task = "none (other than RecordTypeName__c which is handled specially by the agent)"


            task1_compare_and_structure = self.tasks_provider.compare_data_and_output_json_task(
                agent=comparator_agent,
                salesforce_record_data_json_str=self.salesforce_record_data_json_str, # Full JSON from original_record_data_dict
                document_text=self.document_text,
                verifiable_apex_field_list_str=verifiable_apex_field_list_str_for_task
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
                return f"Error: Test Score Verification crew produced an invalid or empty report. Raw output: {str(final_report_string)[:200]}..."


            return final_report_string.strip()

        except Exception as e:
            logger.error(f"TestScoreVerificationCrew execution failed: {e}", exc_info=True)
            return f"Error during test score verification crew processing: {str(e)}"