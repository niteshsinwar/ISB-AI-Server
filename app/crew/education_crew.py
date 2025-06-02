# project_root/app/crew/education_crew.py
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
gemini_llm_education = None
if GOOGLE_API_KEY:
    try:
        gemini_llm_education = ChatGoogleGenerativeAI(
            model=GEMINI_MODEL_NAME,
            temperature=0.2,
            google_api_key=GOOGLE_API_KEY,
        )
        logger.info(f"EducationVerificationCrew LLM initialized with model: {GEMINI_MODEL_NAME}")
    except Exception as e:
        logger.error(f"Failed to initialize LLM for EducationVerificationCrew ({GEMINI_MODEL_NAME}): {e}", exc_info=True)
        gemini_llm_education = None
else:
    logger.critical("EDUCATION_CREW: GOOGLE_API_KEY environment variable not set. LLM will not be available.")

# Canonical fields for education verification
EDUCATION_VERIFICATION_FIELDS = [
    "Applicant Name", "Institution Name", "Degree Name", "Degree Level",
    "Field of Study", "Major/Specialization",
    "Start Date", "End Date", "Passing Year", "GPA/Percentage"
]

# Mapping from Salesforce Apex field names to canonical EDUCATION_VERIFICATION_FIELDS names
SALESFORCE_TO_CANONICAL_FIELD_MAP = {
    "SF Full Name": "Applicant Name",
    "School/Institute/Campus": "Institution Name",
    "SF Degree Name": "Degree Name",             # Using "SF Degree Name" as the primary source for Degree Name from SF
    "Contact Degree/Qualification": "Degree Name", # Could be a secondary source or for cross-check if needed
    "degreeLevel": "Degree Level",
    "SF Field of Study": "Field of Study",
    "Major/Specialization": "Major/Specialization", # Apex key matches canonical
    "From": "Start Date",
    "To": "End Date",
    "SF Passing Year": "Passing Year",
    "SF CGPA/Percentage": "GPA/Percentage",
    # Add direct mappings for keys that might already be canonical
    "Applicant Name": "Applicant Name",
    "Institution Name": "Institution Name",
    "Degree Name": "Degree Name",
    "Degree Level": "Degree Level",
    "Field of Study": "Field of Study",
    # "Major/Specialization": "Major/Specialization", # Already covered by Apex key
    "Start Date": "Start Date",
    "End Date": "End Date",
    "Passing Year": "Passing Year",
    "GPA/Percentage": "GPA/Percentage"
}

