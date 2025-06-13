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
    SUSPICIOUS_THRESHOLD_REQUESTS, SUSPICIOUS_WINDOW_SECONDS, SUSPICIOUS_BLOCK_DURATION_SECONDS,
    MAX_CONCURRENT_PROCESSING_SLOTS, RECENTLY_PROCESSED_TTL_SECONDS, ACTIVE_PROCESSING_TIMEOUT_SECONDS
)

logger = logging.getLogger(__name__)

# --- Shared State ---
_processing_lock = asyncio.Lock()
_rate_limit_lock = asyncio.Lock()
_processing_semaphore: Optional[asyncio.Semaphore] = None
_application_processing_status: Dict[str, Dict[str, Any]] = {}
_global_request_timestamps: Deque[datetime] = deque()
_client_request_timestamps: Dict[str, Deque[datetime]] = defaultdict(deque)
_client_app_last_request: Dict[str, Dict[str, datetime]] = defaultdict(dict)
_suspicious_clients: Dict[str, Dict[str, Any]] = {}
_active_processing_slots: int = 0

def initialize_processing_semaphore():
    global _processing_semaphore
    if _processing_semaphore is None:
        slots = MAX_CONCURRENT_PROCESSING_SLOTS
        _processing_semaphore = asyncio.Semaphore(slots)
        logger.info(f"Processing semaphore initialized with {slots} slots.")

# --- Client Fingerprinting ---
def generate_client_fingerprint(request_headers: Dict[str, str], client_host: Optional[str]) -> str:
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
    return hashlib.sha256(fingerprint_data.encode()).hexdigest()[:16]

# --- Rate Limiting ---
async def check_and_update_global_rate_limit() -> Tuple[bool, str]:
    async with _rate_limit_lock:
        now = datetime.now(timezone.utc)
        while _global_request_timestamps and (now - _global_request_timestamps[0]).total_seconds() > GLOBAL_RATE_LIMIT_WINDOW_SECONDS:
            _global_request_timestamps.popleft()
        if len(_global_request_timestamps) >= MAX_GLOBAL_REQUESTS_PER_WINDOW:
            return False, "Global rate limit exceeded."
        _global_request_timestamps.append(now)
        return True, ""

async def check_and_update_client_rate_limit(client_fingerprint: str) -> Tuple[bool, str]:
    async with _rate_limit_lock:
        now = datetime.now(timezone.utc)
        if client_fingerprint in _suspicious_clients:
            suspicious_data = _suspicious_clients[client_fingerprint]
            if (now - suspicious_data["blocked_at"]).total_seconds() < SUSPICIOUS_BLOCK_DURATION_SECONDS:
                remaining = SUSPICIOUS_BLOCK_DURATION_SECONDS - int((now - suspicious_data['blocked_at']).total_seconds())
                return False, f"Client temporarily blocked. Try again in {remaining} seconds."
            else:
                del _suspicious_clients[client_fingerprint]
        timestamps = _client_request_timestamps[client_fingerprint]
        while timestamps and (now - timestamps[0]).total_seconds() > CLIENT_RATE_LIMIT_WINDOW_SECONDS:
            timestamps.popleft()
        if len(timestamps) >= SUSPICIOUS_THRESHOLD_REQUESTS:
            _suspicious_clients[client_fingerprint] = {"blocked_at": now}
            return False, "Client blocked due to suspicious activity."
        if len(timestamps) >= MAX_CLIENT_REQUESTS_PER_WINDOW:
            return False, "Per-client rate limit exceeded."
        timestamps.append(now)
        return True, ""

async def check_rapid_fire_protection(client_fingerprint: str, app_id: str) -> Tuple[bool, str]:
    async with _rate_limit_lock:
        now = datetime.now(timezone.utc)
        last_request_time = _client_app_last_request.get(client_fingerprint, {}).get(app_id)
        if last_request_time and (now - last_request_time).total_seconds() < MIN_SECONDS_BETWEEN_SAME_APP_REQUESTS:
            return False, "Rapid-fire protection enabled."
        if client_fingerprint not in _client_app_last_request:
            _client_app_last_request[client_fingerprint] = {}
        _client_app_last_request[client_fingerprint][app_id] = now
        return True, ""

# --- Slot Management ---
async def acquire_processing_slot():
    global _active_processing_slots, _processing_semaphore
    if _processing_semaphore is None: raise RuntimeError("Semaphore not initialized")
    await _processing_semaphore.acquire()
    async with _processing_lock:
        _active_processing_slots += 1
    logger.info(f"Slot acquired. Active: {_active_processing_slots}/{MAX_CONCURRENT_PROCESSING_SLOTS}")

async def release_processing_slot():
    global _active_processing_slots, _processing_semaphore
    if _processing_semaphore is None: return
    async with _processing_lock:
        if _active_processing_slots > 0: _active_processing_slots -= 1
    _processing_semaphore.release()
    logger.info(f"Slot released. Active: {_active_processing_slots}/{MAX_CONCURRENT_PROCESSING_SLOTS}")

# --- Status Management ---
async def update_processing_status(app_id: str, status: str, job_id: str, **kwargs):
    async with _processing_lock:
        now = datetime.now(timezone.utc)
        
        # FIX: The key 'timestamp' is renamed to 'last_updated_at' to match the Pydantic model.
        # This resolves the ValidationError in the /queue-overview endpoint.
        _application_processing_status[app_id] = {
            "status": status, 
            "job_id": job_id, 
            "last_updated_at": now,
            "created_at": _application_processing_status.get(app_id, {}).get("created_at", now),
            **kwargs
        }

async def get_processing_status(app_id: str) -> Optional[Dict[str, Any]]:
    async with _processing_lock:
        entry = _application_processing_status.get(app_id)
        if entry and entry["status"] == "processing" and (datetime.now(timezone.utc) - entry["last_updated_at"]).total_seconds() > ACTIVE_PROCESSING_TIMEOUT_SECONDS:
            entry["status"] = "failed"
            entry["error_message"] = "Processing timed out."
        return entry

# --- Admin Functions ---
async def get_all_processing_statuses() -> Dict[str, Dict[str, Any]]:
    async with _processing_lock:
        return dict(_application_processing_status)

async def get_active_processing_slots_count() -> int:
    async with _processing_lock:
        return _active_processing_slots

async def get_suspicious_clients_info() -> Dict[str, Dict[str, Any]]:
    async with _rate_limit_lock:
        return dict(_suspicious_clients)

async def admin_clear_processing_status(application_id: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    async with _processing_lock:
        if application_id in _application_processing_status:
            old_status = _application_processing_status.pop(application_id)
            if old_status.get("status") == "processing":
                logger.warning(f"Admin cleared a job '{application_id}' that was 'processing'. A semaphore slot may be lost if the job was truly stuck and did not handle its exception correctly.")
            return True, old_status
        return False, None

async def admin_unblock_client(client_fingerprint: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    async with _rate_limit_lock:
        if client_fingerprint in _suspicious_clients:
            old_info = _suspicious_clients.pop(client_fingerprint)
            return True, old_info
        return False, None
