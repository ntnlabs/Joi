import base64
import glob
import hashlib
import logging
import os
import queue
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Callable, Dict, List, Optional

# Add api/ and parent dirs to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # api/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # joi/

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from hmac_auth import (
    NonceStore,
    create_request_headers,
    get_shared_secret,
    verify_hmac,
    verify_timestamp,
    DEFAULT_TIMESTAMP_TOLERANCE_MS,
)
from policy_manager import PolicyManager
from hmac_rotator import HMACRotator

from config import (
    load_settings,
    get_prompt_for_conversation,
    get_prompt_for_conversation_optional,
    get_model_for_conversation,
    get_context_for_conversation,
    get_knowledge_scopes_for_conversation,
    ensure_prompts_dir,
    sanitize_scope,
)
from ingestion import run_auto_ingestion, INGESTION_DIR
from llm import OllamaClient
from memory import MemoryConsolidator, MemoryStore

logger = logging.getLogger("joi.api")


# --- Priority Message Queue ---

@dataclass(order=True)
class PrioritizedMessage:
    """Message wrapper for priority queue. Lower priority number = higher priority."""
    priority: int
    timestamp: float = field(compare=False)
    message_id: str = field(compare=False)
    handler: Callable = field(compare=False)
    result: Any = field(default=None, compare=False)
    error: Optional[str] = field(default=None, compare=False)
    done_event: threading.Event = field(default_factory=threading.Event, compare=False)


class MessageQueue:
    """Global message queue with priority support and single worker."""

    PRIORITY_OWNER = 0  # Owner messages processed first
    PRIORITY_NORMAL = 1  # Other allowed senders

    def __init__(self):
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._worker_thread: Optional[threading.Thread] = None
        self._running = False
        self._current_message_id: Optional[str] = None

    def start(self):
        """Start the worker thread."""
        if self._worker_thread is not None:
            return
        self._running = True
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        logger.info("Message queue worker started")

    def stop(self):
        """Stop the worker thread."""
        self._running = False
        # Put a sentinel to unblock the queue
        self._queue.put(PrioritizedMessage(
            priority=999,
            timestamp=time.time(),
            message_id="__stop__",
            handler=lambda: None,
        ))
        if self._worker_thread:
            self._worker_thread.join(timeout=5.0)
            self._worker_thread = None
        logger.info("Message queue worker stopped")

    def enqueue(self, message_id: str, handler: Callable, is_owner: bool = False, timeout: float = 300.0) -> Any:
        """
        Add message to queue and wait for processing.

        Args:
            message_id: Unique message identifier
            handler: Function to call for processing (returns result)
            is_owner: If True, gets priority processing
            timeout: Max seconds to wait for processing

        Returns:
            Result from handler

        Raises:
            TimeoutError: If processing takes too long
            Exception: If handler raises an error
        """
        priority = self.PRIORITY_OWNER if is_owner else self.PRIORITY_NORMAL
        msg = PrioritizedMessage(
            priority=priority,
            timestamp=time.time(),
            message_id=message_id,
            handler=handler,
        )

        queue_size = self._queue.qsize()
        priority_label = "owner" if priority == self.PRIORITY_OWNER else "normal"
        logger.info("Queue ADD: message_id=%s priority=%s queue_size=%d", message_id, priority_label, queue_size)

        self._queue.put(msg)

        # Wait for processing to complete
        if not msg.done_event.wait(timeout=timeout):
            raise TimeoutError(f"Message {message_id} processing timed out after {timeout}s")

        if msg.error:
            raise Exception(msg.error)

        return msg.result

    def _worker_loop(self):
        """Process messages from queue sequentially."""
        while self._running:
            try:
                msg = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if msg.message_id == "__stop__":
                break

            self._current_message_id = msg.message_id
            start_time = time.time()
            priority_label = "owner" if msg.priority == self.PRIORITY_OWNER else "normal"
            logger.info("Queue START: message_id=%s priority=%s", msg.message_id, priority_label)

            try:
                msg.result = msg.handler()
            except Exception as e:
                logger.error("Queue ERROR: message_id=%s error=%s", msg.message_id, e)
                msg.error = str(e)
            finally:
                elapsed = time.time() - start_time
                logger.info("Queue DONE: message_id=%s elapsed=%.2fs", msg.message_id, elapsed)
                self._current_message_id = None
                msg.done_event.set()

    def get_queue_size(self) -> int:
        """Get current queue size."""
        return self._queue.qsize()


# Global message queue instance
message_queue = MessageQueue()


# --- Outbound Rate Limiter ---

class OutboundRateLimiter:
    """
    Rate limiter for outbound messages to mesh.

    Uses sliding window to track messages per hour.
    Critical messages bypass rate limiting.
    """

    DEFAULT_MAX_PER_HOUR = 120  # Configurable via env

    def __init__(self, max_per_hour: Optional[int] = None):
        self._max_per_hour = max_per_hour or int(os.getenv("JOI_OUTBOUND_MAX_PER_HOUR", str(self.DEFAULT_MAX_PER_HOUR)))
        self._timestamps: List[float] = []
        self._lock = threading.Lock()
        self._blocked_count = 0

    def _cleanup_old(self, now: float) -> None:
        """Remove timestamps older than 1 hour."""
        one_hour_ago = now - 3600
        self._timestamps = [ts for ts in self._timestamps if ts > one_hour_ago]

    def check_and_record(self, is_critical: bool = False) -> tuple[bool, str]:
        """
        Check if send is allowed and record it.

        Args:
            is_critical: If True, bypass rate limiting

        Returns:
            (allowed, reason)
        """
        now = time.time()

        with self._lock:
            self._cleanup_old(now)
            current_count = len(self._timestamps)

            # Critical messages always allowed
            if is_critical:
                self._timestamps.append(now)
                return True, "critical_bypass"

            # Check rate limit
            if current_count >= self._max_per_hour:
                self._blocked_count += 1
                logger.warning(
                    "Outbound rate limit: %d/%d per hour (blocked %d total)",
                    current_count, self._max_per_hour, self._blocked_count
                )
                return False, "rate_limited"

            self._timestamps.append(now)
            return True, "allowed"

    def get_stats(self) -> dict:
        """Get rate limiter statistics."""
        now = time.time()
        with self._lock:
            self._cleanup_old(now)
            return {
                "current_hour_count": len(self._timestamps),
                "max_per_hour": self._max_per_hour,
                "blocked_total": self._blocked_count,
            }


# Global outbound rate limiter
outbound_limiter = OutboundRateLimiter()


# --- Background Scheduler ---