class EducationVerificationAgents:
    def data_comparator_agent(self) -> Agent:
        if not gemini_llm_education:
            raise RuntimeError("LLM for EducationVerificationAgents not initialized. Cannot create agent.")

        field_list_str = ", ".join([f"'{field}'" for field in EDUCATION_VERIFICATION_FIELDS])

        return Agent(
            role="Intelligent Education Detail Verification Analyst with Human-like Intuition",
            goal=(
                "You are provided with: "
                "1. Structured Salesforce education record data (as a JSON string, using canonical field names). "
                "2. Raw text extracted from a supporting academic document (e.g., marksheet, degree certificate). "
                "Your multi-step goal is to meticulously verify the education details against the document. "
                f"The specific fields to verify are ONLY: {field_list_str}. "
                "Your process is as follows:\n"
                "  a. **Identify Document Type**: Briefly assess if the document is primarily a degree certificate, a transcript/marksheet, or other academic proof to set context for expected information.\n"
                "  b. **Field Extraction & Verification**: For each field in the predefined list, extract its value from the document text. "
                "     - If a field's value is not found in the document, its 'document_value' must be 'Not Found in Document'.\n"
                "  c. **Apply Specific Verification Rules**: \n"
                "     - **'Applicant Name'**: Aim for a 100% match with the record. Account for common name order variations. Minor middle name/initial discrepancies with otherwise matching first/last names can be 'Partially Matched (Acceptable Variation)'. Significant differences are 'Mismatched'.\n"
                "     - **'Institution Name'**: Handle abbreviations (e.g., 'IIT' for 'Indian Institute of Technology'). If a document shows a college name (e.g., 'K J Somaiya College of Engineering') that is part of a larger university mentioned in the record data (e.g., 'University of Mumbai'), this is 'Partially Matched (Affiliated)'. If only a Board Name (e.g., 'CBSE') is found, use that as the document's institution name and note it.\n"
                "     - **'Degree Name'**: Recognize equivalencies (e.g., '12th', 'HSC', 'Senior Secondary School Examination'; 'B.Tech' and 'Bachelor of Technology'). Note the equivalence.\n"
                "     - **'Degree Level'**: Infer from Degree Name or other document cues if not explicitly stated (e.g., 'Bachelor', 'Master', 'Secondary').\n"
                "     - **'Field of Study' / 'Major/Specialization'**: 'Partially Matched (Acceptable Variation)' is appropriate if one is a subset/superset of the other in the same domain (e.g., Record Field of Study: 'Engineering', Document Major: 'Computer Science and Engineering'; Record Major: 'Computer Science', Document Major: 'Computer Science and Engineering - Data Science Focus'). 'Mismatched' for fundamentally different fields.\n"
                "     - **'Start Date' / 'End Date'**: If record has YYYY-MM-DD and document has YYYY-MM or YYYY, and available components match, status is 'Partially Matched (Detail Variance)'. Slight differences in day/month for graduation can be acceptable if year aligns.\n"
                "     - **'Passing Year'**: If record has YYYY-MM-DD (from 'To' date) or YYYY and document has YYYY, and years match, status is 'Matched'. Note detail difference. If 'End Date' implies 'Currently Studying' and document aligns, note this for 'Passing Year'.\n"
                "     - **'GPA/Percentage'**: \n"
                "         - **Extraction/Calculation**: First, try to find an explicit GPA/Percentage in the document. If not explicit, and the document provides subject-wise marks, total obtained marks, and maximum total marks (or enough data to derive these), **attempt to calculate the overall percentage (Percentage = (Total Obtained / Total Maximum) * 100)**. Set 'document_value' to the explicitly found value or 'Calculated: [calculated_value]%'. In 'notes' for calculation, specify 'Calculated from [details like Total Obtained Marks/Total Max Marks]' and list the raw data used.\n"
                "         - **Comparison & Status**: Compare this 'document_value' (explicit or calculated) with the 'record_value' for 'GPA/Percentage'.\n"
                "           - If they match precisely (e.g. 69.68 vs calculated 69.69% can be considered a precise match after rounding or within a tiny tolerance like +/- 0.1%), status is 'Matched'.\n"
                "           - If scales differ (e.g., 3.2/4.0 vs 7.72/10.0 CGPA, or percentage vs CGPA and no clear conversion is provided/standard), it's 'Mismatched (Scale Difference)'. Note the differing values and scales clearly.\n"
                "           - If values on the same scale differ significantly (e.g. > +/- 5% difference), it's 'Mismatched'.\n"
                "           - If data for calculation is present but ambiguous, or a clear conversion formula to the record's scale isn't obvious, set status to 'Needs Manual Calculation/Clarification' and provide raw marks data in 'notes'.\n"
                "           - If insufficient data for extraction or calculation, 'document_value' is 'Not Found in Document'.\n"
                "  d. **Output JSON**: For each field in the predefined list ({field_list_str}) ONLY, create a JSON object with: 'field_name', 'record_value' (use 'Not Provided in Record' if missing from the prepared Salesforce data for that predefined field), 'document_value', 'status' ('Matched', 'Mismatched', 'Partially Matched (Acceptable Variation)', 'Partially Matched (Detail Variance)', 'Partially Matched (Affiliated)', 'Mismatched (Scale Difference)', 'Found in Record Only', 'Found in Document Only', 'Not Found in Either', 'Needs Manual Calculation/Clarification'), 'confidence' (High, Medium, Low), and 'notes' (explaining status, conditional logic, calculations, or discrepancies)."
                "     - Use 'Matched' only when both record and document provide comparable values that align per rules. If 'record_value' is 'Not Provided in Record' or null, and 'document_value' is found (and not 'Not Found in Document'), the status must be 'Found in Document Only'. If 'document_value' is 'Not Found in Document' and 'record_value' exists, use 'Found in Record Only'."
                "Output a single, valid JSON array string of these comparison objects for the predefined fields ONLY."
            ),
            backstory=(
                "You are an expert AI system for verifying educational qualifications. "
                "You understand academic terminology, common variations, and can perform percentage calculations from marksheet data if clearly indicated. "
                "You meticulously extract, apply contextual rules, compare, and output structured findings strictly for the requested fields."
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
                "You will receive a JSON array string detailing field-by-field comparisons for education data (using canonical field names). "
                "Format this into a single, human-readable string report starting with 'Education Verification Details:'. "
                "List each field's comparison clearly, using the 'field_name' from the JSON for display. "
                "After the field-by-field breakdown, provide a concise 1-2 line 'Overall Feedback'. "
                "This feedback must intelligently summarize the verification outcome, focusing on CRITICAL discrepancies "
                "(e.g., 'Mismatched' GPA/Percentage especially after calculation, fundamentally different 'Degree Name' or 'Institution Name') or critical missing information in the record when found in document. "
                "If GPA/Percentage was calculated, highlight if this calculated value matches/differs from the record. "
                "Downplay minor partial matches if notes indicate reasonable explanations or acceptable levels of detail differences."
            ),
            backstory=(
                "You are a skilled report writer, synthesizing complex education verification data into clear, concise, and actionable summaries, highlighting what truly matters based on the detailed comparison and client guidelines."
            ),
            llm=gemini_llm_education,
            verbose=True,
            allow_delegation=False,
        )

