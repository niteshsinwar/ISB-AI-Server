# project_root/app/crew/education_crew.py
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


VERIFICATION_FIELDS = [
    "Applicant Name", "Institution Name", "Degree Name", "Degree Level", 
    "Field of Study", "Major/Specialization", "Program Name", 
    "Start Date", "End Date", "Passing Year", "GPA/Percentage", "Registration Number"
]

class EducationVerificationAgents:
    def data_comparator_agent(self) -> Agent:
        if not gemini_llm_education:
            raise RuntimeError("LLM for EducationVerificationAgents not initialized. Cannot create agent.")
        return Agent(
            role="Intelligent Education Verification Analyst with Human-like Intuition",
            goal=(
                "You are provided with: "
                "1. Structured Salesforce record data (as a JSON string). "
                "2. Raw text extracted from a supporting academic document. "
                "Your multi-step goal is to: "
                "  a. From the raw document text, meticulously extract values for the predefined fields: "
                f"     `{', '.join(VERIFICATION_FIELDS)}`. Apply nuanced understanding for variations. "
                "     If a value is not found, use 'Not Found in Document'. "
                "  b. Apply specific client rules and human-like intuition during extraction and comparison: "
                "     - **Dates (Passing Year, Start Date, End Date)**: "
                "         - For 'Passing Year': If the record has YYYY-MM-DD and the document has only YYYY, and the years match, status is 'Matched'. Note the detail difference. "
                "         - For 'Start Date'/'End Date': If record has YYYY-MM-DD and document has YYYY-MM or YYYY, and the available components match, status is 'Partially Matched (Detail Variance)'. "
                "           Note: A slight difference in day (e.g., end of month vs. start of next for graduation) might still be considered acceptable alignment. "
                "     - **Textual Fields (Institution Name, Degree Name, Field of Study, Major/Specialization, Applicant Name)**: "
                "         - Status 'Matched' for exact matches or common, verifiable abbreviations (e.g., 'IIT' for 'Indian Institute of Technology'). "
                "         - Status 'Partially Matched (Acceptable Variation)' if one value is a more specific or general version of the other but clearly within the same core subject/entity (e.g., Record Field of Study: 'Engineering', Document: 'Computer Science and Engineering'; Record Major: 'Computer Science', Document: 'Computer Science and Engineering - Data Science'). This is acceptable. "
                "         - Status 'Mismatched' if core concepts are different (e.g., Record Degree: 'Engineering', Document: 'Arts')."
                "     - **GPA/Percentage**: Must match precisely if scales are the same. If scales differ (e.g. 3.2 vs 7.72 CGPA), it's 'Mismatched'. Note the differing values and scales clearly. If calculation 'X/Y' is possible from document, note this as 'Needs Calculation' if not directly stated. "
                "     - **Registration Number**: Extract if present. If present in one source but not the other, use 'Found in Record Only' or 'Found in Document Only'. "
                "  c. Compare the (processed/extracted) document values against the Salesforce record data, field by field for ALL predefined fields. "
                "  d. For each predefined field, create a JSON object detailing this comparison: "
                "     'field_name', 'record_value', 'document_value', "
                "     'status' ('Matched', 'Mismatched', 'Partially Matched (Acceptable Variation)', 'Partially Matched (Detail Variance)', 'Found in Record Only', 'Found in Document Only', 'Not Found in Either', 'Needs Calculation'), "
                "     'confidence' (High, Medium, Low) for the status, "
                "     'notes' (Brief, very specific explanation for the status, especially for mismatches, partial matches, calculations, equivalencies applied, or if information is missing from one source). "
                "Output a single, valid JSON array string where each element is an object representing the comparison for one predefined field."
            ),
            backstory=(
                "You are an expert AI system designed for nuanced analysis of educational qualifications. "
                "You mimic human intuition by understanding acceptable variations, abbreviations, and hierarchical relationships in academic data. "
                "You meticulously extract, apply contextual rules, compare, and output structured findings."
            ),
            llm=gemini_llm_education,
            verbose=True,
            allow_delegation=False,
        )

    def final_report_generator_agent(self) -> Agent:
        if not gemini_llm_education:
            raise RuntimeError("LLM for EducationVerificationAgents not initialized. Cannot create agent.")
        return Agent(
            role="Insightful Verification Report Synthesizer",
            goal=(
                "You will receive a JSON array string detailing field-by-field comparisons. "
                "Format this into a single, human-readable string report starting with 'Verification Details:'. "
                "List each field's comparison clearly. "
                "After the field-by-field breakdown, provide a concise 1-2 line 'Overall Feedback'. "
                "This feedback must intelligently summarize the verification outcome, focusing on CRITICAL discrepancies "
                "(e.g., 'Mismatched' GPA, fundamentally different 'Degree Name' or 'Institution Name') or critical missing information. "
                "Downplay 'Partially Matched (Acceptable Variation)' or 'Partially Matched (Detail Variance)' statuses if notes indicate reasonable explanations or acceptable levels of detail differences. "
                "The feedback should reflect a human-like assessment of whether the document substantially supports the record, despite minor variances."
            ),
            backstory=(
                "You are a skilled report writer who synthesizes complex verification data into insightful summaries. "
                "You can distinguish between critical errors and acceptable data variations."
            ),
            llm=gemini_llm_education,
            verbose=True,
            allow_delegation=False,
        )

