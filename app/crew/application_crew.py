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

# --- Configuration for Fields to Exclude from Agent Processing ---
# Define this list at the top for easy modification.
# These fields will be removed from the data dictionary before agents process it.
FIELDS_TO_EXCLUDE_FROM_PROCESSING: List[str] = [
    'Applicant__c',
    'type',         # Assuming 'type' is a generic field you always want to exclude
    'Contact',      # Assuming 'Contact' is a generic field you always want to exclude
    'recordId',     # Common system ID often not needed for verification content
    # Add any other field names you want to globally exclude from agent processing here
]

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

class ApplicationVerificationAgents:
    def data_comparator_agent(self, verifiable_apex_field_names: List[str]) -> Agent: # Parameter name reflects it's already filtered
        if not gemini_llm_application:
            raise RuntimeError("LLM for ApplicationVerificationAgents not initialized. Cannot create agent.")

        apex_field_list_str_for_prompt = ", ".join([f"'{field}'" for field in verifiable_apex_field_names])
        if not apex_field_list_str_for_prompt:
             apex_field_list_str_for_prompt = "an empty list (no verifiable fields after initial filtering)"

        agent_prompt = f"""
You are an Intelligent Application Detail Verification Analyst with human-like intuition, specializing in identity documents.
Your goal is to meticulously verify application details against a supporting identity document, based on the type of ID provided.
Most Important: Ignore Case Differences, Formatting Variations. The Record Data you receive has already had some system-level, non-verifiable fields removed. Focus your verification on the fields present in the provided Record Data and listed as 'Apex Field Names To Process'.

**Input You Receive:**
1.  **Record Data (JSON String)**: Data from Apex, accessible via `salesforce_record_data_json_str`. This data has been pre-processed to remove certain system fields.
2.  **Document Text (Raw Identity Document Content)**.
3.  **List of Apex Field Names To Process**: This is the definitive list of fields from the Record Data that you should consider for verification. It is provided in the task description as `verifiable_apex_field_list_str` (e.g., {apex_field_list_str_for_prompt}).

**Core Application & Identity Concepts & Specific Verification Rules:**
You have a deep understanding of the following canonical concepts. Your final JSON output should include an entry for EACH of these canonical concepts.

1.  **Canonical Concept: 'ID Proof Type'**
    * *Typical Apex Input Field Names*: 'ID Proof Type', 'ID_Proof_Type_From_SF', 'DocumentType', 'ProofType'.
    * *Verification Rule*: This is a CRUCIAL field.
        * First, determine the record's 'ID Proof Type' value by mapping an Apex field (from the 'List of Apex Field Names To Process') to this concept.
        * Then, analyze the document text for keywords (e.g., 'Passport', 'Aadhar Card', 'Republic of India', 'UIDAI', 'Permanent Account Number', 'Driving Licence') to infer the actual document type.
        * 'document_value' should be the inferred document type (e.g., "Passport (inferred from document)", "Aadhar (inferred from document)").
        * 'status' should reflect if the record's 'ID Proof Type' aligns with the inferred document type ('Matched'), differs ('Mismatched - e.g., Record: Passport, Document: Aadhar'), or if the document type is unclear ('Partially Matched (Document Type Unclear)').
        * **The determined actual document type (from document text analysis) will govern the applicability of other fields.** Let's call this the 'Determined Document Type'.

(Once the 'Determined Document Type' is established, process the following canonical concepts)

2.  **Canonical Concept: 'Full Name'**
    * *Typical Apex Input Field Names*: 'Full Name', 'Applicant_Full_Name', 'CandidateName', 'Name', 'CompleteName'.
    * *Verification Rule*: Always applicable. Aim for a 100% match with the record. Account for common name order variations. Minor middle name/initial discrepancies can be 'Partially Matched (Acceptable Variation)'. Significant differences are 'Mismatched'.

3.  **Canonical Concept: 'Passport Number'**
    * *Typical Apex Input Field Names*: 'Passport Number', 'Passport_ID', 'PassportNo'.
    * *Verification Rule*: **Applicable only if 'Determined Document Type' is 'Passport'.**
        * If applicable: Extract from document. Must match record value fully for 'Matched'.
        * If not applicable: 'document_value' is 'Not Applicable (Document is [Determined Document Type])', status 'Not Applicable'.

4.  **Canonical Concept: 'Birthdate'** (or 'Date of Birth')
    * *Typical Apex Input Field Names*: 'Birthdate', 'Date_of_Birth', 'DOB'.
    * *Verification Rule*: Always applicable. Must match. Be robust with date formats (e.g., DD/MM/YY, MM/DD/YY, YYYY-MM-DD, Month D, YYYY). If document has less detail (e.g., only YYYY-MM) but aligns with record, use 'Partially Matched (Detail Variance)'.

5.  **Canonical Concept: 'Gender'**
    * *Typical Apex Input Field Names*: 'Gender', 'Sex'.
    * *Verification Rule*: Always applicable. Aim for 'Matched' for exact matches or common, verifiable abbreviations (M/F/O/Male/Female/Other).

6.  **Canonical Concept: 'Passport Expiry Date'** (or 'PassportExpiryDate')
    * *Typical Apex Input Field Names*: 'PassportExpiryDate', 'Passport_Expiry', 'ExpiryDateOfPassport'.
    * *Verification Rule*: **Applicable only if 'Determined Document Type' is 'Passport'.**
        * If applicable: Extract from document. Note if the passport is expired (date in the past) in 'notes'. Compare with record.
        * If not applicable: 'document_value' is 'Not Applicable (Document is [Determined Document Type])', status 'Not Applicable'.

7.  **Canonical Concept: 'Nationality'**
    * *Typical Apex Input Field Names*: 'Nationality', 'Citizenship', 'CountryOfCitizenship'.
    * *Verification Rule*:
        * **If 'Determined Document Type' is 'Passport'**: Extract the issuing country from the document. This is the 'document_value' for 'Nationality'. Compare with record's 'Nationality'.
        * **If 'Determined Document Type' is 'Aadhar'**: The document's implied nationality is 'India'. Use 'India (implied by Aadhar)' as 'document_value' for 'Nationality'. Compare with record's 'Nationality'.
        * **For other 'Determined Document Types'**: If nationality is clearly inferable from the document, use that. Otherwise, 'document_value' is 'Not Determinable from Document'.
        * This concept is generally applicable but its document value derivation is conditional.

8.  **Canonical Concept: 'Aadhar Card Number'** (or 'Aadhar No')
    * *Typical Apex Input Field Names*: 'Aadhar Card Number', 'Aadhar No', 'UID', 'AadhaarNumber'.
    * *Verification Rule*: **Applicable only if 'Determined Document Type' is 'Aadhar'.**
        * If applicable: Extract from document. Must match record value fully. Handle masked Aadhar (e.g., 'XXXX-XXXX-1234') by comparing visible last 4 digits; status 'Matched (Masked Aadhar Verified)'.
        * If not applicable: 'document_value' is 'Not Applicable (Document is [Determined Document Type])', status 'Not Applicable'.

**Your Verification Process & Output Structure:**
Let `processed_apex_fields` be a set to keep track of Apex field names from the **List of Apex Field Names To Process** that have been mapped to a Canonical Concept.
Let `results_list` be an empty list to store your output objects.

**Part 1: Determine 'ID Proof Type' and 'Determined Document Type' (Special Handling for the First Concept)**
   - Process the **'ID Proof Type'** Canonical Concept:
     A.  **Find Record Value**:
         * Examine its 'Typical Apex Input Field Names'.
         * For each typical name, check if it is present in the **List of Apex Field Names To Process**.
         * If a match is found (use the first one found):
           * Set `record_value` to the value of this matched Apex field from the Record Data.
           * Set `original_apex_field_name` to this matched Apex field name. Add it to `processed_apex_fields`.
           * Proceed to B.
         * If no typical name for 'ID Proof Type' is found in the **List of Apex Field Names To Process**:
           * Set `record_value` to 'Not Provided in Record'.
           * Set `original_apex_field_name` to 'N/A'.
     B.  **Apply Verification Rule**: Follow the 'ID Proof Type' Verification Rule (analyze document, determine 'Determined Document Type', etc.).
     C.  **Construct Output Object**: Create the JSON output object for 'ID Proof Type' with all required keys.
     D.  Add this object to `results_list`.

**Part 2: Process Other Canonical Concepts (In Order)**
   - For EACH of the *other* Canonical Concepts listed above (from 'Full Name' to 'Aadhar Card Number'), in the specified order:
     A.  **Find Record Value for the current Canonical Concept**:
         * Examine its 'Typical Apex Input Field Names'.
         * For each typical name, check if it is present in the **List of Apex Field Names To Process** AND has NOT already been added to `processed_apex_fields`.
         * If such a match is found (use the first one found that hasn't been processed):
           * Set `record_value` to the value of this matched Apex field from Record Data.
           * Set `original_apex_field_name` to this matched Apex field name. Add it to `processed_apex_fields`.
           * Break from checking other typical names for this current Canonical Concept.
         * If, after checking all its typical names, no suitable match is found in the **List of Apex Field Names To Process** (or all were already processed):
           * Set `record_value` to 'Not Provided in Record'.
           * Set `original_apex_field_name` to 'N/A'.
     B.  **Check Applicability**: Based on the 'Determined Document Type' (from Part 1), is this Canonical Concept applicable?
     C.  **Extract from Document & Verify (If Applicable)**:
         * If applicable: Extract value from Document Text. Apply the Specific Verification Rule for this Canonical Concept. Determine 'status' and 'confidence'.
         * If not applicable: Set 'document_value' to 'Not Applicable (Document is [Determined Document Type])' and 'status' to 'Not Applicable'. 'confidence' is 'High'.
     D.  **Construct Output Object**: Create the JSON output object for this Canonical Concept. Add it to `results_list`.

**Part 3: Handle Unmapped Custom Apex Fields (Fields from the 'List of Apex Field Names To Process' that weren't mapped to Canonical Concepts)**
    - Iterate through each `apex_field_name` in the **List of Apex Field Names To Process**.
    - If `apex_field_name` is NOT in `processed_apex_fields`:
        * This is a custom field.
        * Construct an output object:
            * `'field_name'`: Use the `apex_field_name` itself.
            * `'original_apex_field_name'`: Same as `apex_field_name`.
            * `'record_value'`: Its value from Record Data.
            * `'document_value'`: Attempt generic extraction or set to 'Not Applicable (Custom Field)'.
            * `'status'`: 'Info Extracted (Custom Field)' or 'Needs Manual Review (Custom Field)'.
            * `'confidence'`: 'Medium' or 'Low'.
            * `'notes'`: "Processed as a custom field not matching predefined canonical concepts."
        * Add this custom field object to `results_list`.

**Final Output**:
Return `results_list` as a single, valid JSON array string. Each object must include:
* `'field_name'`: The Canonical Concept name (or original Apex name for custom fields).
* `'original_apex_field_name'`: The actual field name from the Apex payload that mapped to this concept (or 'N/A', or same as field_name for custom).
* `'record_value'`: Value from Apex record, or 'Not Provided in Record'.
* `'document_value'`: Value from document, 'Not Found in Document', 'Not Applicable...', or 'Calculated...'.
* `'status'`: 'Matched', 'Mismatched', 'Partially Matched (Acceptable Variation)', 'Partially Matched (Detail Variance)', 'Matched (Masked Aadhar Verified)', 'Found in Record Only', 'Found in Document Only', 'Not Found in Either', 'Not Applicable', 'Record Inconsistency', 'Info Extracted (Custom Field)', 'Needs Manual Review (Custom Field)'.
* `'confidence'`: High, Medium, Low.
* `'notes'`: Crucial for explaining status, conditional logic, mapping decisions, discrepancies, etc.
"""
        return Agent(
            role="Advanced Application Detail Verification Analyst",
            goal=agent_prompt,
            backstory=(
                "You are an AI expert at verifying application identity details against official documents. "
                "You understand varied field names, apply contextual rules with precision, especially conditional logic based on ID Proof Type, "
                "and can map inputs (from a pre-filtered set of verifiable fields) to known concepts. You handle unmapped verifiable fields gracefully."
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
                "Each object includes 'field_name' (a canonical concept or original Apex name), 'original_apex_field_name', and other verification details.\n"
                "Format this into a single, human-readable string report starting with 'Application Identity Verification Details:'.\n"
                "Most Important: Ignore Case Differences, Formatting Variations, and DO NOT Include Field Like Record Id\n"
                "For each field from the JSON, list:\n"
                "- Field: [field_name] (Original Apex Field: [original_apex_field_name])\n"
                "  Record Value: [record_value]\n"
                "  Document Value: [document_value]\n"
                "  Status: [status] (Confidence: [confidence])\n"
                "  Notes: [notes]\n\n"
                "After the field-by-field breakdown, provide a concise 1-2 line 'Overall Feedback'. "
                "This feedback must intelligently summarize the verification outcome, focusing on CRITICAL discrepancies "
                "(e.g., 'Mismatched' ID numbers for the *relevant* ID type, significantly different 'Full Name', an expired 'PassportExpiryDate' if applicable, "
                "a mismatch in 'Nationality' vs. document-implied nationality, or a 'Mismatched' 'ID Proof Type' itself) or critical missing information. "
                "Downplay minor partial matches if notes indicate reasonable explanations or adherence to specific rules (e.g., masked Aadhar, 'Not Applicable' fields due to ID type)."
            ),
            backstory=(
                "You are a skilled report writer, synthesizing application verification data into clear, concise, and actionable summaries, highlighting what truly matters based on the detailed comparison, client guidelines, and the conditional nature of identity field verification."
            ),
            llm=gemini_llm_application,
            verbose=True,
            allow_delegation=False,
        )

