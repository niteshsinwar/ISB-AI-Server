# RECOMMENDER VERIFICATION - PROCESSOR & LANGGRAPH ARCHITECTURE

## Overview

Recommender verification uses a **hybrid deterministic + LLM-based approach**:
- **Deterministic nodes (4)**: Pattern matching, string comparison, status checks
- **LLM-powered nodes (2)**: Context analysis, relationship assessment
- **Report aggregation (1)**: Combines all findings into structured AVS record

---

## Architecture Diagram

```
Recommender Processor (recommender_processor.py)
    ├─ Fetch recommender_record (ISB_Recommender_Details__c)
    ├─ Fetch responses (ISB_Recommender_Response__c)
    ├─ Fetch applicant_personal_detail (for parents name from Govt ID)
    └─ Call LangGraph Orchestrator
         ↓
         RecommenderGraphOrchestrator (recommender_graph.py)
             │
             ├─ NODE 1: DETERMINISTIC - Submission Validator
             │   └─ Check: Status == "Submitted"?
             │       Input: recommender.Status__c
             │       Output: is_submitted boolean
             │
             ├─ NODE 2: DETERMINISTIC - Email Classifier
             │   └─ Check: Personal vs Corporate email?
             │       Input: recommender.Email__c
             │       Pattern match: @gmail, @yahoo, @hotmail, @outlook, etc.
             │       Output: email_type = "personal" | "corporate"
             │
             ├─ NODE 3: DETERMINISTIC - Name Matcher
             │   └─ Checks:
             │       ├─ First name: exact match (case-insensitive)?
             │       ├─ Last name: exact match (case-insensitive)?
             │       └─ If last_name_match: Flag potential family
             │       Input: recommender names vs applicant names
             │       Output: first_name_match, last_name_match, potential_family_flag
             │
             ├─ CONDITIONAL EDGE 1: If email_type == "personal" THEN:
             │   │
             │   └─ NODE 4: LLM-POWERED - Personal Email Reason Analyzer
             │       └─ Check: Why was personal email used?
             │           Input: Recommendation content
             │           LLM Analysis:
             │             - Extract context clues (retired, freelance, no company email?)
             │             - Assess professionalism despite personal email
             │             - Determine if deliberate vs accidental choice
             │           Output: Reason analysis + confidence
             │
             ├─ CONDITIONAL EDGE 2: If last_name_match == true THEN:
             │   │
             │   └─ NODE 5: LLM-POWERED - Family Relationship Detector
             │       └─ Check: Is recommender likely a family member?
             │           Input: 
             │             - Recommender name
             │             - Applicant name
             │             - Parents name (from Govt ID)
             │             - Recommendation content
             │           LLM Analysis:
             │             - Name matching with parents (formal vs informal variations)
             │             - Recommendation tone (family-like language patterns?)
             │             - Context clues (personal knowledge, parental tone, etc.)
             │           Output: Family relationship probability (Low/Medium/High) + evidence
             │
             └─ NODE 6: DETERMINISTIC - Report Builder
                 └─ Aggregates all findings
                     Input: All findings from nodes 1-5
                     Output: 
                       - field_comparison_summary (structured text)
                       - overall_feedback (human readable)
                       - confidence_range (0-100)
                       - mismatched_field_list (semi-colon separated flags)
                       - verification_analysis_report (structured array)
                         ↓
Upsert to Application_Verification_Summary__c
    Application__c = application_id
    Name = "Recommender Analysis"
    Overall_Feedback__c = overall_feedback
    Percentage_Confidence__c = confidence_range
    Field_Comparison_Summary__c = field_comparison_summary
    Mismatched_Field_List__c = mismatched_field_list (e.g., "personal_email_used;name_match_detected;family_relationship_suspected")
```

---

## Node Details

### NODE 1: SUBMISSION VALIDATOR (Deterministic)

**Input**: `recommender.Status__c`
**Logic**: String equality check
**Output**: 
```python
state["is_submitted"] = (status == "Submitted")
state["findings"].append({
    "field": "submission_status",
    "check": "Is recommendation submitted?",
    "result": "PASS" if is_submitted else "FAIL",
    "value": status,
    "type": "deterministic"
})
```
**Why Deterministic**: No ambiguity - either status equals "Submitted" or it doesn't.

---

### NODE 2: EMAIL CLASSIFIER (Deterministic)

