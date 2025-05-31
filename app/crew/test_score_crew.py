# project_root/app/crew/test_score_crew.py
import os
import logging
import json
from typing import Dict, Any, List
from crewai import Agent, Task, Crew, Process
from langchain_google_genai import ChatGoogleGenerativeAI

# Import shared configurations
from app.config import GOOGLE_API_KEY, GEMINI_MODEL_NAME

logger = logging.getLogger(__name__)

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


# Comprehensive list of fields. The agent will determine relevance based on Test_Name__c from the record.
TEST_SCORE_VERIFICATION_FIELDS = [
    "Test_Name__c", "Test_Date__c", "Email__c", "Test_ID__c", "Appointment_Number__c",
    "Verbal_Score__c", "Verbal_Percentile__c", "Quantitative_Score__c", "Quantitative_Percentile__c",
    "Total_Score__c", "Total_Percentile__c",
    "Data_Insights_Score__c", "Data_Insights_Percentile__c", # GMAT Focus
    "Integrated_Reasoning_Score__c", # GMAT (non-Focus)
    "Analytical_Writing_Score__c", "Analytical_Writing_Percentile__c" # GMAT (non-Focus) & GRE
]

class TestScoreVerificationAgents:
    def data_comparator_agent(self) -> Agent:
        if not gemini_llm_test_score:
            raise RuntimeError("LLM for TestScoreVerificationAgents not initialized. Cannot create agent.")
        return Agent(
            role="Meticulous Test Score Verification Analyst",
            goal=(
                "You are provided with: "
                "1. Structured Salesforce record data for a Test Score (as a JSON string), which includes 'Test_Name__c' (e.g., 'GMAT Focus Edition', 'GRE General Test', 'GMAT'). "
                "2. Raw text extracted from a supporting official test score document. "
                "Your multi-step goal is to: "
                "  a. Based on the 'Test_Name__c' in the Salesforce record, identify the relevant fields for that specific test type (e.g., GMAT Focus will have Data Insights, older GMAT might have Integrated Reasoning/AWA, GRE will have Analytical Writing). "
                "  b. From the raw document text, meticulously extract values for ALL predefined test score fields that are relevant to the given test type: "
                f"     Fields to consider: `{', '.join(TEST_SCORE_VERIFICATION_FIELDS)}`. "
                "     If a value for a relevant field is not found in the document, use 'Not Found in Document'. "
                "  c. Apply specific verification rules: "
                "     - **Test_Name__c**: Document should confirm the test type stated in the record. Minor variations are acceptable (e.g., 'GMAT Focus Edition' vs 'GMAT FOCUS'). "
                "     - **Key Identifiers**: "
                "         - `Test_ID__c` (GMAT ID for GMAT, GRE Registration Number for GRE): Must match exactly between record and document. "
                "         - `Email__c` (on test report): Must match exactly. "
                "         - `Appointment_Number__c` (primarily for GMAT): Must match exactly if present in both. "
                "     - **Test_Date__c**: Aim for exact match (YYYY-MM-DD). If record has YYYY-MM-DD and document has only YYYY-MM or YYYY, and available components match, status is 'Partially Matched (Detail Variance)'. "
                "     - **Scores & Percentiles** (Verbal, Quantitative, Data Insights, Integrated Reasoning, Analytical Writing, Total - as applicable to the test type): Must match precisely. Allow minor numeric formatting differences (e.g., 99 vs 99.00). "
                "  d. Compare the extracted document values against the Salesforce record data for ALL relevant predefined fields. "
                "  e. For each field, create a JSON object detailing: 'field_name', 'record_value', 'document_value', "
                "     'status' ('Matched', 'Mismatched', 'Partially Matched (Detail Variance)', 'Found in Record Only', 'Found in Document Only', 'Not Found in Either', 'Potentially Update Record'), "
                "     'confidence' (High, Medium, Low), "
                "     'notes' (Crucial for explaining status. E.g., if 'Potentially Update Record', state: 'Record value is X, document shows Y. Other key identifiers and scores align, suggesting record may need update.'). "
                "  f. **'Official Score' Indication & Handling Discrepancies (in 'notes' and 'status'):** "
                "     - If all critical scores, percentiles, and key identifiers (Test_ID__c, Email__c, relevant Appointment_Number__c) match exactly, the notes for `Test_Name__c` (or a summary note) should indicate: 'Document strongly supports the record as official.' "
                "     - If a score/percentile in the record is blank or different from the document, BUT other key identifiers (e.g., Test_ID__c, Email__c) AND a majority of other scores/percentiles match the document, set status for the differing field to 'Potentially Update Record'. Notes must clearly state record value, document value, and why an update might be considered (e.g., 'Record: 700, Document: 710. Test ID and Email match, other section scores align with document. Potential update to record score.'). "
                "     - If critical identifiers (Test_ID__c, Email__c) do not match, this is a major discrepancy, status 'Mismatched' for those identifiers, and overall confidence in the match should be low, even if some scores appear similar. "
                "Output a single, valid JSON array string where each element is an object representing the comparison for one predefined field."
            ),
            backstory=(
                "You are an expert AI system designed for precise and rule-based analysis of standardized test score reports (GMAT, GRE, etc.). "
                "You meticulously extract data, understand the nuances of different test types, apply strict verification rules, and clearly report findings, paying close attention to discrepancies that might require record updates or indicate official score validation."
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
                "You will receive a JSON array string detailing field-by-field comparisons for test scores. "
                "Format this into a single, human-readable string report starting with 'Test Score Verification Details:'. "
                "List each field's comparison clearly. "
                "After the field-by-field breakdown, provide a concise 1-2 line 'Overall Feedback'. "
                "This feedback must intelligently summarize the verification outcome, focusing on CRITICAL discrepancies "
                "(e.g., 'Mismatched' Total_Score__c, Test_Name__c, Test_ID__c, Email__c, or significantly different Test_Date__c year). "
                "Also highlight if the comparison suggests the document strongly supports the record as 'official' or if there are fields with status 'Potentially Update Record'. "
                "Downplay minor date detail variances if core components align and it's noted. "
                "The feedback should reflect a human-like assessment of whether the document substantially supports and validates the recorded test score information."
            ),
            backstory=(
                "You are a skilled report writer who synthesizes complex test score verification data into insightful summaries. "
                "You can distinguish between critical errors, minor data variations, and situations suggesting record updates based on official documents."
            ),
            llm=gemini_llm_test_score,
            verbose=True,
            allow_delegation=False,
        )

