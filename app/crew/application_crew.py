# project_root/app/crew/application_crew.py
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
gemini_llm_application = None
if GOOGLE_API_KEY:
    try:
        gemini_llm_application = ChatGoogleGenerativeAI(
            model=GEMINI_MODEL_NAME,
            temperature=0.2, 
            google_api_key=GOOGLE_API_KEY,
        )
        logger.info(f"ApplicationVerificationCrew LLM initialized with model: {GEMINI_MODEL_NAME}")
    except Exception as e:
        logger.error(f"Failed to initialize LLM for ApplicationVerificationCrew ({GEMINI_MODEL_NAME}): {e}", exc_info=True)
        gemini_llm_application = None
else:
    logger.critical("APPLICATION_CREW: GOOGLE_API_KEY environment variable not set. LLM will not be available.")


# Define fields based on the client's explicit instructions
APPLICATION_VERIFICATION_FIELDS = [
    "ID Proof Type",    # Crucial for conditional logic
    "Full Name",        # As specified by client
    "Passport Number",  # Conditional
    "Birthdate",
    "Gender",
    "PassportExpiryDate", # Conditional
    "Nationality",      # Logic depends on ID Proof Type
    "Aadhar Card Number" # Conditional (covers "Aadhar No")
]

class ApplicationVerificationAgents:
    def data_comparator_agent(self) -> Agent:
        if not gemini_llm_application:
            raise RuntimeError("LLM for ApplicationVerificationAgents not initialized. Cannot create agent.")
        
        # Construct the field list string for the prompt
        field_list_str = ", ".join([f"'{field}'" for field in APPLICATION_VERIFICATION_FIELDS])

        return Agent(
            role="Intelligent Application Detail Verification Analyst with Human-like Intuition",
            goal=(
                "You are provided with: "
                "1. Structured Salesforce application record data (as a JSON string), which crucially includes an 'ID Proof Type' field. "
                "2. Raw text extracted from a supporting identity document. "
                "Your multi-step goal is to meticulously verify the application details against the document based on the Salesforce 'ID Proof Type'. "
                f"The specific fields to verify are: {field_list_str}. "
                "Your process is as follows:\n"
                "  a. **Identify Document Type**: Use the 'ID Proof Type' from the Salesforce record as the primary indicator of the document provided (e.g., 'Passport', 'Aadhar'). Corroborate this by analyzing the document text for keywords (e.g., 'Passport', 'Aadhar Card', 'Republic of India', 'UIDAI').\n"
                "  b. **Conditional Field Extraction & Verification**: For each field in the predefined list, extract its value from the document text *only if it's relevant to the identified ID Document Type*. "
                "     - If a field is not applicable to the document type (e.g., 'Passport Number' when 'ID Proof Type' is 'Aadhar'), set its 'document_value' to 'Not Applicable (Document is [Identified Type])'. "
                "     - If an *applicable* field's value is not found in the document, use 'Not Found in Document'.\n"
                "  c. **Apply Specific Verification Rules**: \n"
                "     - **'Full Name'**: Aim for a 100% match. Account for common name order variations. Minor middle name/initial discrepancies with otherwise matching first/last names can be 'Partially Matched (Acceptable Variation)'. Significant differences are 'Mismatched'.\n"
                "     - **'Passport Number'**: **Only if 'ID Proof Type' (record) indicates 'Passport' AND document appears to be a Passport**: Extract from document. Must match record value fully.\n"
                "     - **'Birthdate'**: Must match. Be robust with date formats (e.g., DD/MM/YY, MM/DD/YY, YYYY-MM-DD, Month D, YYYY). If document has less detail (e.g., only YYYY-MM) but aligns with record, use 'Partially Matched (Detail Variance)'.\n"
                "     - **'Gender'**: Aim for 'Matched' for exact matches or common, verifiable abbreviations.\n"
                "     - **'PassportExpiryDate'**: **Only if 'ID Proof Type' (record) indicates 'Passport' AND document appears to be a Passport**: Extract from document. Note if the passport is expired (date in the past).\n"
                "     - **'Nationality'**: "
                "         - **If 'ID Proof Type' (record) indicates 'Passport' AND document is a Passport**: Extract the issuing country from the document. This extracted country is the 'document_value' for 'Nationality'. Compare with record's 'Nationality'.\n"
                "         - **If 'ID Proof Type' (record) indicates 'Aadhar' AND document is an Aadhar card**: Assume document's implied nationality is 'India'. Use 'India (implied by Aadhar)' as 'document_value' for 'Nationality'. Compare with record's 'Nationality'.\n"
                "         - For other ID types, if nationality is clearly inferable from document, use that. Otherwise, 'Not Determinable from Document'.\n"
                "     - **'Aadhar Card Number'** (covers 'Aadhar No'): **Only if 'ID Proof Type' (record) indicates 'Aadhar' AND document appears to be an Aadhar card**: Extract from document. Must match record value fully. Handle masked Aadhar (e.g., 'XXXX-XXXX-1234') by comparing visible last 4 digits; status 'Matched (Masked Aadhar Verified)'.\n"
                "     - **'Application Name'**: Compare record value with any similar identifying name or application reference found in the document. Often for contextual alignment.\n"
                "     - **'ID Proof Type' (Field Itself)**: Compare the 'ID Proof Type' from the Salesforce record with the type of document inferred from the document text. Status should reflect if they align ('Matched'), differ ('Mismatched - e.g., Record: Passport, Document: Aadhar'), or if the document type is unclear ('Partially Matched (Document Type Unclear)').\n"
                "  d. **Output JSON**: For each field in the predefined list, create a JSON object with: 'field_name', 'record_value' (use 'Not Provided' if missing from Salesforce data), 'document_value', 'status' ('Matched', 'Mismatched', 'Partially Matched (Acceptable Variation)', 'Partially Matched (Detail Variance)', 'Matched (Masked Aadhar Verified)', 'Found in Record Only', 'Found in Document Only', 'Not Found in Either', 'Not Applicable', 'Record Inconsistency'), 'confidence' (High, Medium, Low), and 'notes' (explaining status, conditional logic, or discrepancies)."
                "Output a single, valid JSON array string of these comparison objects."
            ),
            backstory=(
                "You are an expert AI system for verifying application identity details against official documents. "
                "You understand common data variations, apply contextual rules with precision, and adhere to client-specific instructions, especially regarding conditional field verification based on ID Proof Type."
            ),
            llm=gemini_llm_application,
            verbose=True,
            allow_delegation=False,
        )

    def final_report_generator_agent(self) -> Agent:
        if not gemini_llm_application:
            raise RuntimeError("LLM for ApplicationVerificationAgents not initialized. Cannot create agent.")
        return Agent(
            role="Insightful Application Verification Report Synthesizer",
            goal=(
                "You will receive a JSON array string detailing field-by-field comparisons for application data. "
                "Format this into a single, human-readable string report starting with 'Application Identity Verification Details:'. "
                "List each field's comparison clearly. "
                "After the field-by-field breakdown, provide a concise 1-2 line 'Overall Feedback'. "
                "This feedback must intelligently summarize the verification outcome, focusing on CRITICAL discrepancies "
                "(e.g., 'Mismatched' ID numbers for the *relevant* ID type, significantly different 'Full Name', an expired 'PassportExpiryDate' if applicable, or mismatch in 'Nationality' vs. document-implied nationality) or critical missing information. "
                "Downplay minor partial matches if notes indicate reasonable explanations or adherence to specific client rules (e.g., masked Aadhar processing, 'Not Applicable' fields due to ID type)."
            ),
            backstory=(
                "You are a skilled report writer, synthesizing application verification data into clear, concise, and actionable summaries, highlighting what truly matters based on the detailed comparison and client guidelines, especially the conditional nature of field verification."
            ),
            llm=gemini_llm_application,
            verbose=True,
            allow_delegation=False,
        )

