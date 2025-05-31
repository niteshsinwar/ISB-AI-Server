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
gemini_llm_employment = None
if GOOGLE_API_KEY:
    try:
        gemini_llm_employment = ChatGoogleGenerativeAI(
            model=GEMINI_MODEL_NAME,
            temperature=0.15, # Slightly lower temperature for more deterministic financial/date extraction
            google_api_key=GOOGLE_API_KEY,
        )
        logger.info(f"EmploymentVerificationCrew LLM initialized with model: {GEMINI_MODEL_NAME}")
    except Exception as e:
        logger.error(f"Failed to initialize LLM for EmploymentVerificationCrew ({GEMINI_MODEL_NAME}): {e}", exc_info=True)
        gemini_llm_employment = None
else:
    logger.critical("EMPLOYMENT_CREW: GOOGLE_API_KEY environment variable not set. LLM will not be available.")


# Refined VERIFICATION_FIELDS based on client instructions and common needs.
# 'Currently Employed Status' will be derived by the agent.
# 'Bonus Amount' is added to assist with salary calculations.
VERIFICATION_FIELDS = [
    "Applicant Name",           # To cross-verify the document pertains to the applicant
    "Company Name",             # SF: Company_Name__c on Affiliation
    "Designation/Job Title",    # SF: Designation_Name__c on Affiliation
    "Start Date",               # SF: Start_Date_Formula__c on Affiliation
    "End Date",                 # SF: End_Date_Formula__c on Affiliation
    "Currently Employed Status",# Derived: Yes/No/Unclear
    "Salary Amount (Gross)",    # SF: Compensation_End_Amount__c on Affiliation (assumed annual)
    "Salary Currency",          # Extracted from document
    "Salary Frequency",         # Extracted from document (e.g., Monthly, Annual, Weekly)
    "Bonus Amount (if specified separately)" # Extracted from document
]

