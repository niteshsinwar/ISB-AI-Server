import logging
import json
from typing import Dict, Any, List
from langgraph.graph import StateGraph, END

from app.config import (
    MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION,
    LLM_FIELD_EXCLUSIONS
)
from app.langgraph.graph_prompts import (
    TEST_SCORE_DATA_COMPARATOR_AGENT_GOAL,
    TEST_SCORE_DATA_COMPARATOR_AGENT_BACKSTORY,
    TEST_SCORE_DATA_COMPARISON_TASK_DESCRIPTION,
    TEST_SCORE_DATA_COMPARISON_EXPECTED_OUTPUT,
)
from app.langgraph.state import VerificationState
from app.langgraph.graph_utils import (
    get_llm, format_record_for_llm, format_fields_for_llm
)
from app.langgraph.report_builder import build_verification_report, parse_comparison_json
from app.langgraph.schemas import ValidatedCrewReport

logger = logging.getLogger(__name__)

# Test_Mode is graph-specific: used for early-exit automation, excluded from LLM
# Additional exclusions: internal hed__Test__c fields not verifiable from a test scorecard
_TEST_SCORE_INTERNAL_FIELDS: List[str] = [
    # ISB / application metadata
    'Applicant_Test_Status__c', 'Cohort_Name__c', 'Program_Name__c',
    'Consider_for_calculation__c', 'More_than_onces__c', 'Repeated__c',
    'ReportISBScoreBoard__c', 'Status__c',
    # Salesforce object internals
    'OwnerId', 'RecordTypeId', 'Name',
    # Lookup IDs (non-verifiable foreign keys)
    'Application__c', 'Test_Score__c',
    # EDA / HED internal fields
    'hed__Credentialing_Identifier__c', 'hed__Credits_Earned__c',
    'hed__Source__c', 'hed__Verification_Status__c', 'hed__Verification_Status_Date__c',
    'hed__Test_Type__c', 'hed__Contact__c',
    # Misc internal
    'Location_ID__c', 'Probability__c', 'Appeared__c',
]
FIELDS_TO_EXCLUDE_FROM_PROCESSING: List[str] = [*LLM_FIELD_EXCLUSIONS, 'Test_Mode', *_TEST_SCORE_INTERNAL_FIELDS]

_TEST_SCORE_BASE_CRITICAL_FIELDS = {
    "applicantName",
    "Applicant_Name",
    "Candidate_Name__c",
    "RecordTypeName__c",
    "testType",
    "Test Type",
    "hed__Test_Date__c",
    "Test Date",
    "Birthdate__c",
    "Applicant_Birthdate",
    "API_Birthdate",
    "Registration_No",
    "Registration_No__c",
    "Test_ID",
    "Test_ID__c",
    "Email",
    "Email__c",
}


def _test_score_critical_fields(record_data: Dict[str, Any]) -> set[str]:
    critical = set(_TEST_SCORE_BASE_CRITICAL_FIELDS)
    for field_name in record_data:
        normalized = field_name.casefold()
        if any(token in normalized for token in ("score", "percentile", "birthdate", "test_date", "registration", "test_id", "email")):
            critical.add(field_name)
    return critical