class EducationVerificationTasks:
    def compare_data_and_output_json_task(self, agent: Agent, salesforce_record_data_json_str: str, document_text: str) -> Task:
        field_list_str = ", ".join(VERIFICATION_FIELDS)
        return Task(
            description=(
                "Perform a comprehensive verification of education details following specific rules. You will work with Salesforce record data and raw document text.\n\n"
                f"**Predefined Fields for Verification:**\n`{field_list_str}`\n\n"
                f"**Salesforce Record Data (JSON String):**\n```json\n{salesforce_record_data_json_str}\n```\n\n"
                f"**Document Text (Raw):**\n```text\n{document_text}\n```\n\n"
                "**Your Process & Rules:**\n"
                "1.  **Extract from Document Text**: For each predefined field, extract its value from the 'Document Text'. If not found, value is 'Not Found in Document'.\n"
                "    - **Institute Name**: Handle abbreviations (e.g., 'PSBB' for 'Padma Seshadri Bala Bhavan'). If a document shows a college name (e.g., 'K J Somaiya') that is part of a larger university mentioned in the record data (e.g., 'University of Mumbai'), this is a strong partial match. If only a Board Name (e.g., 'CBSE') is found, use that as the document's institution name and note it.\n"
                "    - **Degree Name**: Recognize equivalencies (e.g., '12th', 'HSC', 'Senior Secondary School Examination' are equivalent; 'BMS' is 'Bachelor of Management Studies').\n"
                "    - **Field of Study/Major/Specialization**: 'Partially Matched (Acceptable Variation)' is appropriate if one is a subset/superset of the other in the same domain (e.g., Record Field of Study: 'Engineering', Document: 'Computer Science and Engineering'; Record Major: 'Computer Science', Document: 'Computer Science and Engineering - Data Science'). A 'Mismatched' status is for fundamentally different fields (e.g., Record: 'Engineering', Document: 'Arts').\n"
                "    - **CGPA/Percentage**: Directly compare. If scales differ (e.g. 3.2 vs 7.72 CGPA), status is 'Mismatched', note the values and scales. If document says 'X out of Y', note as 'Needs Calculation' and provide X and Y in notes.\n"
                "    - **Passing Year**: If Record has YYYY-MM-DD and Document has YYYY, and years match, status is 'Matched'. Note detail difference.\n"
                "    - **Start Date/End Date**: If record is YYYY-MM-DD and doc is YYYY-MM or YYYY, if available parts match, status 'Partially Matched (Detail Variance)'. If End Date implies 'Currently Studying', note this for Passing Year if doc also aligns.\n"
                "    - **Applicant Name**: Strive for exact match but allow minor variations (missing middle initial if first/last are strong matches). Note variations.\n"
                "2.  **Compare & Structure Output**: For each predefined field, create a JSON object with 'field_name', 'record_value' (from Salesforce JSON, use 'Not Provided' if missing), 'document_value' (from your extraction), 'status' (options: 'Matched', 'Mismatched', 'Partially Matched (Acceptable Variation)', 'Partially Matched (Detail Variance)', 'Found in Record Only', 'Found in Document Only', 'Not Found in Either', 'Needs Calculation'), 'confidence' ('High', 'Medium', 'Low'), and 'notes' (crucial for explaining status, especially partials and rules applied).\n\n"
                "**Final Output of this Task**: A single, valid JSON array string of these comparison objects."
            ),
            agent=agent,
            expected_output=(
                "A valid JSON array string. Each object details a field's comparison: "
                "'field_name', 'record_value', 'document_value', 'status', 'confidence', 'notes'."
            )
        )

    def generate_formatted_report_task(self, agent: Agent, context_tasks: list) -> Task:
        return Task(
            description=(
                "You will receive a JSON array string (from the previous task's context) detailing field-by-field comparisons. "
                "Each object in the array contains 'field_name', 'record_value', 'document_value', 'status', 'confidence', and 'notes'.\n\n"
                "Format this into a single, human-readable string report, starting with 'Verification Details:'.\n"
                "For each field, list:\n"
                "- Field: [field_name]\n"
                "  Record Value: [record_value]\n"
                "  Document Value: [document_value]\n"
                "  Status: [status] (Confidence: [confidence])\n"
                "  Notes: [notes]\n\n"
                "After listing all fields, provide a concise 1-2 line 'Overall Feedback'. This feedback should intelligently summarize the outcome, "
                "emphasizing critical mismatches (e.g., GPA, fundamentally different Degree or Institution) or essential missing information. "
                "Downplay 'Partially Matched' statuses if the notes indicate acceptable, explainable variations (like more specific document info or minor date detail differences where core components like year/month align). "
                "The goal is to reflect a human-like assessment of the document's support for the record."
            ),
            agent=agent,
            context=context_tasks,
            expected_output=(
                "A single string: 'Verification Details:' section with field-by-field breakdown, followed by 'Overall Feedback:' (1-2 lines)."
            )
        )

class EducationVerificationCrewOrchestrator:
    def __init__(self, record_data_dict: Dict[str, Any], document_text: str):
        self.salesforce_record_data_json_str = json.dumps(record_data_dict, indent=2)
        self.document_text = document_text
        self.agents_provider = EducationVerificationAgents()
        self.tasks_provider = EducationVerificationTasks()
        logger.info(f"EducationVerificationCrewOrchestrator initialized. Doc length: {len(document_text)}.")

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
                verbose=1 # Consider making verbose level configurable
            )
            
            final_report_string = crew.kickoff()
            
            logger.info(f"EducationVerificationCrew execution completed. Report length: {len(final_report_string if final_report_string else '')}")
            
            if not final_report_string or not isinstance(final_report_string, str):
                logger.error(f"EducationVerificationCrew produced an invalid or empty report. Type: {type(final_report_string)}")
                raw_output_str = str(final_report_string)
                return f"Error: Education verification crew produced an invalid report. Raw output snippet: {raw_output_str[:200]}..."

            return final_report_string.strip()

        except Exception as e:
            logger.error(f"EducationVerificationCrew execution failed: {e}", exc_info=True)
            return f"Error during education verification crew processing: {str(e)}"
