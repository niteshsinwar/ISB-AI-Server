#project_root/app/crew/employment_crew.py
import os
import logging
import json
from typing import Dict, Any, List
from crewai import Agent, Task, Crew, Process
from langchain_google_genai import ChatGoogleGenerativeAI

# Import shared configurations
# Ensure app/config.py exists or environment variables are set
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

# LLM Initialization
gemini_llm_employment = None
if GOOGLE_API_KEY:
    try:
        gemini_llm_employment = ChatGoogleGenerativeAI(
            model=GEMINI_MODEL_NAME,
            temperature=0.15, # Optimized for extraction
            google_api_key=GOOGLE_API_KEY,
        )
        logger.info(f"EmploymentVerificationCrew LLM initialized with model: {GEMINI_MODEL_NAME}")
    except Exception as e:
        logger.error(f"Failed to initialize LLM for EmploymentVerificationCrew ({GEMINI_MODEL_NAME}): {e}", exc_info=True)
else:
    logger.critical("EMPLOYMENT_CREW: GOOGLE_API_KEY environment variable not set. LLM will not be available.")

# Canonical VERIFICATION_FIELDS based on client's requirements
VERIFICATION_FIELDS = [
    "Applicant Name",
    "Company Name",
    "Employment Designation",
    "Start Date",
    "End Date",
    "Compensation",
]

# Mapping from potential Salesforce Apex field names to canonical VERIFICATION_FIELDS names
SALESFORCE_TO_CANONICAL_FIELD_MAP = {
    "applicantName": "Applicant Name",
    "employerName": "Company Name",
    "jobTitle": "Employment Designation",
    "startDate": "Start Date",
    "endDate": "End Date",
    "compensation": "Compensation",
    # Add direct mappings if some SF keys might already match canonical names
    "Applicant Name": "Applicant Name",
    "Company Name": "Company Name",
    "Employment Designation": "Employment Designation",
    "Start Date": "Start Date",
    "End Date": "End Date",
    "Compensation": "Compensation",
}