class Scheduler:
    """
    Background scheduler for periodic tasks (wind/impulse, reminders, etc.)

    Runs as a daemon thread inside the API process.
    Only runs when the service is up - no external cron needed.
    """

    def __init__(self, interval_seconds: float = 60.0, startup_delay: float = 10.0):
        self._interval = interval_seconds
        self._startup_delay = startup_delay
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False
        self._last_tick: Optional[float] = None
        self._tick_count = 0
        self._error_count = 0

    def start(self):
        """Start the scheduler thread."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._thread.start()
        logger.info("Scheduler started (interval: %.1fs, startup delay: %.1fs)",
                    self._interval, self._startup_delay)

    def stop(self):
        """Stop the scheduler thread gracefully."""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("Scheduler stopped (ticks: %d, errors: %d)",
                    self._tick_count, self._error_count)

    def _scheduler_loop(self):
        """Main scheduler loop."""
        # Wait for startup delay to let service stabilize
        if self._startup_delay > 0:
            logger.debug("Scheduler waiting %.1fs for startup...", self._startup_delay)
            if self._stop_event.wait(self._startup_delay):
                return  # Stop requested during startup delay

        # Push config to mesh on startup
        self._startup_config_push()

        logger.info("Scheduler active, first tick in %.1fs", self._interval)

        while self._running:
            # Wait for interval (or stop signal)
            if self._stop_event.wait(self._interval):
                break  # Stop requested

            if not self._running:
                break

            # Execute tick with error isolation
            try:
                self._tick()
                self._tick_count += 1
                self._last_tick = time.time()
            except Exception as e:
                self._error_count += 1
                logger.error("Scheduler tick error (%d total): %s", self._error_count, e)
                # Continue running - don't let errors kill the scheduler

    def _tick(self):
        """
        Single scheduler tick - check for pending work.

        TODO: Implement actual logic:
        - Check for due reminders/tasks
        - Calculate impulse scores for wind
        - Send proactive messages if thresholds met
        """
        logger.debug("Scheduler tick #%d", self._tick_count + 1)

        # Auto-ingestion check every tick (cheap if no files)
        self._check_ingestion()

        # Config sync check every 10 ticks (~10 min with 60s interval)
        if self._tick_count % 10 == 0:
            self._check_config_sync()

        # Tamper detection every tick (SHA256 is cheap)
        self._check_tamper()

        # Low-priority maintenance tasks every 60 ticks (~1 hour with 60s interval)
        if self._tick_count % 60 == 0:
            self._cleanup_nonces()

        # Weekly HMAC rotation check once per day (1440 ticks with 60s interval)
        if self._tick_count % 1440 == 0 and self._tick_count > 0:
            self._check_hmac_rotation()

        # Refresh membership cache (only runs if business mode + dm_group_knowledge)
        # Check every 15 ticks (~15 min with 60s interval) but actual refresh is controlled by cache
        if self._tick_count % 15 == 0:
            self._refresh_membership()

        # Placeholder for future implementation:
        # - self._check_reminders()
        # - self._check_wind_impulse()

    def _check_tamper(self):
        """Check for config file tampering. Shuts down service if detected."""
        try:
            changed = _check_fingerprints()
            if changed:
                logger.critical("SECURITY: %d config file(s) tampered - SHUTTING DOWN", len(changed))
                for path in changed:
                    logger.critical("SECURITY: Tampered file: %s", path)
                # Give logs time to flush
                time.sleep(1)
                os._exit(78)  # EX_CONFIG - configuration error
        except Exception as e:
            logger.warning("Scheduler: tamper check failed: %s", e)

    def _cleanup_nonces(self):
        """Cleanup expired nonces from the replay protection store."""
        if nonce_store:
            try:
                deleted = nonce_store.cleanup_expired()
                if deleted > 0:
                    logger.info("Scheduler: cleaned up %d expired nonces", deleted)
            except Exception as e:
                logger.warning("Scheduler: nonce cleanup failed: %s", e)

    def _check_ingestion(self):
        """Check for pending knowledge files to ingest."""
        try:
            run_auto_ingestion(memory)
        except Exception as e:
            logger.warning("Scheduler: auto-ingestion failed: %s", e)

    def _check_config_sync(self):
        """Check if config needs to be pushed to mesh."""
        if not config_push_client:
            return
        try:
            # First check: did local config change?
            if config_push_client.needs_push():
                logger.info("Scheduler: local config changed, pushing to mesh")
                success, result = config_push_client.push_config()
                if success:
                    logger.info("Scheduler: config push successful, hash=%s", result[:16])
                else:
                    logger.warning("Scheduler: config push failed: %s", result)
                return  # Done for this tick

            # Second check: does mesh have what we expect?
            in_sync, reason = config_push_client.check_mesh_sync()
            if not in_sync:
                if reason == "mesh_unreachable":
                    logger.debug("Scheduler: mesh unreachable, will retry next tick")
                elif reason == "mesh_empty":
                    logger.info("Scheduler: mesh has no config (restart?), pushing...")
                    success, result = config_push_client.push_config(force=True)
                    if success:
                        logger.info("Scheduler: config push successful, hash=%s", result[:16])
                    else:
                        logger.warning("Scheduler: config push failed: %s", result)
                elif reason == "mesh_drift":
                    logger.warning("Scheduler: mesh config drift detected, pushing fresh config...")
                    success, result = config_push_client.push_config(force=True)
                    if success:
                        logger.info("Scheduler: config push successful, hash=%s", result[:16])
                    else:
                        logger.warning("Scheduler: config push failed: %s", result)
        except Exception as e:
            logger.warning("Scheduler: config sync check failed: %s", e)

    def _check_hmac_rotation(self):
        """Check if HMAC rotation is due (weekly)."""
        if not hmac_rotator:
            return
        try:
            if hmac_rotator.should_rotate():
                logger.info("Scheduler: HMAC rotation due, rotating...")
                success, result = hmac_rotator.rotate(use_grace_period=True)
                if success:
                    logger.info("Scheduler: HMAC rotation successful")
                else:
                    logger.warning("Scheduler: HMAC rotation failed: %s", result)
        except Exception as e:
            logger.warning("Scheduler: HMAC rotation check failed: %s", e)

    def _refresh_membership(self):
        """Refresh group membership cache (only if feature is active)."""
        try:
            # membership_cache.refresh() internally checks if it should be active
            if membership_cache.refresh():
                logger.debug("Scheduler: membership cache refreshed")
        except Exception as e:
            logger.warning("Scheduler: membership refresh failed: %s", e)

    def _startup_config_push(self):
        """Push config to mesh on startup to ensure sync."""
        if not config_push_client:
            return
        try:
            logger.info("Startup: pushing config to mesh...")
            success, result = config_push_client.push_config(force=True)
            if success:
                logger.info("Startup: config push successful, hash=%s", result[:16])
            else:
                logger.warning("Startup: config push failed: %s", result)
        except Exception as e:
            logger.warning("Startup: config push failed: %s", e)

    def get_status(self) -> dict:
        """Get scheduler status for health endpoint."""
        return {
            "running": self._running,
            "interval_seconds": self._interval,
            "tick_count": self._tick_count,
            "error_count": self._error_count,
            "last_tick": self._last_tick,
        }


# Scheduler settings
SCHEDULER_ENABLED = os.getenv("JOI_SCHEDULER_ENABLED", "1") == "1"
SCHEDULER_INTERVAL = float(os.getenv("JOI_SCHEDULER_INTERVAL", "60"))
SCHEDULER_STARTUP_DELAY = float(os.getenv("JOI_SCHEDULER_STARTUP_DELAY", "10"))

# Global scheduler instance (created only if enabled)
scheduler: Optional[Scheduler] = None
if SCHEDULER_ENABLED:
    scheduler = Scheduler(
        interval_seconds=SCHEDULER_INTERVAL,
        startup_delay=SCHEDULER_STARTUP_DELAY,
    )


settings = load_settings()

app = FastAPI(title="joi-api", version="0.1.0")

# HMAC authentication settings
HMAC_SECRET = get_shared_secret()  # Initial secret from env (fallback)
HMAC_ENABLED = HMAC_SECRET is not None
HMAC_TIMESTAMP_TOLERANCE_MS = int(os.getenv("JOI_HMAC_TIMESTAMP_TOLERANCE_MS", str(DEFAULT_TIMESTAMP_TOLERANCE_MS)))


def _get_current_hmac_secret() -> Optional[bytes]:
    """
    Get current HMAC secret for signing outbound requests.

    Uses rotator's live secret if available, otherwise falls back to startup secret.
    """
    if hmac_rotator:
        return hmac_rotator.get_current_secret()
    return HMAC_SECRET


def _get_valid_hmac_secrets() -> list[bytes]:
    """
    Get list of valid HMAC secrets for verification.

    During rotation, both current and old keys are valid.
    """
    if hmac_rotator:
        return hmac_rotator.get_valid_secrets()
    if HMAC_SECRET:
        return [HMAC_SECRET]
    return []

# Initialize Ollama client
LLM_TIMEOUT = float(os.getenv("JOI_LLM_TIMEOUT", "180"))
llm = OllamaClient(
    base_url=settings.ollama_url,
    model=settings.ollama_model,
    timeout=LLM_TIMEOUT,
    num_ctx=settings.ollama_num_ctx,
)

# Initialize memory store
memory = MemoryStore(
    db_path=os.getenv("JOI_MEMORY_DB", "/var/lib/joi/memory.db"),
    encryption_key=os.getenv("JOI_MEMORY_KEY"),
)

# Initialize nonce store for replay protection (separate unencrypted DB - nonces are ephemeral)
nonce_store: Optional[NonceStore] = None
if HMAC_ENABLED:
    nonce_db_path = os.getenv("JOI_NONCE_DB", "/var/lib/joi/nonces.db")
    nonce_store = NonceStore(nonce_db_path)

# Number of recent messages to include in context
CONTEXT_MESSAGE_COUNT = int(os.getenv("JOI_CONTEXT_MESSAGES", "50"))

# Memory consolidation settings (count-based compaction)
# When message_count > CONTEXT_MESSAGE_COUNT, compact oldest COMPACT_BATCH_SIZE messages
COMPACT_BATCH_SIZE = int(os.getenv("JOI_COMPACT_BATCH_SIZE", "20"))
CONSOLIDATION_ARCHIVE = os.getenv("JOI_CONSOLIDATION_ARCHIVE", "0") == "1"  # Default: delete
CONSOLIDATION_MODEL = os.getenv("JOI_CONSOLIDATION_MODEL")  # Optional: separate model for compaction

# Validate compaction constraints: 10 <= batch_size < context_size // 2
def _validate_compaction_settings():
    if COMPACT_BATCH_SIZE < 10:
        raise ValueError(f"JOI_COMPACT_BATCH_SIZE must be >= 10, got {COMPACT_BATCH_SIZE}")
    if CONTEXT_MESSAGE_COUNT < 22:
        raise ValueError(f"JOI_CONTEXT_MESSAGES must be >= 22 to allow compaction, got {CONTEXT_MESSAGE_COUNT}")
    max_batch = CONTEXT_MESSAGE_COUNT // 2
    if COMPACT_BATCH_SIZE >= max_batch:
        raise ValueError(
            f"JOI_COMPACT_BATCH_SIZE ({COMPACT_BATCH_SIZE}) must be < context_size // 2 ({max_batch})"
        )

_validate_compaction_settings()

# RAG settings
RAG_ENABLED = os.getenv("JOI_RAG_ENABLED", "1") == "1"  # Default: enabled
RAG_MAX_TOKENS = int(os.getenv("JOI_RAG_MAX_TOKENS", "500"))  # Max tokens for RAG context

# Time awareness - inject current datetime into system prompt
TIME_AWARENESS_ENABLED = os.getenv("JOI_TIME_AWARENESS", "0") == "1"  # Default: disabled
TIME_AWARENESS_TIMEZONE = os.getenv("JOI_TIMEZONE", "Europe/Bratislava")  # User timezone

# Response cooldown - minimum seconds between sends to same conversation
RESPONSE_COOLDOWN_DM_SECONDS = float(os.getenv("JOI_RESPONSE_COOLDOWN_SECONDS", "5.0"))
RESPONSE_COOLDOWN_GROUP_SECONDS = float(os.getenv("JOI_RESPONSE_COOLDOWN_GROUP_SECONDS", "2.0"))
_last_send_times: Dict[str, float] = {}  # conversation_id -> timestamp
_send_lock = threading.Lock()

# Initialize memory consolidator
consolidator = MemoryConsolidator(
    memory=memory,
    llm_client=llm,
    consolidation_model=CONSOLIDATION_MODEL,
)

# Initialize policy manager for mesh config sync
policy_manager = PolicyManager()


# --- Group Membership Cache ---

class GroupMembershipCache:
    """Cache of group memberships from signal-cli.

    Only active when business mode + dm_group_knowledge enabled.
    Queries mesh /groups/members endpoint to get real group membership.
    """

    def __init__(self):
        self._cache: Dict[str, List[str]] = {}  # group_id -> [member_ids]
        self._last_refresh: float = 0
        self._lock = threading.Lock()
        # Configurable via env (default 15 min)
        self._refresh_minutes = int(os.getenv("JOI_MEMBERSHIP_REFRESH_MINUTES", "15"))

    def _should_be_active(self) -> bool:
        """Only run when the attack vector exists (business mode + dm_group_knowledge)."""
        return (policy_manager.is_business_mode() and
                policy_manager.is_dm_group_knowledge_enabled())

    def refresh(self) -> bool:
        """Fetch fresh membership from mesh (only if active)."""
        if not self._should_be_active():
            return False  # Skip - not needed

        try:
            url = f"{settings.mesh_url}/groups/members"
            headers = {"Content-Type": "application/json"}
            current_secret = _get_current_hmac_secret()
            if current_secret:
                hmac_headers = create_request_headers(b"", current_secret)
                headers.update(hmac_headers)

            with httpx.Client(timeout=10.0) as client:
                resp = client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json().get("data", {})

            with self._lock:
                self._cache = data
                self._last_refresh = time.time()
            logger.debug("Refreshed group membership: %d groups", len(data))
            return True
        except Exception as e:
            logger.warning("Failed to refresh group membership: %s", e)
            return False

    def get_user_groups(self, user_id: str) -> List[str]:
        """Get list of groups where user is a member."""
        if not self._should_be_active():
            return []  # Feature disabled

        # Auto-refresh if stale
        refresh_seconds = self._refresh_minutes * 60
        with self._lock:
            time_since_refresh = time.time() - self._last_refresh
            has_cache = len(self._cache) > 0

        if time_since_refresh > refresh_seconds:
            if not self.refresh():
                # Refresh failed - use stale cache if available, else fail closed
                if has_cache:
                    logger.warning("Using stale membership cache (refresh failed)")
                else:
                    logger.warning("No membership cache and refresh failed - denying group access")
                    return []

        with self._lock:
            return [gid for gid, members in self._cache.items() if user_id in members]

    def get_cache_age_seconds(self) -> float:
        """Get age of cache in seconds."""
        with self._lock:
            if self._last_refresh == 0:
                return -1  # Never refreshed
            return time.time() - self._last_refresh

    def get_cache_size(self) -> int:
        """Get number of groups in cache."""
        with self._lock:
            return len(self._cache)


# Global membership cache instance
membership_cache = GroupMembershipCache()


class ConfigPushClient:
    """
    Pushes config to mesh and tracks sync state.

    Joi is authoritative for mesh policy. This client pushes updates
    to mesh whenever policy changes, on startup, and periodically.
    """

    def __init__(
        self,
        mesh_url: str,
        policy: PolicyManager,
    ):
        self._mesh_url = mesh_url
        self._policy = policy
        self._last_push_hash: Optional[str] = None
        self._last_push_time: Optional[float] = None
        self._lock = threading.Lock()

    def push_config(self, force: bool = False) -> tuple[bool, str]:
        """
        Push current config to mesh.

        Args:
            force: Push even if hash unchanged

        Returns:
            (success, mesh_reported_hash or error message)
        """
        import json

        with self._lock:
            current_hash = self._policy.get_config_hash()

            # Skip if unchanged (unless forced)
            if not force and self._last_push_hash == current_hash:
                logger.debug("Config unchanged, skipping push")
                return True, current_hash

            config = self._policy.get_config_for_push()
            body = json.dumps(config).encode("utf-8")

            headers = {"Content-Type": "application/json"}
            # Get current secret dynamically (supports rotation)
            current_secret = _get_current_hmac_secret()
            if current_secret:
                hmac_headers = create_request_headers(body, current_secret)
                headers.update(hmac_headers)

            url = f"{self._mesh_url}/config/sync"

            try:
                with httpx.Client(timeout=10.0) as client:
                    resp = client.post(url, content=body, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()

                if data.get("status") == "ok":
                    mesh_hash = data.get("data", {}).get("config_hash", "")

                    # Verify hash match
                    if mesh_hash and mesh_hash != current_hash:
                        logger.warning(
                            "Config hash mismatch: local=%s mesh=%s",
                            current_hash[:16], mesh_hash[:16],
                        )

                    self._last_push_hash = mesh_hash
                    self._last_push_time = time.time()
                    logger.info(
                        "Config push successful, hash=%s",
                        mesh_hash[:16] if mesh_hash else "none",
                    )
                    return True, mesh_hash

                error = data.get("error", "unknown")
                logger.error("Mesh returned error on config push: %s", error)
                return False, error

            except httpx.HTTPStatusError as exc:
                logger.error("Config push HTTP error: %s", exc)
                return False, f"http_error_{exc.response.status_code}"
            except Exception as exc:
                logger.error("Config push failed: %s", exc)
                return False, str(exc)

    def verify_sync(self, mesh_hash: str) -> bool:
        """Verify mesh config hash matches local."""
        return mesh_hash == self._policy.get_config_hash()

    def needs_push(self) -> bool:
        """Check if config has changed since last push."""
        return self._last_push_hash != self._policy.get_config_hash()

    def get_mesh_status(self) -> tuple[bool, Optional[str]]:
        """
        Poll mesh for its current config hash.

        Returns:
            (success, mesh_hash or None)
        """
        url = f"{self._mesh_url}/config/status"
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(url)
                resp.raise_for_status()
                data = resp.json()

            if data.get("status") == "ok":
                mesh_hash = data.get("data", {}).get("config_hash")
                return True, mesh_hash
            return False, None

        except Exception as exc:
            logger.debug("Failed to get mesh status: %s", exc)
            return False, None

    def check_mesh_sync(self) -> tuple[bool, str]:
        """
        Check if mesh has the expected config.

        Returns:
            (in_sync, reason)
            - (True, "in_sync") - mesh has expected hash
            - (False, "mesh_empty") - mesh has no config
            - (False, "mesh_drift") - mesh has different hash
            - (False, "mesh_unreachable") - couldn't contact mesh
        """
        success, mesh_hash = self.get_mesh_status()

        if not success:
            return False, "mesh_unreachable"

        if not mesh_hash:
            return False, "mesh_empty"

        if mesh_hash != self._last_push_hash:
            logger.info(
                "Mesh config drift: expected=%s actual=%s",
                self._last_push_hash[:16] if self._last_push_hash else "none",
                mesh_hash[:16] if mesh_hash else "none",
            )
            return False, "mesh_drift"

        return True, "in_sync"

    def get_status(self) -> dict:
        """Get sync status info."""
        with self._lock:
            local_hash = self._policy.get_config_hash()
            return {
                "local_hash": local_hash,
                "last_push_hash": self._last_push_hash,
                "last_push_time": self._last_push_time,
                "in_sync": self._last_push_hash == local_hash,
            }


# Initialize config push client (None if HMAC not enabled)
config_push_client: Optional[ConfigPushClient] = None
hmac_rotator: Optional[HMACRotator] = None
if HMAC_ENABLED:
    # Note: hmac_rotator must be initialized first so _get_current_hmac_secret() works
    hmac_rotator = HMACRotator(
        mesh_url=settings.mesh_url,
        policy_manager=policy_manager,
    )
    config_push_client = ConfigPushClient(
        mesh_url=settings.mesh_url,
        policy=policy_manager,
    )

# Ensure prompts directory exists
ensure_prompts_dir()

# Default names that Joi responds to in group messages (comma-separated)
# Default name for @mention detection in groups (can be overridden per-group via mesh)
JOI_NAME_DEFAULT = ["Joi"]


def _build_address_regex(names: list) -> re.Pattern:
    """Build regex pattern for addressing detection from list of names.

    Only matches explicit @Name mentions (Signal group mention style).
    """
    patterns = []
    for name in names:
        escaped = re.escape(name)
        patterns.extend([
            rf"^@{escaped}(?:\s|$|[,:.!?])",   # "@Name" at start
            rf"\s@{escaped}(?:\s|$|[,:.!?])",  # "@Name" in middle/end
        ])
    return re.compile("|".join(patterns), re.IGNORECASE)


# Cache for compiled regexes per name list
_address_regex_cache: Dict[tuple, re.Pattern] = {}


# Patterns for "remember this" requests (English only for now)
# Must be explicit fact statements about the user, not general statements
REMEMBER_PATTERNS = [
    r"remember\s+that\s+(?:i|my)\s+(.+)",  # "remember that I..." or "remember that my..."
    r"don'?t\s+forget\s+that\s+(?:i|my)\s+(.+)",  # "don't forget that I/my..."
    r"keep\s+in\s+mind\s+that\s+(?:i|my)\s+(.+)",  # "keep in mind that I/my..."
    r"^my\s+name\s+is\s+(\w+)",  # "my name is X" at start
    r"^i'?m\s+called\s+(\w+)",  # "I'm called X" at start
    r"^my\s+(\w+)\s+is\s+(.+)",  # "my birthday is March 5th" at start
    r"^i\s+(?:really\s+)?(?:like|love|hate|prefer)\s+(.+)",  # "I like X" at start
]
REMEMBER_REGEX = re.compile("|".join(REMEMBER_PATTERNS), re.IGNORECASE)


def _detect_remember_request(text: str) -> Optional[str]:
    """Check if user is asking Joi to remember something. Returns the thing to remember."""
    match = REMEMBER_REGEX.search(text)
    if match:
        # Return the first non-None group
        for group in match.groups():
            if group:
                return group.strip()
    return None


def _extract_and_save_fact(text: str, remember_what: str, conversation_id: str = "") -> Optional[str]:
    """Use LLM to extract a structured fact and save it. Returns confirmation message."""
    prompt = f"""The user said: "{text}"