class TestScoreGraphNodes:
    def __init__(self):
        # Using 2.5 Flash for LangGraph processing
        self.llm_comparator = get_llm(MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION)

    def comparator_node(self, state: VerificationState) -> Dict[str, Any]:
        """
        Comparison Step: Compares Record Data vs Document Text for Test Scores.
        Implements three-way validation (API vs Applicant vs Document).
        Uses LLM-friendly plain text formatting for efficient token usage.
        """
        logger.info("Executing Test Score Comparator Node")

        # Format data as LLM-friendly plain text (not JSON)
        formatted_fields = format_fields_for_llm(state['verifiable_fields'])
        formatted_record = format_record_for_llm(state['record_data'], state['verifiable_fields'])

        prompt = f"""
{TEST_SCORE_DATA_COMPARATOR_AGENT_GOAL}
{TEST_SCORE_DATA_COMPARATOR_AGENT_BACKSTORY}

TASK:
{TEST_SCORE_DATA_COMPARISON_TASK_DESCRIPTION.format(
    verifiable_fields=formatted_fields,
    record_data=formatted_record,
    document_text=state['document_text']
)}

EXPECTED OUTPUT:
{TEST_SCORE_DATA_COMPARISON_EXPECTED_OUTPUT}
"""

        comparisons = None
        for attempt in range(2):
            response = self.llm_comparator.invoke(prompt)
            try:
                comparisons = parse_comparison_json(response.content)
                break
            except ValueError:
                if attempt == 1:
                    raise
                logger.warning("Test Score comparator returned malformed JSON; retrying once")
                prompt += (
                    "\n\nRETRY REQUIREMENT: Your previous response was not valid JSON. "
                    "Return only the required JSON object with `verification_analysis_report` and no surrounding prose."
                )

        return {
            "comparison_task_output": json.dumps(
                {"verification_analysis_report": comparisons},
                ensure_ascii=False,
            )
        }

    def reporter_node(self, state: VerificationState) -> Dict[str, Any]:
        """
        Reporting Step: Generates the final JSON report for Test Score verification.
        """
        logger.info("Executing Test Score Reporter Node")

        try:
            comparisons = parse_comparison_json(state['comparison_task_output'])
            final_json = build_verification_report(
                comparisons,
                critical_field_names=_test_score_critical_fields(state.get("record_data") or {}),
            )
            validated = ValidatedCrewReport(**final_json)
            return {"final_report": validated.model_dump()}
        except Exception as e:
            logger.error(f"Error in Test Score Reporter Node: {e}")
            raise


def build_test_score_graph():
    nodes = TestScoreGraphNodes()
    workflow = StateGraph(VerificationState)

    workflow.add_node("comparator", nodes.comparator_node)
    workflow.add_node("reporter", nodes.reporter_node)

    workflow.set_entry_point("comparator")
    workflow.add_edge("comparator", "reporter")
    workflow.add_edge("reporter", END)

    return workflow.compile()


class TestScoreGraphOrchestrator:
    def __init__(self, record_data: Dict[str, Any], document_text: str):
        self.record_data = record_data
        self.document_text = document_text
        self.app = build_test_score_graph()

    def run(self) -> Dict[str, Any]:
        verifiable_fields = [
            f for f in self.record_data.keys()
            if f not in FIELDS_TO_EXCLUDE_FROM_PROCESSING
        ]

        initial_state = VerificationState(
            record_data=self.record_data,
            document_text=self.document_text,
            verifiable_fields=verifiable_fields,
            comparison_task_output=None,
            final_report=None,
            usage_metrics={},
            model_config={
                "comparator_model": MODEL_STANDARD_VERIFICATION,
                "reporter_model": "deterministic-python"
            }
        )

        # Execute Graph
        result_state = self.app.invoke(initial_state)

        # Extract Results
        report_data = result_state.get("final_report")
        if not report_data:
            raise ValueError("Test Score Graph execution failed to produce final report.")

        # Replicate Usage Metrics
        from app.langgraph.llm_utils import _GLOBAL_TOKEN_USAGE
        usage_metrics = {
            "total_tokens": _GLOBAL_TOKEN_USAGE["total_tokens"],
            "prompt_tokens": _GLOBAL_TOKEN_USAGE["prompt_tokens"],
            "completion_tokens": _GLOBAL_TOKEN_USAGE["completion_tokens"],
            "successful_requests": _GLOBAL_TOKEN_USAGE["successful_requests"],
            "total_cost_usd": _GLOBAL_TOKEN_USAGE["total_cost_usd"],
            "source": "LangGraph",
            "model_config": result_state["model_config"]
        }

        return {
            **report_data,
            "usage_metrics": usage_metrics
        }