class EmploymentVerificationAgents:
    def data_comparator_agent(self) -> Agent:
        if not gemini_llm_employment:
            raise RuntimeError("LLM for EmploymentVerificationAgents not initialized. Cannot create agent.")
        
        field_list_str = ", ".join([f"'{field}'" for field in VERIFICATION_FIELDS])
        
        return Agent(
            role="Expert Employment & Compensation Verification Analyst",
            goal=(
                "You are provided with: "
                "1. Structured Salesforce record data (JSON string from an Affiliation object, detailing one employment). "
                "2. Raw text from a supporting employment document (e.g., offer letter, experience letter, payslip, relieving letter, ITR, Form16). "
                "Your multi-step goal is to: "
                f"  a. From the document text, meticulously extract values for ONLY the predefined fields: {field_list_str}. "
                "     Apply nuanced understanding for variations. If a value is not explicitly found for a predefined field, its 'document_value' must be 'Not Found in Document'. "
                "  b. For each predefined field, compare its extracted document value against the corresponding Salesforce record value, applying specific client rules and human-like intuition: "
                "     - **'Applicant Name'**: Verify if the name on the document closely matches the applicant's name from the Salesforce record. Note any variations. "
                "     - **'Company Name'**: "
                "         - Match should be positive for variations like 'PwC India' vs. 'India Pricewaterhouse&Co' or 'Accordion (formerly known as Merilytics)' vs. 'ACCORDION PARTNERS INDIA PRIVATE LIMITED'. Handle common legal suffixes (Pvt Ltd, Inc., LLC, Limited). Status 'Matched' or 'Partially Matched (Acceptable Variation)' with notes. "
                "     - **'Employment Designation'**: "
                "         - Aim for exact match (e.g., 'Associate' = 'Associate'). Status 'Matched'. "
                "         - Different distinct roles are 'Mismatched' (e.g., 'Software Test Engineer' vs 'Functional Test Engineer'). "
                "         - Minor variations or clear hierarchical/synonymous terms (e.g. 'Sr. Developer' vs 'Senior Software Engineer') can be 'Partially Matched (Acceptable Variation)'. "
                "     - **'Start Date', 'End Date'**: "
                "         - Extract dates, aiming for 'YYYY-MM-DD' format. Handle common variations (e.g., DD-MMM-YYYY, Month DD, YYYY, YYYY-MM). Note original format if ambiguous. "
                "         - **For 'End Date' specifically**: "
                "           - If the Salesforce record 'End Date' is null or 'Not Provided in Record' (implying current employment in SF): "
                "             - If your extracted 'document_value' for 'End Date' also implies ongoing employment (e.g., is 'Present', 'Till Date', or 'Not Found in Document' because the document is a recent payslip with no end date), then the status for 'End Date' is 'Implied Match (Ongoing)'. Notes must clarify this (e.g., 'Both record and document suggest current employment.')."
                "             - If the document explicitly provides a past 'End Date', this is a 'Mismatched' status. Note the discrepancy against the record's implication of ongoing employment."
                "           - If the Salesforce record 'End Date' is a specific date: "
                "             - If your extracted 'document_value' for 'End Date' implies ongoing employment, this is 'Mismatched'."
                "             - If the 'document_value' for 'End Date' is also a specific date, compare them. Exact matches are 'Matched'. If Year/Month match but day differs, or one source has day and other doesn't, status is 'Partially Matched (Detail Variance)'. Significant differences are 'Mismatched'."
                "             - If the 'document_value' for 'End Date' is 'Not Found in Document', and the SF record specifies an end date, status is 'Found in Record Only'."
                "     - **'Compensation'**: "
                "         - From the document, extract Gross Salary Amount, its Currency (map symbols like ₹ to INR, $ to USD), and Frequency (Weekly, Monthly, Annually from cues like 'p.m.', 'p.a.'). Also extract any separately mentioned Bonus Amount from the document (and its frequency, e.g., annual, one-time). "
                "         - The Salesforce 'Compensation' value (if present) is assumed to be ANNUAL. "
                "         - **Calculation for Comparison**: Convert document salary to an ANNUAL figure. "
                "           - If document frequency is Monthly: (Document Monthly Gross * 12) + Total Annualized Document Bonus (if any). "
                "           - If document frequency is Weekly: (Document Weekly Gross * 52) + Total Annualized Document Bonus (if any). "
                "           - If document frequency is Annual: Document Annual Gross + Total Annualized Document Bonus (if any). "
                "         - **Comparison & Status**: Compare this calculated Annual Document Salary with the Annual Salesforce Compensation. "
                "           - If they match exactly OR the calculated Annual Document Salary is within a **20% variation** (i.e., between 80% and 120%) of the Annual Salesforce Compensation, the status is **'Matched'**. Note the calculated values and the exact percentage difference if not an exact match. "
                "           - If the difference is outside the 20% variation, the status is **'Mismatched'**. Note calculated values and the percentage difference. "
                "           - If the currency extracted from the document differs from the implied currency of the SF record (assume local currency if not specified in SF record), status is 'Mismatched', even if numbers are close. Note both currencies. "
                "           - If compensation frequency cannot be reliably determined from the document to perform annualization, status is 'Needs Human Review' for 'Compensation'. Note any figures or partial information found. "
                "  c. For each of the **predefined fields** ({field_list_str}), create a JSON object detailing: 'field_name', 'record_value' (from SF JSON, use 'Not Provided in Record' if missing/null for that predefined field), 'document_value' (your extraction/derivation for that predefined field), "
                "     'status' ('Matched', 'Mismatched', 'Partially Matched (Acceptable Variation)', 'Partially Matched (Detail Variance)', 'Implied Match (Ongoing)', 'Found in Record Only', 'Found in Document Only', 'Not Found in Either', 'Needs Human Review'), "
                "     'confidence' (High, Medium, Low), 'notes' (CRUCIAL: explain status, variations, calculations performed for Compensation, rules applied, missing info, or why it 'Needs Human Review'). "
                "     - Use 'Matched' status only when both record and document provide comparable values that align per rules. If 'record_value' is 'Not Provided in Record' or null, and 'document_value' is found, the status must be 'Found in Document Only'. If 'document_value' is 'Not Found in Document' and 'record_value' exists, use 'Found in Record Only'."
                "Output a single, valid JSON array string containing objects **ONLY for these predefined comparison fields**."
            ),
            backstory=(
                "You are an AI system with deep expertise in verifying employment and compensation details from diverse documentation. You meticulously apply client-specific rules for matching company names, employment designations, dates (including nuanced handling of ongoing employment), and complex compensation calculations involving annualization, bonuses, and a 20% acceptable variation threshold. Your structured output, strictly limited to predefined fields, is vital for due diligence."
            ),
            llm=gemini_llm_employment,
            verbose=True,
            allow_delegation=False,
        )

    def final_report_generator_agent(self) -> Agent:
        # This agent's definition remains the same as provided in the problem
        if not gemini_llm_employment:
            raise RuntimeError("LLM for EmploymentVerificationAgents not initialized. Cannot create agent.")
        return Agent(
            role="Insightful Employment Verification Report Finalizer",
            goal=(
                "You will receive a JSON array string detailing field-by-field comparisons for employment verification. The field names in this JSON are the canonical names. "
                "Format this into a single, human-readable string report starting with 'Employment Verification Details:'. "
                "List each field's comparison clearly, using the 'field_name' from the JSON for display. "
                "After the field-by-field breakdown, provide a concise 1-2 line 'Overall Feedback'. "
                "This feedback must intelligently summarize the verification outcome, focusing on CRITICAL discrepancies "
                "(e.g., 'Mismatched' 'Company Name', 'Mismatched' 'Employment Designation' if roles are fundamentally different, "
                "substantial differences in 'Start Date'/'End Date' affecting tenure, major 'Compensation' disparities beyond the 20% acceptable variation). "
                "Also highlight if 'Needs Human Review' status appears for critical fields like 'Compensation'. "
                "Downplay 'Partially Matched (Acceptable Variation)' or 'Partially Matched (Detail Variance)' statuses if notes indicate reasonable explanations. "
                "The feedback should reflect a human-like assessment of whether the document substantially supports the employment claims on record."
            ),
            backstory=(
                "You are a skilled report writer who synthesizes complex employment verification data into clear, actionable summaries. You focus on materiality and the overall picture of an employment claim's veracity based on predefined rules including acceptable compensation variations."
            ),
            llm=gemini_llm_employment,
            verbose=True,
            allow_delegation=False,
        )