They want me to remember: "{remember_what}"

Extract this as a fact with these exact fields:
- category: one of "personal", "preference", "relationship", "work", "routine", "interest"
- key: short identifier (2-3 words max)
- value: the fact itself

Return ONLY valid JSON, no explanation:
{{"category": "...", "key": "...", "value": "..."}}

JSON:"""

    try:
        response = llm.generate(prompt=prompt)
        if response.error:
            logger.warning("LLM error extracting fact: %s", response.error)
            return None

        # Parse JSON
        import json
        text_resp = response.text.strip()
        # Try to find JSON object
        start = text_resp.find("{")
        end = text_resp.rfind("}") + 1
        if start >= 0 and end > start:
            fact = json.loads(text_resp[start:end])
            if all(k in fact for k in ["category", "key", "value"]):
                memory.store_fact(
                    category=fact["category"],
                    key=fact["key"],
                    value=str(fact["value"]),
                    confidence=0.95,  # High confidence - user explicitly stated
                    source="stated",
                    conversation_id=conversation_id,
                )
                logger.info("Saved stated fact for %s: %s.%s = %s", conversation_id or "global", fact["category"], fact["key"], fact["value"])
                return fact["value"]
    except Exception as e:
        logger.warning("Failed to extract/save fact: %s", e)

    return None


def _is_addressing_joi(text: str, names: Optional[List[str]] = None) -> bool:
    """Check if the message is addressing Joi directly via @mention."""
    if names is None:
        names = JOI_NAME_DEFAULT

    # Use cached regex if available
    names_key = tuple(sorted(names))
    if names_key not in _address_regex_cache:
        _address_regex_cache[names_key] = _build_address_regex(names)

    return bool(_address_regex_cache[names_key].search(text))


# --- Request/Response Models (per api-contracts.md) ---

class InboundSender(BaseModel):
    id: str
    transport_id: str
    display_name: Optional[str] = None


class InboundConversation(BaseModel):
    type: str  # "direct" or "group"
    id: str


class InboundContent(BaseModel):
    type: str  # "text", "reaction", etc.
    text: Optional[str] = None
    reaction: Optional[str] = None
    transport_native: Optional[Dict[str, Any]] = None


class InboundMessage(BaseModel):
    transport: str
    message_id: str
    sender: InboundSender
    conversation: InboundConversation
    priority: str = "normal"
    content: InboundContent
    timestamp: int
    quote: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    store_only: bool = False  # If True, store for context but don't respond
    group_names: Optional[List[str]] = None  # Names Joi responds to in this group
    bot_mentioned: bool = False  # True if bot was @mentioned via Signal mention


class InboundResponse(BaseModel):
    status: str
    message_id: Optional[str] = None
    error: Optional[str] = None


class DocumentIngestRequest(BaseModel):
    filename: str
    content_base64: str
    content_type: str
    scope: str
    sender_id: str


class DocumentIngestResponse(BaseModel):
    status: str
    filename: Optional[str] = None  # Original filename
    stored_filename: Optional[str] = None  # Actual stored name (with timestamp)
    scope: Optional[str] = None
    error: Optional[str] = None


# --- HMAC Middleware ---

@app.middleware("http")
async def hmac_verification_middleware(request: Request, call_next):
    """Verify HMAC authentication for mesh → joi requests."""
    # Skip HMAC for health endpoint (monitoring)
    if request.url.path == "/health":
        return await call_next(request)

    # Admin endpoints: read-only status = IP check only, sensitive actions = require HMAC
    if request.url.path.startswith("/admin/"):
        # Sensitive admin actions require HMAC (even from local)
        sensitive_admin_paths = [
            "/admin/hmac/rotate",
            "/admin/security/kill-switch",
            "/admin/security/privacy-mode",
            "/admin/config/push",
        ]
        if request.url.path not in sensitive_admin_paths:
            # Read-only status endpoints - IP check is sufficient
            return await call_next(request)
        # Sensitive endpoints fall through to HMAC verification below

    # Fail-closed: reject if HMAC not configured
    if not HMAC_ENABLED:
        logger.error("HMAC auth failed: HMAC not configured (fail-closed)")
        return JSONResponse(
            status_code=503,
            content={"status": "error", "error": {"code": "hmac_not_configured", "message": "HMAC authentication not configured"}}
        )

    # Extract headers
    nonce = request.headers.get("X-Nonce")
    timestamp_str = request.headers.get("X-Timestamp")
    signature = request.headers.get("X-HMAC-SHA256")

    # Check all required headers present
    if not all([nonce, timestamp_str, signature]):
        logger.warning("HMAC auth failed: missing headers")
        return JSONResponse(
            status_code=401,
            content={"status": "error", "error": {"code": "hmac_missing_headers", "message": "Missing authentication headers"}}
        )

    # Parse timestamp
    try:
        timestamp = int(timestamp_str)
    except ValueError:
        logger.warning("HMAC auth failed: invalid timestamp format")
        return JSONResponse(
            status_code=401,
            content={"status": "error", "error": {"code": "hmac_invalid_timestamp", "message": "Invalid timestamp format"}}
        )

    # Verify timestamp freshness
    ts_valid, ts_error = verify_timestamp(timestamp, HMAC_TIMESTAMP_TOLERANCE_MS)
    if not ts_valid:
        logger.warning("HMAC auth failed: %s", ts_error)
        return JSONResponse(
            status_code=401,
            content={"status": "error", "error": {"code": ts_error, "message": "Request timestamp out of tolerance"}}
        )

    # Verify nonce not replayed
    nonce_valid, nonce_error = nonce_store.check_and_store(nonce, source="mesh")
    if not nonce_valid:
        logger.warning("HMAC auth failed: %s nonce=%s", nonce_error, nonce[:8])
        return JSONResponse(
            status_code=401,
            content={"status": "error", "error": {"code": nonce_error, "message": "Nonce already used"}}
        )

    # Read body for HMAC verification
    body = await request.body()

    # Verify HMAC signature - try all valid secrets (supports grace period)
    valid_secrets = _get_valid_hmac_secrets()
    signature_valid = False
    for secret in valid_secrets:
        if verify_hmac(nonce, timestamp, body, signature, secret):
            signature_valid = True
            break

    if not signature_valid:
        logger.warning("HMAC auth failed: invalid signature")
        return JSONResponse(
            status_code=401,
            content={"status": "error", "error": {"code": "hmac_invalid_signature", "message": "Invalid HMAC signature"}}
        )

    logger.debug("HMAC auth passed for %s", request.url.path)
    return await call_next(request)


# --- Runtime Fingerprint (Tamper Detection) ---

_startup_fingerprints: Dict[str, str] = {}


def _redact_filename_pii(filename: str) -> str:
    """Redact phone numbers in filenames for privacy mode.

    Examples:
        +421905867511.txt -> +***7511.txt
        +421905867511.model -> +***7511.model
    """
    import re
    # Match phone number pattern at start of filename (with or without extension)
    pattern = r'^\+\d+(?=\.|$)'
    def redact_match(m):
        phone = m.group(0)
        if len(phone) > 5:
            return f"+***{phone[-4:]}"
        return "+***"
    return re.sub(pattern, redact_match, filename)


def _compute_file_hash(path: str) -> str:
    """Compute SHA256 hash of a file."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return "missing"


