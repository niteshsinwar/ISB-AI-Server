"""
Process Manager for spawning and managing worker processes.
Each job runs in an isolated process - absolute zero memory/thread leakage.

Key features:
- Spawns worker process per job
- Monitors worker health with timeout
- Handles worker crashes gracefully
- Enforces resource limits per worker
- Guarantees cleanup via process termination (SIGTERM → SIGKILL)
"""

import os
import sys
import json
import asyncio
import logging
from asyncio.subprocess import Process
from typing import Dict, Any, Optional, Callable, Awaitable

from app.config import JOB_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


class WorkerProcessError(Exception):
    """Raised when worker process fails to execute properly."""
    pass


class ProcessManager:
    """
    Manages worker processes for job execution.
    Ensures absolute resource cleanup via OS process termination.
    """

    def __init__(self):
        self._active_workers: Dict[str, Process] = {}
        self._lock = asyncio.Lock()
        self.worker_script_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'workers',
            'job_worker.py'
        )

        if not os.path.exists(self.worker_script_path):
            raise RuntimeError(f"Worker script not found: {self.worker_script_path}")

        logger.info(f"ProcessManager initialized. Worker script: {self.worker_script_path}")

    async def execute_job_in_worker(
        self,
        job_id: str,
        application_id: str,
        sf_config: Dict[str, str],
        prefetched_data: Dict[str, Any],
        timeout_seconds: Optional[int] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None
    ) -> Dict[str, Any]:
        """
        Execute a job in an isolated worker process.

        Args:
            job_id: Unique job identifier
            application_id: Salesforce application record ID
            sf_config: Salesforce connection credentials
            prefetched_data: Pre-fetched record IDs and processor configuration
            timeout_seconds: Maximum execution time (default from config: 14 minutes)

        Returns:
            Result dictionary with status, progress, and any errors

        Raises:
            WorkerProcessError: If worker fails to spawn or execute
            asyncio.TimeoutError: If worker exceeds timeout

        Note:
            Worker fetches existing logs from Salesforce at completion time.
            This ensures logs are never cleared during intermediate updates.
        """
        # Use provided timeout or fall back to configured default (14 minutes)
        timeout = timeout_seconds if timeout_seconds is not None else JOB_TIMEOUT_SECONDS

        logger.info(f"Spawning worker process for job {job_id} (timeout: {timeout}s / {timeout/60:.1f} minutes)")

        # Prepare job data for worker
        # Note: existing_logs not passed - worker fetches at completion time
        job_data = {
            'job_id': job_id,
            'application_id': application_id,
            'sf_config': sf_config,
            'prefetched_data': prefetched_data
        }

        # Spawn worker process
        worker_process: Optional[Process] = None

        try:
            worker_process = await self._spawn_worker(job_id, job_data)

            # Wait for completion with timeout
            result = await asyncio.wait_for(
                self._wait_for_worker(job_id, worker_process, progress_callback=progress_callback),
                timeout=timeout
            )

            logger.info(f"Worker for job {job_id} completed with status: {result.get('status')}")
            return result

        except asyncio.TimeoutError:
            logger.error(f"Worker for job {job_id} exceeded timeout ({timeout}s / {timeout/60:.1f} minutes)")
            await self._kill_worker(job_id, worker_process)
            raise WorkerProcessError(
                f"Job exceeded maximum execution time of {timeout} seconds ({timeout/60:.1f} minutes)"
            )

        except Exception as e:
            logger.error(f"Worker for job {job_id} failed: {e}", exc_info=True)
            if worker_process:
                await self._kill_worker(job_id, worker_process)
            raise WorkerProcessError(f"Worker process failed: {str(e)}")

        finally:
            # Cleanup tracking
            async with self._lock:
                self._active_workers.pop(job_id, None)

    async def _spawn_worker(
        self,
        job_id: str,
        job_data: Dict[str, Any]
    ) -> Process:
        """
        Spawn a new worker process.

        Args:
            job_id: Job identifier for tracking
            job_data: Data to pass to worker via stdin

        Returns:
            Popen process object

        Raises:
            WorkerProcessError: If spawn fails
        """
        try:
            # Serialize job data
            job_data_json = json.dumps(job_data)

            # Spawn worker process
            # Use same Python interpreter as main process
            python_executable = sys.executable

            # Prepare environment with project root in PYTHONPATH
            env = os.environ.copy()
            # __file__ is app/core/process_manager.py -> root is 3 levels up
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            python_path = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{project_root}:{python_path}" if python_path else project_root

            process = await asyncio.create_subprocess_exec(
                python_executable,
                self.worker_script_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Pass the modified environment
                env=env,
                # Process group for easier cleanup
                preexec_fn=os.setpgrp if hasattr(os, 'setpgrp') else None
            )

            # Track active worker
            async with self._lock:
                self._active_workers[job_id] = process

            logger.info(f"Spawned worker process PID {process.pid} for job {job_id}")

            # Send job data to worker via stdin
            process.stdin.write(job_data_json.encode('utf-8'))
            await process.stdin.drain()
            process.stdin.close()

            return process

        except Exception as e:
            logger.error(f"Failed to spawn worker for job {job_id}: {e}", exc_info=True)
            raise WorkerProcessError(f"Failed to spawn worker process: {str(e)}")

    async def _wait_for_worker(
        self,
        job_id: str,
        process: Process,
        progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        """
        Wait for worker process to complete and return result while consuming streamed progress updates.
        """
        try:
            final_result: Optional[Dict[str, Any]] = None

            if process.stdout:
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    decoded = line.decode('utf-8', errors='replace').strip()
                    if not decoded:
                        continue
                    try:
                        message = json.loads(decoded)
                    except json.JSONDecodeError:
                        logger.warning(f"Worker {process.pid} emitted non-JSON output: {decoded[:200]}")
                        continue

                    msg_type = message.get('type')
                    if msg_type == 'progress':
                        payload = message.get('progress') or message.get('data')
                        if progress_callback and isinstance(payload, dict):
                            try:
                                await progress_callback(payload)
                            except Exception as progress_err:
                                logger.error(
                                    f"Progress callback failed for job {job_id}: {progress_err}",
                                    exc_info=True,
                                )
                    else:
                        final_result = message
            else:
                await process.wait()

            stderr_text = ''
            if process.stderr:
                stderr_bytes = await process.stderr.read()
                if stderr_bytes:
                    stderr_text = stderr_bytes.decode('utf-8', errors='replace')

            exit_code = await process.wait()

            if stderr_text:
                if exit_code != 0:
                    logger.error(f"Worker {process.pid} stderr:\n{stderr_text}")
                else:
                    logger.info(f"Worker {process.pid} stderr:\n{stderr_text}")

            if not final_result:
                raise WorkerProcessError(
                    f"Worker process {process.pid} produced no output. Exit code: {exit_code}"
                )

            if isinstance(final_result, dict) and final_result.get('type') == 'result':
                final_result = {k: v for k, v in final_result.items() if k != 'type'}

            if not isinstance(final_result, dict) or 'status' not in final_result:
                raise WorkerProcessError(
                    f"Worker produced invalid result structure: {final_result}"
                )

            if exit_code != 0:
                error_msg = final_result.get('error') or final_result.get('message') or 'Unknown error'
                logger.warning(
                    f"Worker {process.pid} exited with code {exit_code}: {error_msg}"
                )

            logger.info(f"Worker {process.pid} for job {job_id} completed: {final_result.get('status')}")
            return final_result

        except asyncio.CancelledError:
            logger.warning(f"Wait cancelled for worker {process.pid} (job {job_id})")
            raise

        except Exception as e:
            if not isinstance(e, WorkerProcessError):
                logger.error(f"Error waiting for worker {process.pid}: {e}", exc_info=True)
                raise WorkerProcessError(f"Worker communication failed: {str(e)}")
            raise

    async def _kill_worker(
        self,
        job_id: str,
        process: Process,
        timeout: float = 5.0
    ) -> None:
        """
        Forcefully terminate a worker process.
        Tries SIGTERM first, then SIGKILL after timeout.

        Args:
            job_id: Job identifier
            process: Process to terminate
            timeout: Seconds to wait for graceful termination
        """
        if not process or process.returncode is not None:
            return  # Already terminated

        logger.warning(f"Terminating worker {process.pid} for job {job_id}")

        try:
            # Try graceful termination (SIGTERM)
            process.terminate()

            try:
                # Wait for graceful shutdown
                await asyncio.wait_for(process.wait(), timeout=timeout)
                logger.info(f"Worker {process.pid} terminated gracefully")
                return
            except asyncio.TimeoutError:
                logger.warning(
                    f"Worker {process.pid} did not terminate gracefully, forcing kill"
                )

            # Force kill (SIGKILL)
            process.kill()
            await process.wait()
            logger.info(f"Worker {process.pid} killed forcefully")

        except Exception as e:
            logger.error(f"Error killing worker {process.pid}: {e}")

    async def get_active_workers(self) -> Dict[str, int]:
        """
        Get currently active worker processes.

        Returns:
            Dictionary mapping job_id to process PID
        """
        async with self._lock:
            return {
                job_id: process.pid
                for job_id, process in self._active_workers.items()
                if process.returncode is None
            }

    async def kill_job_worker(self, job_id: str) -> bool:
        """
        Forcefully terminate a specific job's worker process.

        Args:
            job_id: Job identifier

        Returns:
            True if worker was killed, False if not found
        """
        async with self._lock:
            process = self._active_workers.get(job_id)

        if not process:
            logger.warning(f"No active worker found for job {job_id}")
            return False

        await self._kill_worker(job_id, process)
        return True

    async def kill_all_workers(self) -> int:
        """
        Emergency function: Kill all active worker processes.

        Returns:
            Number of workers killed
        """
        async with self._lock:
            workers = list(self._active_workers.items())

        killed = 0
        for job_id, process in workers:
            try:
                await self._kill_worker(job_id, process)
                killed += 1
            except Exception as e:
                logger.error(f"Failed to kill worker for job {job_id}: {e}")

        logger.warning(f"Emergency shutdown: killed {killed} worker processes")
        return killed


# Global singleton instance
_process_manager_instance: Optional[ProcessManager] = None
_process_manager_lock = asyncio.Lock()


async def get_process_manager() -> ProcessManager:
    """
    Get or create the global ProcessManager singleton.

    Returns:
        ProcessManager instance
    """
    global _process_manager_instance

    if _process_manager_instance is None:
        async with _process_manager_lock:
            if _process_manager_instance is None:
                _process_manager_instance = ProcessManager()
                logger.info("ProcessManager singleton created")

    return _process_manager_instance