class TestScoreVerificationTasks:
    def compare_data_and_output_json_task(self, agent: Agent, salesforce_record_data_json_str: str, document_text: str) -> Task:
        comprehensive_field_list_str = ", ".join(TEST_SCORE_VERIFICATION_FIELDS)
        return Task(
            description=(
                "Perform a rule-based, comprehensive verification of test score details comparing Salesforce record data against text from an official test score document.\n\n"
                f"**Salesforce Test Score Record Data (JSON String, includes 'Test_Name__c' to identify test type like 'GMAT Focus Edition', 'GRE General Test', etc.):**\n```json\n{salesforce_record_data_json_str}\n```\n\n"
                f"**Document Text (Raw Official Test Score Report Text):**\n```text\n{document_text}\n```\n\n"
                f"**Predefined Fields to Consider (extract and compare fields relevant to the specific 'Test_Name__c' from the record):**\n`{comprehensive_field_list_str}`\n\n"
                "**Your Process & Strict Rules:**\n"
                "1.  **Identify Test Type**: Use 'Test_Name__c' from the Salesforce record to understand the test structure (e.g., GMAT Focus, GRE) and determine which fields are relevant for extraction and comparison.\n"
                "2.  **Extract from Document**: For all *relevant* fields, extract values from the 'Document Text'. If a relevant field's value is not found, use 'Not Found in Document'.\n"
                "3.  **Apply Verification Rules During Comparison:**\n"
                "    - **Test_Name__c**: Ensure document confirms the test type in the record. Minor variations (e.g., 'GMAT Focus' vs. 'GMAT Focus Edition') are 'Matched'. Major differences are 'Mismatched'.\n"
                "    - **Key Identifiers** (`Test_ID__c` [GMAT ID/GRE Reg No], `Email__c`, `Appointment_Number__c` [for GMAT]): Must match *exactly*. Status 'Matched' or 'Mismatched'. These are critical for validation.\n"
                "    - **Test_Date__c**: Aim for exact YYYY-MM-DD match. If record is YYYY-MM-DD and doc has YYYY-MM or YYYY with matching parts, status 'Partially Matched (Detail Variance)'. Otherwise, 'Matched' or 'Mismatched'.\n"
                "    - **Scores & Percentiles** (all relevant sections like Verbal, Quantitative, Total, Data Insights, AWA, IR): Must match *precisely*. Allow minor numeric formatting (e.g., 99 vs 99.0 or 99.00). Status 'Matched' or 'Mismatched'.\n"
                "4.  **Structure Output & Handle Discrepancies:** For each *relevant* field, create a JSON object with:\n"
                "    - `field_name`, `record_value`, `document_value`\n"
                "    - `status`: ('Matched', 'Mismatched', 'Partially Matched (Detail Variance)', 'Found in Record Only', 'Found in Document Only', 'Not Found in Either', 'Potentially Update Record')\n"
                "    - `confidence`: ('High', 'Medium', 'Low')\n"
                "    - `notes`: Explain the status. Especially important for:\n"
                "        - **'Potentially Update Record'**: Use this status if a score/percentile in the record is blank or differs from the document, BUT key identifiers (Test_ID__c, Email__c) and a majority of other scores/percentiles *do* match the document. The note must state: 'Record: [value], Document: [value]. Key identifiers and other scores align. Potential update to record from document.'\n"
                "        - **'Official' Indication**: If all critical scores, percentiles, and key identifiers match exactly, the notes for the `Test_Name__c` field should include: 'Document strongly supports the record as official based on matched data.'\n"
                "        - **Critical Mismatches**: If `Test_ID__c` or `Email__c` are 'Mismatched', state this clearly as it heavily impacts overall validation.\n\n"
                "**Final Output of this Task**: A single, valid JSON array string of these detailed comparison objects for all fields relevant to the test type."
            ),
            agent=agent,
            expected_output=(
                "A valid JSON array string. Each object details a field's comparison relevant to the test type: "
                "'field_name', 'record_value', 'document_value', 'status', 'confidence', 'notes' (with specific handling for discrepancies and official indications)."
            )
        )

    def generate_formatted_report_task(self, agent: Agent, context_tasks: list) -> Task:
        return Task(
            description=(
                "You will receive a JSON array string (from the previous task's context) detailing field-by-field test score comparisons. "
                "Each object in the array contains 'field_name', 'record_value', 'document_value', 'status', 'confidence', and 'notes'.\n\n"
                "Format this into a single, human-readable string report, starting with 'Test Score Verification Details:'.\n"
                "For each field, list:\n"
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
                "A single string: 'Test Score Verification Details:' section with field-by-field breakdown, followed by 'Overall Feedback:' (1-2 lines) highlighting critical findings, official status implications, and potential record updates."
            )
        )