def _get_config_files() -> List[str]:
    """Get list of config files to monitor."""
    files = []

    # System config
    for path in ["/etc/default/joi-api", "/etc/joi/memory.key", "/etc/joi/hmac.key"]:
        if os.path.exists(path):
            files.append(path)

    # Policy file
    policy_file = os.getenv("JOI_POLICY_FILE", "/var/lib/joi/policy/mesh-policy.json")
    if os.path.exists(policy_file):
        files.append(policy_file)

    # Prompts directory
    prompts_dir = os.getenv("JOI_PROMPTS_DIR", "/var/lib/joi/prompts")
    for pattern in ["*.txt", "*.model", "*.context", "users/*", "groups/*"]:
        for path in glob.glob(os.path.join(prompts_dir, pattern)):
            if os.path.isfile(path):
                files.append(path)

    return files


def _compute_fingerprints() -> Dict[str, str]:
    """Compute fingerprints for all config files."""
    return {path: _compute_file_hash(path) for path in _get_config_files()}


def _init_fingerprints():
    """Initialize fingerprints at startup."""
    global _startup_fingerprints
    _startup_fingerprints = _compute_fingerprints()
    if _startup_fingerprints:
        # Check privacy mode for log redaction
        privacy_mode = False
        try:
            privacy_mode = policy_manager.is_privacy_mode()
        except Exception:
            pass  # policy_manager may not be initialized yet

        def format_entry(path, hash):
            name = os.path.basename(path)
            if privacy_mode:
                name = _redact_filename_pii(name)
            return f"{name}:{hash}"

        summary = " | ".join(format_entry(p, h) for p, h in sorted(_startup_fingerprints.items()))
        logger.info("Runtime fingerprint initialized: %s", summary)


