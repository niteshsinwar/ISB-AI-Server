# project_root/app/crew/employment_crew.py
import os
import logging
import json
from typing import Dict, Any, List, Literal
from crewai import Agent, Task, Crew, Process
from pydantic import BaseModel, Field, constr
from app.config import (
    GOOGLE_API_KEY, CONFIDENCE_PICKLIST_RANGES,
    MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION,
    MODEL_HTML_SYNTHESIS, TEMP_HTML_SYNTHESIS
)
from app.crew.crew_utils import (
    initialize_llm, clean_and_extract_json, log_error, CrewErrorHandler
)

logger = logging.getLogger(__name__)

# Pydantic Validation Model
class ValidatedCrewReport(BaseModel):
    field_comparison_summary: constr(min_length=1)
    overall_feedback: constr(min_length=1)
    confidence_range: int = Field(..., ge=0, le=100)

# Fields to Exclude
FIELDS_TO_EXCLUDE_FROM_PROCESSING: List[str] = [
     'Applicant__c', 'type', 'Contact', 'recordId', 'Task_Id','triggeringLogId','Id', 'DocumentchecklistItem_Id']

# LLM Initialization
llm_comparator = initialize_llm(MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION, GOOGLE_API_KEY)
llm_reporter = initialize_llm(MODEL_HTML_SYNTHESIS, TEMP_HTML_SYNTHESIS, GOOGLE_API_KEY)

if llm_comparator and llm_reporter:
    logger.info("EmploymentCrew LLMs initialized.")
else:
    logger.critical("Failed to initialize one or more LLMs for EmploymentCrew.")

class EmploymentVerificationAgents:
    def data_comparator_agent(self):
        return Agent(
            role='Data Comparator',
            goal="Compare extracted text from an employment document against Salesforce record data.",
            backstory="A meticulous agent focused on verifying employment history with high accuracy.",
            llm=llm_comparator, verbose=True, allow_delegation=False, max_iter=5
        )

    def final_report_generator_agent(self):
        return Agent(
            role='Final Report Generator',
            goal="Synthesize the comparison analysis into a structured JSON object.",
            backstory="A reporting specialist who creates clean, standardized JSON outputs.",
            llm=llm_reporter, verbose=True, allow_delegation=False, max_iter=3
        )

class EmploymentVerificationTasks:
    def compare_data_task(self, agent: Agent, document_text: str, record_data: Dict[str, Any], verifiable_fields: List[str]):
        return Task(
            description=f"""
            Analyze the provided `DOCUMENT_TEXT` and compare it against the `SALESFORCE_RECORD_DATA`.
            Focus *only* on the following fields from the Salesforce data: {verifiable_fields}.

            For each field, determine if the value from Salesforce is 'Matched', 'Partially Matched', 'Not Matched - Different Format', or 'Not Found in Document'.
            Provide a clear 'Note' explaining your reasoning for each comparison, especially for partial matches or mismatches.
            Format your entire analysis as a single, comprehensive string.

            ---
            DOCUMENT_TEXT:
            {document_text}
            ---
            SALESFORCE_RECORD_DATA:
            {record_data}
            ---
            """,
            agent=agent,
            expected_output="""
            A single string containing a detailed, field-by-field comparison analysis.
            Example for one field: "Company Name: The name 'ABC Corp' in Salesforce matches the name 'ABC Corporation' in the document. Confidence: Matched. Note: Minor variation in name."
            """
        )

    def generate_final_report_task(self, agent: Agent, context: str):
        return Task(
            description=f"""
            Based on the provided comparison analysis, generate a final JSON object with three keys:
            1. 'field_comparison_summary': An HTML table string summarizing the field-by-field analysis. The table must have columns: 'Field Name', 'Record Value', 'Document Value', 'Confidence', and 'Note'.
            2. 'overall_feedback': A concise, one-sentence text summary of the overall findings.
            3. 'confidence_range': An integer between 0 and 100 representing the overall confidence in the match. Base this on the number of matched vs. mismatched fields.

            ---
            COMPARISON_ANALYSIS:
            {context}
            ---
            """,
            agent=agent,
            expected_output="""
            A single, clean, and valid JSON object adhering to the specified structure. Do not include any markdown formatting like ```json.
            Example:
            {
              "field_comparison_summary": "<div...><table...>...</table></div>",
              "overall_feedback": "Key details like company name and dates align, but the job title differs slightly.",
              "confidence_range": 85
            }
            """
        )

class EmploymentVerificationCrewOrchestrator:
    def __init__(self, record_data: Dict[str, Any], document_text: str):
        self.record_data = record_data
        self.document_text = document_text

    @CrewErrorHandler()
    def run(self) -> Dict[str, Any]:
        if not llm_comparator or not llm_reporter:
            raise RuntimeError("LLMs for EmploymentCrew are not initialized.")

        verifiable_apex_field_names = [f for f in self.record_data.keys() if f not in FIELDS_TO_EXCLUDE_FROM_PROCESSING]
        agents = EmploymentVerificationAgents()
        tasks = EmploymentVerificationTasks()

        comparator_agent = agents.data_comparator_agent()
        report_agent = agents.final_report_generator_agent()

        compare_task = tasks.compare_data_task(comparator_agent, self.document_text, self.record_data, verifiable_apex_field_names)
        report_task = tasks.generate_final_report_task(report_agent, "{compare_task_output}")

        crew = Crew(agents=[comparator_agent, report_agent], tasks=[compare_task, report_task], process=Process.sequential, verbose=2)
        result = crew.kickoff()
        
        final_json_str = clean_and_extract_json(str(result))
        if not final_json_str:
            raise ValueError("Crew returned an invalid result. Could not extract JSON.")

        try:
            final_json = json.loads(final_json_str)
            validated_report = ValidatedCrewReport(**final_json)
            return validated_report.model_dump()
        except Exception as e:
            raise ValueError(f"Failed to parse or validate final crew report: {e}")
