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

# --- Shared State for Rate Limiting and Processing ---
_processing_lock = asyncio.Lock() # Lock for accessing shared processing status & slots
_rate_limit_lock = asyncio.Lock() # Separate lock for rate limiting data structures

# Stores status of application IDs currently being processed or recently processed
# Structure: { "application_id": {"status": "processing" | "completed" | "failed", "timestamp": datetime, "job_id": str, "client_fingerprint": str} }
_application_processing_status: Dict[str, Dict[str, Any]] = {}

# Global rate limiter
_global_request_timestamps: Deque[datetime] = deque()

# Per-client rate limiter
_client_request_timestamps: Dict[str, Deque[datetime]] = defaultdict(deque)

# Rapid-fire protection
_client_app_last_request: Dict[str, Dict[str, datetime]] = defaultdict(dict)

# Suspicious activity tracking
_suspicious_clients: Dict[str, Dict[str, Any]] = {}

# Active processing slots management
_active_processing_slots: int = 0


# --- Client Fingerprinting ---
def generate_client_fingerprint(request_headers: Dict[str, str], client_host: Optional[str]) -> str:
    """
    Generate a unique fingerprint for the client based on IP, User-Agent, and other headers.
    """
    ip_address = client_host if client_host else "unknown_host"
    
    # Check for forwarded IP headers
    forwarded_for = request_headers.get("x-forwarded-for", "").split(",")[0].strip()
    real_ip = request_headers.get("x-real-ip", "").strip()
    
    if forwarded_for:
        ip_address = forwarded_for
    elif real_ip:
        ip_address = real_ip
    
    try:
        normalized_ip = str(ipaddress.ip_address(ip_address))
    except ValueError:
        normalized_ip = ip_address # Use as is if not a valid IP format

    user_agent = request_headers.get("user-agent", "unknown_agent")
    accept = request_headers.get("accept", "")
    accept_language = request_headers.get("accept-language", "")
    
    fingerprint_data = f"{normalized_ip}|{user_agent}|{accept}|{accept_language}"
    return hashlib.sha256(fingerprint_data.encode()).hexdigest()[:16]


# --- Rate Limiting Functions ---
async def check_and_update_global_rate_limit() -> Tuple[bool, str]:
    async with _rate_limit_lock:
        now = datetime.now(timezone.utc)
        while _global_request_timestamps and \
              (now - _global_request_timestamps[0]).total_seconds() > GLOBAL_RATE_LIMIT_WINDOW_SECONDS:
            _global_request_timestamps.popleft()
        
        if len(_global_request_timestamps) >= MAX_GLOBAL_REQUESTS_PER_WINDOW:
            return False, f"Global rate limit exceeded. Max {MAX_GLOBAL_REQUESTS_PER_WINDOW} processing requests per {GLOBAL_RATE_LIMIT_WINDOW_SECONDS}s."
        
        _global_request_timestamps.append(now)
        return True, ""

async def check_and_update_client_rate_limit(client_fingerprint: str) -> Tuple[bool, str]:
    async with _rate_limit_lock:
        now = datetime.now(timezone.utc)
        
        if client_fingerprint in _suspicious_clients:
            suspicious_data = _suspicious_clients[client_fingerprint]
            if (now - suspicious_data["blocked_at"]).total_seconds() < SUSPICIOUS_BLOCK_DURATION_SECONDS:
                remaining_block_time = SUSPICIOUS_BLOCK_DURATION_SECONDS - int((now - suspicious_data['blocked_at']).total_seconds())
                return False, f"Client temporarily blocked due to suspicious activity. Try again in {remaining_block_time} seconds."
            else:
                del _suspicious_clients[client_fingerprint] # Unblock
                logger.info(f"Client {client_fingerprint[:8]}... automatically unblocked after suspicious activity period.")

        timestamps = _client_request_timestamps[client_fingerprint]
        while timestamps and (now - timestamps[0]).total_seconds() > CLIENT_RATE_LIMIT_WINDOW_SECONDS:
            timestamps.popleft()
        
        if len(timestamps) >= SUSPICIOUS_THRESHOLD_REQUESTS: # Check before adding current, for slightly stricter check
            _suspicious_clients[client_fingerprint] = {
                "request_count": len(timestamps) + 1, # Include current request in count for blocking
                "blocked_at": now,
                "reason": f"{len(timestamps) + 1} requests in ~{CLIENT_RATE_LIMIT_WINDOW_SECONDS}s window."
            }
            logger.warning(f"Client {client_fingerprint[:8]}... blocked for suspicious activity: {len(timestamps) + 1} requests.")
            return False, "Client blocked due to suspicious activity. Please try again later."

        if len(timestamps) >= MAX_CLIENT_REQUESTS_PER_WINDOW:
            return False, f"Per-client rate limit exceeded. Max {MAX_CLIENT_REQUESTS_PER_WINDOW} requests per {CLIENT_RATE_LIMIT_WINDOW_SECONDS}s."
        
        timestamps.append(now)
        return True, ""

async def check_rapid_fire_protection(client_fingerprint: str, app_id: str) -> Tuple[bool, str]:
    async with _rate_limit_lock:
        now = datetime.now(timezone.utc)
        last_request_time = _client_app_last_request.get(client_fingerprint, {}).get(app_id)
        
        if last_request_time:
            time_diff = (now - last_request_time).total_seconds()
            if time_diff < MIN_SECONDS_BETWEEN_SAME_APP_REQUESTS:
                wait_time = MIN_SECONDS_BETWEEN_SAME_APP_REQUESTS - int(time_diff)
                return False, f"Rapid-fire protection: Please wait {wait_time} more seconds for this application."
        
        if client_fingerprint not in _client_app_last_request:
            _client_app_last_request[client_fingerprint] = {}
        _client_app_last_request[client_fingerprint][app_id] = now
        return True, ""