class EmploymentVerificationTasks:
    def compare_data_and_output_json_task(self, agent: Agent, salesforce_record_data_json_str: str, document_text: str) -> Task:
        # This task's description remains largely the same, as the core logic is in the agent's goal.
        field_list_str = ", ".join([f"'{field}'" for field in VERIFICATION_FIELDS])
        return Task(
            description=(
                "Perform a detailed verification of employment information. You are given Salesforce record data (from an Affiliation object representing one employment) and raw text from a supporting document (like an offer letter, experience letter, payslip, ITR, or Form 16).\n\n"
                f"**Predefined Fields for Verification & Extraction from Document (these are the ONLY fields you should output):**\n`{field_list_str}`\n\n"
                f"**Salesforce Record Data (JSON String - Affiliation Details, values mapped to canonical field names):**\n```json\n{salesforce_record_data_json_str}\n```\n"
                f"This Salesforce data uses canonical field names and includes an ANNUAL 'Compensation' if present.\n\n"
                f"**Document Text (Raw):**\n```text\n{document_text}\n```\n\n"
                "**Your Mandated Process & Client Rules (refer to your detailed role and goal for specifics on each field, especially 'End Date' and 'Compensation' including the 20% variation rule and annualization logic):**\n"
                "1.  **Extract from Document Text**: For each predefined field, extract its value. If not found, state 'Not Found in Document'.\n"
                "2.  **Compare and Determine Status**: Compare document values with record values using all specified rules.\n"
                "3.  **Structure Output**: For EACH predefined field ONLY, create a JSON object: {'field_name', 'record_value', 'document_value', 'status', 'confidence', 'notes'}.\n\n"
                "**Final Output Requirement**: A single, valid JSON array string of these structured comparison objects for the predefined fields ONLY, adhering to all rules."
            ),
            agent=agent,
            expected_output=(
                "A valid JSON array string. Each object details a field's comparison: "
                "'field_name', 'record_value', 'document_value', 'status', 'confidence', 'notes'. "
                "Output MUST ONLY contain objects for the predefined VERIFICATION_FIELDS. "
                "Compensation comparison must adhere to the 20% variation rule and annualization logic. "
                "End Date logic must correctly handle ongoing employment scenarios."
            )
        )

    def generate_formatted_report_task(self, agent: Agent, context_tasks: list) -> Task:
        # This task's definition remains the same.
        return Task(
            description=(
                "You have received a JSON array string (from the context of a previous task) which contains detailed field-by-field comparisons of employment data including complex compensation analysis based on client rules (like 20% variation acceptability). "
                "Each object in this array includes 'field_name', 'record_value', 'document_value', 'status', 'confidence', and 'notes'. The 'field_name' is the canonical name.\n\n"
                "Your task is to transform this JSON array into a single, human-readable string report. The report must start with the heading 'Employment Verification Details:'.\n"
                "Following this heading, for each field comparison object from the JSON array, list the information in this format, using the 'field_name' from the JSON for display:\n"
                "- Field: [field_name]\n"
                "  Record Value: [record_value]\n"
                "  Document Value: [document_value]\n"
                "  Status: [status] (Confidence: [confidence])\n"
                "  Notes: [notes]\n\n"
                "After detailing all fields, provide a concise 'Overall Feedback' section of 1-2 lines. This feedback should be an intelligent summary of the verification. "
                "Focus on critical discrepancies, such as: 'Mismatched' 'Company Name' (after applying equivalence rules), significantly different employment tenures implied by 'Start Date'/'End Date', "
                "'Compensation' amounts that are 'Mismatched' (i.e., outside the 20% acceptable variation after annualization and bonus considerations), "
                "fundamentally different 'Employment Designation'. "
                "Also, explicitly mention if 'Compensation' or other key fields have a status of 'Needs Human Review'. "
                "If 'Partially Matched' statuses are due to acceptable variations clearly explained in the notes (like salary frequency conversion resulting in a 'Matched' status due to the 20% rule, or minor name tweaks for known company aliases), these should not be the primary focus of a negative overall feedback unless they collectively indicate a larger issue. "
                "The feedback's aim is to give a human-like assessment of whether the provided document substantially supports the key employment claims found in the Salesforce record, considering all client-specified rules."
            ),
            agent=agent,
            context=context_tasks,
            expected_output=(
                "A single string containing the 'Employment Verification Details:' section with a clear, field-by-field breakdown using canonical field names, "
                "followed by a concise 'Overall Feedback:' (1-2 lines) summarizing the findings, explicitly considering the 20% compensation variation rule and other client instructions."
            )
        )