def _check_fingerprints() -> List[str]:
    """Check for tampered files. Returns list of changed file paths."""
    if not _startup_fingerprints:
        return []

    # Check privacy mode for log redaction
    privacy_mode = False
    try:
        privacy_mode = policy_manager.is_privacy_mode()
    except Exception:
        pass

    def display_path(path):
        name = os.path.basename(path)
        return _redact_filename_pii(name) if privacy_mode else name

    current = _compute_fingerprints()
    changed = []

    # Check for modified or deleted files
    for path, original_hash in _startup_fingerprints.items():
        current_hash = current.get(path, "deleted")
        if current_hash != original_hash:
            changed.append(path)
            logger.warning("TAMPER DETECTED: %s changed (%s -> %s)", display_path(path), original_hash, current_hash)

    # Check for new files
    for path in current:
        if path not in _startup_fingerprints:
            changed.append(path)
            logger.warning("TAMPER DETECTED: new file %s", display_path(path))

    return changed


# --- Lifecycle Events ---

@app.on_event("startup")
def startup_event():
    """Start the message queue worker and scheduler on app startup."""
    message_queue.start()
    if scheduler:
        scheduler.start()
    hmac_status = "HMAC enabled" if HMAC_ENABLED else "HMAC DISABLED - set JOI_HMAC_SECRET"
    scheduler_status = f"scheduler enabled (interval: {SCHEDULER_INTERVAL}s)" if scheduler else "scheduler disabled"
    time_status = f"time awareness enabled (tz: {TIME_AWARENESS_TIMEZONE})" if TIME_AWARENESS_ENABLED else "time awareness disabled"
    logger.info("Joi API started: %s, %s, %s", hmac_status, scheduler_status, time_status)
    _init_fingerprints()


@app.on_event("shutdown")
def shutdown_event():
    """Stop the message queue worker and scheduler on app shutdown."""
    if scheduler:
        scheduler.stop()
    message_queue.stop()
    logger.info("Joi API shutting down")


# --- Endpoints ---

@app.get("/health")
def health():
    msg_count = memory.get_message_count()
    facts = memory.get_facts(min_confidence=0.0, limit=1000)
    summaries = memory.get_recent_summaries(days=30, limit=100)
    knowledge_sources = memory.get_knowledge_sources()
    knowledge_chunks = sum(s["chunk_count"] for s in knowledge_sources)
    config_sync = config_push_client.get_status() if config_push_client else {"enabled": False}
    return {
        "status": "ok",
        "model": settings.ollama_model,
        "num_ctx": settings.ollama_num_ctx if settings.ollama_num_ctx > 0 else "default",
        "mode": {
            "type": policy_manager.get_mode(),
            "dm_group_knowledge": policy_manager.is_dm_group_knowledge_enabled(),
            "membership_checks_active": membership_cache._should_be_active(),
            "membership_cache_groups": membership_cache.get_cache_size(),
            "membership_cache_age_seconds": membership_cache.get_cache_age_seconds(),
            "membership_refresh_minutes": membership_cache._refresh_minutes,
        },
        "memory": {
            "messages": msg_count,
            "facts": len(facts),
            "summaries": len(summaries),
            "context_size": CONTEXT_MESSAGE_COUNT,
        },
        "rag": {
            "enabled": RAG_ENABLED,
            "sources": len(knowledge_sources),
            "chunks": knowledge_chunks,
        },
        "queue": {
            "inbound": message_queue.get_queue_size(),
        },
        "outbound": outbound_limiter.get_stats(),
        "scheduler": scheduler.get_status() if scheduler else {"running": False},
        "config_sync": config_sync,
    }


