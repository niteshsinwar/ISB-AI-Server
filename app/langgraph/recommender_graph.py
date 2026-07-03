"""LangGraph orchestrator for recommender detail verification."""
import logging
import re
from typing import Any, Dict, Optional

from langgraph.graph import StateGraph, START, END

from app.config import MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION
from app.langgraph.state import RecommenderState
from app.langgraph.graph_utils import get_llm
from app.langgraph.graph_prompts import (
    RECOMMENDER_PERSONAL_EMAIL_ANALYZER_GOAL,
    RECOMMENDER_PERSONAL_EMAIL_ANALYZER_TASK,
    RECOMMENDER_FAMILY_DETECTOR_GOAL,
    RECOMMENDER_FAMILY_DETECTOR_TASK,
)

logger = logging.getLogger(__name__)


def _normalize_phone(value: Optional[str]) -> str:
    """Reduce a phone number to comparable digits (ignores +91, spaces, dashes)."""
    digits = re.sub(r"\D", "", value or "")
    # Compare on the last 10 digits so "+91 98765 43210" == "9876543210"
    return digits[-10:] if len(digits) >= 10 else digits


def _extract_probability(analysis: str) -> str:
    """Extract Low/Medium/High from LLM analysis, anchored to 'probability' context.

    Avoids false positives like the word 'high' inside 'highly unlikely'.
    """
    text = (analysis or "").lower()
    # Drop template echoes like "(Low/Medium/High)" so they can't be matched
    text = re.sub(r"\(\s*low\s*/\s*medium\s*/\s*high\s*\)", "", text)
    # Prefer explicit statements such as "probability: high" / "probability is low"
    anchored = re.search(r"probability[^.\n]{0,40}?\b(low|medium|high)\b", text)
    if anchored:
        return anchored.group(1).capitalize()
    # Fallback: a standalone rating word right before 'probability'
    reversed_anchor = re.search(r"\b(low|medium|high)\b[^.\n]{0,40}?probability", text)
    if reversed_anchor:
        return reversed_anchor.group(1).capitalize()
    return "Unknown"


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
        self.llm = get_llm(MODEL_STANDARD_VERIFICATION, TEMP_STANDARD_VERIFICATION)

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
        NODE 1 (DETERMINISTIC): Validate submission status, response content
        sufficiency, and declared family relationship.

        These three checks mirror the Apex checklist automation
        (ApplicationVerificationGateway) so the Python result is a strict
        superset — nothing is lost when this report overwrites the Apex one.
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

        # Content sufficiency: at least one answer with >= 4 words (Apex parity)
        has_sufficient = any(
            len(str(resp.get("Answer__c") or "").split()) >= 4
            for resp in (self.responses or [])
        )
        state["has_sufficient_response"] = has_sufficient
        state["findings"].append({
            "field": "response_content",
            "check": "Has a response with sufficient content (min. 4 words)?",
            "result": "PASS" if has_sufficient else "FAIL",
            "value": f"{len(self.responses or [])} response(s)",
            "type": "deterministic"
        })

        # Declared family relationship in Other_Relationship__c (Apex parity)
        family_keywords = {
            "brother", "sister", "father", "mother", "son", "daughter", "uncle",
            "aunt", "cousin", "nephew", "niece", "husband", "wife",
        }
        other_rel = str(self.recommender_record.get("Other_Relationship__c") or "").lower()
        declared_family = bool(other_rel) and any(k in other_rel for k in family_keywords)
        state["declared_family_relationship"] = declared_family
        if declared_family:
            state["findings"].append({
                "field": "declared_relationship",
                "check": "Relationship declared by recommender",
                "result": "FAIL (family member)",
                "value": self.recommender_record.get("Other_Relationship__c"),
                "type": "deterministic"
            })

        logger.info(
            f"Submission: {status} | sufficient_response={has_sufficient} | declared_family={declared_family}"
        )

        return state

    def _email_classifier_node(self, state: RecommenderState) -> RecommenderState:
        """
        NODE 2 (DETERMINISTIC): Classify email as personal or corporate.

        Personal: @gmail.com, @yahoo.com, @hotmail.com, @outlook.com, @aol.com
        Corporate: Other domains or company-specific emails
        """
        logger.info("Running email classifier node")

        email = (self.recommender_record.get("Email__c") or "").lower()
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
        recommender_mobile = _normalize_phone(self.recommender_record.get("MobilePhone__c"))

        # Extract applicant name from personal detail record
        applicant_first = ""
        applicant_last = ""
        applicant_email = ""
        applicant_mobile = ""

        if self.applicant_personal_detail:
            applicant_first = (self.applicant_personal_detail.get("First_Name__c") or "").strip().lower()
            applicant_last = (self.applicant_personal_detail.get("Last_Name__c") or "").strip().lower()
            applicant_email = (self.applicant_personal_detail.get("Email") or "").strip().lower()
            applicant_mobile = _normalize_phone(self.applicant_personal_detail.get("MobilePhone"))

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

        # Extract probability level from analysis (anchored to 'probability' context)
        probability = _extract_probability(analysis)

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

        # Build field_comparison_summary as an HTML table so the AVS
        # Verification_Analysis_Report field renders consistently with every
        # other analysis type (education/employment/etc.).
        import html as _html

        def _table_row(check, result, detail, ok):
            background = "#e8f5e9" if ok else "#fde8e8"
            cells = "".join(
                f"<td style='border:1px solid #ddd;padding:8px;'>{_html.escape(str(v if v is not None else 'N/A'))}</td>"
                for v in (check, result, detail)
            )
            return f"<tr style='background:{background};'>{cells}</tr>"

        rows = []
        rows.append(_table_row(
            "Recommendation Submitted",
            "Yes" if state.get('is_submitted') else "No",
            f"Status: {state.get('submission_status', 'Unknown')}",
            bool(state.get('is_submitted')),
        ))
        rows.append(_table_row(
            "Response Content (min. 4 words)",
            "Sufficient" if state.get('has_sufficient_response') else "Insufficient",
            "At least one substantive answer required",
            bool(state.get('has_sufficient_response')),
        ))
        rows.append(_table_row(
            "Email Classification",
            (state.get('email_type') or 'unknown').upper(),
            state.get('email') or 'Not provided',
            state.get('email_type') != 'personal',
        ))
        rows.append(_table_row(
            "Recommender vs Applicant Name",
            f"First: {'Match' if state.get('first_name_match') else 'Different'} / Last: {'Match' if state.get('last_name_match') else 'Different'}",
            f"{state.get('recommender_first_name')} {state.get('recommender_last_name')} vs {state.get('applicant_first_name')} {state.get('applicant_last_name')}",
            not state.get('last_name_match'),
        ))
        rows.append(_table_row(
            "Email Cross-Match (fraud)",
            "MATCH — SUSPICIOUS" if state.get('email_match') else "No match",
            "Recommender email equals applicant email" if state.get('email_match') else "",
            not state.get('email_match'),
        ))
        rows.append(_table_row(
            "Mobile Cross-Match (fraud)",
            "MATCH — SUSPICIOUS" if state.get('mobile_match') else "No match",
            "Recommender mobile equals applicant mobile" if state.get('mobile_match') else "",
            not state.get('mobile_match'),
        ))
        if state.get('declared_family_relationship'):
            rows.append(_table_row(
                "Declared Relationship",
                "FAMILY MEMBER",
                "Other_Relationship__c contains a family keyword",
                False,
            ))
        if state.get('email_type') == 'personal':
            rows.append(_table_row(
                "Personal Email Reason (AI)",
                "Analyzed",
                (state.get('personal_email_reason') or 'Analysis not performed')[:500],
                True,
            ))
        if state.get('last_name_match'):
            prob = state.get('family_relationship_probability', 'Unknown')
            rows.append(_table_row(
                "Family Relationship Probability (AI)",
                prob,
                (state.get('family_relationship_analysis') or 'Analysis not performed')[:500],
                prob == 'Low',
            ))

        header = "".join(
            f"<th style='border:1px solid #ddd;padding:8px;text-align:left;'>{h}</th>"
            for h in ("Check", "Result", "Details")
        )
        field_comparison_summary = (
            "<div style='font-family:Arial;'>"
            "<table style='width:100%;border-collapse:collapse;border:1px solid #ddd;'>"
            f"<thead><tr style='background:#f2f2f2;'>{header}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )

        # Build overall_feedback
        feedback_parts = []

        if not state.get('is_submitted'):
            feedback_parts.append("⚠️ Recommendation has not been submitted yet.")

        if not state.get('has_sufficient_response'):
            feedback_parts.append("⚠️ Recommender has not provided a response with sufficient content (min. 4 words).")

        if state.get('declared_family_relationship'):
            feedback_parts.append("⚠️ Recommender's declared relationship appears to be a family member.")

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
        if not state.get('has_sufficient_response'):
            confidence -= 20
        if state.get('declared_family_relationship'):
            confidence -= 30
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
        if not state.get('is_submitted'):
            mismatched_fields.append('not_submitted')
        if not state.get('has_sufficient_response'):
            mismatched_fields.append('insufficient_response_content')
        if state.get('declared_family_relationship'):
            mismatched_fields.append('family_relationship_declared')
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
