"""LangGraph orchestrator for recommender detail verification."""
import logging
from typing import Any, Dict, Optional

from langgraph.graph import StateGraph, START, END

from app.langgraph.state import RecommenderState
from app.langgraph.graph_utils import get_llm
from app.langgraph.graph_prompts import (
    RECOMMENDER_SUBMISSION_VALIDATOR_GOAL,
    RECOMMENDER_SUBMISSION_VALIDATOR_TASK,
    RECOMMENDER_EMAIL_CLASSIFIER_GOAL,
    RECOMMENDER_EMAIL_CLASSIFIER_TASK,
    RECOMMENDER_NAME_MATCHER_GOAL,
    RECOMMENDER_NAME_MATCHER_TASK,
    RECOMMENDER_PERSONAL_EMAIL_ANALYZER_GOAL,
    RECOMMENDER_PERSONAL_EMAIL_ANALYZER_TASK,
    RECOMMENDER_FAMILY_DETECTOR_GOAL,
    RECOMMENDER_FAMILY_DETECTOR_TASK,
)

logger = logging.getLogger(__name__)


class RecommenderGraphOrchestrator:
    """Orchestrates recommender detail verification through LangGraph."""

    def __init__(
        self,
        recommender_record: Dict[str, Any],
        responses: list,
        applicant_personal_detail: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize orchestrator with recommender data.

        Args:
            recommender_record: ISB_Recommender_Details__c record
            responses: List of ISB_Recommender_Response__c records
            applicant_personal_detail: Applicant's personal detail (for parents name)
        """
        self.recommender_record = recommender_record
        self.responses = responses
        self.applicant_personal_detail = applicant_personal_detail
        self.llm = get_llm("gemini-2.0-flash", 0.7)

    def build_graph(self):
        """Build the verification graph."""
        graph = StateGraph(RecommenderState)

        # Node 1: Submission Check (Deterministic)
        graph.add_node("submission_validator", self._submission_validator_node)

        # Node 2: Email Classifier (Deterministic)
        graph.add_node("email_classifier", self._email_classifier_node)

        # Node 3: Name Matcher (Deterministic)
        graph.add_node("name_matcher", self._name_matcher_node)

        # Node 4: Personal Email Reason Analyzer (LLM)
        graph.add_node("personal_email_analyzer", self._personal_email_analyzer_node)

        # Node 5: Family Relationship Detector (LLM)
        graph.add_node("family_relationship_detector", self._family_relationship_detector_node)

        # Node 6: Report Builder (Deterministic)
        graph.add_node("report_builder", self._report_builder_node)

        # Add edges
        graph.add_edge(START, "submission_validator")
        graph.add_edge("submission_validator", "email_classifier")

        # Conditional edge: if personal email, analyze reason first, then name_matcher
        def route_personal_email(state):
            return "personal_email_analyzer" if state.get("email_type") == "personal" else "name_matcher"

        graph.add_conditional_edges("email_classifier", route_personal_email)

        graph.add_edge("personal_email_analyzer", "name_matcher")

        # Conditional edge: if last name matches, check family relationship
        def route_family_check(state):
            return "family_relationship_detector" if state.get("last_name_match") else "report_builder"

        graph.add_conditional_edges("name_matcher", route_family_check)

        graph.add_edge("family_relationship_detector", "report_builder")
        graph.add_edge("report_builder", END)

        return graph.compile()

    def _submission_validator_node(self, state: RecommenderState) -> RecommenderState:
        """
        NODE 1 (DETERMINISTIC): Validate if recommendation is submitted.

        Checks: Is recommendation status "Submitted"?
        """
        logger.info("Running submission validator node")

        status = self.recommender_record.get("Status__c")
        is_submitted = status == "Submitted"

        state["submission_status"] = status
        state["is_submitted"] = is_submitted
        state["findings"].append({
            "field": "submission_status",
            "check": "Is recommendation submitted?",
            "result": "PASS" if is_submitted else "FAIL",
            "value": status,
            "type": "deterministic"
        })

        logger.info(f"Submission status: {status} - {'SUBMITTED' if is_submitted else 'NOT SUBMITTED'}")

        return state

    def _email_classifier_node(self, state: RecommenderState) -> RecommenderState:
        """
        NODE 2 (DETERMINISTIC): Classify email as personal or corporate.

        Personal: @gmail.com, @yahoo.com, @hotmail.com, @outlook.com, @aol.com
        Corporate: Other domains or company-specific emails
        """
        logger.info("Running email classifier node")

        email = self.recommender_record.get("Email__c", "").lower()
        email_type = "unknown"

        personal_domains = {
            "@gmail.com", "@yahoo.com", "@hotmail.com", "@outlook.com", "@aol.com",
            "@yahoo.co.in", "@rediffmail.com", "@indiatimes.com"
        }

        for domain in personal_domains:
            if domain in email:
                email_type = "personal"
                break
        else:
            if "@" in email:
                email_type = "corporate"

        state["email"] = email
        state["email_type"] = email_type
        state["findings"].append({
            "field": "email_type",
            "check": "Email classification (personal vs corporate)",
            "result": email_type.upper(),
            "value": email,
            "type": "deterministic"
        })

        logger.info(f"Email classified as: {email_type}")

        return state

    def _name_matcher_node(self, state: RecommenderState) -> RecommenderState:
        """
        NODE 3 (DETERMINISTIC): Match recommender and applicant names & contact info.

        Checks:
        - First name exact match (case-insensitive)
        - Last name exact match (case-insensitive)
        - If last name matches, flag potential family relationship
        - Email exact match (highly suspicious)
        - Mobile exact match (highly suspicious)
        """
        logger.info("Running contact/name matcher node")

        recommender_first = (self.recommender_record.get("First_Name__c") or "").strip().lower()
        recommender_last = (self.recommender_record.get("Last_Name__c") or "").strip().lower()
        recommender_email = (self.recommender_record.get("Email__c") or "").strip().lower()
        recommender_mobile = (self.recommender_record.get("Mobile__c") or "").strip()

        # Extract applicant name from personal detail record
        applicant_first = ""
        applicant_last = ""
        applicant_email = ""
        applicant_mobile = ""

        if self.applicant_personal_detail:
            applicant_first = (self.applicant_personal_detail.get("First_Name__c") or "").strip().lower()
            applicant_last = (self.applicant_personal_detail.get("Last_Name__c") or "").strip().lower()
            applicant_email = (self.applicant_personal_detail.get("Email") or "").strip().lower()
            applicant_mobile = (self.applicant_personal_detail.get("MobilePhone") or "").strip()

        first_name_match = recommender_first == applicant_first and recommender_first != ""
        last_name_match = recommender_last == applicant_last and recommender_last != ""
        
        # Cross-contact matching
        email_match = recommender_email == applicant_email and recommender_email != ""
        mobile_match = recommender_mobile == applicant_mobile and recommender_mobile != ""

        state["recommender_first_name"] = recommender_first
        state["recommender_last_name"] = recommender_last
        state["applicant_first_name"] = applicant_first
        state["applicant_last_name"] = applicant_last
        state["first_name_match"] = first_name_match
        state["last_name_match"] = last_name_match
        state["email_match"] = email_match
        state["mobile_match"] = mobile_match

        state["findings"].append({
            "field": "first_name_match",
            "check": "First name comparison",
            "result": "MATCH" if first_name_match else "NO MATCH",
            "recommender": recommender_first,
            "applicant": applicant_first,
            "type": "deterministic"
        })

        state["findings"].append({
            "field": "last_name_match",
            "check": "Last name comparison",
            "result": "MATCH" if last_name_match else "NO MATCH",
            "recommender": recommender_last,
            "applicant": applicant_last,
            "type": "deterministic"
        })

        if email_match:
            logger.warning(f"⚠️ SUSPICIOUS: Recommender email matches Applicant email ({recommender_email})")
            state["findings"].append({
                "field": "email_cross_match",
                "check": "Email matching candidate",
                "result": "MATCH (SUSPICIOUS)",
                "recommender": recommender_email,
                "applicant": applicant_email,
                "type": "deterministic"
            })

        if mobile_match:
            logger.warning(f"⚠️ SUSPICIOUS: Recommender mobile matches Applicant mobile ({recommender_mobile})")
            state["findings"].append({
                "field": "mobile_cross_match",
                "check": "Mobile matching candidate",
                "result": "MATCH (SUSPICIOUS)",
                "recommender": recommender_mobile,
                "applicant": applicant_mobile,
                "type": "deterministic"
            })

        if last_name_match and not first_name_match:
            state["potential_family_flag"] = True
            logger.warning(f"⚠️ POTENTIAL FAMILY RELATIONSHIP: Last names match but first names differ")
        else:
            state["potential_family_flag"] = False

        return state

    def _personal_email_analyzer_node(self, state: RecommenderState) -> RecommenderState:
        """
        NODE 4 (LLM): Analyze reason for using personal email.

        Only triggered if email_type == "personal"

        LLM analyzes recommendation content to understand why personal email was used.
        """
        logger.info("Running personal email analyzer node (LLM)")

        if not self.responses:
            logger.warning("No responses available for LLM analysis")
            state["personal_email_reason"] = "No recommendation content available"
            return state

        # Aggregate response content
        recommendation_text = "\n".join([
            f"Q: {resp.get('Question__c', 'N/A')}\nA: {resp.get('Answer__c', 'N/A')}"
            for resp in self.responses
        ])

        # Create prompt for LLM
        prompt = f"""{RECOMMENDER_PERSONAL_EMAIL_ANALYZER_GOAL}

{RECOMMENDER_PERSONAL_EMAIL_ANALYZER_TASK}

Recommender Email: {state.get('email')}
Recommender Name: {state.get('recommender_first_name')} {state.get('recommender_last_name')}

Recommendation Content:
{recommendation_text}

Analyze the recommendation text. Why might this recommender have used a personal email address?
- Look for clues about their context (are they retired, freelance, work at organization without email?)
- Is the recommendation professional despite personal email?
- Does the personal email seem deliberate or accidental?
- Any explanations in the text itself?

Provide:
1. Most likely reason for using personal email
2. Confidence level (low/medium/high) that it was deliberate choice vs oversight
3. How this affects credibility of recommendation
"""

        response = self.llm.invoke(prompt)
        analysis = response.content if hasattr(response, 'content') else str(response)

        state["personal_email_reason"] = analysis
        state["findings"].append({
            "field": "personal_email_reason",
            "check": "Why personal email used?",
            "analysis": analysis,
            "type": "llm"
        })

        logger.info("Personal email reason analysis complete")

        return state

    def _family_relationship_detector_node(self, state: RecommenderState) -> RecommenderState:
        """
        NODE 5 (LLM): Detect if recommender is likely a family member.

        Only triggered if last_name_match == True

        LLM analyzes:
        - Does recommender name match applicant's parents names?
        - Recommendation tone for family-like patterns
        - Context clues suggesting family relationship
        """
        logger.info("Running family relationship detector node (LLM)")

        if not state.get("last_name_match"):
            logger.info("Skipping family detector - no last name match")
            return state

        if not self.applicant_personal_detail:
            logger.warning("No applicant personal detail available for family check")
            state["family_relationship_probability"] = "Unknown - applicant details not available"
            return state

        # Get parents names from applicant personal detail
        parents_name = self.applicant_personal_detail.get("Parents_Name_From_Government_ID__c", "")
        parents_name_alt = self.applicant_personal_detail.get("Parents_Name__c", "")

        recommender_full_name = f"{state.get('recommender_first_name')} {state.get('recommender_last_name')}"
        applicant_full_name = f"{state.get('applicant_first_name')} {state.get('applicant_last_name')}"

        # Aggregate response content for tone analysis
        recommendation_text = "\n".join([
            resp.get('Answer__c', '')
            for resp in self.responses
            if resp.get('Answer__c')
        ])

        # Create prompt for LLM
        prompt = f"""{RECOMMENDER_FAMILY_DETECTOR_GOAL}

{RECOMMENDER_FAMILY_DETECTOR_TASK}

Recommender Name: {recommender_full_name}
Applicant Name: {applicant_full_name}
Applicant Parents Name (from Govt ID): {parents_name}
Alternative Parents Info: {parents_name_alt}

Key observation: Last names MATCH between recommender and applicant. Different first names.

Recommendation Content:
{recommendation_text}

Analyze the possibility of family relationship:
1. Does recommender name match either parent? (Compare first/last names)
2. Could recommender be parent/sibling/relative using formal vs informal name?
3. Analyze recommendation tone for family-like language patterns:
   - Overly protective language?
   - Personal investment level unusual for professional recommender?
   - Intimate knowledge of personal/family matters?
   - References to personal relationships?
4. Any other context clues suggesting family bond?

Provide:
1. Family relationship probability (Low/Medium/High)
2. Specific evidence supporting conclusion
3. Name matching analysis
4. Confidence in assessment
"""

        response = self.llm.invoke(prompt)
        analysis = response.content if hasattr(response, 'content') else str(response)

        # Extract probability level from analysis
        probability = "Unknown"
        if "high" in analysis.lower():
            probability = "High"
        elif "medium" in analysis.lower():
            probability = "Medium"
        elif "low" in analysis.lower():
            probability = "Low"

        state["family_relationship_probability"] = probability
        state["family_relationship_analysis"] = analysis
        state["findings"].append({
            "field": "family_relationship",
            "check": "Potential family relationship (last name match + name matching parents)",
            "probability": probability,
            "analysis": analysis,
            "type": "llm"
        })

        logger.info(f"Family relationship probability: {probability}")

        return state

    def _report_builder_node(self, state: RecommenderState) -> RecommenderState:
        """
        NODE 6 (DETERMINISTIC): Build comprehensive report.

        Aggregates all findings into structured report for AVS record.
        """
        logger.info("Running report builder node")

        # Build field_comparison_summary
        summary_parts = []

        # Submission status
        summary_parts.append(f"Recommendation Status: {state.get('submission_status', 'Unknown')}")
        summary_parts.append(f"Is Submitted: {'Yes' if state.get('is_submitted') else 'No'}")

        # Email classification
        summary_parts.append(f"\nEmail Classification: {state.get('email_type', 'unknown').upper()}")
        summary_parts.append(f"Email Address: {state.get('email', 'Not provided')}")

        if state.get('email_type') == 'personal':
            summary_parts.append(f"\nPersonal Email Reason Analysis:\n{state.get('personal_email_reason', 'Analysis not performed')}")

        # Name matching
        summary_parts.append(f"\nContact and Name Matching:")
        summary_parts.append(f"  Recommender Name: {state.get('recommender_first_name')} {state.get('recommender_last_name')}")
        summary_parts.append(f"  Applicant Name: {state.get('applicant_first_name')} {state.get('applicant_last_name')}")
        summary_parts.append(f"  First Name Match: {'Yes' if state.get('first_name_match') else 'No'}")
        summary_parts.append(f"  Last Name Match: {'Yes' if state.get('last_name_match') else 'No'}")
        
        if state.get('email_match'):
            summary_parts.append(f"  Email Match: YES (Highly Suspicious)")
        if state.get('mobile_match'):
            summary_parts.append(f"  Mobile Match: YES (Highly Suspicious)")

        # Family relationship (if last name match)
        if state.get('last_name_match'):
            summary_parts.append(f"\nFamily Relationship Assessment:")
            summary_parts.append(f"  Probability: {state.get('family_relationship_probability', 'Unknown')}")
            summary_parts.append(f"  Analysis:\n{state.get('family_relationship_analysis', 'Analysis not performed')}")

        field_comparison_summary = "\n".join(summary_parts)

        # Build overall_feedback
        feedback_parts = []

        if not state.get('is_submitted'):
            feedback_parts.append("⚠️ Recommendation has not been submitted yet.")

        if state.get('email_type') == 'personal':
            feedback_parts.append("ℹ️ Recommender used personal email address instead of corporate email.")
            
        if state.get('email_match'):
            feedback_parts.append("🛑 FRAUD ALERT: Recommender email matches Applicant email.")
            
        if state.get('mobile_match'):
            feedback_parts.append("🛑 FRAUD ALERT: Recommender mobile matches Applicant mobile.")

        if state.get('last_name_match') and state.get('family_relationship_probability') in ['Medium', 'High']:
            feedback_parts.append(f"⚠️ POTENTIAL FAMILY RELATIONSHIP DETECTED: {state.get('family_relationship_probability')} probability based on name matching and content analysis.")

        if not feedback_parts:
            feedback = "✓ All recommender detail checks completed. No significant issues detected."
        else:
            feedback = " ".join(feedback_parts)

        # Calculate confidence
        confidence = 100
        if not state.get('is_submitted'):
            confidence -= 20
        if state.get('family_relationship_probability') == 'High':
            confidence -= 30
        elif state.get('family_relationship_probability') == 'Medium':
            confidence -= 15
            
        if state.get('email_match'):
            confidence -= 50
        if state.get('mobile_match'):
            confidence -= 50

        confidence = max(0, min(confidence, 100))

        # Build mismatched_field_list
        mismatched_fields = []
        if state.get('email_type') == 'personal':
            mismatched_fields.append('personal_email_used')
        if state.get('last_name_match'):
            mismatched_fields.append('name_match_detected')
        if state.get('family_relationship_probability') in ['Medium', 'High']:
            mismatched_fields.append('family_relationship_suspected')
        if state.get('email_match'):
            mismatched_fields.append('email_cross_match_fraud')
        if state.get('mobile_match'):
            mismatched_fields.append('mobile_cross_match_fraud')

        mismatched_field_list = '; '.join(mismatched_fields) if mismatched_fields else ""

        # Build verification_analysis_report
        verification_report = []
        for finding in state.get('findings', []):
            verification_report.append(finding)

        state["field_comparison_summary"] = field_comparison_summary
        state["overall_feedback"] = feedback
        state["confidence_range"] = str(int(confidence))
        state["mismatched_field_list"] = mismatched_field_list
        state["verification_analysis_report"] = verification_report

        logger.info(f"Report built with confidence: {confidence}%")

        return state

    def run(self) -> Dict[str, Any]:
        """Execute the verification graph."""
        logger.info("Starting recommender verification graph execution")

        graph = self.build_graph()

        initial_state = RecommenderState(
            findings=[]
        )

        final_state = graph.invoke(initial_state)

        report_dict = {
            "field_comparison_summary": final_state.get("field_comparison_summary", ""),
            "overall_feedback": final_state.get("overall_feedback", ""),
            "confidence_range": final_state.get("confidence_range", "50"),
            "mismatched_field_list": final_state.get("mismatched_field_list", ""),
            "verification_analysis_report": final_state.get("verification_analysis_report", []),
        }

        logger.info("Recommender verification graph execution complete")
        return report_dict