class ApplicationVerificationTasks:
    def compare_data_and_output_json_task(self, agent: Agent, salesforce_record_data_json_str: str, document_text: str) -> Task:
        # The agent's goal now contains the dynamic field list, so no need to pass it separately here.
        return Task(
            description=(
                "Perform a comprehensive verification of application identity details based on client instructions and the 'ID Proof Type' in the Salesforce record.\n\n"
                f"**Salesforce Record Data (JSON String):**\n```json\n{salesforce_record_data_json_str}\n```\n\n"
                f"**Document Text (Raw):**\n```text\n{document_text}\n```\n\n"
                "**Your Process & Rules:** Strictly follow the multi-step process, conditional extraction guidelines (based on 'ID Proof Type'), and comparison rules defined in your agent's goal. Ensure all fields from the client-specified list are addressed, respecting their applicability to the identified document type.\n"
                "**Final Output of this Task**: A single, valid JSON array string of comparison objects for each predefined field, adhering to the structure specified in your agent goal."
            ),
            agent=agent,
            expected_output=(
                "A valid JSON array string. Each object in the array must detail a field's comparison and include: "
                "'field_name', 'record_value', 'document_value', 'status', 'confidence', and 'notes'."
            )
        )

    def generate_formatted_report_task(self, agent: Agent, context_tasks: list) -> Task:
        return Task(
            description=(
                "You will receive a JSON array string (from the previous task's context) detailing field-by-field application data comparisons. "
                "Format this into a single, human-readable string report, starting with 'Application Identity Verification Details:'.\n"
                "For each field, list:\n"
                "- Field: [field_name]\n"
                "  Record Value: [record_value]\n"
                "  Document Value: [document_value]\n"
                "  Status: [status] (Confidence: [confidence])\n"
                "  Notes: [notes]\n\n"
                "After listing all fields, provide a concise 1-2 line 'Overall Feedback', summarizing critical findings as per your agent's goal, especially considering client-specific rules and the conditional nature of the verification (e.g., passport validity only if it's a passport)."
            ),
            agent=agent,
            context=context_tasks,
            expected_output=(
                "A single string containing the 'Application Identity Verification Details:' section with a clear field-by-field breakdown, followed by a concise 'Overall Feedback:' (1-2 lines)."
            )
        )