class EmploymentVerificationAgents:
    def data_comparator_agent(self) -> Agent:
        if not gemini_llm_employment:
            raise RuntimeError("LLM for EmploymentVerificationAgents not initialized. Cannot create agent.")
        return Agent(
            role="Expert Employment & Compensation Verification Analyst",
            goal=(
                "You are provided with: "
                "1. Structured Salesforce record data (JSON string from an Affiliation object, detailing one employment). "
                "2. Raw text from a supporting employment document (e.g., offer letter, experience letter, payslip, relieving letter, ITR, Form16). "
                "Your multi-step goal is to: "
                "  a. From the document text, meticulously extract values for the predefined fields: "
                f"     `{', '.join(VERIFICATION_FIELDS)}`. Apply nuanced understanding for variations. "
                "     If a value is not explicitly found, use 'Not Found in Document'. "
                "  b. For 'Currently Employed Status', infer 'Yes' if End Date implies ongoing employment (e.g., 'Present', document date is recent and role is current) or if no end date is mentioned in a context suggesting current employment. Infer 'No' if a clear past End Date is found. Otherwise, 'Unclear'. "
                "  c. Apply specific client rules and human-like intuition during extraction and comparison: "
                "     - **Applicant Name**: Verify if the name on the document closely matches the expected applicant's name (usually part of the SF record or context). Note any variations. "
                "     - **Company Name**: "
                "         - Match should be positive for variations like 'PwC India' vs. 'India Pricewaterhouse&Co' or 'Accordion (formerly known as Merilytics)' vs. 'ACCORDION PARTNERS INDIA PRIVATE LIMITED'. Handle common legal suffixes (Pvt Ltd, Inc., LLC, Limited). Status 'Matched' or 'Partially Mat हम (Acceptable Variation)' with notes. "
                "     - **Designation/Job Title**: "
                "         - Aim for exact match (e.g., 'Associate' = 'Associate'). Status 'Matched'. "
                "         - Different distinct roles are 'Mismatched' (e.g., 'Software Test Engineer' vs 'Functional Test Engineer'). "
                "         - Minor variations or clear hierarchical/synonymous terms (e.g. 'Sr. Developer' vs 'Senior Software Engineer') can be 'Partially Matched (Acceptable Variation)'. "
                "     - **Dates (Start Date, End Date)**: "
                "         - Handle YYYY-MM-DD, YYYY-MM, Month YYYY (e.g., '07' or 'July'). "
                "         - If End Date in document implies ongoing ('Present', 'Till Date', current date on payslip) and Salesforce record has no End Date, this is 'Matched' for 'Currently Employed Status' and 'Implied Match (Ongoing)' for 'End Date' field itself. "
                "         - If Year/Month match but day differs, or one source has day and other doesn't, status 'Partially Matched (Detail Variance)'. "
                "     - **Salary (Amount, Currency, Frequency) & Bonus**: "
                "         - Extract Gross Salary Amount, its Currency (map symbols like ₹ to INR, $ to USD), and Frequency (Weekly, Monthly, Annually from cues like 'p.m.', 'p.a.'). Extract any separately mentioned Bonus Amount. "
                "         - The Salesforce 'Salary Amount (Gross)' is assumed to be ANNUAL. "
                "         - **Calculation for Comparison**: Convert document salary to an ANNUAL figure. "
                "           - If document frequency is Monthly: (Document Monthly Gross * 12) + Document Annual Bonus (if any, or annualized monthly bonus). "
                "           - If document frequency is Weekly: (Document Weekly Gross * 52) + Document Annual Bonus (if any). "
                "           - If document frequency is Annual: Use Document Annual Gross + Document Annual Bonus (if any). "
                "         - **Comparison & Status**: Compare this calculated Annual Document Salary with the Annual Salesforce Salary. "
                "           - If they match exactly or are within a **20% variation**, status is 'Matched'. Note the calculated values and percentage difference if not exact. "
                "           - If outside 20% variation, status is 'Mismatched'. Note calculated values and percentage difference. "
                "           - If currency in document differs from implied currency of SF record (assume local if not specified), it's 'Mismatched', even if numbers are close. Note both currencies. "
                "           - If frequency cannot be determined from the document, status is 'Needs Human Review' for salary, note available figures. "
                "  d. For each predefined field, create a JSON object detailing: 'field_name', 'record_value' (from SF JSON, use 'Not Provided in Record' if missing/null), 'document_value' (your extraction/derivation), "
                "     'status' ('Matched', 'Mismatched', 'Partially Matched (Acceptable Variation)', 'Partially Matched (Detail Variance)', 'Implied Match (Ongoing)', 'Found in Record Only', 'Found in Document Only', 'Not Found in Either', 'Needs Human Review'), "
                "     'confidence' (High, Medium, Low), 'notes' (CRUCIAL: explain status, variations, calculations performed, rules applied, missing info, or why it 'Needs Human Review'). "
                "Output a single, valid JSON array string of these comparison objects."
            ),
            backstory=(
                "You are an AI system with deep expertise in verifying employment and compensation details from diverse documentation. You meticulously apply client-specific rules for matching company names, designations, dates, and complex salary calculations including bonuses and acceptable variations. Your structured output is vital for due diligence."
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
                "Format this into a single, human-readable string report starting with 'Employment Verification Details:'. "
                "List each field's comparison clearly. "
                "After the field-by-field breakdown, provide a concise 1-2 line 'Overall Feedback'. "
                "This feedback must intelligently summarize the verification outcome, focusing on CRITICAL discrepancies "
                "(e.g., 'Mismatched' Company Name, 'Mismatched' Designation if roles are fundamentally different, "
                "substantial differences in Start/End Dates affecting tenure, major Salary disparities beyond the 20% acceptable variation, "
                "or if 'Currently Employed Status' is 'Mismatched'). "
                "Also highlight if 'Needs Human Review' status appears for critical fields like Salary. "
                "Downplay 'Partially Matched (Acceptable Variation)' or 'Partially Matched (Detail Variance)' statuses if notes indicate reasonable explanations. "
                "The feedback should reflect a human-like assessment of whether the document substantially supports the employment claims on record."
            ),
            backstory=(
                "You are a skilled report writer who synthesizes complex employment verification data into clear, actionable summaries. You focus on materiality and the overall picture of an employment claim's veracity based on predefined rules including acceptable salary variations."
            ),
            llm=gemini_llm_employment,
            verbose=True,
            allow_delegation=False,
        )

