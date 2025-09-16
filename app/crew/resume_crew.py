# project_root/app/crew/resume_crew.py
import logging
import json
from typing import Dict, Any, Literal
from crewai import Agent, Task, Crew, Process
from pydantic import BaseModel, constr

from app.config import CREW_GOOGLE_API_KEY, MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION
from app.crew.crew_utils import initialize_llm, clean_and_extract_json, CrewErrorHandler
from app.crew.crew_prompts import (
    RESUME_ANALYZER_AGENT_GOAL,
    RESUME_ANALYZER_AGENT_BACKSTORY,
    RESUME_ANALYSIS_TASK_DESCRIPTION,
    RESUME_ANALYSIS_EXPECTED_OUTPUT,
)

logger = logging.getLogger(__name__)

# MODIFIED Pydantic Validation Model to include 'reason'
class ValidatedResumeReport(BaseModel):
    status: Literal["Accepted", "Not Verified"]
    reason: constr(min_length=1)

# NOTE: Per-job LLM instances will be created inside the Orchestrator to avoid global state.

class ResumeVerificationAgents:
    def resume_screener_agent(self, llm_instance):
        return Agent(
            role='Resume Content Screener',
            goal=RESUME_ANALYZER_AGENT_GOAL,
            backstory=RESUME_ANALYZER_AGENT_BACKSTORY,
            llm=llm_instance,
            verbose=True,
            allow_delegation=False,
            max_iter=3,
        )

class ResumeVerificationTasks:
    def screen_resume_task(self, agent: Agent, document_text: str):
        return Task(
            description=RESUME_ANALYSIS_TASK_DESCRIPTION.format(document_text=document_text),
            agent=agent,
            expected_output=RESUME_ANALYSIS_EXPECTED_OUTPUT,
        )

class ResumeVerificationCrewOrchestrator:
    def __init__(self, document_text: str, resource_manager=None):
        self.document_text = document_text
        self.resource_manager = resource_manager
        # Create isolated LLM instance for this job with resource tracking
        self.llm_screener = initialize_llm(MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION, CREW_GOOGLE_API_KEY, resource_manager)
        if not self.llm_screener:
            raise RuntimeError("Failed to initialize LLM for ResumeCrew")

    @CrewErrorHandler()
    def run(self) -> Dict[str, Any]:
        agents = ResumeVerificationAgents()
        tasks = ResumeVerificationTasks()

        screener_agent = agents.resume_screener_agent(self.llm_screener)
        screen_task = tasks.screen_resume_task(screener_agent, self.document_text)

        crew = Crew(
            agents=[screener_agent],
            tasks=[screen_task],
            process=Process.sequential,
            verbose=2,
        )

        result = crew.kickoff()
        final_json_str = clean_and_extract_json(str(result))
        if not final_json_str:
            raise ValueError("Resume crew returned an invalid or empty result.")

        try:
            final_json = json.loads(final_json_str)
            # MODIFICATION: The model now expects 'reason' as well
            validated_report = ValidatedResumeReport(**final_json)
            return validated_report.model_dump()
        except Exception as e:
            raise ValueError(f"Failed to parse or validate the final resume crew report: {e}")