class ApplicationVerificationCrewOrchestrator:
    def __init__(self, record_data_dict: Dict[str, Any], document_text: str):
        # Filter the record_data_dict to include only keys present in APPLICATION_VERIFICATION_FIELDS
        # This ensures that the JSON string passed to the agent only contains relevant fields.
        filtered_record_data = {}
        for key_sf in record_data_dict.keys():
            # Simple direct mapping if key_sf is in APPLICATION_VERIFICATION_FIELDS
            if key_sf in APPLICATION_VERIFICATION_FIELDS:
                filtered_record_data[key_sf] = record_data_dict[key_sf]
            # Handle potential variations like "Aadhar No" from SF data mapping to "Aadhar Card Number"
            elif key_sf == "Aadhar No" and "Aadhar Card Number" in APPLICATION_VERIFICATION_FIELDS:
                 filtered_record_data["Aadhar Card Number"] = record_data_dict[key_sf]
            # Add other specific mappings here if SF keys differ from APPLICATION_VERIFICATION_FIELDS keys

        # Ensure all APPLICATION_VERIFICATION_FIELDS are present in filtered_record_data, even if with None value,
        # so the agent knows the complete list of fields it's expected to process from the record side.
        for field in APPLICATION_VERIFICATION_FIELDS:
            if field not in filtered_record_data:
                filtered_record_data[field] = record_data_dict.get(field, None) # Get original if present, else None


        self.salesforce_record_data_json_str = json.dumps(filtered_record_data, indent=2)
        self.document_text = document_text
        self.agents_provider = ApplicationVerificationAgents()
        self.tasks_provider = ApplicationVerificationTasks()
        logger.info(f"ApplicationVerificationCrewOrchestrator initialized. SF Fields for agent: {list(filtered_record_data.keys())}. Doc length: {len(document_text)}.")

    def run(self) -> str:
        if not gemini_llm_application:
            logger.error("ApplicationVerificationCrew cannot run: LLM not initialized.")
            return "Error: LLM for application verification not available. Please check GOOGLE_API_KEY and ensure Gemini model initialization succeeded."
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
            
            logger.info(f"ApplicationVerificationCrew execution completed. Report length: {len(final_report_string if final_report_string else '')}")
            
            if not final_report_string or not isinstance(final_report_string, str):
                logger.error(f"ApplicationVerificationCrew produced an invalid or empty report. Type: {type(final_report_string)}")
                raw_output_str = str(final_report_string) 
                return f"Error: Application verification crew produced an invalid report. Raw output snippet: {raw_output_str[:200]}..."

            return final_report_string.strip()

        except Exception as e:
            logger.error(f"ApplicationVerificationCrew execution failed: {e}", exc_info=True)
            return f"Error during application verification crew processing: {str(e)}"