"""
Process-isolated worker for job execution.
Each job runs in a separate process - when process terminates, ALL resources are freed by OS.
This guarantees ABSOLUTE zero memory/thread leakage.

NO resource manager needed - OS kernel cleanup is absolute.

LOGGING STRATEGY:
- During processing: Track all token/cost data in memory only (NO logs DML)
- At job completion: Fetch existing logs from SF, append new attempt, upsert
- This ensures logs are NEVER cleared during intermediate status updates
"""

import sys
import os
import json
import logging
import asyncio
import importlib
import io
from typing import Dict, Any, Optional

# Import job run logger for tracking token usage and costs
from app.core.job_run_logger import reset_job_logger

# Preserve the original stdout pipe so we can emit JSON results cleanly.
_RESULT_PIPE = sys.stdout

# Ensure project root is on sys.path when worker runs as subprocess
WORKER_DIR = os.path.dirname(os.path.abspath(__file__))            # .../app/workers
APP_DIR = os.path.dirname(WORKER_DIR)                              # .../app
PROJECT_ROOT = os.path.dirname(APP_DIR)                            # repo root
for path in (APP_DIR, PROJECT_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)


class _StdoutRedirector(io.TextIOBase):
    """Redirects all stdout writes to stderr to keep JSON channel clean."""

    def write(self, s):
        sys.stderr.write(s)
        sys.stderr.flush()
        return len(s)

    def flush(self):
        sys.stderr.flush()


# Redirect everything that writes to sys.stdout (e.g., CrewAI verbose logs)
# so our JSON output remains the only content on the saved _RESULT_PIPE.
sys.stdout = _StdoutRedirector()

# Setup logging for worker process
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - WORKER-%(process)d - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def _emit_json(payload: Dict[str, Any]) -> None:
    """Write JSON payload to the preserved stdout pipe."""
    _RESULT_PIPE.write(json.dumps(payload) + "\n")
    _RESULT_PIPE.flush()


def _emit_progress_update(progress: Optional[Dict[str, Any]]):
    if not progress:
        return
    try:
        snapshot = json.loads(json.dumps(progress))
    except Exception:
        snapshot = progress
    _emit_json({"type": "progress", "progress": snapshot})


async def _fetch_existing_logs(sf_service, application_id: str) -> Optional[str]:
    """
    Fetch existing logs from Salesforce at job completion time.
    This ensures we always get the freshest data before appending.
    """
    try:
        job_data = await sf_service.get_latest_ai_server_job(application_id)
        if job_data:
            return job_data.get("logs")
        return None
    except Exception as e:
        logger.warning(f"Could not fetch existing logs for {application_id}: {e}")
        return None


