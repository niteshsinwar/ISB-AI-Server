# ISB AI Server Python API Architecture

This document explains the Python FastAPI service as a standalone AI document verification microservice. It intentionally treats Salesforce as an external CRM/API system and does not depend on Salesforce Apex internals.

## Purpose

The Python service accepts verification requests, queues long-running work, extracts document text, runs AI verification graphs, persists status/logs/results back to Salesforce through API calls, and exposes health/status/admin endpoints.

The service supports two source-record workflows:

- Admission Application verification.
- EEDL / Opportunity verification.

Both workflows share the same queue, worker isolation, status updates, logging, concurrency controls, and Salesforce connection layer. They differ in how source records are discovered, how documents are fetched, and which result objects are updated.

## Application Entry Points

Primary app module:

```text
app/main.py
```

Executable server entry:

```text
main.py
```

Router assembly:

```text
app/endpoints/main_router.py
```

The app registers route families twice:

- Multi-org routes with `/{org_alias}` prefix.
- Default routes without org prefix, which use the configured `dev` Salesforce org.

## Route Families

### Admissions

```text
POST /api/v1/application/analyze
GET  /api/v1/application/status/{application_id}
GET  /api/v1/application/queue-overview
```

Multi-org equivalent:

```text
POST /{org_alias}/api/v1/application/analyze
GET  /{org_alias}/api/v1/application/status/{application_id}
GET  /{org_alias}/api/v1/application/queue-overview
```

### EEDL

```text
POST /api/v1/eedl/analyze
GET  /api/v1/eedl/status/{opportunity_id}
GET  /api/v1/eedl/queue-overview
```

Multi-org equivalent:

```text
POST /{org_alias}/api/v1/eedl/analyze
GET  /{org_alias}/api/v1/eedl/status/{opportunity_id}
GET  /{org_alias}/api/v1/eedl/queue-overview
```

### Admin

```text
GET    /api/v1/admin/health
DELETE /api/v1/admin/processing-status/{source_record_id}
```

Multi-org equivalent:

```text
GET    /{org_alias}/api/v1/admin/health
DELETE /{org_alias}/api/v1/admin/processing-status/{source_record_id}
```

## API Contracts

### Analyze Admissions Request

```json
{
  "record_id": "SALESFORCE_APPLICATION_ID"
}
```

Response:

```json
{
  "request_id": "SERVER_JOB_ID",
  "application_record_id": "SALESFORCE_APPLICATION_ID",
  "status": "processing_queued",
  "message": "Request accepted and queued for processing.",
  "status_url": "...",
  "created_at": "...",
  "related_records_metadata": [],
  "estimated_completion": {}
}
```

### Analyze EEDL Request

```json
{
  "record_id": "SALESFORCE_OPPORTUNITY_ID"
}
```

Response:

```json
{
  "request_id": "SERVER_JOB_ID",
  "opportunity_record_id": "SALESFORCE_OPPORTUNITY_ID",
  "status": "processing_queued",
  "message": "EEDL verification request accepted and queued for processing.",
  "status_url": "...",
  "created_at": "...",
  "related_records_metadata": [],
  "estimated_completion": {}
}
```

## Salesforce Org Configuration

Configuration lives in:

```text
app/config.py
```

Known org aliases:

- `dev`
- `uat`
- `prod`
- `cee_dev`

Each org needs:

- `*_SALESFORCE_CLIENT_ID`
- `*_SALESFORCE_CLIENT_SECRET`
- `*_SALESFORCE_TOKEN_URL`

`SalesforceConnectionManager` caches one `SalesforceService` per org alias.

Authentication uses Salesforce client credentials flow:

```text
grant_type=client_credentials
```

## Shared Job Model

Python job representation:

```text
app/core/job_manager.py
```

Fields:

- `job_id`
- `salesforce_job_record_id`
- `application_id`
- `opportunity_id`
- `org_alias`
- `status`
- `message`
- `client_fingerprint`
- `created_at`
- `last_updated_at`
- `progress`
- `is_stale`
- `logs`

Important convention:

- Admissions jobs use `application_id`.
- EEDL jobs use the Opportunity ID as `application_id` for shared queue identity and also set `opportunity_id`.
- If `opportunity_id` is present, status/log upserts route to EEDL-specific Salesforce methods.

## Queue and Capacity Rules

Concurrency constant:

```text
MAX_CONCURRENT_PROCESSING_SLOTS
```

Default:

```text
15
```

Analyze endpoints enforce:

1. Source record ID must be 15 or 18 characters.
2. Only one active job per source record.
3. Queue cannot exceed configured processing slot count.
4. Accepted requests create or update a Salesforce job record.
5. Work is executed as a FastAPI background task that spawns an isolated worker process.

## Worker Isolation

Worker manager:

```text
app/core/process_manager.py
```

Worker script:

```text
app/workers/job_worker.py
```

Each job runs in a separate Python process. The parent process:

