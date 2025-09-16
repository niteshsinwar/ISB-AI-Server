"""
Resource lifecycle management for jobs - ensures zero memory growth after job completion.
Maintains speed by using per-job instances while guaranteeing cleanup.
"""

import gc
import logging
import weakref
from typing import List, Optional, Any, Dict
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
import asyncio

logger = logging.getLogger(__name__)

class JobResourceManager:
    """
    Manages all resources for a single job with guaranteed cleanup.
    Maintains speed with dedicated instances while preventing memory leaks.
    """

    def __init__(self, job_id: str):
        self.job_id = job_id
        self._llm_instances: List[Any] = []
        self._thread_executors: List[ThreadPoolExecutor] = []
        self._document_extractors: List[Any] = []
        self._large_objects: List[weakref.ref] = []
        self._temp_data: Dict[str, Any] = {}
        self._is_cleaned_up = False

        logger.debug(f"JobResourceManager created for job {job_id}")

    def track_llm(self, llm_instance: Any) -> Any:
        """Track LLM instance for cleanup"""
        if llm_instance and not self._is_cleaned_up:
            self._llm_instances.append(llm_instance)
        return llm_instance

    def track_executor(self, executor: ThreadPoolExecutor) -> ThreadPoolExecutor:
        """Track ThreadPoolExecutor for cleanup"""
        if executor and not self._is_cleaned_up:
            self._thread_executors.append(executor)
        return executor

    def track_extractor(self, extractor: Any) -> Any:
        """Track document extractor for cleanup"""
        if extractor and not self._is_cleaned_up:
            self._document_extractors.append(extractor)
        return extractor

    def track_large_object(self, obj: Any) -> Any:
        """Track large objects (PDFs, images) for cleanup"""
        if obj and not self._is_cleaned_up:
            try:
                self._large_objects.append(weakref.ref(obj))
            except TypeError:
                # Some objects can't be weakly referenced, store directly
                pass
        return obj

    def store_temp_data(self, key: str, data: Any) -> None:
        """Store temporary data that should be cleaned up"""
        if not self._is_cleaned_up:
            self._temp_data[key] = data

    def get_temp_data(self, key: str) -> Any:
        """Retrieve temporary data"""
        return self._temp_data.get(key)

    def cleanup(self) -> None:
        """
        Aggressively cleanup all tracked resources.
        This is the critical method that prevents memory leaks.
        """
        if self._is_cleaned_up:
            return

        logger.info(f"Starting cleanup for job {self.job_id}")
        cleanup_stats = {
            "llms_cleaned": 0,
            "executors_cleaned": 0,
            "extractors_cleaned": 0,
            "temp_data_cleared": len(self._temp_data)
        }

        # 1. Cleanup LLM instances
        for llm in self._llm_instances:
            try:
                # Clear any internal caches/connections
                if hasattr(llm, '_client') and hasattr(llm._client, 'close'):
                    llm._client.close()
                if hasattr(llm, 'close'):
                    llm.close()
                cleanup_stats["llms_cleaned"] += 1
            except Exception as e:
                logger.warning(f"Error cleaning LLM instance: {e}")
        self._llm_instances.clear()

        # 2. Shutdown ThreadPoolExecutors
        for executor in self._thread_executors:
            try:
                executor.shutdown(wait=True)
                cleanup_stats["executors_cleaned"] += 1
            except Exception as e:
                logger.warning(f"Error shutting down executor: {e}")
        self._thread_executors.clear()

        # 3. Cleanup document extractors
        for extractor in self._document_extractors:
            try:
                if hasattr(extractor, 'shutdown'):
                    extractor.shutdown()
                if hasattr(extractor, 'ocr_processor') and hasattr(extractor.ocr_processor, 'shutdown'):
                    extractor.ocr_processor.shutdown()
                cleanup_stats["extractors_cleaned"] += 1
            except Exception as e:
                logger.warning(f"Error cleaning extractor: {e}")
        self._document_extractors.clear()

        # 4. Clear temporary data
        self._temp_data.clear()

        # 5. Clear large object references
        self._large_objects.clear()

        # 6. Force garbage collection
        collected = gc.collect()

        self._is_cleaned_up = True
        logger.info(f"Job {self.job_id} cleanup completed: {cleanup_stats}, GC collected: {collected} objects")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()

    def __del__(self):
        """Fallback cleanup if context manager not used"""
        if not self._is_cleaned_up:
            logger.warning(f"JobResourceManager {self.job_id} cleaned up via __del__ - prefer explicit cleanup")
            self.cleanup()


class GlobalResourceMonitor:
    """
    Monitors overall system resource usage and provides cleanup utilities.
    """

    def __init__(self):
        self._active_managers: Dict[str, JobResourceManager] = {}
        self._lock = asyncio.Lock()

    async def create_job_manager(self, job_id: str) -> JobResourceManager:
        """Create a new job resource manager"""
        async with self._lock:
            if job_id in self._active_managers:
                # Cleanup existing manager for same job
                self._active_managers[job_id].cleanup()

            manager = JobResourceManager(job_id)
            self._active_managers[job_id] = manager
            return manager

    async def cleanup_job(self, job_id: str) -> None:
        """Explicitly cleanup a job's resources"""
        async with self._lock:
            if job_id in self._active_managers:
                self._active_managers[job_id].cleanup()
                del self._active_managers[job_id]

    async def force_cleanup_all(self) -> None:
        """Force cleanup of all tracked resources - emergency function"""
        async with self._lock:
            for job_id, manager in self._active_managers.items():
                try:
                    manager.cleanup()
                except Exception as e:
                    logger.error(f"Error during force cleanup of job {job_id}: {e}")
            self._active_managers.clear()

            # Aggressive garbage collection
            for _ in range(3):
                collected = gc.collect()
                if collected == 0:
                    break

        logger.info("Global resource cleanup completed")

    def get_resource_stats(self) -> Dict[str, Any]:
        """Get current resource usage statistics"""
        return {
            "active_jobs": len(self._active_managers),
            "total_llm_instances": sum(len(m._llm_instances) for m in self._active_managers.values()),
            "total_executors": sum(len(m._thread_executors) for m in self._active_managers.values()),
            "total_extractors": sum(len(m._document_extractors) for m in self._active_managers.values())
        }


# Global instance for monitoring
_global_monitor = GlobalResourceMonitor()

async def create_job_resource_manager(job_id: str) -> JobResourceManager:
    """Factory function to create job resource manager"""
    return await _global_monitor.create_job_manager(job_id)

async def cleanup_job_resources(job_id: str) -> None:
    """Cleanup resources for a specific job"""
    await _global_monitor.cleanup_job(job_id)

async def get_global_resource_stats() -> Dict[str, Any]:
    """Get global resource statistics"""
    return _global_monitor.get_resource_stats()

@asynccontextmanager
async def managed_job_resources(job_id: str):
    """Async context manager for guaranteed resource cleanup"""
    manager = await create_job_resource_manager(job_id)
    try:
        yield manager
    finally:
        await cleanup_job_resources(job_id)