class EmploymentVerificationTasks:
    def compare_data_and_output_json_task(self, agent: Agent, salesforce_record_data_json_str: str, document_text: str) -> Task:
        field_list_str = ", ".join(VERIFICATION_FIELDS)
        return Task(
            description=(
                "Perform a detailed verification of employment information. You are given Salesforce record data (from an Affiliation object representing one employment) and raw text from a supporting document (like an offer letter, experience letter, payslip, ITR, or Form 16).\n\n"
                f"**Predefined Fields for Verification & Extraction from Document:**\n`{field_list_str}`\n\n"
                f"**Salesforce Record Data (JSON String - Affiliation Details):**\n```json\n{salesforce_record_data_json_str}\n```\n"
                f"This Salesforce data typically includes fields like 'Company Name', 'Designation/Job Title', 'Start Date', 'End Date', and an ANNUAL 'Salary Amount (Gross)'.\n\n"
                f"**Document Text (Raw):**\n```text\n{document_text}\n```\n\n"
                "**Your Mandated Process & Client Rules:**\n"
                "1.  **Extract from Document Text**: For each predefined field, extract its value from the 'Document Text'. If not found, state 'Not Found in Document'.\n"
                "    - **Applicant Name**: Cross-verify with applicant's expected name.\n"
                "    - **Company Name**: Crucially, match variations like 'PwC India' with 'India Pricewaterhouse&Co' or 'Accordion (formerly known as Merilytics)' with 'ACCORDION PARTNERS INDIA PRIVATE LIMITED'. Note such equivalent matches.\n"
                "    - **Designation/Job Title**: 'Associate' must match 'Associate'. 'Software Test Engineer' is a MISMATCH to 'Functional Test Engineer' if they are distinct roles. Minor synonyms are 'Partially Matched (Acceptable Variation)'.\n"
                "    - **Start Date / End Date**: Handle formats like YYYY-MM-DD, YYYY-MM, or Month YYYY (e.g., '07' or 'July').\n"
                "    - **Currently Employed Status**: Infer 'Yes' if End Date is 'Present', 'Till Date', or document context (e.g., recent payslip for current role) implies ongoing. Infer 'No' for clear past End Date. Otherwise 'Unclear'.\n"
                "    - **Salary & Bonus**: From the document, extract Gross Salary Amount, Currency (map ₹ to INR, $ to USD etc.), and Frequency (Weekly, Monthly, Annual). Also extract any separately mentioned Bonus Amount (annual or otherwise).\n"
                "2.  **Salary Comparison (Critical)**:\n"
                "    a.  The 'Salary Amount (Gross)' from the Salesforce record is assumed to be an ANNUAL figure.\n"
                "    b.  Calculate Total Annual Document Salary: \n"
                "        - If doc frequency is Monthly: (Doc Monthly Gross * 12) + Total Annualized Bonus from Doc (if any).\n"
                "        - If doc frequency is Weekly: (Doc Weekly Gross * 52) + Total Annualized Bonus from Doc (if any).\n"
                "        - If doc frequency is Annual: Doc Annual Gross + Total Annualized Bonus from Doc (if any).\n"
                "    c.  Compare this Calculated Annual Document Salary with the Annual Salesforce Salary.\n"
                "    d.  **20% Variation Rule**: If the Calculated Annual Document Salary is within a 20% difference (higher or lower) of the Annual Salesforce Salary, the status for 'Salary Amount (Gross)' is 'Matched'. Note the percentage difference.\n"
                "    e.  If the difference is > 20%, the status is 'Mismatched'. Note the percentage difference.\n"
                "    f.  If document currency differs from the implied currency of the SF record, salary status is 'Mismatched' (note both currencies).\n"
                "    g.  If salary frequency cannot be determined from the document to perform annualization, set salary status to 'Needs Human Review' and provide any figures found.\n"
                "3.  **Structure Output**: For EACH predefined field, create a JSON object: {'field_name', 'record_value', 'document_value', 'status', 'confidence', 'notes'}. 'record_value' should be 'Not Provided in Record' if missing. 'status' options: 'Matched', 'Mismatched', 'Partially Matched (Acceptable Variation)', 'Partially Matched (Detail Variance)', 'Implied Match (Ongoing)', 'Found in Record Only', 'Found in Document Only', 'Not Found in Either', 'Needs Human Review'. 'notes' MUST explain the status, especially for salary calculations, variations, company name equivalencies, and date interpretations.\n\n"
                "**Final Output Requirement**: A single, valid JSON array string of these structured comparison objects, adhering to all rules."
            ),
            agent=agent,
            expected_output=(
                "A valid JSON array string. Each object details a field's comparison: "
                "'field_name', 'record_value', 'document_value', 'status', 'confidence', 'notes'. "
                "Salary comparison must adhere to the 20% variation rule and annualization logic."
            )
        )

    def generate_formatted_report_task(self, agent: Agent, context_tasks: list) -> Task:
        return Task(
            description=(
                "You have received a JSON array string (from the context of a previous task) which contains detailed field-by-field comparisons of employment data including complex salary analysis based on client rules (like 20% variation acceptability). "
                "Each object in this array includes 'field_name', 'record_value', 'document_value', 'status', 'confidence', and 'notes'.\n\n"
                "Your task is to transform this JSON array into a single, human-readable string report. The report must start with the heading 'Employment Verification Details:'.\n"
                "Following this heading, for each field comparison object from the JSON array, list the information in this format:\n"
                "- Field: [field_name]\n"
                "  Record Value: [record_value]\n"
                "  Document Value: [document_value]\n"
                "  Status: [status] (Confidence: [confidence])\n"
                "  Notes: [notes]\n\n"
                "After detailing all fields, provide a concise 'Overall Feedback' section of 1-2 lines. This feedback should be an intelligent summary of the verification. "
                "Focus on critical discrepancies, such as: 'Mismatched' Company Name (after applying equivalence rules), significantly different employment tenures implied by Start/End Dates, "
                "Salary Amounts that are 'Mismatched' (i.e., outside the 20% acceptable variation after annualization and bonus considerations), "
                "fundamentally different job titles, or a 'Mismatched' 'Currently Employed Status'. "
                "Also, explicitly mention if 'Salary Amount (Gross)' or other key fields have a status of 'Needs Human Review'. "
                "If 'Partially Matched' statuses are due to acceptable variations clearly explained in the notes (like salary frequency conversion resulting in a 'Matched' status due to the 20% rule, or minor name tweaks for known company aliases), these should not be the primary focus of a negative overall feedback unless they collectively indicate a larger issue. "
                "The feedback's aim is to give a human-like assessment of whether the provided document substantially supports the key employment claims found in the Salesforce record, considering all client-specified rules."
            ),
            agent=agent,
            context=context_tasks,
            expected_output=(
                "A single string containing the 'Employment Verification Details:' section with a clear, field-by-field breakdown, "
                "followed by a concise 'Overall Feedback:' (1-2 lines) summarizing the findings, explicitly considering the 20% salary variation rule and other client instructions."
            )
        )

class EmploymentVerificationCrewOrchestrator:
    def __init__(self, record_data_dict: Dict[str, Any], document_text: str):
        self.salesforce_record_data_json_str = json.dumps(record_data_dict, indent=2)
        self.document_text = document_text
        self.agents_provider = EmploymentVerificationAgents()
        self.tasks_provider = EmploymentVerificationTasks()
        logger.info(f"EmploymentVerificationCrewOrchestrator initialized. Document text length: {len(document_text)}.")

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
            
            if not final_report_string or not isinstance(final_report_string, str):
                logger.error(f"EmploymentVerificationCrew produced an invalid or empty report. Type: {type(final_report_string)}")
                raw_output_str = str(final_report_string) if final_report_string is not None else "None"
                return f"Error: Employment verification crew produced an invalid report. Raw output snippet: {raw_output_str[:200]}..."

            return final_report_string.strip()

        except Exception as e:
            logger.error(f"EmploymentVerificationCrew execution failed: {e}", exc_info=True)
            return f"Error during employment verification crew processing: {str(e)}"