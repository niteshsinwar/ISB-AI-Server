# project_root/app/crew/employment_crew.py
import os
import logging
import json
from typing import Dict, Any, List, Literal
from crewai import Agent, Task, Crew, Process
from pydantic import BaseModel, Field, constr
from app.config import (
    CREW_GOOGLE_API_KEY, CONFIDENCE_PICKLIST_RANGES,
    MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION,
    MODEL_HTML_SYNTHESIS, TEMP_HTML_SYNTHESIS
)
from app.crew.crew_utils import (
    initialize_llm, clean_and_extract_json, log_error, CrewErrorHandler
)
from app.crew.crew_prompts import (
    EMPLOYMENT_DATA_COMPARATOR_AGENT_GOAL,
    EMPLOYMENT_DATA_COMPARATOR_AGENT_BACKSTORY,
    FINAL_REPORT_GENERATOR_AGENT_GOAL,
    FINAL_REPORT_GENERATOR_AGENT_BACKSTORY,
    EMPLOYMENT_DATA_COMPARISON_TASK_DESCRIPTION,
    EMPLOYMENT_DATA_COMPARISON_EXPECTED_OUTPUT,
    FINAL_REPORT_GENERATION_TASK_DESCRIPTION,
    FINAL_REPORT_GENERATION_EXPECTED_OUTPUT
)

logger = logging.getLogger(__name__)

# Pydantic Validation Model
class ValidatedCrewReport(BaseModel):
    field_comparison_summary: constr(min_length=1)
    overall_feedback: constr(min_length=1)
    confidence_range: int = Field(..., ge=0, le=100)
    mismatched_field_list: constr(min_length=1)

# Fields to Exclude
FIELDS_TO_EXCLUDE_FROM_PROCESSING: List[str] = [
     'Applicant__c', 'type', 'Contact', 'recordId', 'Task_Id','triggeringLogId','Id', 'DocumentchecklistItem_Id']

# NOTE: Per-job LLM instances will be created inside the Orchestrator to avoid global state.

class EmploymentVerificationAgents:
    def data_comparator_agent(self, llm_instance):
        return Agent(
            role='Data Comparator',
            goal=EMPLOYMENT_DATA_COMPARATOR_AGENT_GOAL,
            backstory=EMPLOYMENT_DATA_COMPARATOR_AGENT_BACKSTORY,
            llm=llm_instance, 
            verbose=True, 
            allow_delegation=False, 
            max_iter=5
        )

    def final_report_generator_agent(self, llm_instance):
        return Agent(
            role='Final Report Generator',
            goal=FINAL_REPORT_GENERATOR_AGENT_GOAL,
            backstory=FINAL_REPORT_GENERATOR_AGENT_BACKSTORY,
            llm=llm_instance, 
            verbose=True, 
            allow_delegation=False, 
            max_iter=3
        )

class EmploymentVerificationTasks:
    def compare_data_task(self, agent: Agent, document_text: str, record_data: Dict[str, Any], verifiable_fields: List[str]):
        return Task(
            description=EMPLOYMENT_DATA_COMPARISON_TASK_DESCRIPTION.format(
                verifiable_fields=verifiable_fields,
                document_text=document_text,
                record_data=record_data
            ),
            agent=agent,
            expected_output=EMPLOYMENT_DATA_COMPARISON_EXPECTED_OUTPUT
        )

    def generate_final_report_task(self, agent: Agent, context: str):
        return Task(
            description=FINAL_REPORT_GENERATION_TASK_DESCRIPTION.format(context=context),
            agent=agent,
            expected_output=FINAL_REPORT_GENERATION_EXPECTED_OUTPUT
        )

class EmploymentVerificationCrewOrchestrator:
    def __init__(self, record_data: Dict[str, Any], document_text: str, resource_manager=None):
        self.record_data = record_data
        self.document_text = document_text
        self.resource_manager = resource_manager
        # Create isolated LLM instances for this job with resource tracking
        self.llm_comparator = initialize_llm(MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION, CREW_GOOGLE_API_KEY, resource_manager)
        self.llm_reporter = initialize_llm(MODEL_HTML_SYNTHESIS, TEMP_HTML_SYNTHESIS, CREW_GOOGLE_API_KEY, resource_manager)
        if not self.llm_comparator or not self.llm_reporter:
            raise RuntimeError("Failed to initialize LLMs for EmploymentCrew")

    @CrewErrorHandler()
    def run(self) -> Dict[str, Any]:
        if not self.llm_comparator or not self.llm_reporter:
            raise RuntimeError("LLMs for EmploymentCrew are not initialized.")

        verifiable_apex_field_names = [f for f in self.record_data.keys() if f not in FIELDS_TO_EXCLUDE_FROM_PROCESSING]
        agents = EmploymentVerificationAgents()
        tasks = EmploymentVerificationTasks()

        comparator_agent = agents.data_comparator_agent(self.llm_comparator)
        report_agent = agents.final_report_generator_agent(self.llm_reporter)

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