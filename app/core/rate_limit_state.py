# project_root/app/core/rate_limit_state.py
import asyncio
from collections import deque, defaultdict
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, Deque
import hashlib
import ipaddress
import logging

from app.config import (
    MAX_GLOBAL_REQUESTS_PER_WINDOW, GLOBAL_RATE_LIMIT_WINDOW_SECONDS,
    MAX_CLIENT_REQUESTS_PER_WINDOW, CLIENT_RATE_LIMIT_WINDOW_SECONDS,
    MIN_SECONDS_BETWEEN_SAME_APP_REQUESTS,
    SUSPICIOUS_THRESHOLD_REQUESTS, SUSPICIOUS_BLOCK_DURATION_SECONDS,
    MAX_CONCURRENT_PROCESSING_SLOTS
)

logger = logging.getLogger(__name__)

# --- Shared State ---
# Job-specific state has been moved to JobManager.
_processing_lock = asyncio.Lock()
_rate_limit_lock = asyncio.Lock()
_processing_semaphore: Optional[asyncio.Semaphore] = None
_global_request_timestamps: Deque[datetime] = deque()
_client_request_timestamps: Dict[str, Deque[datetime]] = defaultdict(deque)
_client_app_last_request: Dict[str, Dict[str, datetime]] = defaultdict(dict)
_suspicious_clients: Dict[str, Dict[str, Any]] = {}
_active_processing_slots: int = 0

# --- Functions ---
def initialize_processing_semaphore():
    """Initializes the global processing semaphore on application startup."""
    global _processing_semaphore
    if _processing_semaphore is None:
        slots = MAX_CONCURRENT_PROCESSING_SLOTS
        _processing_semaphore = asyncio.Semaphore(slots)
        logger.info(f"Processing semaphore initialized with {slots} slots.")

def generate_client_fingerprint(request_headers: Dict[str, str], client_host: Optional[str]) -> str:
    """Creates a unique hash for a client based on IP and User-Agent."""
    ip_address = client_host or "unknown_host"
    forwarded_for = request_headers.get("x-forwarded-for", "").split(",")[0].strip()
    real_ip = request_headers.get("x-real-ip", "").strip()
    if forwarded_for:
        ip_address = forwarded_for
    elif real_ip:
        ip_address = real_ip
    try:
        normalized_ip = str(ipaddress.ip_address(ip_address))
    except ValueError:
        normalized_ip = ip_address
    user_agent = request_headers.get("user-agent", "unknown_agent")
    fingerprint_data = f"{normalized_ip}|{user_agent}"
    return hashlib.sha256(fingerprint_data.encode()).hexdigest()

async def check_and_update_global_rate_limit() -> Tuple[bool, str]:
    """Checks and enforces the server-wide global rate limit."""
    async with _rate_limit_lock:
        now = datetime.now(timezone.utc)
        while _global_request_timestamps and (now - _global_request_timestamps[0]).total_seconds() > GLOBAL_RATE_LIMIT_WINDOW_SECONDS:
            _global_request_timestamps.popleft()
        if len(_global_request_timestamps) >= MAX_GLOBAL_REQUESTS_PER_WINDOW:
            return False, "Global rate limit exceeded. Please try again shortly."
        _global_request_timestamps.append(now)
        return True, ""

async def is_client_blocked(client_fingerprint: str) -> Optional[str]:
    """
    Checks if a client is currently blocked. Automatically unblocks them if the duration has passed.
    """
    async with _rate_limit_lock:
        now = datetime.now(timezone.utc)
        if client_fingerprint in _suspicious_clients:
            suspicious_data = _suspicious_clients[client_fingerprint]
            seconds_since_block = (now - suspicious_data["blocked_at"]).total_seconds()

            if seconds_since_block < SUSPICIOUS_BLOCK_DURATION_SECONDS:
                remaining = SUSPICIOUS_BLOCK_DURATION_SECONDS - int(seconds_since_block)
                return f"Client temporarily blocked due to high request volume. Try again in {remaining} seconds."
            else:
                # Block has expired, remove them from the list.
                del _suspicious_clients[client_fingerprint]
                logger.info(f"Automatic block expired for client {client_fingerprint[:8]}...")
    return None

async def check_and_update_client_rate_limit(client_fingerprint: str) -> Tuple[bool, str]:
    """
    Handles per-client rate limiting and automatically blocks clients who
    exceed the suspicious activity threshold.
    """
    async with _rate_limit_lock:
        now = datetime.now(timezone.utc)
        timestamps = _client_request_timestamps[client_fingerprint]
        
        while timestamps and (now - timestamps[0]).total_seconds() > CLIENT_RATE_LIMIT_WINDOW_SECONDS:
            timestamps.popleft()

        if len(timestamps) >= SUSPICIOUS_THRESHOLD_REQUESTS:
            _suspicious_clients[client_fingerprint] = {
                "blocked_at": now,
                "reason": f"Exceeded {len(timestamps) + 1} requests in window."
            }
            logger.warning(f"Client {client_fingerprint[:8]}... has been automatically blocked for {SUSPICIOUS_BLOCK_DURATION_SECONDS}s.")
            return False, f"Client automatically blocked for {SUSPICIOUS_BLOCK_DURATION_SECONDS} seconds due to excessive requests."

        if len(timestamps) >= MAX_CLIENT_REQUESTS_PER_WINDOW:
            return False, "Per-client rate limit exceeded. Please slow down."

        timestamps.append(now)
        return True, ""

async def check_rapid_fire_protection(client_fingerprint: str, app_id: str) -> Tuple[bool, str]:
    """Prevents a single client from submitting the same application ID repeatedly in a short time."""
    async with _rate_limit_lock:
        now = datetime.now(timezone.utc)
        last_request_time = _client_app_last_request.get(client_fingerprint, {}).get(app_id)
        if last_request_time and (now - last_request_time).total_seconds() < MIN_SECONDS_BETWEEN_SAME_APP_REQUESTS:
            return False, "Rapid-fire protection enabled for this application."
        _client_app_last_request.setdefault(client_fingerprint, {})[app_id] = now
        return True, ""

# --- Processing Slot Management ---
async def acquire_processing_slot():
    """Acquires a slot from the semaphore, blocking if none are available."""
    # FIX: Explicitly declare that this function modifies the global variable.
    global _active_processing_slots
    if _processing_semaphore is None: raise RuntimeError("Semaphore not initialized")
    await _processing_semaphore.acquire()
    async with _processing_lock:
        _active_processing_slots += 1
    logger.info(f"Slot acquired. Active: {_active_processing_slots}/{MAX_CONCURRENT_PROCESSING_SLOTS}")

async def release_processing_slot():
    """Releases a slot back to the semaphore."""
    # FIX: Explicitly declare that this function modifies the global variable.
    global _active_processing_slots
    if _processing_semaphore is None: return
    _processing_semaphore.release()
    async with _processing_lock:
        if _active_processing_slots > 0: 
            _active_processing_slots -= 1
    logger.info(f"Slot released. Active: {_active_processing_slots}/{MAX_CONCURRENT_PROCESSING_SLOTS}")

async def get_active_processing_slots_count() -> int:
    """Returns the current number of active processing slots."""
    async with _processing_lock:
        return _active_processing_slots
