# project_root/app/crew/employment_crew.py
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
FIELDS_TO_EXCLUDE_FROM_PROCESSING: List[str] = [
    'Applicant__c', # If this ID field comes with employment data and isn't directly part of employment verification
    'type',
    'Contact',
    'Id', # Salesforce record ID, not needed for verification
    'recordId',
    'triggeringLogId', # Specific to employment logs, likely not for verification content
    # Add any other field names specific to Employment data you want to globally exclude
]

# LLM Initialization
gemini_llm_employment = None
if GOOGLE_API_KEY:
    try:
        gemini_llm_employment = ChatGoogleGenerativeAI(
            model=GEMINI_MODEL_NAME,
            temperature=0.15,
            google_api_key=GOOGLE_API_KEY,
        )
        logger.info(f"EmploymentVerificationCrew LLM initialized with model: {GEMINI_MODEL_NAME}")
    except Exception as e:
        logger.error(f"Failed to initialize LLM for EmploymentVerificationCrew ({GEMINI_MODEL_NAME}): {e}", exc_info=True)
        gemini_llm_employment = None # Ensure it's None if init fails
else:
    logger.critical("EMPLOYMENT_CREW: GOOGLE_API_KEY environment variable not set. LLM will not be available.")


class EmploymentVerificationAgents:
    def data_comparator_agent(self, verifiable_apex_field_names: List[str]) -> Agent: # Parameter name updated
        if not gemini_llm_employment:
            raise RuntimeError("LLM for EmploymentVerificationAgents not initialized. Cannot create agent.")

        apex_field_list_str_for_prompt = ", ".join([f"'{field}'" for field in verifiable_apex_field_names])
        if not apex_field_list_str_for_prompt:
             apex_field_list_str_for_prompt = "an empty list (no verifiable fields after initial filtering)"

        agent_prompt = f"""
You are an Expert Employment & Compensation Verification Analyst with human-like intuition and intelligence.
Your primary goal is to meticulously verify employment details from a supporting document against a provided data record from an external system (e.g., Salesforce Apex).
Most Important: Ignore Case Differences, Formatting Variations. The Record Data you receive has already had some system-level, non-verifiable fields removed. Focus your verification on the fields present in the provided Record Data and listed as 'Apex Field Names To Process'.

**Input You Receive:**
1.  **Record Data (JSON String)**: Data from Apex, accessible via `sf_record_data_json_with_apex_keys`. This data has been pre-processed.
2.  **Document Text (Raw Employment Document Content)**: E.g., offer letter, experience letter, payslip, relieving letter, ITR, Form16.
3.  **List of Apex Field Names To Process**: This is the definitive list of fields from the Record Data that you should consider for verification. It is provided in the task description as `verifiable_apex_field_list_str` (e.g., {apex_field_list_str_for_prompt}).

**Core Employment Concepts & Specific Verification Rules:**
You have a deep understanding of the following canonical employment concepts. Your final JSON output must include an entry for EACH of these canonical concepts, processed in the order listed.

1.  **Canonical Concept: 'Applicant Name'**
    * *Typical Apex Input Field Names*: 'applicantName', 'Applicant Name', 'CandidateName', 'Name', 'EmployeeName'.
    * *Verification Rule*: Verify if the name on the document closely matches the applicant's name from the record. Note any variations. Status 'Matched' for close matches, 'Partially Matched (Acceptable Variation)' for minor differences, 'Mismatched' for significant differences.

2.  **Canonical Concept: 'Company Name'**
    * *Typical Apex Input Field Names*: 'employerName', 'Company Name', 'OrganizationName', 'Employer', 'hed__Account__r.Name'.
    * *Verification Rule*: Match should be positive for variations like 'PwC India' vs. 'India Pricewaterhouse&Co'. Handle legal suffixes (Pvt Ltd, Inc.) intelligently. Status 'Matched' or 'Partially Matched (Acceptable Variation)' with notes.

3.  **Canonical Concept: 'Employment Designation'**
    * *Typical Apex Input Field Names*: 'jobTitle', 'Employment Designation', 'Designation', 'Role', 'Position'.
    * *Verification Rule*: Exact match: 'Matched'. Different roles: 'Mismatched'. Minor variations or clear hierarchical/synonymous terms (e.g. 'Sr. Developer' vs 'Senior Software Engineer'): 'Partially Matched (Acceptable Variation)'.

4.  **Canonical Concept: 'Start Date'**
    * *Typical Apex Input Field Names*: 'startDate', 'Start Date', 'DateofJoining', 'FromDate', 'CommencementDate'.
    * *Verification Rule*: Extract dates (YYYY-MM-DD). Handle variations. Exact matches: 'Matched'. Year/Month match but day differs, or one source has day and other doesn't: 'Partially Matched (Detail Variance)'.

5.  **Canonical Concept: 'End Date'**
    * *Typical Apex Input Field Names*: 'endDate', 'End Date', 'DateofLeaving', 'ToDate', 'SeparationDate', 'LastWorkingDay'.
    * *Verification Rule*:
        * Extract dates as per 'Start Date'.
        * If record implies ongoing (null, empty, 'Present'):
            * If document also implies ongoing (e.g., 'Present', 'Till Date', or 'Not Found' on recent payslip): Status 'Implied Match (Ongoing)'.
            * If document shows a past 'End Date': 'Mismatched'.
        * If record has a past date:
            * If document implies ongoing: 'Mismatched'.
            * If document also has a specific date: Compare. Exact: 'Matched'. Year/Month match, day differs: 'Partially Matched (Detail Variance)'. Significant difference: 'Mismatched'.
            * If 'document_value' is 'Not Found' and record has end date: 'Found in Record Only'.

6.  **Canonical Concept: 'Compensation'**
    * *Typical Apex Input Field Names*: 'compensation', 'Compensation', 'Salary', 'CTC', 'AnnualSalary', 'MonthlySalary', 'GrossPay'.
    * *Verification Rule*:
        * Document: Extract Gross Salary, Currency (₹ to INR, $ to USD), Frequency (Weekly, Monthly, Annually), and any Bonus Amount.
        * Record: Assume ANNUAL unless Apex field name implies otherwise (e.g., 'MonthlySalaryFromApex').
        * **Calculate Annual Document Salary**: (Monthly Gross * 12 OR Weekly Gross * 52 OR Annual Gross) + Total Annualized Document Bonus.
        * **Compare**: Calculated Annual Document Salary vs. Annual Record Compensation.
            * Match or within **20% variation** (80%-120%): **'Matched'**. Note values and % diff.
            * Outside 20% variation: **'Mismatched'**. Note values and % diff.
            * Different currency: 'Mismatched (Currency Difference)'.
            * Unclear frequency: 'Needs Human Review'.

**Your Verification Process & Output Structure:**
Let `processed_apex_fields` be a set to keep track of Apex field names from the **List of Apex Field Names To Process** that have been mapped.
Let `results_list` be an empty list for your output objects.
Briefly **Identify Document Type** (e.g., offer letter, payslip) from Document Text once at the beginning.

**Part 1: Process Canonical Employment Concepts (In Order)**
   - For EACH of the Canonical Employment Concepts listed above (from 'Applicant Name' to 'Compensation'), in the specified order:
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
         * Extract the corresponding value from the Document Text. If not found, 'document_value' must be 'Not Found in Document' (or as specified by rule, e.g., for Compensation calculation).
         * Apply the Specific Verification Rule for this Canonical Concept. Determine 'status' and 'confidence'.
     C.  **Construct Output Object**: Create the JSON output object. Add it to `results_list`.

**Part 2: Handle Unmapped Custom Apex Fields (Fields from the 'List of Apex Field Names To Process' that weren't mapped to Canonical Concepts)**
    - Iterate through each `apex_field_name` in the **List of Apex Field Names To Process**.
    - If `apex_field_name` is NOT in `processed_apex_fields`:
        * This is a custom field.
        * Construct an output object:
            * `'field_name'`: Use the `apex_field_name` itself.
            * `'original_apex_field_name'`: Same as `apex_field_name`.
            * `'record_value'`: Its value from Record Data.
            * `'document_value'`: Attempt generic extraction or 'Not Applicable (Custom Field)'.
            * `'status'`: 'Info Extracted (Custom Field)' or 'Needs Manual Review (Custom Field)'.
            * `'notes'`: "Processed as a custom field."
        * Add this custom field object to `results_list`.

**Final Output**: Return `results_list` as a single, valid JSON array string. Each object needs: 'field_name', 'original_apex_field_name', 'record_value', 'document_value', 'status', 'confidence', 'notes'.
"""
        return Agent(
            role="Advanced Employment & Compensation Verification Analyst with Prioritized Mapping",
            goal=agent_prompt,
            backstory=(
                "You are an AI system with deep expertise in verifying employment and compensation details. "
                "You map varied field names (from a pre-filtered set) to known concepts using typical names first, "
                "apply precise rules (including 20% compensation variance, ongoing employment logic), and handle unknown fields as custom. Your output is vital for due diligence."
            ),
            llm=gemini_llm_employment,
            verbose=True,
            allow_delegation=False,
        )

    def final_report_generator_agent(self) -> Agent:
        if not gemini_llm_employment:
            raise RuntimeError("LLM for EmploymentVerificationAgents not initialized. Cannot create agent.")
        return Agent(
            role="Insightful Employment Verification Report Finalizer",
            goal=(
                "You will receive a JSON array string detailing field-by-field comparisons for employment verification. "
                "Each object in the array will have a 'field_name' (which could be a canonical concept or an original Apex name if unmapped), "
                "Most Important: Ignore Case Differences, Formatting Variations, and DO NOT Include Field Like Record Id\n"
                "'original_apex_field_name', 'record_value', 'document_value', 'status', 'confidence', and 'notes'.\n"
                "Format this into a single, human-readable string report starting with 'Employment Verification Details:'.\n"
                "For each field from the JSON, list:\n"
                "- Field: [field_name] (Original Apex Field: [original_apex_field_name])\n"
                "  Record Value: [record_value]\n"
                "  Document Value: [document_value]\n"
                "  Status: [status] (Confidence: [confidence])\n"
                "  Notes: [notes]\n\n"
                "After the field-by-field breakdown, provide a concise 1-2 line 'Overall Feedback'. "
                "This feedback must intelligently summarize the verification outcome, focusing on CRITICAL discrepancies "
                "(e.g., 'Mismatched' 'Company Name', 'Mismatched' 'Employment Designation' if roles are fundamentally different, "
                "substantial differences in 'Start Date'/'End Date' affecting tenure, major 'Compensation' disparities beyond the 20% acceptable variation). "
                "Also highlight if 'Needs Human Review' status appears for critical fields like 'Compensation', or if custom fields were noted as needing review. "
                "Downplay 'Partially Matched (Acceptable Variation)' or 'Partially Matched (Detail Variance)' statuses if notes indicate reasonable explanations. "
                "The feedback should reflect a human-like assessment of whether the document substantially supports the employment claims on record."
            ),
            backstory=(
                "You are a skilled report writer who synthesizes complex employment verification data into clear, actionable summaries. You focus on materiality and the overall picture of an employment claim's veracity based on predefined rules including acceptable compensation variations and handling of custom data fields."
            ),
            llm=gemini_llm_employment,
            verbose=True,
            allow_delegation=False,
        )