**Input**: `recommender.Email__c`
**Logic**: Pattern matching against known personal email domains
**Personal Domains**:
```
@gmail.com, @yahoo.com, @hotmail.com, @outlook.com, @aol.com,
@yahoo.co.in, @rediffmail.com, @indiatimes.com
```
**Output**:
```python
state["email_type"] = "personal" or "corporate"
state["findings"].append({
    "field": "email_type",
    "check": "Email classification",
    "result": email_type.upper(),
    "value": email,
    "type": "deterministic"
})
```
**Why Deterministic**: Domain matching is pure string pattern recognition, no judgment needed.

---

### NODE 3: NAME MATCHER (Deterministic)

**Input**: 
- `recommender.First_Name__c` + `recommender.Last_Name__c`
- `applicant.First_Name__c` + `applicant.Last_Name__c` (from personal detail record)

**Logic**: Case-insensitive string comparison

**Checks**:
```python
first_name_match = (recommender_first.lower() == applicant_first.lower())
last_name_match = (recommender_last.lower() == applicant_last.lower())

# Flag potential family if last match but first doesn't
if last_name_match and not first_name_match:
    state["potential_family_flag"] = True
```

**Output**:
```python
state["first_name_match"] = boolean
state["last_name_match"] = boolean
state["potential_family_flag"] = boolean (triggers NODE 5 if True)
```

**Why Deterministic**: Pure string comparison, no context needed at this stage.
**Why This Triggers LLM**: If names match but only partially (same last name), deeper analysis needed to confirm family relationship.

---

### NODE 4: PERSONAL EMAIL REASON ANALYZER (LLM-POWERED)

**Trigger**: Only if `email_type == "personal"`

**Input**:
- Email address used
- Recommender name
- Full recommendation content (all ISB_Recommender_Response__c answers aggregated)

**LLM Analysis**:

The LLM reads the recommendation text to understand WHY personal email was chosen:

1. **Context Extraction**:
   - "I'm retired..." → Explains why no company email
   - "I work as a freelancer..." → Explains personal email use
   - "I no longer work at XYZ Corp..." → Context for email choice

2. **Professionalism Assessment**:
   - Despite personal email, is the recommendation detailed and professional?
   - Or is it brief and casual?

3. **Deliberateness**:
   - Does text suggest conscious choice? ("I prefer to keep this personal")
   - Or seems accidental? (Generic template, format issues)

**Output**:
```python
state["personal_email_reason"] = """
Reason Analysis:
- Most likely explanation: [extracted from text or inferred]
- Confidence in deliberate choice: [low/medium/high]
- Credibility impact: [assessment]
"""
```

**Example**:
> "The recommender explains they are retired and don't have access to corporate email. 
> The recommendation itself is detailed and professional despite personal email use. 
> Appears to be deliberate and reasonable choice. Confidence: HIGH that choice is intentional, not oversight."

**Why LLM**: Requires understanding context, tone, implicit meaning. Pure pattern matching can't determine intent.

---

### NODE 5: FAMILY RELATIONSHIP DETECTOR (LLM-Powered)

**Trigger**: Only if `last_name_match == true` (from NODE 3)

**Input**:
- Recommender full name: `{first} {last}`
- Applicant full name: `{first} {last}`
- Parents name from Govt ID: `{parents_name}`
- Full recommendation content

**LLM Analysis**:

The LLM performs sophisticated analysis to detect family relationships:

1. **Name Matching with Parents**:
   ```
   Recommender: "Raj Kumar"
   Applicant: "Arjun Kumar"
   Parents: "Rajesh Kumar" & "Priya Sharma"
   
   Analysis: Raj could be short for Rajesh. Same last name. Possible father.
   ```

2. **Name Variation Analysis**:
   - Cultural conventions (formal vs informal first names)
   - Name shortening patterns
   - Regional naming conventions

3. **Tone & Language Analysis**:
   ```
   Look for family-like patterns:
   - "My child/daughter/son shows exceptional promise..."
   - "I have known [applicant] since childhood..."
   - "As a parent, I can attest..."
   - "Family values of [applicant] are..."
   - Overly protective language
   - Personal investment unusual for professional recommender
   ```

4. **Context Clues**:
   - References to personal/family matters
   - Intimate knowledge of applicant's background
   - Recommendation goes beyond typical professional assessment

**Output**:
```python
state["family_relationship_probability"] = "Low" | "Medium" | "High"
state["family_relationship_analysis"] = """
Analysis Summary:
- Name matching: [evidence]
- Tone analysis: [findings]
- Context clues: [observations]
- Conclusion: [probability and confidence]
"""
```

**Example**:
> "Recommender 'Rajesh Kumar' matches applicant's father name perfectly. 
> Recommendation uses phrases like 'I have watched him grow' and 'as his father'. 
> Multiple family references. PROBABILITY: HIGH that recommender is family member."