class EmploymentVerificationCrewOrchestrator:
    def __init__(self, record_data_dict: Dict[str, Any], document_text: str):
        processed_record_data = {}
        # First, create a temporary dictionary with keys mapped to canonical names
        temp_mapped_data = {}
        for sf_key, value in record_data_dict.items():
            canonical_key = SALESFORCE_TO_CANONICAL_FIELD_MAP.get(sf_key)
            if canonical_key: # If a mapping exists for the Salesforce key
                temp_mapped_data[canonical_key] = value
            elif sf_key in VERIFICATION_FIELDS: # If the Salesforce key is already a canonical name
                temp_mapped_data[sf_key] = value
            # else:
                # logger.debug(f"Salesforce key '{sf_key}' not in direct map or VERIFICATION_FIELDS, will be ignored for processed_record_data.")

        # Ensure all VERIFICATION_FIELDS are present in processed_record_data, even if with None
        for field in VERIFICATION_FIELDS:
            processed_record_data[field] = temp_mapped_data.get(field) # Get by canonical name, defaults to None if not found

        self.salesforce_record_data_json_str = json.dumps(processed_record_data, indent=2)
        self.document_text = document_text
        self.agents_provider = EmploymentVerificationAgents()
        self.tasks_provider = EmploymentVerificationTasks()
        logger.info(f"EmploymentVerificationCrewOrchestrator initialized. SF Fields for agent: {list(processed_record_data.keys())}. Document text length: {len(document_text)}.")
        # logger.debug(f"Processed Salesforce Record for Agent: {self.salesforce_record_data_json_str}")


    def run(self) -> str:
        if not gemini_llm_employment:
            logger.error("EmploymentVerificationCrew cannot run: LLM not initialized.")
            return "Error: LLM for employment verification not available. Please check GOOGLE_API_KEY and ensure Gemini model initialization succeeded."
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
            
            logger.info(f"EmploymentVerificationCrew execution completed. Report length: {len(final_report_string if final_report_string else '')}")
            
            # Check for duplicate overall feedback - simple check for now
            if final_report_string:
                overall_feedback_sentinel = "Overall Feedback:"
                if final_report_string.count(overall_feedback_sentinel) > 1:
                    logger.warning("Duplicate 'Overall Feedback' detected in the report. Attempting to consolidate.")
                    parts = final_report_string.split(overall_feedback_sentinel)
                    if len(parts) > 1: # Should always be true if count > 1
                        # Keep the first part (details) and the first overall feedback
                        final_report_string = parts[0] + overall_feedback_sentinel + parts[1].strip()


            if not final_report_string or not isinstance(final_report_string, str):
                logger.error(f"EmploymentVerificationCrew produced an invalid or empty report. Type: {type(final_report_string)}")
                raw_output_str = str(final_report_string) if final_report_string is not None else "None"
                return f"Error: Employment verification crew produced an invalid report. Raw output snippet: {raw_output_str[:200]}..."

            return final_report_string.strip()

        except Exception as e:
            logger.error(f"EmploymentVerificationCrew execution failed: {e}", exc_info=True)
            return f"Error during employment verification crew processing: {str(e)}"