class ApplicationVerificationTasks:
    def compare_data_and_output_json_task(self, agent: Agent, salesforce_record_data_json_str: str, document_text: str, verifiable_apex_field_list_str: str) -> Task:
        return Task(
            description=(
                "Perform a comprehensive verification of application identity details using the provided pre-processed Salesforce Record Data and the list of verifiable Apex fields.\n\n"
                f"**Salesforce Record Data (JSON String from Apex - pre-processed to remove certain system fields):**\n```json\n{salesforce_record_data_json_str}\n```\n\n"
                f"**Document Text (Raw Identity Document):**\n```text\n{document_text}\n```\n\n"
                f"**List of Apex Field Names To Process (this list has already been filtered):** {verifiable_apex_field_list_str}\n\n"
                "Most Important: Ignore Case Differences, Formatting Variations.\n"
                "**Your Process & Rules:** Strictly follow the multi-part process defined in your agent's goal. This includes: "
                "1. Determining the 'ID Proof Type' and 'Determined Document Type' by mapping known Apex fields from the 'List of Apex Field Names To Process'. "
                "2. For all other defined Canonical Concepts: attempt to map known Apex fields (from 'Typical Apex Input Field Names') found within the 'List of Apex Field Names To Process'. Then check applicability, extract from document, and apply rules. "
                "3. Handle any fields from the 'List of Apex Field Names To Process' that were not mapped to canonical concepts as custom fields. "
                "Ensure your output JSON array covers all predefined canonical concepts (reporting 'Not Provided in Record' if no data maps from the processed list) and any additional custom fields identified from the processed list.\n"
                "**Final Output of this Task**: A single, valid JSON array string of comparison objects."
            ),
            agent=agent,
            expected_output=(
                "A valid JSON array string. Each object in the array must detail a field's comparison and include: "
                "'field_name' (canonical concept name or original Apex name for custom fields), 'original_apex_field_name', "
                "'record_value', 'document_value', 'status', 'confidence', and 'notes'. "
                "The output array should consistently cover all predefined canonical concepts and any custom fields identified from the 'List of Apex Field Names To Process', respecting conditional applicability."
            )
        )

    def generate_formatted_report_task(self, agent: Agent, context_tasks: list) -> Task:
        return Task(
            description=(
                "You have received a JSON array string (from context) detailing field-by-field application data comparisons. "
                "This JSON includes 'original_apex_field_name' alongside 'field_name'.\n"
                "Transform this JSON into a single, human-readable string report as per your agent's goal. "
                "Ensure your report displays both 'field_name' and 'original_apex_field_name' for each item for maximum clarity. "
                "Your 'Overall Feedback' should be insightful, considering conditional field applicability (e.g., Passport specific fields) and any custom fields."
            ),
            agent=agent,
            context=context_tasks,
            expected_output=(
                "A single string containing 'Application Identity Verification Details:' with a field-by-field breakdown "
                "(showing both logical 'field_name' and 'original_apex_field_name'), followed by a concise 'Overall Feedback:'."
            )
        )

