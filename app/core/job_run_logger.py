"""
Job Run Logger - Tracks token usage and costs per job attempt.

This module provides utilities to:
1. Track token usage per record within a job attempt
2. Calculate and store costs with model information
3. Append attempt logs without affecting previous retry data
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field, asdict

from app.crew.crew_utils import get_job_cost_summary, reset_global_usage

logger = logging.getLogger(__name__)


@dataclass
class RecordLog:
    """Token usage log for a single record processed (simple format)."""
    record_type: str
    input_token: int = 0
    output_token: int = 0
    cost: float = 0.0
    model: str = "unknown"
    status: str = "completed"
    error: Optional[str] = None


@dataclass
class DetailedRecordLog:
    """
    Detailed token usage log for a record with separate doc extraction and crew costs.

    This captures:
    - Document extraction (OCR): Uses vision model (gemini-2.5-pro) for image/PDF processing
    - Crew processing: Uses text model (gemini-2.5-flash) for verification analysis
    """
    record_type: str
    # Document extraction costs (OCR/vision processing)
    doc_input: int = 0
    doc_output: int = 0
    doc_cost: float = 0.0
    doc_model: str = "unknown"
    # Crew processing costs (verification analysis)
    crew_input: int = 0
    crew_output: int = 0
    crew_cost: float = 0.0
    crew_model: str = "unknown"
    # Status and validation errors
    status: str = "completed"
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "record_type": self.record_type,
            "doc_input": self.doc_input,
            "doc_output": self.doc_output,
            "doc_cost": round(self.doc_cost, 6),
            "doc_model": self.doc_model,
            "crew_input": self.crew_input,
            "crew_output": self.crew_output,
            "crew_cost": round(self.crew_cost, 6),
            "crew_model": self.crew_model,
            "total_cost": round(self.doc_cost + self.crew_cost, 6),
            "status": self.status,
            "error": self.error
        }


@dataclass
class AttemptLog:
    """Log for a single job attempt."""
    count: int
    timestamp: str  # ISO 8601 format timestamp
    status: str  # "success" or "failed"
    error: Optional[str] = None
    records: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = {
            "count": self.count,
            "timestamp": self.timestamp,
            "status": self.status,
            "records": self.records
        }
        if self.error:
            result["error"] = self.error
        return result


class JobRunLogger:
    """
    Tracks token usage and costs for a job run.

    Usage:
        logger = JobRunLogger()
        logger.load_existing_logs(existing_logs_json)  # Load from Salesforce
        logger.start_attempt()

        # After each record is processed:
        logger.add_record_log("education_XII", input_tokens, output_tokens, cost, model)

        # After attempt completes:
        logger.finalize_attempt(status="success")  # or status="failed", error="..."

        logs_json = logger.get_logs_json()  # Save to Salesforce
    """

    def __init__(self):
        self._attempts: List[AttemptLog] = []
        self._current_attempt: Optional[AttemptLog] = None
        self._current_records: List[Dict[str, Any]] = []

    def load_existing_logs(self, logs_json: Optional[str]) -> int:
        """
        Load existing logs from Salesforce to determine retry count.

        Args:
            logs_json: JSON string from AI_Server_Job__c.Logs__c field

        Returns:
            Current retry count (number of existing attempts)
        """
        if not logs_json:
            self._attempts = []
            return 0

        try:
            existing_logs = json.loads(logs_json)
            if isinstance(existing_logs, list):
                # Reconstruct AttemptLog objects from dicts
                for log_dict in existing_logs:
                    attempt = AttemptLog(
                        count=log_dict.get("count", 0),
                        timestamp=log_dict.get("timestamp", ""),
                        status=log_dict.get("status", "unknown"),
                        error=log_dict.get("error"),
                        records=log_dict.get("records", [])
                    )
                    self._attempts.append(attempt)
                logger.info(f"Loaded {len(self._attempts)} existing attempt(s) from logs")
                return len(self._attempts)
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse existing logs JSON: {e}")
        except Exception as e:
            logger.warning(f"Error loading existing logs: {e}")

        self._attempts = []
        return 0

    def get_current_retry_count(self) -> int:
        """Get the current retry count based on existing attempts."""
        return len(self._attempts)

    def start_attempt(self):
        """
        Start tracking a new attempt.
        Resets the global token usage accumulator for fresh tracking.
        """
        reset_global_usage()
        self._current_records = []
        next_count = len(self._attempts) + 1
        timestamp = datetime.now(timezone.utc).isoformat()
        self._current_attempt = AttemptLog(count=next_count, timestamp=timestamp, status="in_progress")
        logger.info(f"Started tracking attempt #{next_count} at {timestamp}")

    def add_record_log(
        self,
        record_type: str,
        input_tokens: int,
        output_tokens: int,
        cost: float,
        model: str,
        status: str = "completed",
        error: Optional[str] = None
    ):
        """
        Add token usage log for a single record.

        Args:
            record_type: Type of record (e.g., "education_XII", "employment_full_time")
            input_tokens: Number of input/prompt tokens used
            output_tokens: Number of output/completion tokens used
            cost: Total cost in USD
            model: Model name used for this record
            status: Status of the record processing ("completed" or "failed")
            error: Error message if status is "failed"
        """
        record_log = RecordLog(
            record_type=record_type,
            input_token=input_tokens,
            output_token=output_tokens,
            cost=round(cost, 6),
            model=model,
            status=status,
            error=error
        )
        self._current_records.append(asdict(record_log))
        logger.debug(f"Added record log: {record_type} - {input_tokens} in, {output_tokens} out, ${cost:.6f}")

    def add_record_log_from_cost_summary(self, record_type: str, reset_after: bool = True):
        """
        Add record log using the current global cost summary.
        Useful when you want to capture all token usage since last reset.

        Args:
            record_type: Type of record being logged (e.g., "education_XII", "doc_extraction")
            reset_after: Whether to reset the global usage after logging
        """
        summary = get_job_cost_summary()

        # Get the primary model from the breakdown
        model = "unknown"
        breakdown = summary.get("detailed_breakdown", [])
        if breakdown:
            # Use the most recent model from breakdown
            model = breakdown[-1].get("model", "unknown")

        totals = summary.get("totals", {})
        self.add_record_log(
            record_type=record_type,
            input_tokens=totals.get("prompt_tokens", 0),
            output_tokens=totals.get("completion_tokens", 0),
            cost=totals.get("total_cost_usd", 0.0),
            model=model
        )

        # Reset for next record if requested
        if reset_after:
            reset_global_usage()

    def add_document_extraction_log(self, doc_type: str, reset_after: bool = True):
        """
        Add log for document extraction (OCR) processing.

        Args:
            doc_type: Type of document being processed (e.g., "pdf", "image")
            reset_after: Whether to reset the global usage after logging
        """
        self.add_record_log_from_cost_summary(
            record_type=f"doc_extraction_{doc_type}",
            reset_after=reset_after
        )

    def capture_current_usage(self) -> Dict[str, Any]:
        """
        Capture the current global usage without resetting.

        Returns:
            Dictionary with input_tokens, output_tokens, cost, model
        """
        summary = get_job_cost_summary()
        model = "unknown"
        breakdown = summary.get("detailed_breakdown", [])
        if breakdown:
            model = breakdown[-1].get("model", "unknown")

        totals = summary.get("totals", {})
        return {
            "input_tokens": totals.get("prompt_tokens", 0),
            "output_tokens": totals.get("completion_tokens", 0),
            "cost": totals.get("total_cost_usd", 0.0),
            "model": model
        }

    def add_detailed_record_log(
        self,
        record_type: str,
        doc_usage: Dict[str, Any],
        crew_usage: Dict[str, Any],
        status: str = "completed",
        error: Optional[str] = None
    ):
        """
        Add a detailed record log with separate doc extraction and crew processing costs.

        Args:
            record_type: Type of record (e.g., "Education Records_a1b2c3d4")
            doc_usage: Dictionary with doc extraction usage {input_tokens, output_tokens, cost, model}
            crew_usage: Dictionary with crew processing usage {input_tokens, output_tokens, cost, model}
            status: Status of the record processing ("completed" or "failed")
            error: Error message if status is "failed"
        """
        detailed_log = DetailedRecordLog(
            record_type=record_type,
            doc_input=doc_usage.get("input_tokens", 0),
            doc_output=doc_usage.get("output_tokens", 0),
            doc_cost=doc_usage.get("cost", 0.0),
            doc_model=doc_usage.get("model", "unknown"),
            crew_input=crew_usage.get("input_tokens", 0),
            crew_output=crew_usage.get("output_tokens", 0),
            crew_cost=crew_usage.get("cost", 0.0),
            crew_model=crew_usage.get("model", "unknown"),
            status=status,
            error=error
        )
        self._current_records.append(detailed_log.to_dict())
        total_cost = detailed_log.doc_cost + detailed_log.crew_cost
        logger.info(
            f"Added detailed record log: {record_type} - "
            f"Status: {status} | "
            f"Doc: {doc_usage.get('input_tokens', 0)} in, {doc_usage.get('output_tokens', 0)} out, ${doc_usage.get('cost', 0):.6f} | "
            f"Crew: {crew_usage.get('input_tokens', 0)} in, {crew_usage.get('output_tokens', 0)} out, ${crew_usage.get('cost', 0):.6f} | "
            f"Total: ${total_cost:.6f}"
        )

    def finalize_attempt(self, status: str, error: Optional[str] = None):
        """
        Finalize the current attempt and add it to the attempts list.

        Args:
            status: "success" or "failed"
            error: Error message if status is "failed"
        """
        if not self._current_attempt:
            logger.warning("No current attempt to finalize")
            return

        self._current_attempt.status = status
        self._current_attempt.error = error
        self._current_attempt.records = self._current_records.copy()

        self._attempts.append(self._current_attempt)

        logger.info(
            f"Finalized attempt #{self._current_attempt.count}: "
            f"status={status}, records={len(self._current_records)}"
        )

        self._current_attempt = None
        self._current_records = []

    def get_logs_json(self) -> str:
        """
        Get the complete logs as a JSON string for saving to Salesforce.

        Returns:
            JSON string with all attempts
        """
        logs_list = [attempt.to_dict() for attempt in self._attempts]
        return json.dumps(logs_list, ensure_ascii=False)

    def get_logs_dict(self) -> List[Dict[str, Any]]:
        """
        Get the complete logs as a list of dictionaries.

        Returns:
            List of attempt dictionaries
        """
        return [attempt.to_dict() for attempt in self._attempts]

    def get_latest_attempt_summary(self) -> Optional[Dict[str, Any]]:
        """
        Get a summary of the latest attempt for quick reference.

        Returns:
            Dictionary with attempt summary or None if no attempts
        """
        if not self._attempts:
            return None

        latest = self._attempts[-1]
        total_input = sum(r.get("input_token", 0) for r in latest.records)
        total_output = sum(r.get("output_token", 0) for r in latest.records)
        total_cost = sum(r.get("cost", 0) for r in latest.records)

        return {
            "attempt_number": latest.count,
            "status": latest.status,
            "error": latest.error,
            "record_count": len(latest.records),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cost_usd": round(total_cost, 6)
        }


# Global instance for use across processors
_job_logger_instance: Optional[JobRunLogger] = None


def get_job_logger() -> JobRunLogger:
    """Get the global JobRunLogger instance, creating one if needed."""
    global _job_logger_instance
    if _job_logger_instance is None:
        _job_logger_instance = JobRunLogger()
    return _job_logger_instance


def reset_job_logger():
    """Reset the global JobRunLogger instance for a new job."""
    global _job_logger_instance
    _job_logger_instance = JobRunLogger()
    return _job_logger_instance
