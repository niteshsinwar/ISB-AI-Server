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


# Define fields based on the provided cURL response for application data & client instructions
APPLICATION_VERIFICATION_FIELDS = [
    "SF Full Name", "Birthdate", "Gender", "Nationality", 
    "ID Proof Type", "Conceptual ID Document Type", 
    "Passport Number", "PassportIssuingCountry", "PassportExpiryDate",
    "Aadhar Card Number", "applicationName" 
]

class ApplicationVerificationAgents:
    def data_comparator_agent(self) -> Agent:
        if not gemini_llm_application:
            raise RuntimeError("LLM for ApplicationVerificationAgents not initialized. Cannot create agent.")
        return Agent(
            role="Intelligent Application Detail Verification Analyst with Human-like Intuition",
            goal=(
                "You are provided with: "
                "1. Structured Salesforce application record data (as a JSON string). "
                "2. Raw text extracted from a supporting identity document (e.g., Passport, Aadhar card). "
                "Your multi-step goal is to: "
                f"  a. From the raw document text, meticulously extract values for the predefined fields: `{', '.join(APPLICATION_VERIFICATION_FIELDS)}`. "
                "     Apply nuanced understanding for variations. If a value is not found for a field, state 'Not Found in Document'. Be mindful that document legibility can impact extraction confidence. "
                "  b. Apply specific client rules and human-like intuition for extraction and comparison: "
                "     - **SF Full Name**: Aim for a 100% match with the document name, considering common name order variations (e.g., 'First Last' vs. 'Last, First'). If first and last names are exact matches and in a plausible order, minor variations in middle names/initials (e.g., present in one source, abbreviated or absent in the other) should be 'Partially Matched (Acceptable Variation)' with clear notes. Otherwise, if core components differ significantly, it's 'Mismatched'. "
                "     - **ID Numbers**: "
                "         - **Passport Number**: Must match the document value fully. "
                "         - **Aadhar Card Number**: Must match the document value fully. If the Aadhar number in the document appears masked (e.g., 'XXXX-XXXX-1234' or only last 4 digits visible), verification should be based on comparing these visible last 4 digits with the corresponding last 4 digits from the Salesforce record. The status should reflect this (e.g., 'Matched (Masked Aadhar Verified)'). "
                "     - **Birthdate**: The Date of Birth must match. Be robust in interpreting and comparing dates from various common formats (e.g., DD/MM/YY, MM/DD/YY, YYYY-MM-DD, Month D, YYYY), as long as the interpreted day, month, and year are identical. If the document provides less detail (e.g., only year, or year and month) while the record has a full date, use 'Partially Matched (Detail Variance)' if the available components align. "
                "     - **PassportExpiryDate**: Extract the expiry date from the document. Clearly note this date and state if the passport appears to be expired (i.e., the date is in the past). For status, compare with record data if available; if record data is blank, state 'Found in Document Only'. "
                "     - **PassportIssuingCountry**: Extract the passport's issuing country if visible on the document. "
                "     - **Nationality**: Compare the 'Nationality' from the Salesforce record with the extracted 'PassportIssuingCountry' (if the ID document is a passport). If the record's 'Nationality' is not India and the 'ID Proof Type' is 'Passport', the 'PassportIssuingCountry' from the document should ideally match the record's 'Nationality'. Note any discrepancies or confirmations. "
                "     - **Gender, ID Proof Type, Conceptual ID Document Type**: Aim for 'Matched' for exact matches or common, verifiable abbreviations. Use 'Partially Matched (Acceptable Variation)' for minor, reasonable differences. "
                "     - **applicationName**: This is primarily for context. Note if it appears or aligns with any information in the document, if applicable. "
                "  c. Compare the (processed/extracted) document values against the Salesforce record data, field by field for ALL predefined fields. Ensure every field from `APPLICATION_VERIFICATION_FIELDS` is addressed. "
                "  d. For each predefined field, create a JSON object detailing this comparison: "
                "     'field_name', 'record_value' (use 'Not Provided' if missing from Salesforce data), 'document_value' (from your extraction), "
                "     'status' ('Matched', 'Mismatched', 'Partially Matched (Acceptable Variation)', 'Partially Matched (Detail Variance)', 'Matched (Masked Aadhar Verified)', 'Found in Record Only', 'Found in Document Only', 'Not Found in Either'), "
                "     'confidence' (High, Medium, Low) for the status, "
                "     'notes' (Brief, very specific explanation for the status, particularly for any partial matches, mismatches, how rules like masked Aadhar were applied, or if information is missing from one source). "
                "Output a single, valid JSON array string where each element is an object representing the comparison for one predefined field."
            ),
            backstory=(
                "You are an expert AI system for verifying application identity details against official documents. "
                "You understand common data variations, apply contextual rules with precision, and adhere to client-specific instructions like handling masked Aadhar numbers and passport details. Your goal is thoroughness and accuracy."
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
                "(e.g., 'Mismatched' ID numbers, significantly different 'SF Full Name', an expired 'PassportExpiryDate', or mismatch in 'Nationality' vs 'PassportIssuingCountry' when relevant) or critical missing information. "
                "Downplay minor partial matches (like 'Partially Matched (Acceptable Variation)' or 'Partially Matched (Detail Variance)') if notes indicate reasonable explanations or adherence to specific client rules (e.g., masked Aadhar processing)."
            ),
            backstory=(
                "You are a skilled report writer, synthesizing application verification data into clear, concise, and actionable summaries, highlighting what truly matters based on the detailed comparison and client guidelines."
            ),
            llm=gemini_llm_application,
            verbose=True,
            allow_delegation=False,
        )

