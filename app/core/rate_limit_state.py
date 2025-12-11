# project_root/app/core/rate_limit_state.py
"""
SIMPLIFIED Rate Limiting & Processing Slot Management

Philosophy: Keep it simple - semaphore handles capacity, we just prevent spam.
- Processing slots: Managed by asyncio.Semaphore (automatic blocking when full)
- Duplicate prevention: Handled by job_manager.is_job_active()
- Spam prevention: Simple 1-req/sec throttle per client

Removed: Complex O(log n) rate limiting, LRU cache, suspicious client tracking
"""
import asyncio
import time
import hashlib
import logging
from typing import Dict, Optional

from app.config import MAX_CONCURRENT_PROCESSING_SLOTS, MIN_REQUEST_INTERVAL_SECONDS

logger = logging.getLogger(__name__)

# --- Shared State ---
_processing_lock = asyncio.Lock()
_processing_semaphore: Optional[asyncio.Semaphore] = None
_active_processing_slots: int = 0

# Simple throttle: Track last request time per client
_client_last_request: Dict[str, float] = {}  # client_fp -> last_request_timestamp


# --- Semaphore Management (Keep - Used by job_manager) ---
def initialize_processing_semaphore():
    """Initializes the global processing semaphore on application startup."""
    global _processing_semaphore
    if _processing_semaphore is None:
        slots = MAX_CONCURRENT_PROCESSING_SLOTS
        _processing_semaphore = asyncio.Semaphore(slots)
        logger.info(f"Processing semaphore initialized with {slots} slots.")


async def acquire_processing_slot():
    """Acquires a slot from the semaphore, blocking if none are available."""
    global _active_processing_slots
    if _processing_semaphore is None:
        raise RuntimeError("Semaphore not initialized")

    await _processing_semaphore.acquire()

    async with _processing_lock:
        _active_processing_slots += 1

    logger.info(f"Slot acquired. Active: {_active_processing_slots}/{MAX_CONCURRENT_PROCESSING_SLOTS}")


async def release_processing_slot():
    """Releases a slot back to the semaphore."""
    global _active_processing_slots
    if _processing_semaphore is None:
        return

    _processing_semaphore.release()

    async with _processing_lock:
        if _active_processing_slots > 0:
            _active_processing_slots -= 1

    logger.info(f"Slot released. Active: {_active_processing_slots}/{MAX_CONCURRENT_PROCESSING_SLOTS}")


async def get_active_processing_slots_count() -> int:
    """Returns the current number of active processing slots."""
    async with _processing_lock:
        return _active_processing_slots


# --- Simple Client Fingerprinting (Keep - Useful for throttle) ---
def generate_client_fingerprint(request_headers: Dict[str, str], client_host: Optional[str]) -> str:
    """
    Creates a unique hash for a client based on IP and User-Agent.
    Used for simple throttling to prevent spam.
    """
    ip_address = client_host or "unknown_host"

    # Check for forwarded/real IP headers
    forwarded_for = request_headers.get("x-forwarded-for", "").split(",")[0].strip()
    real_ip = request_headers.get("x-real-ip", "").strip()

    if forwarded_for:
        ip_address = forwarded_for
    elif real_ip:
        ip_address = real_ip

    user_agent = request_headers.get("user-agent", "unknown_agent")

    # Create fingerprint hash
    fingerprint_string = f"{ip_address}|{user_agent}"
    return hashlib.sha256(fingerprint_string.encode()).hexdigest()[:16]


# --- Simple Throttle (New - Replaces Complex Rate Limiting) ---
async def check_simple_throttle(client_fp: str) -> tuple[bool, str]:
    """
    Simple throttle: Ensures minimum time between requests from same client.

    Returns:
        (True, "") if allowed
        (False, error_message) if throttled
    """
    now = time.time()

    if client_fp in _client_last_request:
        time_since_last = now - _client_last_request[client_fp]

        if time_since_last < MIN_REQUEST_INTERVAL_SECONDS:
            wait_time = MIN_REQUEST_INTERVAL_SECONDS - time_since_last
            return False, f"Too many requests. Please wait {wait_time:.1f} seconds."

    # Update last request time
    _client_last_request[client_fp] = now

    # Clean up old entries (simple: keep last 1000 clients)
    if len(_client_last_request) > 1000:
        # Remove oldest half
        sorted_clients = sorted(_client_last_request.items(), key=lambda x: x[1])
        _client_last_request.clear()
        for client, timestamp in sorted_clients[-500:]:
            _client_last_request[client] = timestamp

    return True, ""