class EducationVerificationTasks:
    def compare_data_and_output_json_task(self, agent: Agent, salesforce_record_data_json_str: str, document_text: str) -> Task:
        return Task(
            description=(
                "Perform a comprehensive verification of education details based on client instructions and academic document analysis.\n\n"
                f"**Salesforce Record Data (JSON String, values mapped to canonical field names):**\n```json\n{salesforce_record_data_json_str}\n```\n\n"
                f"**Document Text (Raw Academic Document):**\n```text\n{document_text}\n```\n\n"
                "**Your Process & Rules:** Strictly follow the multi-step process, extraction guidelines, and comparison rules defined in your agent's goal. "
                "Pay special attention to the rules for 'GPA/Percentage', including attempting calculation from raw marks if available and clearly indicated in the document, followed by comparison with the record value. Ensure all fields from the client-specified list (and only those fields) are addressed in your output JSON array.\n"
                "**Final Output of this Task**: A single, valid JSON array string of comparison objects for each predefined field, adhering to the structure specified in your agent goal."
            ),
            agent=agent,
            expected_output=(
                "A valid JSON array string. Each object in the array must detail a field's comparison and include: "
                "'field_name', 'record_value', 'document_value', 'status', 'confidence', and 'notes'. "
                "Output array must only contain objects for the predefined education verification fields."
            )
        )

    def generate_formatted_report_task(self, agent: Agent, context_tasks: list) -> Task:
        return Task(
            description=(
                "You will receive a JSON array string (from the previous task's context) detailing field-by-field education data comparisons, using canonical field names. "
                "Format this into a single, human-readable string report, starting with 'Education Verification Details:'.\n"
                "For each field from the JSON, list:\n"
                "- Field: [field_name]\n" # Use the field_name from the JSON
                "  Record Value: [record_value]\n"
                "  Document Value: [document_value]\n"
                "  Status: [status] (Confidence: [confidence])\n"
                "  Notes: [notes]\n\n"
                "After listing all fields, provide a concise 1-2 line 'Overall Feedback', summarizing critical findings as per your agent's goal, especially considering the outcome of GPA/Percentage verification (including any calculations made and comparison to record)."
            ),
            agent=agent,
            context=context_tasks,
            expected_output=(
                "A single string containing the 'Education Verification Details:' section with a clear field-by-field breakdown (using canonical field names from input JSON), followed by a concise 'Overall Feedback:' (1-2 lines)."
            )
        )