async def execute_job_in_process(job_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a complete job in this isolated process.
    When process terminates, OS guarantees ALL resources are freed.

    Args:
        job_data: Dictionary containing:
            - job_id: str
            - application_id: str
            - sf_config: dict with Salesforce credentials
            - prefetched_data: dict with record IDs and processor info

    Returns:
        Result dictionary with status, progress, and any error info
    """
    job_id = job_data.get('job_id', 'unknown')
    application_id = job_data.get('application_id', 'unknown')

    logger.info(f"Worker process {os.getpid()} starting job {job_id} for application {application_id}")

    # Initialize job run logger for this attempt - start fresh, we'll merge with existing logs at completion
    # This is the correct pattern: track usage in memory during job, merge at the end
    job_logger = reset_job_logger()
    job_logger.start_attempt()  # Start counting from 1, will be adjusted at completion

    try:
        # Import dependencies (fresh in this process)
        from app.services.salesforce_service import SalesforceService
        from app.services.document_extraction_service import create_text_extractor
        from app.config import (
            RELATED_RECORD_PROCESSING_CONFIG,
            APPLICATION_OBJECT_API_NAME,
            READABLE_OBJECT_NAMES
        )

        # Extract parameters
        sf_config = job_data['sf_config']
        prefetched_data = job_data['prefetched_data']

        # Create Salesforce connection (will auto-cleanup when process exits)
        sf_service = SalesforceService(
            client_id=sf_config['client_id'],
            client_secret=sf_config['client_secret'],
            token_url=sf_config['token_url']
        )
        logger.info(f"Worker {os.getpid()}: Connected to Salesforce {sf_service.instance_url}")

        # Create document extractor (process cleanup is absolute)
        job_extractor = create_text_extractor()
        logger.info(f"Worker {os.getpid()}: Document extractor initialized")

        # Initialize progress tracking
        progress = {}
        for record_type, data in prefetched_data.items():
            readable_name = READABLE_OBJECT_NAMES.get(record_type, record_type)
            progress[readable_name] = {
                "status": "pending",
                "total": len(data.get("ids", [])),
                "processed": 0
            }

        # Update Salesforce: processing started
        await sf_service.upsert_ai_server_job(
            job_id=job_id,
            application_id=application_id,
            status="processing",
            message="Worker process started. Initializing analysis...",
            progress_details=json.dumps(progress)
        )
        _emit_progress_update(progress)

        # Sort records by priority
        sorted_records = sorted(
            prefetched_data.items(),
            key=lambda x: next(
                (cfg["priority"] for cfg in RELATED_RECORD_PROCESSING_CONFIG
                 if cfg["target_record_type"] == x[0]), 999
            )
        )

        # Process all records sequentially by priority
        for record_type, data in sorted_records:
            readable_name = READABLE_OBJECT_NAMES.get(record_type, record_type)

            if not data.get("ids"):
                progress[readable_name]["status"] = "skipped"
                await sf_service.upsert_ai_server_job(
                    job_id=job_id,
                    application_id=application_id,
                    status="processing",
                    progress_details=json.dumps(progress)
                )
                _emit_progress_update(progress)
                continue

            progress[readable_name]["status"] = "processing"
            await sf_service.upsert_ai_server_job(
                job_id=job_id,
                application_id=application_id,
                status="processing",
                progress_details=json.dumps(progress)
            )
            _emit_progress_update(progress)

            # Load processor dynamically
            module = importlib.import_module(data["processor_module"])
            func = getattr(module, data["processor_function_name"])

            # Process each record
            for i, r_id in enumerate(data["ids"]):
                logger.info(
                    f"Worker {os.getpid()}: Processing {readable_name} "
                    f"record {i+1}/{len(data['ids'])} (ID: {r_id})"
                )

                # Call processor - processors now handle detailed token logging internally
                # (doc extraction and crew processing are tracked separately)
                if record_type == APPLICATION_OBJECT_API_NAME:
                    await func(
                        sf_service,
                        r_id,
                        application_id,
                        record_type,
                        item_index=(i + 1),
                        extractor_instance=job_extractor,
                    )
                else:
                    await func(
                        sf_service,
                        r_id,
                        application_id,
                        extractor_instance=job_extractor,
                        item_index=(i + 1),
                    )

                progress[readable_name]["processed"] += 1
                await sf_service.upsert_ai_server_job(
                    job_id=job_id,
                    application_id=application_id,
                    status="processing",
                    progress_details=json.dumps(progress)
                )
                _emit_progress_update(progress)

            progress[readable_name]["status"] = "completed"
            await sf_service.upsert_ai_server_job(
                job_id=job_id,
                application_id=application_id,
                status="processing",
                progress_details=json.dumps(progress)
            )
            _emit_progress_update(progress)

        # JOB COMPLETION - Now fetch existing logs, merge, and save
        # This is the ONLY place where logs field is updated
        logger.info(f"Worker {os.getpid()}: Job {job_id} processing complete. Fetching existing logs for merge...")

        # Fetch existing logs from Salesforce (fresh query at completion time)
        existing_logs_json = await _fetch_existing_logs(sf_service, application_id)
        existing_count = 0
        if existing_logs_json:
            try:
                existing_logs = json.loads(existing_logs_json)
                existing_count = len(existing_logs) if isinstance(existing_logs, list) else 0
                logger.info(f"Worker {os.getpid()}: Found {existing_count} existing log attempt(s)")
            except json.JSONDecodeError:
                logger.warning(f"Worker {os.getpid()}: Could not parse existing logs, starting fresh")
                existing_logs = []
        else:
            existing_logs = []
            logger.info(f"Worker {os.getpid()}: No existing logs found, this is attempt #1")

        # Finalize current attempt with correct count
        job_logger.finalize_attempt(status="success")

        # Get current attempt data and adjust its count
        current_attempt = job_logger.get_logs_dict()
        if current_attempt:
            # Adjust the count to be existing_count + 1
            current_attempt[0]["count"] = existing_count + 1

        # Merge: existing logs + current attempt
        merged_logs = existing_logs + current_attempt
        merged_logs_json = json.dumps(merged_logs, ensure_ascii=False)

        logger.info(f"Worker {os.getpid()}: Merged logs - now {len(merged_logs)} total attempt(s)")

        # All processing complete - save merged logs to Salesforce
        await sf_service.upsert_ai_server_job(
            job_id=job_id,
            application_id=application_id,
            status="completed",
            message="All verification tasks completed successfully.",
            progress_details=json.dumps(progress),
            logs=merged_logs_json
        )
        _emit_progress_update(progress)

        logger.info(f"Worker {os.getpid()}: Job {job_id} completed successfully")
        logger.info(f"Worker {os.getpid()}: Job logs: {job_logger.get_latest_attempt_summary()}")

        return {
            "status": "completed",
            "message": "All verification tasks completed successfully.",
            "progress": progress,
            "error": None,
            "logs": merged_logs
        }

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Worker {os.getpid()}: Job {job_id} failed: {error_msg}", exc_info=True)

        # JOB FAILURE - Same pattern: fetch existing logs, merge, and save
        merged_logs = []
        try:
            if 'sf_service' in locals():
                # Fetch existing logs from Salesforce
                existing_logs_json = await _fetch_existing_logs(sf_service, application_id)
                existing_count = 0
                if existing_logs_json:
                    try:
                        existing_logs = json.loads(existing_logs_json)
                        existing_count = len(existing_logs) if isinstance(existing_logs, list) else 0
                    except json.JSONDecodeError:
                        existing_logs = []
                else:
                    existing_logs = []

                # Finalize current attempt as failed
                job_logger.finalize_attempt(status="failed", error=error_msg[:500])

                # Get current attempt and adjust count
                current_attempt = job_logger.get_logs_dict()
                if current_attempt:
                    current_attempt[0]["count"] = existing_count + 1

                # Merge logs
                merged_logs = existing_logs + current_attempt
                merged_logs_json = json.dumps(merged_logs, ensure_ascii=False)

                logger.info(f"Worker {os.getpid()}: Merged logs on failure - now {len(merged_logs)} total attempt(s)")

                # Update Salesforce with error and merged logs
                await sf_service.upsert_ai_server_job(
                    job_id=job_id,
                    application_id=application_id,
                    status="failed",
                    message=error_msg[:131072],
                    progress_details=json.dumps(progress) if 'progress' in locals() else None,
                    logs=merged_logs_json
                )
                if 'progress' in locals():
                    _emit_progress_update(progress)
        except Exception as sf_error:
            logger.error(f"Worker {os.getpid()}: Failed to update Salesforce with error: {sf_error}")

        return {
            "status": "failed",
            "message": error_msg,
            "progress": progress if 'progress' in locals() else {},
            "error": error_msg,
            "logs": merged_logs
        }


def worker_process_main():
    """
    Main entry point for worker process.
    Reads job data from stdin, executes job, writes result to stdout.

    Exit codes:
        0: Job completed successfully
        1: Job failed
        2: Worker process fatal error
    """
    try:
        # Read job data from stdin (JSON)
        job_data_json = sys.stdin.read()

        if not job_data_json:
            logger.error("Worker received empty input")
            sys.exit(2)

        job_data = json.loads(job_data_json)
        logger.info(f"Worker {os.getpid()} received job data: {job_data.get('job_id')}")

        # Execute job
        result = asyncio.run(execute_job_in_process(job_data))

        _emit_json({"type": "result", **result})

        # Exit with appropriate code
        exit_code = 0 if result['status'] == 'completed' else 1
        logger.info(f"Worker {os.getpid()} exiting with code {exit_code}")
        sys.exit(exit_code)

    except json.JSONDecodeError as e:
        logger.critical(f"Worker {os.getpid()}: Invalid JSON input: {e}")
        error_result = {
            "status": "failed",
            "message": f"Worker process received invalid JSON: {str(e)}",
            "error": str(e)
        }
        _emit_json({"type": "result", **error_result})
        sys.exit(2)

    except Exception as e:
        logger.critical(f"Worker {os.getpid()}: Fatal error: {e}", exc_info=True)
        error_result = {
            "status": "failed",
            "message": f"Worker process fatal error: {str(e)}",
            "error": str(e)
        }
        _emit_json({"type": "result", **error_result})
        sys.exit(2)


if __name__ == "__main__":
    worker_process_main()
