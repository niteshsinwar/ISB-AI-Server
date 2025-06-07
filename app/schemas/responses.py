# project_root/app/schemas/responses.py
"""
Pydantic models for API responses to ensure consistency, validation,
and clear documentation.
"""
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional, Union
from datetime import datetime

# --- Health Check Schemas ---

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

class RelatedRecordMetadata(BaseModel):
    target_record_type: str = Field(..., description="The API name of the related SObject.")
    retrieval_method: str = Field(..., description="How the records were found ('direct' or 'via_junction').")
    count: int = Field(..., description="The number of related records found.")
    status: str = Field(..., description="Status of the pre-fetch operation, e.g., 'ids_fetched', 'no_records_found', 'fetch_error'.")
    sample_ids: List[str] = Field(..., description="A sample of up to 5 record IDs found.")

class EstimatedCompletion(BaseModel):
    total_items: int = Field(..., description="Total number of items to be processed (main application + related records).")
    min_seconds: int = Field(..., description="A low-end estimate for completion time in seconds.")
    max_seconds: int = Field(..., description="A high-end estimate for completion time in seconds.")
    human_readable: str = Field(..., description="A user-friendly string describing the estimated time.")

class AnalyzeApplicationBodyRequest(BaseModel):
    record_id: str = Field(..., description="The 15 or 18 character ID of the Salesforce Application__c record.")

class AnalyzeApplicationResponse(BaseModel):
    request_id: str = Field(..., description="The unique ID for this analysis request (job_id).", alias="_id")
    application_record_id: str = Field(..., description="The Salesforce ID of the application being processed.")
    status: str = Field(..., description="Confirms the request was accepted, e.g., 'processing_initiated'.")
    message: str = Field(..., description="A human-readable message about the request status.")
    status_url: str = Field(..., description="The URL to poll for detailed status updates on this specific job.")
    created_at: datetime = Field(..., description="The UTC timestamp when the request was accepted.")
    related_records_metadata: List[RelatedRecordMetadata] = Field(..., description="Metadata about related records that will be processed.")
    estimated_completion: EstimatedCompletion = Field(..., description="An estimation of the processing time.")

# --- Status & Queue Schemas ---

class JobStatusResponse(BaseModel):
    job_id: str = Field(..., description="The unique ID for this analysis job.")
    application_id: str = Field(..., description="The Salesforce ID of the application being processed.")
    status: str = Field(..., description="The overall status of the job: 'processing', 'completed', 'failed'.")
    message: Optional[str] = Field(None, description="An error message if the job failed, or a summary on completion.")
    created_at: datetime = Field(..., description="The UTC timestamp when the job was created.")
    last_updated_at: datetime = Field(..., description="The UTC timestamp when the status was last updated.")
    progress: Optional[Dict[str, Any]] = Field(None, description="Detailed progress of sub-tasks.")
    
class QueueOverviewResponse(BaseModel):
    active_jobs: int = Field(..., description="Number of jobs currently in the 'processing' state.")
    tracked_jobs_total: int = Field(..., description="Total number of jobs being tracked (processing, completed, failed).")
    status_counts: Dict[str, int] = Field(..., description="A breakdown of jobs by their status.")
    slot_utilization: Dict[str, Union[int, float]] = Field(..., description="Details on processing slot usage.")
    recent_failed_jobs: List[JobStatusResponse] = Field(..., description="A list of the 5 most recently failed jobs for quick diagnosis.")

# --- Admin & Security Schemas ---

class BlockedClientInfo(BaseModel):
    client_fingerprint_prefix: str
    full_fingerprint_for_unblock: str
    blocked_at: datetime
    reason: str
    block_time_remaining_seconds: int
    original_request_count: int

class SuspiciousClientsResponse(BaseModel):
    currently_blocked_count: int
    configured_block_duration_seconds: int
    blocked_clients: List[BlockedClientInfo]

class ClearStatusResponse(BaseModel):
    message: str
    cleared: bool = True
    previous_status_info: JobStatusResponse

class UnblockClientResponse(BaseModel):
    message: str
    unblocked: bool = True
    previous_block_info: Dict[str, Any]