# --- Admin Endpoints ---

def _is_local_request(request: Request) -> bool:
    """Check if request is from localhost or Nebula network (10.x.x.x)."""
    client_ip = request.client.host if request.client else ""
    return client_ip in ("127.0.0.1", "::1") or client_ip.startswith("10.")


@app.get("/admin/config/status")
def admin_config_status(request: Request):
    """Get config sync status."""
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    if not config_push_client:
        return {"status": "error", "error": "config_push_not_enabled"}

    return {
        "status": "ok",
        "data": config_push_client.get_status(),
    }


@app.post("/admin/config/push")
def admin_config_push(request: Request):
    """Force push current config to mesh."""
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    if not config_push_client:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": "config_push_not_enabled"},
        )

    success, result = config_push_client.push_config(force=True)
    if success:
        return {"status": "ok", "data": {"mesh_config_hash": result}}

    return JSONResponse(
        status_code=500,
        content={"status": "error", "error": result},
    )


@app.post("/admin/hmac/rotate")
def admin_hmac_rotate(request: Request):
    """
    Manually trigger HMAC key rotation.

    Query params:
        grace: Set to "false" for immediate rotation (no grace period). Default: true.
    """
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    if not hmac_rotator:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": "hmac_rotation_not_enabled"},
        )

    # Check for grace period option
    use_grace = request.query_params.get("grace", "true").lower() != "false"

    success, result = hmac_rotator.rotate(use_grace_period=use_grace)
    if success:
        return {
            "status": "ok",
            "message": "HMAC rotation complete",
            "grace_period": use_grace,
        }

    return JSONResponse(
        status_code=500,
        content={"status": "error", "error": result},
    )


@app.get("/admin/hmac/status")
def admin_hmac_status(request: Request):
    """Get HMAC rotation status."""
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    if not hmac_rotator:
        return {"status": "error", "error": "hmac_rotation_not_enabled"}

    last_rotation = hmac_rotator.get_last_rotation_time()
    return {
        "status": "ok",
        "data": {
            "last_rotation_time": last_rotation,
            "last_rotation_ago_hours": (time.time() - last_rotation) / 3600 if last_rotation else None,
            "rotation_due": hmac_rotator.should_rotate(),
        },
    }


@app.get("/admin/security/status")
def admin_security_status(request: Request):
    """Get security settings status."""
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    return {
        "status": "ok",
        "data": policy_manager.get_security(),
    }


@app.post("/admin/security/privacy-mode")
def admin_set_privacy_mode(request: Request):
    """
    Enable or disable privacy mode.

    Query params:
        enabled: "true" or "false"
    """
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    enabled = request.query_params.get("enabled", "").lower() == "true"
    policy_manager.set_privacy_mode(enabled)

    # Push to mesh
    if config_push_client:
        success, result = config_push_client.push_config(force=True)
        if not success:
            logger.warning("Failed to push privacy mode change to mesh: %s", result)

    return {
        "status": "ok",
        "privacy_mode": enabled,
    }


@app.post("/admin/security/kill-switch")
def admin_set_kill_switch(request: Request):
    """
    Activate or deactivate kill switch.

    When active, mesh will not forward messages to Joi.
    Use in emergencies to immediately stop message processing.

    Query params:
        active: "true" or "false"
    """
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    active = request.query_params.get("active", "").lower() == "true"
    policy_manager.set_kill_switch(active)

    # Push to mesh immediately
    if config_push_client:
        success, result = config_push_client.push_config(force=True)
        if success:
            logger.info("Kill switch %s pushed to mesh", "activated" if active else "deactivated")
        else:
            logger.error("CRITICAL: Failed to push kill switch to mesh: %s", result)
            return JSONResponse(
                status_code=500,
                content={"status": "error", "error": f"push_failed: {result}", "kill_switch": active},
            )

    return {
        "status": "ok",
        "kill_switch": active,
    }


@app.get("/admin/rag/scopes")
def admin_rag_scopes(request: Request):
    """List all RAG scopes and their chunk counts (debug endpoint)."""
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    sources = memory.get_knowledge_sources()
    # Group by scope
    scopes = {}
    for s in sources:
        scope = s["scope"]
        if scope not in scopes:
            scopes[scope] = {"sources": 0, "chunks": 0}
        scopes[scope]["sources"] += 1
        scopes[scope]["chunks"] += s["chunk_count"]

    return {
        "status": "ok",
        "scopes": scopes,
        "total_sources": len(sources),
        "total_chunks": sum(s["chunks"] for s in scopes.values()),
    }


@app.get("/admin/rag/search")
def admin_rag_search(request: Request, q: str, scope: Optional[str] = None):
    """Test RAG search with optional scope filter (debug endpoint)."""
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    scopes = [scope] if scope else None
    chunks = memory.search_knowledge(q, limit=10, scopes=scopes)

    return {
        "status": "ok",
        "query": q,
        "scope_filter": scope,
        "results": [
            {
                "source": c.source,
                "title": c.title,
                "scope": c.scope,
                "content_preview": c.content[:200] if c.content else "",
            }
            for c in chunks
        ],
    }


# --- Document Ingestion Endpoint ---

@app.post("/api/v1/document/ingest", response_model=DocumentIngestResponse)
def ingest_document(req: DocumentIngestRequest):
    """
    Receive a document from mesh for RAG ingestion.

    Documents are saved to the ingestion input directory for processing
    by the scheduler tick. The scope determines which conversations
    can access the knowledge.
    """
    logger.info(
        "Received document: filename=%s content_type=%s scope=%s sender=%s",
        req.filename,
        req.content_type,
        req.scope,
        req.sender_id,
    )

    # Validate filename (basic security check)
    if "/" in req.filename or "\\" in req.filename or ".." in req.filename:
        logger.warning("Invalid filename rejected: %s", req.filename)
        return DocumentIngestResponse(
            status="error",
            error="invalid_filename",
        )

    # Sanitize scope for use as directory name (consistent with RAG lookup)
    safe_scope = sanitize_scope(req.scope)
    if not safe_scope or safe_scope.startswith(".") or ".." in safe_scope:
        logger.warning("Invalid scope rejected: %s", req.scope)
        return DocumentIngestResponse(
            status="error",
            error="invalid_scope",
        )

    # Decode base64 content
    try:
        content = base64.b64decode(req.content_base64)
    except Exception as e:
        logger.warning("Failed to decode base64 content: %s", e)
        return DocumentIngestResponse(
            status="error",
            error="invalid_base64",
        )

    # Create scope directory if needed
    scope_dir = INGESTION_DIR / "input" / safe_scope
    try:
        scope_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error("Failed to create scope directory %s: %s", scope_dir, e)
        return DocumentIngestResponse(
            status="error",
            error="directory_error",
        )

    # Save file with UUID prefix for uniqueness (preserves all uploads)
    # Format: {uuid8}_{original_filename}
    stored_filename = f"{uuid.uuid4().hex[:8]}_{req.filename}"
    filepath = scope_dir / stored_filename

    # Atomic write: temp file with UUID, then rename
    temp_path = scope_dir / f".{uuid.uuid4().hex[:8]}.tmp"
    try:
        temp_path.write_bytes(content)
        temp_path.rename(filepath)  # Atomic on POSIX
        logger.info("Saved document to %s (%d bytes)", filepath, len(content))
    except Exception as e:
        logger.error("Failed to save document %s: %s", filepath, e)
        return DocumentIngestResponse(
            status="error",
            error="save_failed",
        )

    return DocumentIngestResponse(
        status="ok",
        filename=req.filename,
        stored_filename=stored_filename,
        scope=safe_scope,
    )