class ApplicationVerificationTasks:
    def compare_data_and_output_json_task(self, agent: Agent, salesforce_record_data_json_str: str, document_text: str) -> Task:
        field_list_str = ", ".join(APPLICATION_VERIFICATION_FIELDS)
        return Task(
            description=(
                "Perform a comprehensive verification of application identity details based on client instructions.\n\n"
                f"**Predefined Fields for Verification:**\n`{field_list_str}`\n\n"
                f"**Salesforce Record Data (JSON String):**\n```json\n{salesforce_record_data_json_str}\n```\n\n"
                f"**Document Text (Raw):**\n```text\n{document_text}\n```\n\n"
                "**Your Process & Rules:** Strictly follow the multi-step process, extraction guidelines, and comparison rules defined in your agent's goal. Pay special attention to client instructions regarding Full Name matching, masked Aadhar numbers, Passport Expiry, Passport Issuing Country, Nationality, and Birthdate format handling.\n"
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
                "After listing all fields, provide a concise 1-2 line 'Overall Feedback', summarizing critical findings as per your agent's goal, especially considering client-specific rules like ID verification outcomes and passport validity."
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
        filtered_record_data = {
            key: record_data_dict.get(key) 
            for key in APPLICATION_VERIFICATION_FIELDS 
            if key in record_data_dict
        }
        self.salesforce_record_data_json_str = json.dumps(filtered_record_data, indent=2)
        self.document_text = document_text
        self.agents_provider = ApplicationVerificationAgents()
        self.tasks_provider = ApplicationVerificationTasks()
        logger.info(f"ApplicationVerificationCrewOrchestrator initialized. Relevant SF Fields: {list(filtered_record_data.keys())}. Doc length: {len(document_text)}.")

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
                verbose=1 # Consider making verbose level configurable
            )
            
            final_report_string = crew.kickoff()
            
            logger.info(f"ApplicationVerificationCrew execution completed. Report length: {len(final_report_string if final_report_string else '')}")
            
            if not final_report_string or not isinstance(final_report_string, str):
                logger.error(f"ApplicationVerificationCrew produced an invalid or empty report. Type: {type(final_report_string)}")
                raw_output_str = str(final_report_string) # Attempt to stringify for logging
                return f"Error: Application verification crew produced an invalid report. Raw output snippet: {raw_output_str[:200]}..."

            return final_report_string.strip()

        except Exception as e:
            logger.error(f"ApplicationVerificationCrew execution failed: {e}", exc_info=True)
            return f"Error during application verification crew processing: {str(e)}"