1. Serializes job data.
2. Starts `job_worker.py`.
3. Streams JSON progress messages from stdout.
4. Applies a job timeout.
5. Kills stale or timed-out workers.
6. Releases processing slot after completion/failure.

The worker process:

1. Recreates Salesforce connection.
2. Creates a document extractor.
3. Sorts records by configured priority.
4. Dynamically imports the correct processor.
5. Processes each record sequentially.
6. Emits progress after each stage.
7. Fetches existing logs at completion time.
8. Appends the current attempt.
9. Writes final status/logs back to Salesforce.

Logs are intentionally not written during intermediate progress updates. They are written only at completion or failure to avoid clearing previous logs.

## Admissions Pipeline

Configuration:

```text
RELATED_RECORD_PROCESSING_CONFIG
```

Configured record groups:

1. `hed__Application__c`
2. `ISB_Education_Log__c`
3. `ISB_Employment_Log__c`
4. `hed__Test__c`
5. `DocumentChecklistItem`

Admissions processing starts in:

```text
app/endpoints/application_endpoints.py
```

The endpoint prefetches related record IDs using direct Salesforce queries. For the main application record, it uses the source record itself.

The worker then dispatches to:

- `app.processors.application_processor.process_single_application_detail`
- `app.processors.education_processor.process_single_education_history_detail`
- `app.processors.employment_processor.process_single_employment_detail`
- `app.processors.test_score_processor.process_single_test_score_detail`
- `app.processors.resume_processor.process_single_resume_detail`

Admissions processors typically:

1. Request a Salesforce data package through an Apex REST endpoint.
2. Check whether existing verification output can be skipped.
3. Extract document text.
4. Run a LangGraph orchestrator.
5. Upsert `Application_Verification_Summary__c`.
6. Record token/cost usage in the job log.

## Admissions Apex REST Dependency

The Python service maps source object keys to external Apex REST paths:

```text
APEX_ENDPOINT_PATHS
```

Configured paths:

- `documentVerification/application`
- `documentVerification/education`
- `documentVerification/employment`
- `documentVerification/testscore`

The Python service treats these as external data provider endpoints. A successful response is expected to include:

- `recordData`
- `documentPayload`
- `Salesforce_data_issue_Summary`

`documentPayload` generally includes:

- `base64Data`
- `fileExtension`
- file name/title fields
- last modified timestamp

## EEDL Pipeline

Configuration:

```text
EEDL_RECORD_PROCESSING_CONFIG
```

Configured record groups:

1. `ID_Document`
2. `Education__c`

EEDL processing starts in:

```text
app/endpoints/eedl_endpoints.py
```

The endpoint prefetches:

- ID document work item: the Opportunity ID itself.
- Education work items: `Education__c` records linked to the Opportunity contact.

The worker dispatches to:

- `app.processors.eedl_id_processor.process_eedl_id_document`
- `app.processors.eedl_education_processor.process_eedl_education_record`

EEDL processors typically:

1. Query Salesforce directly for Opportunity or Education data.
2. Read files from `ContentDocumentLink` linked to the Opportunity.
3. Match files by filename keywords.
4. Check whether existing EEDL summary output can be skipped.
5. Extract document text.
6. Run a LangGraph orchestrator.
7. Upsert `EEDL_Verification_Summary__c`.
8. For clean ID-document passes, optionally update Opportunity citizenship with CRM picklist values (`India` / `Outside India`).
9. Record token/cost usage in the job log.

## EEDL Salesforce Field Configuration

Important constants:

```text
EEDL_OPPORTUNITY_OBJECT_API_NAME = Opportunity
EEDL_EDUCATION_OBJECT_API_NAME = Education__c
EEDL_OPP_CONTACT_LOOKUP_FIELD = ContactId
EEDL_OPP_CITIZENSHIP_FIELD = APP_Citizeship__c
EEDL_EDU_CONTACT_LOOKUP_FIELD = Contact__c
EEDL_EDU_DEGREE_FIELD = Degree_Type__c
EEDL_EDU_UNIVERSITY_FIELD = University_Name__c
EEDL_EDU_GPA_FIELD = GPA__c
EEDL_EDU_START_DATE_FIELD = From__c
EEDL_EDU_END_DATE_FIELD = To__c
```

These can be overridden through environment variables. They must match the target Salesforce org.

For UAT, `Education__c` does not expose `Degree_Name__c`; use `Degree_Type__c` for degree matching unless the org schema changes.

## EEDL File Matching

Configuration:

```text
EEDL_FILE_MATCHING_CONFIG
```

ID document keywords:

- `aadhaar`
- `aadhar`
- `adhar`
- `adhaar`
- `passport`

Education files are matched by filename keyword and degree value. Examples:

- UG / bachelor keywords map to bachelor degree values.
- PG / master keywords map to master degree values.
- PhD keywords map to doctorate values.
- XII / 12th keywords map to senior secondary values.
- X / 10th keywords map to secondary values.