@app.post("/api/v1/message/inbound", response_model=InboundResponse)
def receive_message(msg: InboundMessage):
    """
    Receive a message from mesh proxy, process with LLM, send response back.

    Messages requiring LLM are queued and processed sequentially.
    Owner messages get priority in the queue.

    For group messages:
    - store_only=True: Store for context but don't respond (non-allowed senders)
    - store_only=False: Check if Joi is addressed before responding
    """
    # Check if sender is owner (id="owner" is set by mesh for allowed senders)
    is_owner = msg.sender.id == "owner"

    logger.info(
        "Received message_id=%s from=%s type=%s convo=%s store_only=%s",
        msg.message_id,
        msg.sender.transport_id,
        msg.content.type,
        msg.conversation.type,
        msg.store_only,
    )

    # Handle reactions - store and respond briefly
    if msg.content.type == "reaction":
        emoji = msg.content.reaction or "?"
        logger.info("Received reaction %s from %s", emoji, msg.sender.transport_id)

        reaction_text = f"[reacted with {emoji}]"
        memory.store_message(
            message_id=msg.message_id,
            direction="inbound",
            content_type="reaction",
            content_text=reaction_text,
            timestamp=msg.timestamp,
            conversation_id=msg.conversation.id,
            sender_id=msg.sender.transport_id,
            sender_name=msg.sender.display_name,
        )

        # Generate brief reaction response (skip for store_only)
        if not msg.store_only:
            response_text = _generate_reaction_response(emoji, msg.conversation.id)
            if response_text:
                _send_to_mesh(
                    recipient_id=msg.sender.id,
                    recipient_transport_id=msg.sender.transport_id,
                    conversation=msg.conversation,
                    text=response_text,
                    reply_to=None,
                )
                # Store outbound
                memory.store_message(
                    message_id=str(uuid.uuid4()),
                    direction="outbound",
                    content_type="text",
                    content_text=response_text,
                    timestamp=int(time.time() * 1000),
                    conversation_id=msg.conversation.id,
                )

        return InboundResponse(status="ok", message_id=msg.message_id)

    # Only handle text messages beyond this point
    if msg.content.type != "text" or not msg.content.text:
        logger.info("Skipping unsupported message type=%s", msg.content.type)
        return InboundResponse(status="ok", message_id=msg.message_id)

    user_text = msg.content.text.strip()
    if not user_text:
        return InboundResponse(status="ok", message_id=msg.message_id)

    # Store inbound message (always store for context)
    memory.store_message(
        message_id=msg.message_id,
        direction="inbound",
        content_type=msg.content.type,
        content_text=user_text,
        timestamp=msg.timestamp,
        conversation_id=msg.conversation.id,
        reply_to_id=msg.quote.get("message_id") if msg.quote else None,
        sender_id=msg.sender.transport_id,
        sender_name=msg.sender.display_name,
    )

    # All facts (explicit and inferred) use conversation_id as key
    # - DMs: phone number (per-user scope)
    # - Groups: group_id (group scope, facts include person names)
    fact_key = msg.conversation.id

    # Check for "remember this" requests (only from allowed senders)
    saved_fact = None
    if not msg.store_only:
        remember_what = _detect_remember_request(user_text)
        if remember_what:
            logger.info("Detected remember request: %s", remember_what[:50])
            saved_fact = _extract_and_save_fact(user_text, remember_what, conversation_id=fact_key)

    # Determine if we should respond
    should_respond = True

    if msg.store_only:
        # Non-allowed sender in group - store only, no response
        logger.info("Message stored for context only (store_only=True)")
        should_respond = False
    elif msg.conversation.type == "group":
        # Group message from allowed sender - only respond if Joi is addressed
        # Check Signal @mention (bot_mentioned) or text-based @name
        if msg.bot_mentioned:
            logger.info("Joi @mentioned in group message (Signal mention), will respond")
            should_respond = True
        else:
            # Fallback: check text for @name pattern
            names_to_check = msg.group_names if msg.group_names else None
            if _is_addressing_joi(user_text, names=names_to_check):
                logger.info("Joi addressed in group message (text pattern), will respond")
                should_respond = True
            else:
                logger.info("Joi not addressed in group message, storing only")
                should_respond = False

    if not should_respond:
        return InboundResponse(status="ok", message_id=msg.message_id)

    # --- Queue the LLM processing ---
    # This ensures messages are processed sequentially with owner priority

    def process_with_llm() -> InboundResponse:
        """Process message with LLM - runs in queue worker thread."""
        # Get per-conversation context size (or use global default)
        custom_context = get_context_for_conversation(
            conversation_type=msg.conversation.type,
            conversation_id=msg.conversation.id,
            sender_id=msg.sender.transport_id,
        )
        context_size = custom_context if custom_context is not None else CONTEXT_MESSAGE_COUNT

        # Build conversation context from recent messages
        recent_messages = memory.get_recent_messages(
            limit=context_size,
            conversation_id=msg.conversation.id,
        )

        # Convert to LLM chat format (with sender prefix for groups)
        is_group_chat = msg.conversation.type == "group"
        chat_messages = _build_chat_messages(recent_messages, is_group=is_group_chat)

        # Get per-conversation model (if any)
        custom_model = get_model_for_conversation(
            conversation_type=msg.conversation.type,
            conversation_id=msg.conversation.id,
            sender_id=msg.sender.transport_id,
        )

        # Get knowledge scopes for RAG access control
        knowledge_scopes = get_knowledge_scopes_for_conversation(
            conversation_type=msg.conversation.type,
            conversation_id=msg.conversation.id,
            sender_id=msg.sender.transport_id,
            is_business_mode=policy_manager.is_business_mode(),
            dm_group_knowledge_enabled=policy_manager.is_dm_group_knowledge_enabled(),
            get_user_groups=membership_cache.get_user_groups,
        )

        # Get system prompt based on whether custom model is used
        if custom_model:
            # Custom model: prompt is optional (Modelfile has baked-in SYSTEM)
            base_prompt = get_prompt_for_conversation_optional(
                conversation_type=msg.conversation.type,
                conversation_id=msg.conversation.id,
                sender_id=msg.sender.transport_id,
            )
            if base_prompt:
                enriched_prompt = _build_enriched_prompt(base_prompt, user_text, conversation_id=fact_key, knowledge_scopes=knowledge_scopes)
            else:
                # No prompt file - only add facts/summaries/RAG if available
                enriched_prompt = _build_enriched_prompt("", user_text, conversation_id=fact_key, knowledge_scopes=knowledge_scopes)
                enriched_prompt = enriched_prompt.strip() or None  # None if empty
        else:
            # No custom model: use prompt with fallback to default
            base_prompt = get_prompt_for_conversation(
                conversation_type=msg.conversation.type,
                conversation_id=msg.conversation.id,
                sender_id=msg.sender.transport_id,
            )
            enriched_prompt = _build_enriched_prompt(base_prompt, user_text, conversation_id=fact_key, knowledge_scopes=knowledge_scopes)

        # Add hint if we just saved a fact
        if saved_fact and enriched_prompt:
            enriched_prompt += f"\n\n[You just saved this to memory: \"{saved_fact}\". Briefly acknowledge you'll remember it.]"
        elif saved_fact:
            enriched_prompt = f"[You just saved this to memory: \"{saved_fact}\". Briefly acknowledge you'll remember it.]"

        # Generate response from LLM with conversation context
        model_info = f"model={custom_model}" if custom_model else "model=default"
        context_info = f"context={context_size}" if custom_context else f"context={context_size}(default)"
        logger.info("Generating LLM response with %d messages (%s, %s)", len(chat_messages), model_info, context_info)
        llm_response = llm.chat(messages=chat_messages, system=enriched_prompt, model=custom_model)

        if llm_response.error:
            logger.error("LLM error: %s", llm_response.error)
            return InboundResponse(
                status="error",
                message_id=msg.message_id,
                error=f"llm_error: {llm_response.error}",
            )

        response_text = llm_response.text.strip()
        if not response_text:
            logger.warning("LLM returned empty response")
            response_text = "I'm not sure how to respond to that."

        logger.info("LLM response: %s", response_text[:50])

        # Send response back via mesh
        outbound_message_id = str(uuid.uuid4())
        send_result = _send_to_mesh(
            recipient_id=msg.sender.id,
            recipient_transport_id=msg.sender.transport_id,
            conversation=msg.conversation,
            text=response_text,
            reply_to=msg.message_id,
        )

        if not send_result:
            logger.error("Failed to send response to mesh")
            return InboundResponse(
                status="error",
                message_id=msg.message_id,
                error="mesh_send_failed",
            )

        # Store outbound message
        memory.store_message(
            message_id=outbound_message_id,
            direction="outbound",
            content_type="text",
            content_text=response_text,
            timestamp=int(time.time() * 1000),
            conversation_id=msg.conversation.id,
            reply_to_id=msg.message_id,
        )

        # Check if memory consolidation needed
        _maybe_run_consolidation()

        return InboundResponse(status="ok", message_id=msg.message_id)

    # Enqueue and wait for processing (owner gets priority)
    try:
        result = message_queue.enqueue(
            message_id=msg.message_id,
            handler=process_with_llm,
            is_owner=is_owner,
            timeout=LLM_TIMEOUT + 30,  # LLM timeout + buffer
        )
        return result
    except TimeoutError:
        logger.error("Message %s timed out in queue", msg.message_id)
        return InboundResponse(
            status="error",
            message_id=msg.message_id,
            error="queue_timeout",
        )
    except Exception as e:
        logger.error("Message %s queue error: %s", msg.message_id, e)
        return InboundResponse(
            status="error",
            message_id=msg.message_id,
            error=str(e),
        )