class TestScoreVerificationCrewOrchestrator:
    def __init__(self, record_data_dict: Dict[str, Any], document_text: str):
        if 'Test_Name__c' not in record_data_dict or not record_data_dict['Test_Name__c']:
            logger.warning("Test_Name__c is missing or empty in record_data_dict. Agent may struggle to determine relevant fields for test score verification.")
        self.salesforce_record_data_json_str = json.dumps(record_data_dict, indent=2)
        self.document_text = document_text
        self.agents_provider = TestScoreVerificationAgents()
        self.tasks_provider = TestScoreVerificationTasks()
        logger.info(f"TestScoreVerificationCrewOrchestrator initialized for Test Name: {record_data_dict.get('Test_Name__c', 'Unknown')}. Doc length: {len(document_text)}.")

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
                verbose=1 # Consider making verbose level configurable
            )
            
            final_report_string = crew.kickoff()
            
            logger.info(f"TestScoreVerificationCrew execution completed. Report length: {len(final_report_string if final_report_string else '')}")
            
            if not final_report_string or not isinstance(final_report_string, str):
                logger.error(f"TestScoreVerificationCrew produced an invalid or empty report. Type: {type(final_report_string)}")
                raw_output_str = str(final_report_string)
                return f"Error: Test Score Verification crew produced an invalid report. Raw output snippet: {raw_output_str[:200]}..."

            return final_report_string.strip()

        except Exception as e:
            logger.error(f"TestScoreVerificationCrew execution failed: {e}", exc_info=True)
            return f"Error during test score verification crew processing: {str(e)}"
