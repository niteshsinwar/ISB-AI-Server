# RECOMMENDER DETAIL VERIFICATION - APEX LOGIC ANALYSIS

## Current System Architecture

### Data Flow
```
ISB_Recommender_Details__c (Record created)
    ↓
ISBRecommenderDetailHandler (Trigger - onAfterUpdate)
    ↓
ISBRecommenderDetailCoreAPI.coreAPI() (Calls external API)
    ↓
External Core Engine (Third-party API)
    ↓
Analysis/Verification Response
    ↓
Update ISB_Recommender_Details__c Status → "Sent to Core Engine"
```

### Object & Field Mapping

#### Primary Object: ISB_Recommender_Details__c
**Key Fields:**
- Id (Record ID)
- Application__c (Lookup to Application)
- First_Name__c (Recommender first name)
- Last_Name__c (Recommender last name)
- Email__c (Recommender email)
- MobilePhone__c (Recommender phone)
- MobilePhone_Code__c (Country code reference)
- Country__c (Lookup to Country)
- City__c (City)
- Postal_Code__c (Postal code)
- Address__c (Full address)
- Email_Counter__c (Email resend count)
- Status__c (Current status)
- Portal_Status__c (Portal-specific status)
- Submission_Due_Date__c (Deadline)
- Submitted_Date__c (When submitted)
- Offline_Reason__c (If offline - reason)
- Submission_Mode__c (Online/Offline)
- Is_Locked__c (Record locked flag)
- Failed_API_sync__c (API sync status)
- Resend_Email_To_Recommender__c (Email resend flag)
- Resend_API_Status__c (Last resend status)
- Last_Resend_Timestamp__c (Last resend time)
- Message_To_Recommender__c (Personal message)
- Recommender_Designation__c (Job designation)
- Recommender_Organization__c (Organization name)
- Relationship_Type__c (Relationship to applicant)
- Overall_Score__c (Calculated score)
- Total_Recommender_Responses__c (Response count)

