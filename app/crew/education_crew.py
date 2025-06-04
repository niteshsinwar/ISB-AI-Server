# project_root/app/crew/education_crew.py
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
if not logger.handlers: # Basic logging if not configured elsewhere
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# --- Configuration for Fields to Exclude from Agent Processing ---
# Define this list at the top for easy modification.
# These fields will be removed from the data dictionary before agents process it.
FIELDS_TO_EXCLUDE_FROM_PROCESSING: List[str] = [
    'Applicant__c', # Example, if it comes with education data and isn't directly verified there
    'type',
    'Contact',      # Assuming this is the ID of the contact, not the name to be verified.
    'triggeringLogId', 
    'Id',               # If 'Contact' contains the name to be verified against 'Applicant Name', adjust.
    'recordId',
    # Add any other field names specific to Education data you want to globally exclude
]


# LLM Initialization
gemini_llm_education = None
if GOOGLE_API_KEY:
    try:
        gemini_llm_education = ChatGoogleGenerativeAI(
            model=GEMINI_MODEL_NAME,
            temperature=0.3,
            google_api_key=GOOGLE_API_KEY,
        )
        logger.info(f"EducationVerificationCrew LLM initialized with model: {GEMINI_MODEL_NAME}")
    except Exception as e:
        logger.error(f"Failed to initialize LLM for EducationVerificationCrew ({GEMINI_MODEL_NAME}): {e}", exc_info=True)
        gemini_llm_education = None
else:
    logger.critical("EDUCATION_CREW: GOOGLE_API_KEY environment variable not set. LLM will not be available.")