# --- Processing Slot Management ---
async def check_processing_slots() -> Tuple[bool, str]:
    global _active_processing_slots
    async with _processing_lock: # Use the processing lock for slot management
        if _active_processing_slots >= MAX_CONCURRENT_PROCESSING_SLOTS:
            return False, f"Server at maximum capacity ({_active_processing_slots}/{MAX_CONCURRENT_PROCESSING_SLOTS} jobs active). Try later."
        return True, ""

async def acquire_processing_slot():
    global _active_processing_slots
    async with _processing_lock:
        _active_processing_slots += 1
        logger.info(f"Processing slot acquired. Active slots: {_active_processing_slots}/{MAX_CONCURRENT_PROCESSING_SLOTS}")

async def release_processing_slot():
    global _active_processing_slots
    async with _processing_lock:
        if _active_processing_slots > 0:
            _active_processing_slots -= 1
        else: # Should not happen if logic is correct
            logger.warning("Attempted to release a processing slot when active_processing_slots was already 0.")
        logger.info(f"Processing slot released. Active slots: {_active_processing_slots}/{MAX_CONCURRENT_PROCESSING_SLOTS}")

# --- Processing Status Management ---
async def update_processing_status(app_id: str, status: str, job_id: str, client_fingerprint: str = "", error_message: Optional[str] = None):
    async with _processing_lock:
        now = datetime.now(timezone.utc)
        # Clean up old "completed" or "failed" entries
        keys_to_delete = [
            k for k, v in _application_processing_status.items()
            if v["status"] in ["completed", "failed"] and \
               (now - v["timestamp"]).total_seconds() > RECENTLY_PROCESSED_TTL_SECONDS
        ]
        for k in keys_to_delete:
            if k in _application_processing_status:
                del _application_processing_status[k]
        
        status_entry: Dict[str, Any] = {
            "status": status, "timestamp": now, "job_id": job_id,
            "client_fingerprint": client_fingerprint # Store for reference
        }
        if error_message:
            status_entry["error_message"] = error_message
        _application_processing_status[app_id] = status_entry
        logger.info(f"Status for App ID {app_id} updated to {status} (Job: {job_id}, Client: {client_fingerprint[:8]}...). Error: {error_message or 'None'}")

async def get_processing_status(app_id: str) -> Optional[Dict[str, Any]]:
    async with _processing_lock:
        now = datetime.now(timezone.utc)
        entry = _application_processing_status.get(app_id)
        if entry and entry["status"] == "processing" and \
           (now - entry["timestamp"]).total_seconds() > ACTIVE_PROCESSING_TIMEOUT_SECONDS:
            job_id_stale = entry.get('job_id', 'N/A')
            logger.warning(f"Found stale 'processing' entry for App ID {app_id} (Job ID: {job_id_stale}). Removing & releasing slot.")
            del _application_processing_status[app_id]
            # Attempt to release the slot, assuming it was held by this stale job
            # This relies on the slot counter being accurate.
            # If a job truly died without releasing, this helps correct.
            global _active_processing_slots
            if _active_processing_slots > 0: # Check before decrementing
                _active_processing_slots -=1
                logger.info(f"Slot released due to stale job {job_id_stale}. Active slots: {_active_processing_slots}/{MAX_CONCURRENT_PROCESSING_SLOTS}")
            else:
                logger.warning(f"Stale job {job_id_stale} found, but active slots already 0. Slot count might be inaccurate.")
            return None # Treat as not found
        return entry

# --- Functions for Admin Endpoints to access state (read-only from here) ---
async def get_all_processing_statuses() -> Dict[str, Dict[str, Any]]:
    async with _processing_lock:
        return dict(_application_processing_status) # Return a copy

async def get_active_processing_slots_count() -> int:
    async with _processing_lock:
        return _active_processing_slots

async def get_global_request_timestamps_count() -> int:
    async with _rate_limit_lock:
        return len(_global_request_timestamps)

async def get_suspicious_clients_info() -> Dict[str, Dict[str, Any]]:
    async with _rate_limit_lock:
        return dict(_suspicious_clients) # Return a copy

async def admin_clear_processing_status(application_id: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Clears status and releases slot if it was 'processing'. Returns (cleared_boolean, old_status_dict)."""
    async with _processing_lock:
        if application_id in _application_processing_status:
            old_status = _application_processing_status.pop(application_id)
            if old_status["status"] == "processing":
                global _active_processing_slots
                if _active_processing_slots > 0:
                    _active_processing_slots -= 1
                logger.info(f"Admin cleared 'processing' status for App ID {application_id}, Job {old_status.get('job_id', 'N/A')}. Slot released. Active: {_active_processing_slots}")
            else:
                logger.info(f"Admin cleared status for App ID {application_id} (was {old_status['status']}).")
            return True, old_status
        return False, None

async def admin_unblock_client(client_fingerprint: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Unblocks a client. Returns (unblocked_boolean, old_block_info_dict)."""
    async with _rate_limit_lock:
        if client_fingerprint in _suspicious_clients:
            old_block_info = _suspicious_clients.pop(client_fingerprint)
            logger.info(f"Admin unblocked client {client_fingerprint[:8]}...")
            return True, old_block_info
        return False, None