class ApplicationVerificationCrewOrchestrator:
    def __init__(self, record_data_dict: Dict[str, Any], document_text: str):
        # self.original_record_data_dict = record_data_dict.copy() # Optional: if you need the true original later

        # Create a working copy of the record data to be cleaned
        self.processed_record_data_dict = record_data_dict.copy()
        for field_to_remove in FIELDS_TO_EXCLUDE_FROM_PROCESSING:
            if field_to_remove in self.processed_record_data_dict:
                del self.processed_record_data_dict[field_to_remove]
        
        # This JSON string is now based on the cleaned dictionary
        self.salesforce_record_data_json_str = json.dumps(self.processed_record_data_dict, indent=2)
        self.document_text = document_text
        self.agents_provider = ApplicationVerificationAgents()
        self.tasks_provider = ApplicationVerificationTasks()

        # This list of field names is now derived from the cleaned dictionary
        self.verifiable_apex_field_names: List[str] = list(self.processed_record_data_dict.keys())

        logger.info(
            f"ApplicationVerificationCrewOrchestrator initialized. "
            f"Original Apex fields received: {list(record_data_dict.keys())}. " # Log original keys for reference
            f"Verifiable Apex Fields for Agent (after excluding {FIELDS_TO_EXCLUDE_FROM_PROCESSING}): {self.verifiable_apex_field_names}. "
            f"Doc length: {len(document_text)}."
        )
        logger.debug(f"Salesforce Record (cleaned for agent processing) for Agent: {self.salesforce_record_data_json_str}")


    def run(self) -> str:
        if not gemini_llm_application:
            logger.error("ApplicationVerificationCrew cannot run: LLM not initialized.")
            return "Error: LLM for application verification not available. Please check GOOGLE_API_KEY."

        if not self.verifiable_apex_field_names: # Check the list of fields intended for processing
            logger.warning("ApplicationVerificationCrew: No verifiable fields to process after initial filtering. Returning empty report.")
            return "Application Identity Verification Details:\n\nNo verifiable data provided from Salesforce record for processing.\n\nOverall Feedback: No verifiable data to process."
        try:
            # Pass the list of fields the agent SHOULD process
            comparator_agent = self.agents_provider.data_comparator_agent(
                verifiable_apex_field_names=self.verifiable_apex_field_names
            )
            report_generator_agent = self.agents_provider.final_report_generator_agent()

            # This string is for the task description, representing the fields the agent will work with
            verifiable_apex_field_list_str_for_task = ", ".join([f"'{f}'" for f in self.verifiable_apex_field_names])
            if not verifiable_apex_field_list_str_for_task: # Should not happen if self.verifiable_apex_field_names is not empty
                verifiable_apex_field_list_str_for_task = "none (all fields were filtered out)"


            task1_compare_and_structure = self.tasks_provider.compare_data_and_output_json_task(
                agent=comparator_agent,
                salesforce_record_data_json_str=self.salesforce_record_data_json_str, # This is now the JSON of the cleaned dict
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

            logger.info(f"ApplicationVerificationCrew execution completed. Report length: {len(final_report_string if final_report_string else '')}")

            if not final_report_string or not isinstance(final_report_string, str):
                logger.error(f"ApplicationVerificationCrew produced an invalid or empty report. Type: {type(final_report_string)}")
                raw_output_str = str(final_report_string) if final_report_string is not None else "None"
                return f"Error: Application verification crew produced an invalid report. Raw output: {raw_output_str[:200]}..."

            return final_report_string.strip()

        except Exception as e:
            logger.error(f"ApplicationVerificationCrew execution failed: {e}", exc_info=True)
            return f"Error during application verification crew processing: {str(e)}"