class EmploymentVerificationTasks:
    def compare_data_and_output_json_task(self, agent: Agent, sf_record_data_json_with_apex_keys: str, document_text: str, verifiable_apex_field_list_str: str) -> Task: # Parameter name updated
        return Task(
            description=(
                "Perform a detailed verification of employment information using the provided pre-processed Salesforce Record Data and the list of verifiable Apex fields.\n\n"
                f"**Record Data (JSON String from Apex - pre-processed to remove certain system fields):**\n```json\n{sf_record_data_json_with_apex_keys}\n```\n\n"
                f"**Document Text (Raw Employment Document):**\n```text\n{document_text}\n```\n\n"
                f"**List of Apex Field Names To Process (this list has already been filtered):** {verifiable_apex_field_list_str}\n\n"
                "Most Important: Ignore Case Differences, Formatting Variations.\n"
                "**Your Mandated Process (refer to your agent goal for specifics on each concept, especially 'End Date' ongoing logic and 'Compensation' 20% variation rule and annualization):**\n"
                "1.  **Process Canonical Concepts**: For each defined Canonical Concept, attempt to map known Apex fields (from 'Typical Apex Input Field Names') found within the 'List of Apex Field Names To Process'. Apply specific verification rules.\n"
                "2.  **Process Unmapped Fields**: Handle any fields from the 'List of Apex Field Names To Process' that were not mapped to canonical concepts as custom fields.\n"
                "**Final Output Requirement**: A single, valid JSON array string of structured comparison objects for ALL predefined canonical concepts and any custom fields identified from the 'List of Apex Field Names To Process'."
            ),
            agent=agent,
            expected_output=(
                "A valid JSON array string. Each object details a field's comparison: "
                "'field_name' (canonical concept name or original Apex name if unmapped), 'original_apex_field_name', "
                "'record_value', 'document_value', 'status', 'confidence', 'notes'. "
                "Output MUST cover all predefined canonical concepts (reporting 'Not Provided in Record' if no data maps from the processed list) and any custom fields identified from the 'List of Apex Field Names To Process'. "
                "Compensation comparison must adhere to the 20% variation rule and annualization. "
                "End Date logic must correctly handle ongoing employment scenarios."
            )
        )

    def generate_formatted_report_task(self, agent: Agent, context_tasks: list) -> Task:
        return Task(
            description=(
                "You have received a JSON array string (from context) detailing field-by-field employment data comparisons. "
                "This JSON includes 'original_apex_field_name' alongside 'field_name'.\n"
                "Transform this JSON into a single, human-readable string report as per your agent's goal. "
                "Ensure your report displays both 'field_name' and 'original_apex_field_name' for each item for maximum clarity. "
                "Your 'Overall Feedback' should be insightful, considering all client rules (like 20% compensation variance) and the handling of any custom fields."
            ),
            agent=agent,
            context=context_tasks,
            expected_output=(
                "A single string containing 'Employment Verification Details:' with a field-by-field breakdown "
                "(showing both logical 'field_name' and 'original_apex_field_name'), followed by a concise 'Overall Feedback:'."
            )
        )