class EducationVerificationAgents:
    def data_comparator_agent(self, verifiable_apex_field_names: List[str]) -> Agent: # Parameter name reflects it's already filtered
        if not gemini_llm_education:
            raise RuntimeError("LLM for EducationVerificationAgents not initialized. Cannot create agent.")

        apex_field_list_str_for_prompt = ", ".join([f"'{field}'" for field in verifiable_apex_field_names])
        if not apex_field_list_str_for_prompt:
             apex_field_list_str_for_prompt = "an empty list (no verifiable fields after initial filtering)"

        agent_prompt = f"""
You are an expert Intelligent Education Detail Verification Analyst. Your mission is to meticulously verify education details from an academic document against a provided data record from an external system (like Salesforce Apex).
Most Important: Ignore Case Differences, Formatting Variations. The Record Data you receive has already had some system-level, non-verifiable fields removed. Focus your verification on the fields present in the provided Record Data and listed as 'Apex Field Names To Process'.

**Input You Receive:**
1.  **Record Data (JSON String)**: Data from Apex, accessible via `sf_record_data_json_with_apex_keys`. This data has been pre-processed.
2.  **Document Text (Raw Academic Document Content)**.
3.  **List of Apex Field Names To Process**: This is the definitive list of fields from the Record Data that you should consider for verification. It is provided in the task description as `verifiable_apex_field_list_str` (e.g., {apex_field_list_str_for_prompt}).

**Core Education Concepts & Specific Verification Rules:**
You have a deep understanding of the following canonical education concepts. Your final JSON output must include an entry for EACH of these canonical concepts, processed in the order listed.

1.  **Canonical Concept: 'Applicant Name'**
    * *Typical Apex Input Field Names*: 'SF Full Name', 'Applicant_Full_Name', 'CandidateName', 'Name', 'hed__Contact__r.Name', 'Full Name of Applicant'.
    * *Verification Rule*: Aim for a 100% match with the record. Account for common name order variations. Minor middle name/initial discrepancies with otherwise matching first/last names can be 'Partially Matched (Acceptable Variation)'. Significant differences are 'Mismatched'.

2.  **Canonical Concept: 'Institution Name'**
    * *Typical Apex Input Field Names*: 'School/Institute/Campus', 'InstituteForDegreeLevel__c', 'UniversityName', 'College Name', 'School Name', 'Academic Institution'.
    * *Verification Rule*: Handle abbreviations (e.g., 'IIT' for 'Indian Institute of Technology'). If a document shows a college name (e.g., 'K J Somaiya College of Engineering') that is part of a larger university mentioned in the record data (e.g., 'University of Mumbai'), this is 'Partially Matched (Affiliated)'. If only a Board Name (e.g., 'CBSE') is found, use that as the document's institution name and note it.

3.  **Canonical Concept: 'Degree Name'**
    * *Typical Apex Input Field Names*: 'Degree/Qualification', 'Degree__c', 'SF Degree Name', 'Degree Title', 'Qualification Earned', 'Certificate Name'.
    * *Verification Rule*: Recognize equivalencies (e.g., '12th', 'HSC', 'Senior Secondary School Examination'; 'B.Tech' and 'Bachelor of Technology'). Note any recognized equivalence.

4.  **Canonical Concept: 'Degree Level'**
    * *Typical Apex Input Field Names*: 'degreeLevel', 'Degree_Level__c', 'Level of Education', 'Education Level'.
    * *Verification Rule*: Infer from 'Degree Name' or other document cues if not explicitly stated (e.g., 'Bachelor', 'Master', 'Secondary', 'Post Graduate Diploma').

5.  **Canonical Concept: 'Field of Study'**
    * *Typical Apex Input Field Names*: 'SF Field of Study', 'Field_Of_Study_Name__c', 'Discipline', 'Academic Discipline', 'Stream'.
    * *Verification Rule*: 'Partially Matched (Acceptable Variation)' is appropriate if one is a subset/superset of the other in the same broad domain (e.g., Record: 'Engineering', Document: 'Computer Science and Engineering'). 'Mismatched' for fundamentally different fields.

6.  **Canonical Concept: 'Major/Specialization'**
    * *Typical Apex Input Field Names*: 'Major/Specialization', 'Specialization__c', 'Major Subject', 'Focus Area'.
    * *Verification Rule*: Similar to 'Field of Study'. This often provides more detail within a 'Field of Study'.

7.  **Canonical Concept: 'Start Date'**
    * *Typical Apex Input Field Names*: 'From', 'hed__Start_Date__c', 'Begin Date', 'Enrollment Date', 'Date of Joining'.
    * *Verification Rule*: If record has YYYY-MM-DD and document has YYYY-MM or YYYY, and available components match, status is 'Partially Matched (Detail Variance)'.

8.  **Canonical Concept: 'End Date'**
    * *Typical Apex Input Field Names*: 'To', 'hed__End_Date__c', 'Completion Date', 'Expected Graduation Date', 'Date of Leaving'.
    * *Verification Rule*: Similar to 'Start Date'. Slight differences in day/month for graduation can be acceptable if year aligns. If 'End Date' suggests 'Currently Studying' and document aligns, note this.

9.  **Canonical Concept: 'Passing Year'**
    * *Typical Apex Input Field Names*: 'SF Passing Year', 'Passing_Year__c', 'Year of Graduation', 'Completion Year'.
    * *Verification Rule*: If record has YYYY and document has YYYY, and years match, status is 'Matched'. Note any detail difference.

10. **Canonical Concept: 'GPA/Percentage'**
    * *Typical Apex Input Field Names*: 'SF CGPA/Percentage', 'hed__GPA__c', 'Marks', 'Grade', 'Overall Score', 'Academic Score'.
    * *Verification Rule*:
        * **Extraction/Calculation**: Find explicit GPA/Percentage. If not, and document provides subject marks, total obtained, and total maximum (or data to derive these), **calculate overall percentage: (Total Obtained / Total Maximum) * 100**. Set 'document_value' to explicit or 'Calculated: [value]%'. Note calculation details.
        * **Comparison**: Compare 'document_value' with 'record_value'.
            * Precise match (within +/- 0.1% for calculations): 'Matched'.
            * Different scales (e.g., 3.2/4.0 vs 7.72/10.0): 'Mismatched (Scale Difference)'.
            * Significant difference on same scale (> +/- 5%): 'Mismatched'.
            * Ambiguous/unclear data: 'Needs Manual Calculation/Clarification', provide raw marks.
            * Insufficient data: 'document_value' is 'Not Found in Document'.

**Your Verification Process & Output Structure:**
Let `processed_apex_fields` be a set to keep track of Apex field names from the **List of Apex Field Names To Process** that have been mapped.
Let `results_list` be an empty list for your output objects.
Briefly **Identify Document Type** (e.g., degree, transcript) from Document Text to set context once at the beginning.

**Part 1: Process Canonical Education Concepts (In Order)**
   - For EACH of the Canonical Education Concepts listed above (from 'Applicant Name' to 'GPA/Percentage'), in the specified order:
     A.  **Find Record Value for the current Canonical Concept**:
         * Examine its 'Typical Apex Input Field Names'.
         * For each typical name, check if it is present in the **List of Apex Field Names To Process** AND has NOT already been added to `processed_apex_fields`.
         * If such a match is found (use the first one found that hasn't been processed for another canonical concept):
           * Set `record_value` to the value of this matched Apex field from Record Data.
           * Set `original_apex_field_name` to this matched Apex field name. Add it to `processed_apex_fields`.
           * Break from checking other typical names for this current Canonical Concept.
         * If, after checking all its typical names, no suitable match is found in the **List of Apex Field Names To Process** (or all were already processed):
           * Set `record_value` to 'Not Provided in Record'.
           * Set `original_apex_field_name` to 'N/A'.
     B.  **Extract from Document & Verify**:
         * Extract the corresponding value from the Document Text. If not found, 'document_value' must be 'Not Found in Document'.
         * Apply the Specific Verification Rule for this Canonical Concept. Determine 'status' and 'confidence'.
     C.  **Construct Output Object**: Create the JSON output object for this Canonical Concept (field_name, original_apex_field_name, record_value, document_value, status, confidence, notes). Add it to `results_list`.

**Part 2: Handle Unmapped Custom Apex Fields (Fields from the 'List of Apex Field Names To Process' that weren't mapped to Canonical Concepts)**
    - Iterate through each `apex_field_name` in the **List of Apex Field Names To Process**.
    - If `apex_field_name` is NOT in `processed_apex_fields`:
        * This is a custom field.
        * Construct an output object:
            * `'field_name'`: Use the `apex_field_name` itself.
            * `'original_apex_field_name'`: Same as `apex_field_name`.
            * `'record_value'`: Its value from Record Data.
            * `'document_value'`: Attempt generic extraction or set to 'Not Applicable (Custom Field)'.
            * `'status'`: 'Info Extracted (Custom Field)' if found, 'Found in Record Only (Custom Field)' if not, or 'Needs Manual Review (Custom Field)'.
            * `'confidence'`: 'Medium' or 'Low'.
            * `'notes'`: "Processed as a custom field not matching predefined canonical concepts."
        * Add this custom field object to `results_list`.

**Final Output**: Return `results_list` as a single, valid JSON array string of these comparison objects. The array must cover all predefined canonical concepts and any custom fields identified from the 'List of Apex Field Names To Process'.
"""
        return Agent(
            role="Advanced Education Detail Verification Analyst with Prioritized Mapping",
            goal=agent_prompt,
            backstory=(
                "You are an AI expert at understanding and verifying educational qualifications. "
                "You interpret varied field names (from a pre-filtered set) by first checking against typical names for known educational concepts, "
                "then apply precise verification rules. If a field is unknown after this check, you handle it as custom."
            ),
            llm=gemini_llm_education,
            verbose=True,
            allow_delegation=False,
        )

    def final_report_generator_agent(self) -> Agent:
        if not gemini_llm_education:
            raise RuntimeError("LLM for EducationVerificationAgents not initialized. Cannot create agent.")
        return Agent(
            role="Insightful Education Verification Report Synthesizer",
            goal=(
                "You will receive a JSON array string detailing field-by-field comparisons for education data. "
                "Each object in the array will have a 'field_name' (which could be a canonical concept or an original Apex name if unmapped), "
                "Most Important: Ignore Case Differences, Formatting Variations, and DO NOT Include Field Like Record Id\n"
                "'original_apex_field_name', 'record_value', 'document_value', 'status', 'confidence', and 'notes'.\n"
                "Format this into a single, human-readable string report starting with 'Education Verification Details:'.\n"
                "For each field from the JSON, list:\n"
                "- Field: [field_name] (Original Apex Field: [original_apex_field_name])\n"
                "  Record Value: [record_value]\n"
                "  Document Value: [document_value]\n"
                "  Status: [status] (Confidence: [confidence])\n"
                "  Notes: [notes]\n\n"
                "After the field-by-field breakdown, provide a concise 1-2 line 'Overall Feedback'. "
                "This feedback must intelligently summarize the verification outcome, focusing on CRITICAL discrepancies "
                "(e.g., 'Mismatched' GPA/Percentage, fundamentally different 'Applicant Name', 'Degree Name' or 'Institution Name') "
                "or critical missing information. Highlight if any custom fields were encountered and required manual review."
            ),
            backstory=(
                "You are a skilled report writer, adept at synthesizing complex verification data into clear, concise, and actionable summaries, highlighting what truly matters based on the detailed comparison."
            ),
            llm=gemini_llm_education,
            verbose=True,
            allow_delegation=False,
        )