This pipeline assumes EEDL files are attached to the Opportunity.

## AI / LangGraph Components

Shared graph utilities:

- `app/langgraph/graph_utils.py`
- `app/langgraph/llm_utils.py`
- `app/langgraph/schemas.py`
- `app/langgraph/state.py`

Admissions graph orchestrators:

- `application_graph.py`
- `education_graph.py`
- `employment_graph.py`
- `test_score_graph.py`
- `resume_graph.py`

EEDL graph orchestrators:

- `eedl_citizenship_graph.py`
- `eedl_education_graph.py`

Model constants:

```text
MODEL_DATA_ANALYSIS = gemini-2.5-flash
MODEL_COMPLEX_REASONING = gemini-2.5-flash
MODEL_TEXT_EXTRACTION = gemini-2.5-flash
MODEL_STANDARD_VERIFICATION = gemini-2.5-flash
MODEL_HTML_SYNTHESIS = gemini-2.5-flash
```

The code currently has a sticky model policy: Gemini 2.5 Flash is the allowed model.

## Document Extraction

Document extraction service:

```text
app/services/document_extraction_service.py
```

The app initializes the extractor in FastAPI lifespan startup.

Extractor prompts:

- `RAW_OCR_PROMPT`: strict transcription.
- `DATA_STRUCTURING_PROMPT`: reconstructs structured Markdown, especially tables.

Text extraction returns a document text string for the downstream graph.

## Skip Logic

Skip utility:

```text
app/core/processing_utils.py
```

`should_skip_processing(...)` skips when:

1. Existing summary confidence is 100, or
2. Existing summary is newer than both the source record and the document.

Admissions uses existing `Application_Verification_Summary__c` metadata.

EEDL uses existing `EEDL_Verification_Summary__c` metadata.

## Status Persistence

Admissions jobs use:

- `upsert_ai_server_job`
- `get_latest_ai_server_job`

EEDL jobs use:

- `upsert_eedl_ai_server_job`
- `get_latest_eedl_ai_server_job`

Both update:

- job ID
- status
- message
- progress details
- client fingerprint
- logs, only when explicitly passed

## Result Persistence

Admissions result method:

```text
upsert_verification_summary
```

Target:

```text
Application_Verification_Summary__c
```

EEDL result method:

```text
upsert_eedl_verification_summary
```

Target:

```text
EEDL_Verification_Summary__c
```

EEDL status mapping currently converts:

- `Passed` to `verified`
- `Failed` to `error`
- `Needs Review` to `insufficient_data`

## Failure Handling

### Request Validation

Analyze endpoints return:

- `400` for invalid Salesforce ID shape.
- `409` when a job for the same source record is already active.
- `429` when the queue is full.
- `500` when initial Salesforce job creation fails.

### Worker Failures

If the worker process fails:

1. Parent catches `WorkerProcessError`.
2. Job status becomes `failed`.
3. Failure message is written to Salesforce.
4. Processing slot is released.
5. Job is removed from active memory.

### Processor Failures

Processors convert lower-level exceptions into record-specific errors. Common categories:

- Salesforce API error.
- Document extraction error.
- Graph/processing error.
- Missing document/data fallback.

When possible, processors create fallback summary records with low confidence instead of failing the entire job.

### Timeout Handling

`JOB_TIMEOUT_SECONDS` controls maximum worker runtime.

Default:

```text
6000
```

When exceeded:

1. Worker process is terminated.
2. Job is marked failed.
3. Error message identifies timeout.

## Admin Behavior

Health endpoint checks:

- Gemini/text extractor availability.
- Salesforce service connection.

Queue overview endpoint returns:

- active processing slot count.
- tracked active jobs.
- slot utilization.
- job status list.

Clear-status endpoint:

- Marks an active in-memory job stale.
- Updates Salesforce job to failed.
- Attempts to kill the worker process.
- Removes the job from active memory.

## Operational Environment

Runtime server defaults:

- `API_HOST=0.0.0.0`
- `API_PORT=443`
- HTTPS certificate paths:
  - `/app/certs/isbcert.key`
  - `/app/certs/isbcert.crt`

The executable entry point exits if HTTPS certs are missing. Local development can run through Uvicorn directly:

```bash
uvicorn main:app --reload --port 8000
```

## Implementation Checklist

Before deploying or enabling a new environment:

1. Configure all Salesforce client credential environment variables.
2. Confirm `AI_SERVER_JOB_OBJECT_API_NAME` and field constants match Salesforce.
3. Confirm EEDL field constants match the target org.
4. Confirm Gemini API credentials are configured.
5. Confirm HTTPS certs exist for production server startup.
6. Confirm queue capacity matches Salesforce-side scheduler capacity.
7. Confirm `AIJ_LOGS_FIELD` matches Salesforce field casing/API name.
8. Confirm EEDL file naming conventions match `EEDL_FILE_MATCHING_CONFIG`.
9. Confirm the external CRM user has object/field/file permissions for all queried objects.