class EmploymentVerificationCrewOrchestrator:
    def __init__(self, record_data_dict: Dict[str, Any], document_text: str):
        # Create a working copy of the record data to be cleaned
        self.processed_record_data_dict = record_data_dict.copy()
        for field_to_remove in FIELDS_TO_EXCLUDE_FROM_PROCESSING:
            if field_to_remove in self.processed_record_data_dict:
                del self.processed_record_data_dict[field_to_remove]

        # This JSON string is now based on the cleaned dictionary
        self.salesforce_record_data_json_str = json.dumps(self.processed_record_data_dict, indent=2)
        self.document_text = document_text
        self.agents_provider = EmploymentVerificationAgents()
        self.tasks_provider = EmploymentVerificationTasks()

        # This list of field names is now derived from the cleaned dictionary
        self.verifiable_apex_field_names: List[str] = list(self.processed_record_data_dict.keys())


        if not self.verifiable_apex_field_names and not record_data_dict:
            logger.warning("EmploymentVerificationCrewOrchestrator initialized with an empty record_data_dict. No fields to process.")
        elif not self.verifiable_apex_field_names and record_data_dict:
             logger.warning(
                f"EmploymentVerificationCrewOrchestrator: All fields from original payload were excluded. "
                f"Original fields: {list(record_data_dict.keys())}. Excluded: {FIELDS_TO_EXCLUDE_FROM_PROCESSING}."
            )
        logger.info(
            f"EmploymentVerificationCrewOrchestrator initialized. "
            f"Original Apex fields received: {list(record_data_dict.keys())}. "
            f"Verifiable Apex Fields for Agent (after excluding {FIELDS_TO_EXCLUDE_FROM_PROCESSING}): {self.verifiable_apex_field_names}. "
            f"Document text length: {len(document_text)}."
        )
        logger.debug(f"Salesforce Record (cleaned for agent processing) for Agent: {self.salesforce_record_data_json_str}")


    def run(self) -> str:
        if not gemini_llm_employment:
            logger.error("EmploymentVerificationCrew cannot run: LLM not initialized.")
            return "Error: LLM for employment verification not available. Please check GOOGLE_API_KEY."

        if not self.verifiable_apex_field_names:
            logger.warning("EmploymentVerificationCrew: No verifiable fields to process after initial filtering. Returning empty report.")
            return "Employment Verification Details:\n\nNo verifiable data provided from Salesforce record for processing.\n\nOverall Feedback: No verifiable data to process."
        try:
            comparator_agent = self.agents_provider.data_comparator_agent(
                verifiable_apex_field_names=self.verifiable_apex_field_names
            )
            report_generator_agent = self.agents_provider.final_report_generator_agent()

            verifiable_apex_field_list_str_for_task = ", ".join([f"'{f}'" for f in self.verifiable_apex_field_names])
            if not verifiable_apex_field_list_str_for_task:
                verifiable_apex_field_list_str_for_task = "none (all fields were filtered out)"


            task1_compare_and_structure = self.tasks_provider.compare_data_and_output_json_task(
                agent=comparator_agent,
                sf_record_data_json_with_apex_keys=self.salesforce_record_data_json_str, # JSON of cleaned dict
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

            logger.info(f"EmploymentVerificationCrew execution completed. Report length: {len(final_report_string if final_report_string else '')}")

            if final_report_string:
                overall_feedback_sentinel = "Overall Feedback:"
                if final_report_string.count(overall_feedback_sentinel) > 1:
                    logger.warning("Duplicate 'Overall Feedback' detected. Consolidating.")
                    parts = final_report_string.split(overall_feedback_sentinel)
                    final_report_string = parts[0] + overall_feedback_sentinel + parts[1].strip()

            if not final_report_string or not isinstance(final_report_string, str):
                logger.error(f"EmploymentVerificationCrew produced an invalid or empty report. Type: {type(final_report_string)}")
                raw_output_str = str(final_report_string) if final_report_string is not None else "None"
                return f"Error: Employment verification crew produced an invalid report. Raw output: {raw_output_str[:200]}..."

            return final_report_string.strip()

        except Exception as e:
            logger.error(f"EmploymentVerificationCrew execution failed: {e}", exc_info=True)
            return f"Error during employment verification crew processing: {str(e)}"