class EducationVerificationTasks:
    def compare_data_and_output_json_task(self, agent: Agent, sf_record_data_json_with_apex_keys: str, document_text: str, verifiable_apex_field_list_str: str) -> Task:
        # Parameter name updated for clarity
        return Task(
            description=(
                "Perform a comprehensive verification of education details using the provided pre-processed Salesforce Record Data and the list of verifiable Apex fields.\n\n"
                f"**Record Data (JSON String from Apex - pre-processed to remove certain system fields):**\n```json\n{sf_record_data_json_with_apex_keys}\n```\n\n"
                f"**Document Text (Raw Academic Document):**\n```text\n{document_text}\n```\n\n"
                f"**List of Apex Field Names To Process (this list has already been filtered):** {verifiable_apex_field_list_str}\n\n"
                "Most Important: Ignore Case Differences, Formatting Variations.\n"
                "**Your Process & Rules:** Strictly follow the multi-part process defined in your agent's goal. This includes: "
                "1. Processing all defined Canonical Education Concepts by attempting to map known Apex fields (from 'Typical Apex Input Field Names') found within the 'List of Apex Field Names To Process'. Apply specific verification rules for these. "
                "2. Handling any fields from the 'List of Apex Field Names To Process' that were not mapped to canonical concepts as custom fields. "
                "Pay special attention to the rules for 'GPA/Percentage', including attempting calculation. Ensure your output JSON array addresses all predefined canonical concepts and any custom fields identified from the processed list.\n"
                "**Final Output of this Task**: A single, valid JSON array string of comparison objects for all predefined canonical concepts and any custom fields identified from the 'List of Apex Field Names To Process'."
            ),
            agent=agent,
            expected_output=(
                "A valid JSON array string. Each object in the array must detail a field's comparison and include: "
                "'field_name' (canonical or original Apex name), 'original_apex_field_name', 'record_value', 'document_value', 'status', 'confidence', and 'notes'. "
                "The output array must cover all predefined canonical concepts (reporting 'Not Provided in Record' if no data maps from the processed list) and all custom fields actually identified from the 'List of Apex Field Names To Process'."
            )
        )

    def generate_formatted_report_task(self, agent: Agent, context_tasks: list) -> Task:
        return Task(
            description=(
                "You will receive a JSON array string (from the previous task's context) detailing field-by-field education data comparisons. "
                "This JSON now includes 'original_apex_field_name' in addition to 'field_name'.\n"
                "Format this into a single, human-readable string report as per your agent's goal, ensuring to display both 'field_name' and 'original_apex_field_name' for clarity if they differ."
            ),
            agent=agent,
            context=context_tasks,
            expected_output=(
                "A single string containing the 'Education Verification Details:' section with a clear field-by-field breakdown (displaying both logical field name and original Apex field name), followed by a concise 'Overall Feedback:' (1-2 lines)."
            )
        )

