"""
Pydantic models for API responses to ensure consistency, validation,
and clear documentation.
"""
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional, Union
from datetime import datetime

# --- Health Check Schemas ---
# No changes needed for HealthResponse or DependencyStatus
class DependencyStatus(BaseModel):
    name: str = Field(..., description="Name of the dependency (e.g., Salesforce, Gemini API).")
    status: str = Field(..., description="Status of the dependency, e.g., 'ok' or 'unavailable'.")
    details: Optional[str] = Field(None, description="Additional details about the status.")

class HealthResponse(BaseModel):
    status: str = Field(..., description="Overall status of the application, e.g., 'ok' or 'degraded'.")
    timestamp: datetime = Field(..., description="The UTC timestamp of the health check.")
    application_version: str = Field(..., description="The current version of the application.")
    checks: List[DependencyStatus] = Field(..., description="A list of critical dependency statuses.")

# --- Analyze Endpoint Schemas ---
# No changes needed for these models
class RelatedRecordMetadata(BaseModel):
    target_record_type: str = Field(..., description="The API name of the related SObject.")
    retrieval_method: str = Field(..., description="How the records were found ('direct' or 'via_junction').")
    count: int = Field(..., description="The number of related records found.")
    status: str = Field(..., description="Status of the pre-fetch operation.")
    sample_ids: List[str] = Field(..., description="A sample of up to 5 record IDs found.")

class EstimatedCompletion(BaseModel):
    total_items: int = Field(..., description="Total number of items to be processed.")
    min_seconds: int = Field(..., description="A low-end estimate for completion time in seconds.")
    max_seconds: int = Field(..., description="A high-end estimate for completion time in seconds.")
    human_readable: str = Field(..., description="A user-friendly string describing the estimated time.")

class AnalyzeApplicationBodyRequest(BaseModel):
    record_id: str = Field(..., description="The 15 or 18 character ID of the Salesforce Application__c record.")

class AnalyzeApplicationResponse(BaseModel):
    request_id: str = Field(..., description="The unique ID for this analysis request (job_id).")
    application_record_id: str = Field(..., description="The Salesforce ID of the application being processed.")
    status: str = Field(..., description="Confirms the request was accepted, e.g., 'processing_queued'.")
    message: str = Field(..., description="A human-readable message about the request status.")
    status_url: str = Field(..., description="The URL to poll for detailed status updates on this specific job.")
    created_at: datetime = Field(..., description="The UTC timestamp when the request was accepted.")
    related_records_metadata: List[RelatedRecordMetadata] = Field(..., description="Metadata about related records.")
    estimated_completion: EstimatedCompletion = Field(..., description="An estimation of the processing time.")

# --- Status & Queue Schemas ---

# MODIFIED: JobStatusResponse updated to include new debugging fields.
class JobStatusResponse(BaseModel):
    job_id: str = Field(..., description="The unique ID for this analysis job.")
    application_id: str = Field(..., description="The Salesforce ID of the application being processed.")
    status: str = Field(..., description="The overall status of the job: 'queued', 'processing', 'completed', 'failed'.")
    message: Optional[str] = Field(None, description="An error message if the job failed, or a summary on completion.")
    created_at: datetime = Field(..., description="The UTC timestamp when the job was created.")
    last_updated_at: datetime = Field(..., description="The UTC timestamp when the status was last updated.")
    progress: Optional[Dict[str, Any]] = Field(None, description="Detailed progress of sub-tasks.")
    salesforce_job_record_id: Optional[str] = Field(None, description="The Salesforce Record ID of the persisted AI_Server_Job__c record.")
    client_fingerprint: Optional[str] = Field(None, description="The fingerprint of the client that initiated the job.")
    
class QueueOverviewResponse(BaseModel):
    active_jobs: int = Field(..., description="Number of jobs currently in the 'processing' state.")
    tracked_jobs_total: int = Field(..., description="Total number of jobs being tracked in active memory.")
    slot_utilization: Dict[str, Union[int, float]] = Field(..., description="Details on processing slot usage.")
    all_jobs: List[JobStatusResponse] = Field(..., description="A list of all active jobs for detailed monitoring.")

# --- Admin Schemas ---

class ClearStatusResponse(BaseModel):
    message: str
    cleared: bool = True
    previous_status_info: JobStatusResponse

# REMOVED: BlockedClientInfo, SuspiciousClientsResponse, and UnblockClientResponse are
# no longer needed as client blocking is automated and not manually managed via the API.