**Why LLM**: Requires understanding:
- Cultural name conventions
- Semantic meaning of text (family-like tone vs professional tone)
- Context inference (not explicit but strongly implied)
- Relationship probability assessment (nuanced judgment)

Pure pattern matching cannot detect these subtle indicators.

---

### NODE 6: REPORT BUILDER (Deterministic)

**Input**: All findings from nodes 1-5

**Logic**: Aggregate and structure findings

**Output**: Structured for AVS record:
```python
field_comparison_summary = """
Recommendation Status: Submitted
Is Submitted: Yes

Email Classification: PERSONAL
Email Address: recommender@gmail.com
Personal Email Reason Analysis: [from NODE 4 if applicable]

Name Matching:
  Recommender Name: Raj Kumar
  Applicant Name: Arjun Kumar
  First Name Match: No
  Last Name Match: Yes

Family Relationship Assessment: [from NODE 5 if applicable]
  Probability: High
  Analysis: [from NODE 4]
"""

overall_feedback = """
✓ Recommendation has been submitted.
ℹ️ Recommender used personal email address.
⚠️ POTENTIAL FAMILY RELATIONSHIP DETECTED: High probability based on name matching and content analysis.
"""

confidence_range = 65  # 100 - 20 (personal email) - 15 (High family prob)

mismatched_field_list = "personal_email_used;name_match_detected;family_relationship_suspected"
```

**Why Deterministic**: Simply combining findings. No new analysis or judgment.

---

## Execution Flow Example

### Scenario: Potential Family Member Using Personal Email

```
ISB_Recommender_Details__c:
  - Status: "Submitted"
  - First_Name: "Rajesh"
  - Last_Name: "Kumar"
  - Email: "rajesh.kumar@gmail.com"

Application Personal Detail:
  - First_Name: "Arjun"
  - Last_Name: "Kumar"
  - Parents_Name (from Govt ID): "Rajesh Kumar, Priya Sharma"

Recommendation Content:
  "I have known Arjun since his childhood and watched him grow..."
  "As a senior professional, I strongly recommend Arjun..."

EXECUTION:

NODE 1: is_submitted = true ✓
NODE 2: email_type = "personal" (gmail) 
NODE 3: last_name_match = true, first_name_match = false → potential_family_flag = true
NODE 4 (triggered): LLM analyzes: Personal email deliberate choice (shared domain @gmail suggests personal preference)
NODE 5 (triggered): LLM analyzes: Name "Rajesh" matches father exactly, tone suggests family relationship, HIGH probability
NODE 6: Aggregates findings

RESULT:
  confidence_range: 65 (flagged for family relationship)
  mismatched_field_list: "personal_email_used;name_match_detected;family_relationship_suspected"
  overall_feedback: "⚠️ POTENTIAL FAMILY RELATIONSHIP DETECTED"
```

---

## Key Differentiators: Deterministic vs LLM

| Check | Deterministic | LLM-Powered | Why |
|-------|---------------|-------------|-----|
| Email domain pattern | ✓ | | Simple regex |
| Email deliberateness | | ✓ | Needs context understanding |
| Name exact match | ✓ | | String equality |
| Family name variations | | ✓ | Cultural/semantic knowledge |
| Recommendation tone | | ✓ | Requires NLP + cultural context |
| Overall family probability | | ✓ | Nuanced judgment |

---

## No Task Creation

**Important**: Unlike Employment/Education verification, Recommender verification **does NOT trigger task creation**.

Instead:
- Analysis is saved to `Application_Verification_Summary__c`
- Flags are stored in `mismatched_field_list`
- AVS record is available for manual review/action
- No automatic task workflow

---

## Files Created

1. **`app/processors/recommender_processor.py`** (125 lines)
   - Fetch recommender, responses, applicant personal details
   - Call LangGraph orchestrator
   - Upsert AVS record

2. **`app/langgraph/recommender_graph.py`** (650+ lines)
   - RecommenderGraphOrchestrator class
   - 6 nodes (4 deterministic + 2 LLM + 1 builder)
   - State management

3. **`app/langgraph/graph_prompts.py`** (added)
   - RECOMMENDER_*_GOAL and _TASK prompts
   - 2 LLM node prompts + descriptions for deterministic nodes

---

## Next Steps

1. Add to trigger handler (when recommender status changes to "Submitted", call processor)
2. Create test suite (unit tests for each node)
3. E2E test with real recommender data
4. Validate prompts against LLM responses