def _generate_reaction_response(emoji: str, conversation_id: str) -> Optional[str]:
    """Generate a brief response to a reaction based on context."""
    # Get recent context to understand what was reacted to
    recent = memory.get_recent_messages(limit=5, conversation_id=conversation_id)
    if not recent:
        return None

    # Find the last Joi message that was likely reacted to
    last_joi_msg = None
    for msg in reversed(recent):
        if msg.direction == "outbound" and msg.content_text:
            last_joi_msg = msg.content_text[:100]
            break

    if not last_joi_msg:
        return None

    # Generate brief contextual response
    prompt = f"""The user reacted to your message with {emoji}.
Your message was: "{last_joi_msg}"

Respond very briefly (1-5 words) acknowledging the reaction in a natural way.
Just the response, no explanation."""

    response = llm.generate(prompt=prompt)
    if response.error or not response.text:
        return None

    text = response.text.strip()
    # Keep it short
    if len(text) > 50:
        return None
    return text


def _build_chat_messages(messages: List, is_group: bool = False) -> List[Dict[str, str]]:
    """Convert stored messages to LLM chat format.

    For group conversations, includes sender name prefix so Joi knows who said what.
    """
    chat_messages = []
    for msg in messages:
        if msg.content_text:
            role = "user" if msg.direction == "inbound" else "assistant"

            if role == "user" and is_group:
                # For group messages, prefix with sender name/id
                sender = msg.sender_name or msg.sender_id or "Unknown"
                content = f"[{sender}]: {msg.content_text}"
            else:
                content = msg.content_text

            chat_messages.append({"role": role, "content": content})
    return chat_messages


def _build_enriched_prompt(
    base_prompt: str,
    user_message: Optional[str] = None,
    conversation_id: Optional[str] = None,
    knowledge_scopes: Optional[List[str]] = None,
) -> str:
    """Build system prompt enriched with user facts, summaries, and RAG context for this conversation."""
    parts = [base_prompt]

    # Add user facts for this conversation
    facts_text = memory.get_facts_as_text(min_confidence=0.6, conversation_id=conversation_id)
    if facts_text:
        parts.append("\n\n" + facts_text)

    # Add recent conversation summaries for this conversation
    summaries_text = memory.get_summaries_as_text(days=7, conversation_id=conversation_id)
    if summaries_text:
        parts.append("\n\n" + summaries_text)

    # Add RAG context if enabled and user message provided
    if RAG_ENABLED and user_message:
        logger.debug("RAG lookup: query=%s scopes=%s", user_message[:50], knowledge_scopes)
        rag_context = memory.get_knowledge_as_context(
            user_message,
            max_tokens=RAG_MAX_TOKENS,
            scopes=knowledge_scopes,
        )
        if rag_context:
            parts.append("\n\n" + rag_context)
            logger.debug("Added RAG context for query: %s (scopes: %s)", user_message[:50], knowledge_scopes)
        else:
            logger.debug("No RAG results for query: %s (scopes: %s)", user_message[:50], knowledge_scopes)

    # Add current datetime if time awareness is enabled
    if TIME_AWARENESS_ENABLED:
        try:
            tz = ZoneInfo(TIME_AWARENESS_TIMEZONE)
        except Exception:
            tz = ZoneInfo("UTC")
        now = datetime.now(tz)
        parts.append(
            f"\n\nSYSTEM CONTEXT:\n"
            f"Current datetime: {now.isoformat()}\n"
            f"User timezone: {TIME_AWARENESS_TIMEZONE}"
        )

    return "".join(parts)


def _maybe_run_consolidation() -> None:
    """Run memory consolidation if context window exceeded."""
    try:
        result = consolidator.run_consolidation(
            context_messages=CONTEXT_MESSAGE_COUNT,
            compact_batch_size=COMPACT_BATCH_SIZE,
            archive_instead_of_delete=CONSOLIDATION_ARCHIVE,
        )
        if result["ran"]:
            action = "archived" if CONSOLIDATION_ARCHIVE else "deleted"
            logger.info(
                "Memory compaction: facts=%d, summarized=%d, %s=%d",
                result["facts_extracted"],
                result["messages_summarized"],
                action,
                result["messages_removed"],
            )
    except Exception as e:
        logger.error("Consolidation error: %s", e)


def _send_to_mesh(
    recipient_id: str,
    recipient_transport_id: str,
    conversation: InboundConversation,
    text: str,
    reply_to: Optional[str] = None,
    is_critical: bool = False,
) -> bool:
    """Send a message back to mesh for delivery via Signal."""
    import json

    # Check outbound rate limit (critical messages bypass)
    allowed, reason = outbound_limiter.check_and_record(is_critical=is_critical)
    if not allowed:
        logger.warning("Outbound blocked by rate limit: %s", reason)
        return False

    # Enforce cooldown between sends to same conversation
    convo_id = conversation.id
    cooldown = RESPONSE_COOLDOWN_GROUP_SECONDS if conversation.type == "group" else RESPONSE_COOLDOWN_DM_SECONDS
    now = time.time()
    with _send_lock:
        last_send = _last_send_times.get(convo_id, 0)
        elapsed = now - last_send
        if elapsed < cooldown:
            wait_time = cooldown - elapsed
            logger.debug("Cooldown: waiting %.1fs before sending to %s", wait_time, convo_id)
            time.sleep(wait_time)
        _last_send_times[convo_id] = time.time()

    url = f"{settings.mesh_url}/api/v1/message/outbound"

    payload = {
        "transport": "signal",
        "recipient": {
            "id": recipient_id,
            "transport_id": recipient_transport_id,
        },
        "priority": "normal",
        "delivery": {
            "target": conversation.type,
            "group_id": conversation.id if conversation.type == "group" else None,
        },
        "content": {
            "type": "text",
            "text": text,
        },
        "reply_to": reply_to,
        "escalated": False,
        "voice_response": False,
    }

    try:
        # Serialize to bytes for HMAC
        body = json.dumps(payload).encode("utf-8")

        # Build headers with HMAC if configured
        headers = {"Content-Type": "application/json"}
        current_secret = _get_current_hmac_secret()
        if current_secret:
            hmac_headers = create_request_headers(body, current_secret)
            headers.update(hmac_headers)

        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, content=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        if data.get("status") == "ok":
            logger.info("Sent response to mesh successfully")
            return True
        else:
            logger.error("Mesh returned error: %s", data.get("error"))
            return False

    except Exception as exc:
        logger.error("Failed to send to mesh: %s", exc)
        return False


# --- Main ---

def main():
    import uvicorn

    from config.prompts import PROMPTS_DIR

    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logger.info("Starting Joi API on %s:%d", settings.bind_host, settings.bind_port)
    ctx_info = f", num_ctx: {settings.ollama_num_ctx}" if settings.ollama_num_ctx > 0 else ""
    logger.info("Ollama: %s (model: %s%s)", settings.ollama_url, settings.ollama_model, ctx_info)
    logger.info("Mesh: %s", settings.mesh_url)
    logger.info("Memory: %s (context: %d messages)",
                os.getenv("JOI_MEMORY_DB", "/var/lib/joi/memory.db"),
                CONTEXT_MESSAGE_COUNT)
    logger.info("Prompts directory: %s", PROMPTS_DIR)
    logger.info("Response cooldown: DM=%.1fs, group=%.1fs", RESPONSE_COOLDOWN_DM_SECONDS, RESPONSE_COOLDOWN_GROUP_SECONDS)

    uvicorn.run(
        app,
        host=settings.bind_host,
        port=settings.bind_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