class EducationVerificationCrewOrchestrator:
    def __init__(self, record_data_dict: Dict[str, Any], document_text: str):
        # Create a working copy of the record data to be cleaned
        self.processed_record_data_dict = record_data_dict.copy()
        for field_to_remove in FIELDS_TO_EXCLUDE_FROM_PROCESSING:
            if field_to_remove in self.processed_record_data_dict:
                del self.processed_record_data_dict[field_to_remove]

        # This JSON string is now based on the cleaned dictionary
        self.salesforce_record_data_json_str = json.dumps(self.processed_record_data_dict, indent=2)
        self.document_text = document_text
        self.agents_provider = EducationVerificationAgents()
        self.tasks_provider = EducationVerificationTasks()

        # This list of field names is now derived from the cleaned dictionary
        self.verifiable_apex_field_names: List[str] = list(self.processed_record_data_dict.keys())

        if not self.verifiable_apex_field_names and not record_data_dict: # Check if original was also empty
             logger.warning("EducationVerificationCrewOrchestrator initialized with an empty record_data_dict. No fields to process.")
        elif not self.verifiable_apex_field_names and record_data_dict: # Original had fields, but all were excluded
            logger.warning(
                f"EducationVerificationCrewOrchestrator: All fields from original payload were excluded. "
                f"Original fields: {list(record_data_dict.keys())}. Excluded: {FIELDS_TO_EXCLUDE_FROM_PROCESSING}."
            )
        logger.info(
            f"EducationVerificationCrewOrchestrator initialized. "
            f"Original Apex fields received: {list(record_data_dict.keys())}. "
            f"Verifiable Apex Fields for Agent (after excluding {FIELDS_TO_EXCLUDE_FROM_PROCESSING}): {self.verifiable_apex_field_names}. "
            f"Doc length: {len(document_text)}."
        )
        logger.debug(f"Salesforce Record (cleaned for agent processing) for Agent: {self.salesforce_record_data_json_str}")


    def run(self) -> str:
        if not gemini_llm_education:
            logger.error("EducationVerificationCrew cannot run: LLM not initialized.")
            return "Error: LLM for education verification not available. Please check GOOGLE_API_KEY."

        if not self.verifiable_apex_field_names:
            logger.warning("EducationVerificationCrew: No verifiable fields to process after initial filtering. Returning empty report.")
            return "Education Verification Details:\n\nNo verifiable data provided from Salesforce record for processing.\n\nOverall Feedback: No verifiable data to process."

        try:
            comparator_agent = self.agents_provider.data_comparator_agent(
                verifiable_apex_field_names=self.verifiable_apex_field_names
            )
            report_generator_agent = self.agents_provider.final_report_generator_agent()

            verifiable_apex_field_list_str_for_task = ", ".join([f"'{f}'" for f in self.verifiable_apex_field_names])
            # This check is technically redundant if the one above (if not self.verifiable_apex_field_names) is hit,
            # but kept for safety in case the list becomes empty between init and here by some other means (unlikely).
            if not verifiable_apex_field_list_str_for_task:
                verifiable_apex_field_list_str_for_task = "none (all fields were filtered out)"


            task1_compare_and_structure = self.tasks_provider.compare_data_and_output_json_task(
                agent=comparator_agent,
                sf_record_data_json_with_apex_keys=self.salesforce_record_data_json_str, # This is now JSON of cleaned dict
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

            logger.info(f"EducationVerificationCrew execution completed. Report length: {len(final_report_string if final_report_string else '')}")

            if final_report_string:
                overall_feedback_sentinel = "Overall Feedback:"
                if final_report_string.count(overall_feedback_sentinel) > 1:
                    logger.warning("Duplicate 'Overall Feedback' detected. Consolidating.")
                    parts = final_report_string.split(overall_feedback_sentinel)
                    final_report_string = parts[0] + overall_feedback_sentinel + parts[1].strip()

            if not final_report_string or not isinstance(final_report_string, str):
                logger.error(f"EducationVerificationCrew produced an invalid or empty report. Type: {type(final_report_string)}")
                raw_output_str = str(final_report_string) if final_report_string else "None"
                return f"Error: Education verification crew produced an invalid report. Raw output: {raw_output_str[:200]}..."

            return final_report_string.strip()

        except Exception as e:
            logger.error(f"EducationVerificationCrew execution failed: {e}", exc_info=True)
            return f"Error during education verification crew processing: {str(e)}"