class EducationVerificationCrewOrchestrator:
    def __init__(self, record_data_dict: Dict[str, Any], document_text: str):
        processed_record_data = {}
        # Map Salesforce keys to canonical keys
        temp_mapped_data = {}
        for sf_key, value in record_data_dict.items():
            canonical_key = SALESFORCE_TO_CANONICAL_FIELD_MAP.get(sf_key)
            if canonical_key:
                # Prioritize specific mappings. If SF Degree Name and Contact Degree/Qualification both map to "Degree Name",
                # decide on a priority or combine, here simply overwriting, last one in map wins for duplicate canonical target.
                # For "Degree Name", SF Degree Name might be preferred.
                if canonical_key == "Degree Name" and sf_key == "SF Degree Name":
                     temp_mapped_data[canonical_key] = value
                elif canonical_key == "Degree Name" and sf_key == "Contact Degree/Qualification" and canonical_key not in temp_mapped_data : # only if SF Degree Name wasn't present
                     temp_mapped_data[canonical_key] = value
                elif canonical_key != "Degree Name": # For other keys
                    temp_mapped_data[canonical_key] = value

            elif sf_key in EDUCATION_VERIFICATION_FIELDS: # If the SF key is already canonical
                temp_mapped_data[sf_key] = value

        # Ensure all EDUCATION_VERIFICATION_FIELDS are present, defaulting to None if not found after mapping
        for field in EDUCATION_VERIFICATION_FIELDS:
            processed_record_data[field] = temp_mapped_data.get(field)

        self.salesforce_record_data_json_str = json.dumps(processed_record_data, indent=2)
        self.document_text = document_text
        self.agents_provider = EducationVerificationAgents()
        self.tasks_provider = EducationVerificationTasks()
        logger.info(f"EducationVerificationCrewOrchestrator initialized. SF Fields for agent: {list(processed_record_data.keys())}. Doc length: {len(document_text)}.")
        logger.debug(f"Processed Salesforce Record for Agent: {self.salesforce_record_data_json_str}")


    def run(self) -> str:
        if not gemini_llm_education:
            logger.error("EducationVerificationCrew cannot run: LLM not initialized.")
            return "Error: LLM for education verification not available. Please check GOOGLE_API_KEY and ensure Gemini model initialization succeeded."
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

            logger.info(f"EducationVerificationCrew execution completed. Report length: {len(final_report_string if final_report_string else '')}")
            
            # Basic check for duplicate overall feedback if it was an issue
            if final_report_string:
                overall_feedback_sentinel = "Overall Feedback:"
                if final_report_string.count(overall_feedback_sentinel) > 1:
                    logger.warning("Duplicate 'Overall Feedback' detected in the report. Consolidating.")
                    parts = final_report_string.split(overall_feedback_sentinel)
                    final_report_string = parts[0] + overall_feedback_sentinel + parts[1].strip()


            if not final_report_string or not isinstance(final_report_string, str):
                logger.error(f"EducationVerificationCrew produced an invalid or empty report. Type: {type(final_report_string)}")
                raw_output_str = str(final_report_string) if final_report_string else "None"
                return f"Error: Education verification crew produced an invalid report. Raw output snippet: {raw_output_str[:200]}..."

            return final_report_string.strip()

        except Exception as e:
            logger.error(f"EducationVerificationCrew execution failed: {e}", exc_info=True)
            return f"Error during education verification crew processing: {str(e)}"