#### Related Object: ISB_Recommender_Response__c
**Stores recommender answers:**
- ISB_Recommender_Details__c (Lookup to recommender detail)
- Question__c (Question text)
- Section_Name__c (Section/Category)
- Answer__c (Recommender's answer)
- Score__c (Score for this answer)
- Data_Migration__c (Migration flag)

### Apex Flow Analysis

#### ISBRecommenderDetailCoreAPI.coreAPI()
**Purpose:** Send recommender details to external Core Engine API

**Input Parameters:**
- List<ID> recommenderDetailId
- Boolean rejected (false for approved, true for rejected)

**Data Sent to API:**
```
RecommenderDetailsWrapper
└── List<RecommenderDetail>
    ├── ApplicationNo
    ├── SFRecordID (ID field)
    ├── SFApplicantionID (Application__c)
    ├── City
    ├── Email
    ├── FirstName
    ├── LastName
    ├── MobileNo
    ├── MobileNoCode
    ├── OfflineReason
    ├── SFRelationshipType
    ├── SFStatus
    ├── SubmissionDueDate
    ├── SFSubmissionMode
    ├── RecommenderNumber (Name field)
    ├── IsLocked
    ├── SFOrganization
    ├── PersonalMessagetoRecommender
    └── Nested Objects:
        ├── Country (Id, Name)
        ├── Designation (Name)
        ├── Applicant (Name, Mobile, Email, Country)
        ├── Application
        │   ├── Term (Id, Name)
        │   ├── AcademicYear (Id, Name)
        │   ├── Round (Id, Name)
        │   ├── ProgramName (Id, Name)
        │   └── Specialisation (if FPM program)
```

**API Response Processing:**
```
ResponseWrapper
└── Data (List of results)
    ├── Id (Record ID)
    ├── CEScheduleDetailID
    ├── PanelMemberSFId
    ├── Error (Boolean)
    ├── HasError (Boolean)
    └── Message (List of error messages)
```

**Update Logic:**
- If NOT rejected: Set Status__c = "Sent to Core Engine"
- If rejected: Set Failed_API_sync__c = false

#### ISBRecommenderDetailHandler.onAfterUpdate()
**Trigger Actions:**
1. **When Status changes to "Approved":**
   - Call ISBRecommenderDetailCoreAPI.coreAPI() with rejected=false
   - Send recommender details to Core Engine

2. **When Status changes to "Rejected":**
   - Call ISBRecommenderDetailCoreAPI.coreAPI() with rejected=true
   - Mark for retry if failed

3. **When Status changes to "Rejected After Submission":**
   - Call ISBRecommenderDetailCoreAPI.coreAPI() with rejected=true

4. **When Status changes to "Submitted":**
   - Trigger ApplicationVerificationGateway.processChecklistVerificationAutomation()
   - This may check if all required recommenders have submitted

5. **When Resend_Email_To_Recommender__c = true:**
   - Call ISBRecommenderDetailCoreAPI.triggerResendEmailPatch()
   - Increment Email_Counter__c
   - Update Resend_API_Status__c

### Deterministic Logic Currently Applied

#### 1. Duplicate Validation (validateDuplicateEmail)
- Per application: No duplicate emails across recommenders
- Per application: No duplicate phone numbers across recommenders
- Validation happens on insert/update

#### 2. Email/Status Transitions
- Status changes trigger email notifications
- Mail flags: isRecommenderAcceptedMailSent__c, isRecommenderSubmittedMailSent__c, etc.

#### 3. External API Call Logic
- Only call API when status is Approved/Rejected
- Track API sync status (Failed_API_sync__c, Resend_API_Status__c)
- Retry failed attempts

---

## Current State: Pure Apex/External API

**What Currently Happens:**
1. Recommender Detail created in Salesforce
2. Status changed to "Approved"
3. API call sent to external Core Engine
4. Core Engine performs analysis/verification
5. Response comes back with results
6. Salesforce status updated to "Sent to Core Engine"

**No Local Verification Summary Yet:**
- No Application_Verification_Summary__c equivalent for recommenders
- No local LLM-based analysis
- No field-level mismatch detection
- Pure external API processing

---

## Proposed Shift to Python-Based Processing

### New Components Needed

#### 1. Recommender Processor
**File:** `app/processors/recommender_processor.py`
- Similar to employment_processor.py
- Fetch recommender detail from Salesforce
- Extract relevant data
- Process through LangGraph

#### 2. Recommender LangGraph
**File:** `app/langgraph/recommender_graph.py`
- Similar to employment_graph.py
- LLM-based recommendation analysis
- Deterministic field comparison
- Confidence scoring
- Mismatch detection for task creation

#### 3. Recommender Verification Summary
**New Salesforce Object:** Recommender_Verification_Summary__c
- Similar to Application_Verification_Summary__c
- Store analysis results
- Mismatched fields
- Confidence levels
- LLM feedback

#### 4. Task Creation for Recommender Mismatches
**File:** app/core/task_builder.py (extension)
- Task-triggering fields for recommender details
- Examples: Organization mismatch, Designation mismatch, etc.

---

## Analysis Mapping: What to Verify

### Fields to Analyze
1. **Contact Information Validity**
   - Email format and validity
   - Phone number format
   - Address completeness

2. **Relationship Verification**
   - Relationship type appropriateness
   - Message to recommender coherence

3. **Temporal Consistency**
   - Submission date vs due date
   - Last resend timestamp patterns

4. **Data Completeness**
   - All required fields populated
   - No contradictory information

5. **Response Quality** (from ISB_Recommender_Response__c)
   - Answer completeness
   - Score consistency
   - Response coherence

---

## Object Relationships

```
Application__c
    ↓
    ├── ISB_Recommender_Details__c (1:N)
    │   ├── Country__c → Country
    │   ├── MobilePhone_Code__c → Country (for ISD)
    │   ├── Application__r → Contains Term, Round, Program, Applicant
    │   └── ISB_Recommender_Response__c (1:N)
    │       ├── Question__c
    │       ├── Section_Name__c
    │       ├── Answer__c
    │       └── Score__c
    │
    └── Recommender_Verification_Summary__c (NEW - 1:N)
        ├── ISB_Recommender_Details__c
        ├── Confidence
        ├── Mismatched_Fields
        └── Analysis_Report
```

---

## Key Considerations for Python Migration

1. **Data Structure:**
   - Most fields are simple strings/dates
   - Relationship data needs proper lookup handling
   - Response data (answers) requires aggregation

2. **Deterministic vs LLM:**
   - Format validation: deterministic
   - Completeness check: deterministic
   - Relationship appropriateness: LLM-based
   - Response quality: LLM-based

3. **External API vs Local:**
   - Currently: All analysis done externally
   - Proposed: Local Python-based with optional external enrichment
   - Task creation: Deterministic based on mismatches

4. **Status Management:**
   - Keep current status transitions
   - Add new verification summary creation
   - Task creation for review items

---

## Next Steps

### Phase 1: Understand Requirements (CURRENT)
- [x] Review current Apex logic
- [x] Map object/field names
- [x] Understand data flow
- [ ] Clarify business rules for verification
- [ ] Define task-triggering fields for recommender

### Phase 2: Design Python-Based System
- [ ] Design recommender_processor.py
- [ ] Design recommender_graph.py
- [ ] Create Recommender_Verification_Summary__c object
- [ ] Define task creation rules

### Phase 3: Implementation
- [ ] Implement recommender processor
- [ ] Implement recommender LangGraph
- [ ] Extend task_builder.py
- [ ] Create recommender verification summary

### Phase 4: Testing & Validation
- [ ] Unit tests for verification logic
- [ ] Integration tests with Salesforce
- [ ] E2E flow testing
- [ ] UAT validation

---

## Business Questions to Answer

1. **What fields should trigger verification tasks?**
   - Organization mismatch?
   - Designation change?
   - Contact information change?

2. **What's considered a "valid" recommender?**
   - All fields required?
   - Submission within deadline?
   - Response completeness threshold?

3. **What analysis should the LLM perform?**
   - Recommendation coherence?
   - Bias detection?
   - Quality assessment?

4. **How should mismatches be handled?**
   - Automatic task creation?
   - Manual review required?
   - Severity levels?
