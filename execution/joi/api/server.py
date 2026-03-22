import base64
import glob
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple

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
from message_queue import MessageQueue, OutboundRateLimiter
from scheduler import Scheduler
from group_cache import GroupMembershipCache
import admin_routes

from config import (
    load_settings,
    get_prompt_for_conversation,
    get_prompt_for_conversation_optional,
    get_prompt_source,
    get_model_source,
    get_model_for_conversation,
    get_context_for_conversation,
    get_knowledge_scopes_for_conversation,
    get_consolidation_model_for_conversation,
    ensure_prompts_dir,
    sanitize_scope,
)
from config.logging_config import configure_logging

# Configure structured logging early
configure_logging()
from ingestion import run_auto_ingestion, INGESTION_DIR
from llm import OllamaClient
from memory import MemoryConsolidator, MemoryStore

# Import Wind module (path already set above)
from wind import WindOrchestrator, WindConfig

# Import Reminder subsystem
from reminders import ReminderManager

logger = logging.getLogger("joi.api")


# --- Input/Output Sanitization (Prompt Injection Defense) ---

# Max input length (defense in depth - mesh enforces 1500 at transport)
MAX_INPUT_LENGTH = int(os.getenv("JOI_MAX_INPUT_LENGTH", "1500"))
# Max document size pushed by mesh (operators should match MESH_MAX_DOCUMENT_SIZE)
JOI_MAX_DOCUMENT_SIZE = int(os.getenv("JOI_MAX_DOCUMENT_SIZE", str(1 * 1024 * 1024)))  # 1 MB default
# Max output length (Signal supports up to ~6000 chars, but long messages can be annoying)
MAX_OUTPUT_LENGTH = int(os.getenv("JOI_MAX_OUTPUT_LENGTH", "2000"))
# Signal formatting - convert **bold** to Unicode bold (Signal doesn't support markdown)
SIGNAL_FORMAT_ENABLED = os.getenv("JOI_SIGNAL_FORMAT_ENABLED", "0") == "1"

# Markers that should never appear in LLM output (system prompt leakage)
OUTPUT_LEAK_MARKERS = [
    "CRITICAL INSTRUCTIONS",
    "NEVER OVERRIDE",
    "=== YOUR PERSONALITY ===",
    "=== CONTEXT FORMAT ===",
    "<system>",
    "</system>",
    "<|system|>",
    "<|assistant|>",
]


def sanitize_input(text: str) -> str:
    """
    Sanitize user input before processing.

    Removes control characters (keeps newlines and valid UTF-8 like Slovak ľščťž).
    Normalizes Unicode to prevent homoglyph attacks.
    """
    if not text:
        return ""

    # Length limit
    if len(text) > MAX_INPUT_LENGTH:
        text = text[:MAX_INPUT_LENGTH]

    # Remove null bytes and ASCII control chars (0x00-0x1F except newline/tab, and 0x7F)
    # This preserves all UTF-8 characters (Slovak, Cyrillic, CJK, emoji, etc.)
    cleaned = []
    for c in text:
        code = ord(c)
        # Keep: tab (9), newline (10), carriage return (13), and printable (32+)
        # Remove: other control chars (0-8, 11-12, 14-31, 127)
        if code == 9 or code == 10 or code == 13 or code >= 32:
            if code != 127:  # DEL character
                cleaned.append(c)
    text = ''.join(cleaned)

    # Unicode normalization (NFKC) - prevents homoglyph attacks
    # e.g., "ⅰgnore" (Roman numeral ⅰ) becomes "ignore"
    text = unicodedata.normalize('NFKC', text)

    return text


def validate_output(response: str) -> Tuple[bool, str]:
    """
    Validate LLM output before sending to user.

    Checks for leaked system prompt markers.
    Returns (is_valid, sanitized_response_or_fallback).
    """
    if not response:
        return True, ""

    # Length limit
    if len(response) > MAX_OUTPUT_LENGTH:
        original_len = len(response)
        response = response[:MAX_OUTPUT_LENGTH]
        logger.info("Output truncated", extra={
            "original_length": original_len,
            "truncated_length": MAX_OUTPUT_LENGTH
        })

    # Check for leaked system prompt markers
    response_lower = response.lower()
    for marker in OUTPUT_LEAK_MARKERS:
        if marker.lower() in response_lower:
            logger.warning("Output validation failed: leaked marker", extra={
                "marker": marker,
                "action": "output_blocked"
            })
            return False, "I had trouble formulating a response. Could you rephrase that?"

    return True, response


def format_for_signal(text: str) -> str:
    """
    Convert Markdown formatting to Signal-compatible formatting.

    Signal doesn't support markdown, so we:
    - Convert **bold** to Unicode Mathematical Sans-Serif Bold
    - Convert [text](url) to just url (Signal auto-links URLs)

    Example: **hello** → 𝗵𝗲𝗹𝗹𝗼
    Example: [click here](https://example.com) → https://example.com

    Known limitations (simple regex-based approach):
    - Nested formatting not supported (e.g., **bold *and italic***)
    - Escaped asterisks not handled (\\*\\*not bold\\*\\*)
    - Multi-line bold text may not convert correctly
    - Only bold (**) is converted, not italic (*) or other markdown
    - Non-ASCII characters inside bold are passed through unchanged
    """
    if not SIGNAL_FORMAT_ENABLED:
        return text

    # Convert markdown links [text](url) to just url
    # Signal auto-detects and makes URLs clickable
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\2', text)

    # Unicode Mathematical Sans-Serif Bold mappings
    # Uppercase: A-Z → U+1D5D4 to U+1D5ED
    # Lowercase: a-z → U+1D5EE to U+1D607
    # Digits: 0-9 → U+1D7EC to U+1D7F5
    def to_bold(match: re.Match) -> str:
        content = match.group(1)
        result = []
        for char in content:
            if 'A' <= char <= 'Z':
                result.append(chr(0x1D5D4 + ord(char) - ord('A')))
            elif 'a' <= char <= 'z':
                result.append(chr(0x1D5EE + ord(char) - ord('a')))
            elif '0' <= char <= '9':
                result.append(chr(0x1D7EC + ord(char) - ord('0')))
            else:
                result.append(char)
        return ''.join(result)

    # Replace **text** with Unicode bold
    text = re.sub(r'\*\*(.+?)\*\*', to_bold, text)

    return text


# Global message queue and rate limiter instances (from queue.py)
message_queue = MessageQueue()
outbound_limiter = OutboundRateLimiter()


# Scheduler settings (scheduler instance created after dependencies are ready)
SCHEDULER_ENABLED = os.getenv("JOI_SCHEDULER_ENABLED", "1") == "1"
SCHEDULER_INTERVAL = float(os.getenv("JOI_SCHEDULER_INTERVAL", "60"))
SCHEDULER_STARTUP_DELAY = float(os.getenv("JOI_SCHEDULER_STARTUP_DELAY", "10"))
scheduler: Optional[Scheduler] = None


settings = load_settings()

app = FastAPI(title="joi-api", version="0.1.0")

# Include admin routes (dependencies set up below after globals are defined)
app.include_router(admin_routes.router)

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
LLM_KEEP_ALIVE = os.getenv("JOI_LLM_KEEP_ALIVE", "30m")
llm = OllamaClient(
    base_url=settings.ollama_url,
    model=settings.ollama_model,
    timeout=LLM_TIMEOUT,
    num_ctx=settings.ollama_num_ctx,
    keep_alive=LLM_KEEP_ALIVE,
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
RAG_MAX_TOKENS = int(os.getenv("JOI_RAG_MAX_TOKENS", "1500"))  # Max tokens for RAG context

# FTS search settings for facts and summaries
FACTS_FTS_ENABLED = os.getenv("JOI_FACTS_FTS_ENABLED", "1") == "1"  # Default: enabled
FACTS_FTS_MAX_TOKENS = int(os.getenv("JOI_FACTS_FTS_MAX_TOKENS", "400"))
SUMMARIES_FTS_ENABLED = os.getenv("JOI_SUMMARIES_FTS_ENABLED", "1") == "1"  # Default: enabled
SUMMARIES_FTS_MAX_TOKENS = int(os.getenv("JOI_SUMMARIES_FTS_MAX_TOKENS", "1500"))

# Brain debug - write full LLM call payload to YAML files
BRAIN_DEBUG = os.getenv("JOI_BRAIN_DEBUG", "0") == "1"
BRAIN_DEBUG_DIR = os.getenv("JOI_BRAIN_DEBUG_DIR", "/var/lib/joi/llm_debug")

# Time awareness - inject current datetime into system prompt
TIME_AWARENESS_ENABLED = os.getenv("JOI_TIME_AWARENESS", "0") == "1"  # Default: disabled
TIME_AWARENESS_TIMEZONE = os.getenv("JOI_TIMEZONE", "Europe/Bratislava")  # User timezone

# Response cooldown - minimum seconds between sends to same conversation
RESPONSE_COOLDOWN_DM_SECONDS = float(os.getenv("JOI_RESPONSE_COOLDOWN_SECONDS", "5.0"))
RESPONSE_COOLDOWN_GROUP_SECONDS = float(os.getenv("JOI_RESPONSE_COOLDOWN_GROUP_SECONDS", "2.0"))
_last_send_times: Dict[str, float] = {}  # conversation_id -> timestamp
_send_locks: Dict[str, threading.Lock] = {}  # per-conversation locks
_send_locks_lock = threading.Lock()  # protects _send_locks dict creation
_SEND_CACHE_MAX_SIZE = 1000  # Max conversations to track
_SEND_CACHE_CLEANUP_AGE = 3600  # Remove entries older than 1 hour


def _cleanup_send_caches():
    """Remove old entries from send caches to prevent unbounded growth.

    Note: Only cleans up timestamps, NOT locks. Locks may still be held by
    threads and deleting them would allow concurrent threads to bypass
    serialization by getting new locks for the same conversation.
    """
    now = time.time()
    cutoff = now - _SEND_CACHE_CLEANUP_AGE
    with _send_locks_lock:
        # Find stale conversation IDs and remove timestamps + idle locks
        stale_ids = [cid for cid, ts in _last_send_times.items() if ts < cutoff]
        for cid in stale_ids:
            _last_send_times.pop(cid, None)
            # Remove the lock only if it is idle (non-blocking acquire succeeds)
            lock = _send_locks.get(cid)
            if lock is not None and lock.acquire(blocking=False):
                lock.release()
                _send_locks.pop(cid, None)
        # If still too large, remove oldest timestamp entries
        if len(_last_send_times) > _SEND_CACHE_MAX_SIZE:
            sorted_ids = sorted(_last_send_times.items(), key=lambda x: x[1])
            for cid, _ in sorted_ids[:len(sorted_ids) - _SEND_CACHE_MAX_SIZE]:
                _last_send_times.pop(cid, None)
                lock = _send_locks.get(cid)
                if lock is not None and lock.acquire(blocking=False):
                    lock.release()
                    _send_locks.pop(cid, None)


def _get_send_lock(convo_id: str) -> threading.Lock:
    """Get or create a lock for a specific conversation (thread-safe)."""
    with _send_locks_lock:
        if convo_id not in _send_locks:
            _send_locks[convo_id] = threading.Lock()
        return _send_locks[convo_id]

# Initialize policy manager for mesh config sync
policy_manager = PolicyManager()

# Initialize Wind orchestrator for proactive messaging
def _get_wind_config() -> WindConfig:
    """Get WindConfig from policy manager."""
    return WindConfig.from_dict(policy_manager.get_wind_config())

wind_orchestrator = WindOrchestrator(
    db_connection_factory=memory._connect,
    config=_get_wind_config(),
    llm_client=llm,
    memory=memory,
    context_message_count=CONTEXT_MESSAGE_COUNT,
    compact_batch_size=COMPACT_BATCH_SIZE,
)

reminder_manager = ReminderManager(db_connection_factory=memory._connect)

# Initialize memory consolidator
consolidator = MemoryConsolidator(
    memory=memory,
    llm_client=llm,
    consolidation_model=CONSOLIDATION_MODEL,
    model_lookup=get_consolidation_model_for_conversation,
    privacy_mode=policy_manager.is_privacy_mode,
)


# --- Wind Snooze Command Patterns ---
_SNOOZE_TRIGGER   = re.compile(r"\b(quiet|shh+|hush|snooze|mute|pause)\b", re.I)
_SNOOZE_CLEAR     = re.compile(r"\b(wake|unsnooze|unmute|resume)\b", re.I)
_DURATION_HOURS   = re.compile(r"(\d+)\s*h(?:ours?)?", re.I)
_DURATION_MINS    = re.compile(r"(\d+)\s*m(?:in(?:utes?)?)?", re.I)
_DURATION_DAYS    = re.compile(r"(\d+)\s*d(?:ays?)?", re.I)
_DURATION_TONIGHT = re.compile(r"\btonight\b", re.I)

# --- Reminder Post-Fire Snooze Patterns ---
_REMINDER_SNOOZE_TRIGGER = re.compile(
    r"\b(remind\s+me\s+again|snooze|later|remind\s+me\s+in)\b", re.I
)

# --- Reminder Command Patterns ---
_REMINDER_TRIGGER = re.compile(r"\bremind\s+me\b", re.I)
_REMINDER_ABOUT   = re.compile(r"\b(?:to|about)\b", re.I)

# --- Reschedule Intent Detection ---
_RESCHEDULE_TRIGGER = re.compile(
    r"\b(?:reschedule|postpone|push\s+back|delay|move\s+(?:the\s+)?(?:meeting|appointment|event|"
    r"service|call|visit)|put\s+off)\b",
    re.I,
)


# --- Group Membership Cache ---
# (class extracted to group_cache.py)
membership_cache = GroupMembershipCache()
membership_cache.set_dependencies(
    mesh_url=settings.mesh_url,
    policy_manager=policy_manager,
    get_current_hmac_secret=_get_current_hmac_secret,
)


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
                        logger.warning("Config hash mismatch", extra={
                            "local_hash": current_hash[:16],
                            "mesh_hash": mesh_hash[:16]
                        })

                    self._last_push_hash = mesh_hash
                    self._last_push_time = time.time()
                    logger.info("Config push successful", extra={
                        "config_hash": mesh_hash[:16] if mesh_hash else "none"
                    })
                    return True, mesh_hash

                error = data.get("error", "unknown")
                logger.error("Mesh returned error on config push", extra={"error": error})
                return False, error

            except httpx.HTTPStatusError as exc:
                logger.error("Config push HTTP error", extra={
                    "status_code": exc.response.status_code,
                    "error": str(exc)
                })
                return False, f"http_error_{exc.response.status_code}"
            except Exception as exc:
                logger.error("Config push failed", extra={"error": str(exc)})
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
            logger.debug("Failed to get mesh status", extra={"error": str(exc)})
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
            logger.info("Mesh config drift", extra={
                "expected_hash": self._last_push_hash[:16] if self._last_push_hash else "none",
                "actual_hash": mesh_hash[:16] if mesh_hash else "none"
            })
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
    # Check HMAC sync on startup — handles crash recovery for pending rotations
    sync_ok = hmac_rotator.startup_sync_check()
    if not sync_ok:
        logger.warning("HMAC sync check failed on startup", extra={
            "action": "hmac_sync_warning"
        })
    config_push_client = ConfigPushClient(
        mesh_url=settings.mesh_url,
        policy=policy_manager,
    )

# Ensure prompts directory exists
ensure_prompts_dir()

# Set up admin routes dependencies
admin_routes.set_dependencies(
    memory=memory,
    policy_manager=policy_manager,
    config_push_client=config_push_client,
    hmac_rotator=hmac_rotator,
)

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


# Keywords that suggest user might want something remembered (hybrid approach)
# If any keyword is present, we ask the LLM to confirm and extract
# Narrowed to explicit intent only - avoids diary-style false positives from "i am", "i like" etc.
REMEMBER_KEYWORDS = [
    "remember", "never forget", "always remember", "don't forget",
    "note that", "keep in mind",
    "call me", "my name is",
]

# Keywords that indicate the fact should always be remembered (marked important)
ALWAYS_REMEMBER_KEYWORDS = ["always remember", "never forget", "important:"]

# Categories that are automatically marked as important (core identity)
IMPORTANT_CATEGORIES = ["personal", "relationship"]
IMPORTANT_KEYS = ["name", "profession", "job", "partner", "spouse", "wife", "husband", "child", "children"]

# Question forms directed at Joi ("do you remember X?") — not instructions to save
_REMEMBER_QUESTION_RE = re.compile(
    r"\b(do|did|can|could|don'?t|doesn'?t|won'?t|will|would)\s+you\b[^.?!]*\bremember\b",
    re.I,
)


def _has_remember_keywords(text: str) -> bool:
    """Quick check if message might contain a remember request."""
    text_lower = text.lower()
    if not any(kw in text_lower for kw in REMEMBER_KEYWORDS):
        return False
    # Exclude question forms asking if Joi remembers — not instructions to save a fact
    if _REMEMBER_QUESTION_RE.search(text):
        return False
    return True


def _should_mark_important(text: str, category: str, key: str) -> bool:
    """
    Determine if a fact should be marked as important (always included in context).

    A fact is important if:
    1. User explicitly says "always remember", "never forget", etc.
    2. Category is in IMPORTANT_CATEGORIES (personal, relationship)
    3. Key is in IMPORTANT_KEYS (name, profession, partner, etc.)
    """
    text_lower = text.lower()

    # Check for explicit "always remember" phrases
    if any(kw in text_lower for kw in ALWAYS_REMEMBER_KEYWORDS):
        return True

    # Check category
    if category.lower() in IMPORTANT_CATEGORIES:
        return True

    # Check key - use word boundary matching to avoid false positives
    # (e.g., "username" should not match "name")
    key_lower = key.lower()
    key_words = set(re.split(r'[_\-\s]+', key_lower))  # Split on common separators
    if key_words & set(IMPORTANT_KEYS):  # Set intersection
        return True

    return False


def _detect_and_extract_fact(
    text: str,
    conversation_id: str = "",
    sender_id: str = "",
    sender_name: str = "",
    is_group: bool = False,
) -> Optional[str]:
    """
    Use LLM to detect if user wants something remembered, and extract it.

    Hybrid approach:
    1. Quick keyword check (cheap)
    2. LLM detection and extraction (accurate)

    For groups, includes sender info in the fact key to distinguish between users.
    Core identity facts (name, profession, relationships) are auto-marked as important.

    Returns the saved fact value, or None if nothing to remember.
    """
    # Quick filter - skip LLM if no relevant keywords
    if not _has_remember_keywords(text):
        return None

    logger.info("Checking for remember request (LLM)")

    # Ask LLM to detect and extract in one call
    prompt = f"""Analyze this message: "{text}"

Is the user TELLING me a fact about themselves that I should store?
This includes: their name, preferences, facts about them, things they like/dislike,
personal info, work info, or explicitly asking me to remember something.

Rules:
- "Do you remember X?" = asking about my memory = false
- "Remember that X" or "Remember X" = instruction to save = true
- "I want you to remember X" = instruction to save = true
- If the user is ASKING a question (even one containing the word "remember"), return false.
- If the user is TELLING me something factual about themselves, return true.
- Casual chat, commands, and rhetorical questions are always false.

If YES, extract the fact as JSON:
{{"remember": true, "category": "personal|preference|work|routine|interest|relationship", "key": "short_id", "value": "the fact"}}

If NO, return:
{{"remember": false}}

Return ONLY valid JSON, nothing else:"""

    try:
        response = llm.generate(prompt=prompt)
        if response.error:
            logger.debug("LLM error in remember detection", extra={"error": response.error})
            return None

        # Parse JSON
        text_resp = response.text.strip()
        start = text_resp.find("{")
        end = text_resp.rfind("}") + 1
        if start >= 0 and end > start:
            result = json.loads(text_resp[start:end])

            if not result.get("remember", False):
                logger.debug("LLM: nothing to remember in message")
                return None

            if all(k in result for k in ["category", "key", "value"]):
                # For groups, prefix key with sender name to distinguish users
                fact_key = result["key"]
                fact_value = str(result["value"])
                if is_group and sender_name:
                    # Use sender name in key: "peter_name" instead of "name"
                    safe_name = sender_name.lower().replace(" ", "_")[:20]
                    fact_key = f"{safe_name}_{result['key']}"

                # Determine if fact is important (core identity)
                is_important = _should_mark_important(text, result["category"], fact_key)

                memory.store_fact(
                    category=result["category"],
                    key=fact_key,
                    value=fact_value,
                    confidence=0.95,  # High confidence - user explicitly stated
                    source="stated",
                    conversation_id=conversation_id,
                    important=is_important,
                )
                important_marker = " [important]" if is_important else ""
                if policy_manager.is_privacy_mode():
                    logger.info("Saved stated fact [privacy mode]", extra={
                        "conversation_id": conversation_id[:8] + "..." if conversation_id else "global",
                        "category": result["category"],
                        "key": fact_key,
                        "important": is_important,
                        "action": "fact_save"
                    })
                else:
                    logger.info("Saved stated fact", extra={
                        "conversation_id": conversation_id or "global",
                        "category": result["category"],
                        "key": fact_key,
                        "value": fact_value,
                        "important": is_important,
                        "action": "fact_save"
                    })
                return fact_value
    except json.JSONDecodeError as e:
        logger.debug("Failed to parse remember response", extra={"error": str(e)})
    except Exception as e:
        logger.warning("Failed to detect/extract fact", extra={"error": str(e)})
        # Rollback to clear failed transaction state (prevents blocking other connections)
        try:
            memory._connect().rollback()
        except Exception:
            pass

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
    is_owner: bool = False  # True if sender is the primary owner (first in allowed_senders)


class InboundResponse(BaseModel):
    status: str
    message_id: Optional[str] = None
    error: Optional[str] = None


class TypingInboundRequest(BaseModel):
    sender: str
    conversation_id: str


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
        logger.error("HMAC auth failed: not configured (fail-closed)", extra={
            "action": "auth_failed",
            "reason": "hmac_not_configured"
        })
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
        logger.warning("HMAC auth failed: missing headers", extra={
            "action": "auth_failed",
            "reason": "missing_headers"
        })
        return JSONResponse(
            status_code=401,
            content={"status": "error", "error": {"code": "hmac_missing_headers", "message": "Missing authentication headers"}}
        )

    # Parse timestamp
    try:
        timestamp = int(timestamp_str)
    except ValueError:
        logger.warning("HMAC auth failed: invalid timestamp format", extra={
            "action": "auth_failed",
            "reason": "invalid_timestamp"
        })
        return JSONResponse(
            status_code=401,
            content={"status": "error", "error": {"code": "hmac_invalid_timestamp", "message": "Invalid timestamp format"}}
        )

    # Verify timestamp freshness (cheap check first)
    ts_valid, ts_error = verify_timestamp(timestamp, HMAC_TIMESTAMP_TOLERANCE_MS)
    if not ts_valid:
        logger.warning("HMAC auth failed: timestamp", extra={
            "action": "auth_failed",
            "reason": ts_error
        })
        return JSONResponse(
            status_code=401,
            content={"status": "error", "error": {"code": ts_error, "message": "Request timestamp out of tolerance"}}
        )

    # Read body for HMAC verification
    body = await request.body()

    # Verify HMAC signature BEFORE storing nonce (prevents unauthenticated nonce DoS)
    valid_secrets = _get_valid_hmac_secrets()
    signature_valid = False
    for secret in valid_secrets:
        if verify_hmac(nonce, timestamp, body, signature, secret):
            signature_valid = True
            break

    if not signature_valid:
        logger.warning("HMAC auth failed: invalid signature", extra={
            "action": "auth_failed",
            "reason": "invalid_signature"
        })
        return JSONResponse(
            status_code=401,
            content={"status": "error", "error": {"code": "hmac_invalid_signature", "message": "Invalid HMAC signature"}}
        )

    # Signature valid - now check and store nonce for replay protection
    nonce_valid, nonce_error = nonce_store.check_and_store(nonce, source="mesh")
    if not nonce_valid:
        logger.warning("HMAC auth failed: nonce replay", extra={
            "action": "auth_failed",
            "reason": nonce_error,
            "nonce": nonce[:8]
        })
        return JSONResponse(
            status_code=401,
            content={"status": "error", "error": {"code": nonce_error, "message": "Nonce already used"}}
        )

    logger.debug("HMAC auth passed", extra={"path": str(request.url.path)})
    return await call_next(request)


# --- Runtime Fingerprint (Tamper Detection) ---

_startup_fingerprints: Dict[str, str] = {}


def _redact_filename_pii(filename: str) -> str:
    """Redact phone numbers and group IDs in filenames for privacy mode.

    Examples:
        +123456789.txt -> +***6789.txt
        XDtVV+4Nz0pW9WCRi00QgY7E5hd29DGPmyKF6i-Z6bY=.txt -> [GRP:XDtV...].txt
    """
    import re

    # Get extension if present
    name, ext = (filename.rsplit('.', 1) + [''])[:2]
    ext = f'.{ext}' if ext else ''

    # Phone number pattern
    if re.match(r'^\+\d+$', name):
        if len(name) > 5:
            return f"+***{name[-4:]}{ext}"
        return f"+***{ext}"

    # Group ID pattern (base64-like, typically 32+ chars with = padding)
    if len(name) > 20 and re.match(r'^[A-Za-z0-9+/=_-]+$', name):
        return f"[GRP:{name[:4]}...]{ext}"

    return filename


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

    # Policy file (must match JOI_MESH_POLICY_PATH used by PolicyManager)
    policy_file = os.getenv("JOI_MESH_POLICY_PATH", "/var/lib/joi/policy/mesh-policy.json")
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
        privacy_mode = policy_manager.is_privacy_mode()

        def format_entry(path, hash):
            name = os.path.basename(path)
            if privacy_mode:
                name = _redact_filename_pii(name)
            return f"{name}:{hash}"

        summary = " | ".join(format_entry(p, h) for p, h in sorted(_startup_fingerprints.items()))
        logger.info("Runtime fingerprint initialized", extra={
            "files": summary,
            "privacy_mode": privacy_mode,
            "action": "fingerprint_init"
        })


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
            logger.warning("TAMPER DETECTED: file changed", extra={
                "file": display_path(path),
                "original_hash": original_hash,
                "current_hash": current_hash,
                "action": "tamper_detect"
            })

    # Check for new files
    for path in current:
        if path not in _startup_fingerprints:
            changed.append(path)
            logger.warning("TAMPER DETECTED: new file", extra={
                "file": display_path(path),
                "action": "tamper_detect"
            })

    return changed


# --- Lifecycle Events ---

@app.on_event("startup")
def startup_event():
    """Start the message queue worker and scheduler on app startup."""
    global scheduler
    message_queue.start()

    # Create and configure scheduler if enabled
    if SCHEDULER_ENABLED:
        scheduler = Scheduler(
            interval_seconds=SCHEDULER_INTERVAL,
            startup_delay=SCHEDULER_STARTUP_DELAY,
        )
        scheduler.set_dependencies(
            memory=memory,
            nonce_store=nonce_store,
            config_push_client=config_push_client,
            hmac_rotator=hmac_rotator,
            membership_cache=membership_cache,
            wind_orchestrator=wind_orchestrator,
            policy_manager=policy_manager,
            consolidator=consolidator,
            check_fingerprints=_check_fingerprints,
            get_wind_config=_get_wind_config,
            generate_proactive_message=_generate_proactive_message,
            generate_reminder_message=_generate_reminder_message,
            send_to_mesh=_send_to_mesh,
            run_auto_ingestion=run_auto_ingestion,
            cleanup_send_caches=_cleanup_send_caches,
            InboundConversation=InboundConversation,
            reminder_manager=reminder_manager,
        )
        scheduler.start()
    wind_config = _get_wind_config()
    logger.info("Joi API started", extra={
        "hmac_enabled": HMAC_ENABLED,
        "scheduler_enabled": scheduler is not None,
        "scheduler_interval": SCHEDULER_INTERVAL if scheduler else None,
        "time_awareness": TIME_AWARENESS_ENABLED,
        "timezone": TIME_AWARENESS_TIMEZONE if TIME_AWARENESS_ENABLED else None,
        "privacy_mode": policy_manager.is_privacy_mode(),
        "wind_enabled": wind_config.enabled,
        "wind_shadow": wind_config.shadow_mode if wind_config.enabled else None,
        "action": "startup"
    })
    _init_fingerprints()


@app.on_event("shutdown")
def shutdown_event():
    """Stop the message queue worker, scheduler, and close connections on app shutdown."""
    if scheduler:
        scheduler.stop()
    message_queue.stop()
    memory.close()
    logger.info("Joi API shutting down")


# --- Endpoints ---

@app.get("/health")
def health():
    msg_count = memory.get_message_count()
    fact_count = memory.count_facts(min_confidence=0.0)
    summary_count = memory.count_summaries(days=30)
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
            "facts": fact_count,
            "summaries": summary_count,
            "context_size": CONTEXT_MESSAGE_COUNT,
        },
        "rag": {
            "enabled": RAG_ENABLED,
            "sources": len(knowledge_sources),
            "chunks": knowledge_chunks,
        },
        "fts": {
            "facts_enabled": FACTS_FTS_ENABLED,
            "summaries_enabled": SUMMARIES_FTS_ENABLED,
        },
        "queue": {
            "inbound": message_queue.get_queue_size(),
        },
        "outbound": outbound_limiter.get_stats(),
        "scheduler": scheduler.get_status() if scheduler else {"running": False},
        "config_sync": config_sync,
        "hmac": hmac_rotator.get_rotation_status() if hmac_rotator else {"enabled": False},
    }


# Admin endpoints are in admin_routes.py (included via router below)

# --- Document Ingestion Endpoint ---

# Rate limiting for document ingestion (prevent DoS via disk exhaustion)
_ingest_times: Dict[str, list] = {}  # scope -> list of timestamps
_ingest_lock = threading.Lock()
_INGEST_RATE_LIMIT = 10  # Max ingests per scope per minute
_INGEST_RATE_WINDOW = 60  # Window in seconds


def _check_ingest_rate_limit(scope: str) -> bool:
    """Check if ingestion is allowed for this scope. Returns True if allowed."""
    now = time.time()
    cutoff = now - _INGEST_RATE_WINDOW

    with _ingest_lock:
        if scope not in _ingest_times:
            _ingest_times[scope] = []

        # Remove old timestamps
        _ingest_times[scope] = [t for t in _ingest_times[scope] if t > cutoff]

        # Prune empty scope keys to prevent unbounded growth
        if not _ingest_times[scope]:
            del _ingest_times[scope]
            return True

        # Check limit
        if len(_ingest_times[scope]) >= _INGEST_RATE_LIMIT:
            return False

        # Record this request
        _ingest_times[scope].append(now)
        return True


@app.post("/api/v1/typing/inbound")
def typing_inbound(req: TypingInboundRequest):
    """
    Receive typing indicator from mesh. Updates Wind's per-conversation typing state
    so proactive messages are suppressed while the user is actively composing.
    Best-effort — always returns 200.
    """
    if wind_orchestrator:
        wind_orchestrator.state_manager.record_typing(req.conversation_id)
    return {"status": "ok"}


@app.post("/api/v1/document/ingest", response_model=DocumentIngestResponse)
def ingest_document(req: DocumentIngestRequest):
    """
    Receive a document from mesh for RAG ingestion.

    Documents are saved to the ingestion input directory for processing
    by the scheduler tick. The scope determines which conversations
    can access the knowledge.

    Rate limited to 10 documents per scope per minute to prevent DoS.
    """
    # Rate limit check
    if not _check_ingest_rate_limit(req.scope):
        logger.warning("Document ingestion rate limit exceeded", extra={"scope": req.scope})
        return DocumentIngestResponse(
            status="error",
            error="rate_limit_exceeded",
        )
    # Log with privacy mode redaction
    privacy_mode = policy_manager.is_privacy_mode()
    logger.info("Received document", extra={
        "doc_filename": "[redacted]" if privacy_mode else req.filename,
        "content_type": req.content_type,
        "scope": _redact_filename_pii(req.scope) if privacy_mode else req.scope,
        "sender_id": _redact_filename_pii(req.sender_id) if privacy_mode else req.sender_id,
        "privacy_mode": privacy_mode,
        "action": "document_receive"
    })

    # Validate filename (basic security check)
    if "/" in req.filename or "\\" in req.filename or ".." in req.filename:
        logger.warning("Invalid filename rejected", extra={"doc_filename": req.filename})
        return DocumentIngestResponse(
            status="error",
            error="invalid_filename",
        )

    # Sanitize scope for use as directory name (consistent with RAG lookup)
    safe_scope = sanitize_scope(req.scope)
    if not safe_scope or safe_scope.startswith(".") or ".." in safe_scope:
        logger.warning("Invalid scope rejected", extra={"scope": req.scope})
        return DocumentIngestResponse(
            status="error",
            error="invalid_scope",
        )

    # Decode base64 content
    try:
        content = base64.b64decode(req.content_base64)
    except Exception as e:
        logger.warning("Failed to decode base64 content", extra={"error": str(e)})
        return DocumentIngestResponse(
            status="error",
            error="invalid_base64",
        )

    # Enforce size limit (2x mesh cap as a safety margin for direct API calls)
    if len(content) > JOI_MAX_DOCUMENT_SIZE * 2:
        logger.warning("Document rejected: content too large", extra={
            "size_bytes": len(content),
            "max_bytes": JOI_MAX_DOCUMENT_SIZE * 2,
        })
        return DocumentIngestResponse(
            status="error",
            error="content_too_large",
        )

    # Create scope directory if needed
    scope_dir = INGESTION_DIR / "input" / safe_scope
    try:
        scope_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error("Failed to create scope directory", extra={"path": str(scope_dir), "error": str(e)})
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
        privacy_mode = policy_manager.is_privacy_mode()
        logger.info("Saved document", extra={
            "path": "[redacted]" if privacy_mode else str(filepath),
            "size_bytes": len(content),
            "privacy_mode": privacy_mode,
            "action": "document_save"
        })
    except Exception as e:
        # Error logs always show path for debugging
        logger.error("Failed to save document", extra={"path": str(filepath), "error": str(e)})
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
    # Check if sender is owner (set by mesh based on first entry in allowed_senders)
    is_owner = msg.is_owner

    # Privacy-aware logging
    privacy_mode = policy_manager.is_privacy_mode()
    logger.info("Received message", extra={
        "message_id": msg.message_id,
        "sender": "[redacted]" if privacy_mode else msg.sender.transport_id,
        "type": msg.content.type,
        "conversation_type": msg.conversation.type,
        "store_only": msg.store_only,
        "is_owner": is_owner,
        "privacy_mode": privacy_mode,
        "action": "message_receive"
    })

    # Handle reactions - store and respond briefly
    if msg.content.type == "reaction":
        emoji = msg.content.reaction or "?"
        logger.info("Received reaction", extra={"emoji": emoji, "sender": msg.sender.transport_id, "action": "reaction_receive"})

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
        # Queue the LLM call to ensure all LLM access is serialized
        if not msg.store_only:
            def process_reaction(queue_msg) -> InboundResponse:
                response_text = _generate_reaction_response(emoji, msg.conversation.id)
                if response_text:
                    # Check if cancelled before committing (deferred commit pattern)
                    if queue_msg.cancelled:
                        logger.warning("Reaction abandoned after timeout", extra={
                            "message_id": msg.message_id,
                            "action": "deferred_abort"
                        })
                        return InboundResponse(status="ok", message_id=msg.message_id)

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

            try:
                return message_queue.enqueue(
                    message_id=f"reaction-{msg.message_id}",
                    handler=process_reaction,
                    is_owner=is_owner,
                    timeout=60.0,  # Shorter timeout for reactions
                )
            except TimeoutError:
                logger.warning("Reaction response timed out", extra={"message_id": msg.message_id})
                return InboundResponse(status="ok", message_id=msg.message_id)
            except Exception as e:
                logger.error("Reaction response failed", extra={"error": str(e)})
                return InboundResponse(status="ok", message_id=msg.message_id)

        return InboundResponse(status="ok", message_id=msg.message_id)

    # Only handle text messages beyond this point
    if msg.content.type != "text" or not msg.content.text:
        logger.info("Skipping unsupported message type", extra={"type": msg.content.type})
        return InboundResponse(status="ok", message_id=msg.message_id)

    user_text = sanitize_input(msg.content.text.strip())
    if not user_text:
        return InboundResponse(status="ok", message_id=msg.message_id)

    # Store inbound message (always store for context, sanitized)
    quote_reply_to_id = msg.quote.get("message_id") if msg.quote else None
    msg_row_id = memory.store_message(
        message_id=msg.message_id,
        direction="inbound",
        content_type=msg.content.type,
        content_text=user_text,
        timestamp=msg.timestamp,
        conversation_id=msg.conversation.id,
        reply_to_id=quote_reply_to_id,
        sender_id=msg.sender.transport_id,
        sender_name=msg.sender.display_name,
    )

    # Check if this was a duplicate (INSERT OR IGNORE returned 0)
    if msg_row_id == 0:
        logger.info("Duplicate message ignored", extra={
            "message_id": msg.message_id,
            "action": "duplicate_ignore"
        })
        return InboundResponse(status="ok", message_id=msg.message_id)

    # Record user interaction for Wind (resets unanswered counter, updates silence timer)
    wind_orchestrator.record_user_interaction(
        msg.conversation.id,
        message_text=user_text,
        reply_to_id=quote_reply_to_id,
    )

    # Wind snooze command: owner can silence proactive messages for a period
    if msg.is_owner and msg.conversation.type == "direct" and user_text:
        snooze_reply = _handle_wind_snooze_command(user_text, msg.conversation.id)
        if snooze_reply:
            _send_to_mesh(
                recipient_id="owner",
                recipient_transport_id=msg.conversation.id,
                conversation=msg.conversation,
                text=snooze_reply,
                reply_to=msg.message_id,
            )
            return InboundResponse(status="ok", message_id=msg.message_id)

    # Reminder post-fire snooze: "remind me again in 1h"
    if msg.is_owner and msg.conversation.type == "direct" and user_text:
        snooze_reminder_reply = _handle_reminder_snooze_command(user_text, msg.conversation.id)
        if snooze_reminder_reply:
            _send_to_mesh(
                recipient_id="owner",
                recipient_transport_id=msg.conversation.id,
                conversation=msg.conversation,
                text=snooze_reminder_reply,
                reply_to=msg.message_id,
            )
            return InboundResponse(status="ok", message_id=msg.message_id)

    # Reminder command: owner can set reminders
    if msg.is_owner and msg.conversation.type == "direct" and user_text:
        reminder_reply = _handle_reminder_command(user_text, msg.conversation.id)
        if reminder_reply:
            _send_to_mesh(
                recipient_id="owner",
                recipient_transport_id=msg.conversation.id,
                conversation=msg.conversation,
                text=reminder_reply,
                reply_to=msg.message_id,
            )
            return InboundResponse(status="ok", message_id=msg.message_id)

    # All facts (explicit and inferred) use conversation_id as key
    # - DMs: phone number (per-user scope)
    # - Groups: group_id (group scope, facts include person names in key)
    fact_key = msg.conversation.id

    # Note: Fact extraction moved into queued handler to ensure all LLM calls
    # go through the queue (fixes concurrent LLM access issue)

    # Determine if we should respond
    should_respond = True

    if msg.store_only:
        # Non-allowed sender in group - store only, no response
        logger.info("Message stored for context only", extra={"store_only": True})
        should_respond = False
    elif msg.conversation.type == "group":
        # Group message from allowed sender - only respond if bot is addressed
        # Check Signal @mention (bot_mentioned) or text-based @name
        bot_name = policy_manager.get_bot_name()
        logger.debug("Group message", extra={"bot_mentioned": msg.bot_mentioned, "group_names": msg.group_names})
        if msg.bot_mentioned:
            logger.info("Bot @mentioned in group message (Signal mention), will respond", extra={
                "bot_name": bot_name,
                "action": "mention_detect"
            })
            should_respond = True
        else:
            # Fallback: check text for @name pattern
            names_to_check = msg.group_names if msg.group_names else None
            logger.debug("Checking address", extra={"names": names_to_check, "text_start": user_text[:50] if user_text else ""})
            if _is_addressing_joi(user_text, names=names_to_check):
                logger.info("Bot addressed in group message (text pattern), will respond", extra={
                    "bot_name": bot_name,
                    "action": "address_detect"
                })
                should_respond = True
            else:
                logger.info("Bot not addressed in group message, storing only", extra={"bot_name": bot_name})
                should_respond = False

    if not should_respond:
        return InboundResponse(status="ok", message_id=msg.message_id)

    # --- Queue the LLM processing ---
    # This ensures messages are processed sequentially with owner priority

    def process_with_llm(queue_msg) -> InboundResponse:
        """Process message with LLM - runs in queue worker thread.

        Uses deferred commit pattern: collect pending work, only commit at end if not cancelled.
        Heartbeat signals keep timeout from firing during long LLM operations.
        """
        # Pending work to commit at the end (deferred commit pattern)
        pending_fact = None
        pending_response = None

        # Emit typing indicator immediately so user gets feedback before any LLM work
        _send_typing_indicator(msg)

        # Check for "remember this" requests (only from allowed senders)
        # Uses hybrid approach: keyword filter + LLM detection/extraction
        # Note: This is inside the queue to ensure all LLM calls are serialized
        if not msg.store_only:
            if queue_msg.cancelled:
                return InboundResponse(status="ok", message_id=msg.message_id)
            queue_msg.heartbeat()  # Signal still working
            pending_fact = _detect_and_extract_fact(
                user_text,
                conversation_id=fact_key,
                sender_id=msg.sender.transport_id,
                sender_name=msg.sender.display_name or "",
                is_group=(msg.conversation.type == "group"),
            )
            # Note: _detect_and_extract_fact already stores the fact, so for true deferred commit
            # we'd need to refactor it. For now, heartbeat extends timeout to reduce partial state.

        # Reschedule intent: owner can reschedule temporal facts via natural language.
        # Does not return early — Joi's normal LLM response confirms the action.
        # Runs inside the queue so the LLM extraction call is serialized with other LLM work.
        if msg.is_owner and msg.conversation.type == "direct" and user_text:
            _handle_reschedule_intent(user_text, msg.conversation.id)

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
        debug_components: dict = {}
        _debug_arg = debug_components if BRAIN_DEBUG else None
        if custom_model:
            # Custom model: prompt is optional (Modelfile has baked-in SYSTEM)
            base_prompt = get_prompt_for_conversation_optional(
                conversation_type=msg.conversation.type,
                conversation_id=msg.conversation.id,
                sender_id=msg.sender.transport_id,
            )
            if base_prompt:
                enriched_prompt = _build_enriched_prompt(base_prompt, user_text, conversation_id=fact_key, knowledge_scopes=knowledge_scopes, _debug=_debug_arg)
            else:
                # No prompt file - only add facts/summaries/RAG if available
                enriched_prompt = _build_enriched_prompt("", user_text, conversation_id=fact_key, knowledge_scopes=knowledge_scopes, _debug=_debug_arg)
                enriched_prompt = enriched_prompt.strip() or None  # None if empty
        else:
            # No custom model: use prompt with fallback to default
            base_prompt = get_prompt_for_conversation(
                conversation_type=msg.conversation.type,
                conversation_id=msg.conversation.id,
                sender_id=msg.sender.transport_id,
            )
            enriched_prompt = _build_enriched_prompt(base_prompt, user_text, conversation_id=fact_key, knowledge_scopes=knowledge_scopes, _debug=_debug_arg)

        # Note: We no longer hint to the LLM about saved facts - this caused meta-reactions
        # and excessive "tagging" behavior. Memory is now invisible to the response generation.

        # Generate response from LLM with conversation context
        model_source = get_model_source(msg.conversation.type, msg.conversation.id, msg.sender.transport_id)
        prompt_source = get_prompt_source(msg.conversation.type, msg.conversation.id, msg.sender.transport_id)
        logger.info("Generating LLM response", extra={
            "message_count": len(chat_messages),
            "model": custom_model or "default",
            "modelfile": model_source,
            "prompt": prompt_source,
            "context_size": context_size,
            "context_source": "custom" if custom_context else "default",
            "action": "llm_generate"
        })

        if BRAIN_DEBUG:
            _write_brain_debug(
                message_id=str(msg.message_id),
                conversation_id=msg.conversation.id or "",
                model=custom_model or "",
                context_size=context_size,
                system=enriched_prompt or "",
                messages=chat_messages,
                debug_components=debug_components,
            )

        queue_msg.heartbeat()  # Signal still working before LLM call
        _stop_typing = _start_typing_refresh(msg)
        try:
            llm_response = llm.chat(messages=chat_messages, system=enriched_prompt, model=custom_model)
        finally:
            _stop_typing()
        queue_msg.heartbeat()  # Signal still working after LLM call

        if llm_response.error:
            logger.error("LLM error", extra={"error": llm_response.error})
            return InboundResponse(
                status="error",
                message_id=msg.message_id,
                error=f"llm_error: {llm_response.error}",
            )

        response_text = llm_response.text.strip()
        if not response_text:
            logger.warning("LLM returned empty response")
            response_text = "I'm not sure how to respond to that."

        # Validate output (prompt injection defense)
        is_valid, response_text = validate_output(response_text)
        if not is_valid:
            logger.warning("Output validation failed, using fallback response")

        # Format for Signal (convert **bold** to Unicode bold)
        response_text = format_for_signal(response_text)

        # Log response (redact content in privacy mode)
        response_len = len(response_text)
        privacy_mode = policy_manager.is_privacy_mode()
        logger.info("LLM response generated", extra={
            "response_length": response_len,
            "preview": None if privacy_mode else response_text.replace("\r", "").replace("\n", " ")[:50],
            "privacy_mode": privacy_mode,
            "action": "llm_response"
        })

        # Check if cancelled before committing (deferred commit pattern)
        if queue_msg.cancelled:
            logger.warning("Message abandoned after timeout (deferred abort)", extra={
                "message_id": msg.message_id,
                "response_generated": True,
                "action": "deferred_abort"
            })
            return InboundResponse(status="ok", message_id=msg.message_id)

        # --- Commit phase: send response and store ---
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
        logger.error("Message timed out in queue", extra={"message_id": msg.message_id})
        return InboundResponse(
            status="error",
            message_id=msg.message_id,
            error="queue_timeout",
        )
    except Exception as e:
        logger.error("Message queue error", extra={"message_id": msg.message_id, "error": str(e)})
        return InboundResponse(
            status="error",
            message_id=msg.message_id,
            error=str(e),
        )


def _generate_proactive_message(
    topic_title: str,
    topic_content: Optional[str],
    conversation_id: str,
) -> Optional[str]:
    """
    Generate a proactive message for Wind based on a topic.

    Uses joi-brain with facts and knowledge to draft a natural message.
    No conversation history - proactive messages are fresh initiations.
    """
    # Simple base personality for Wind messages
    base_prompt = "You are Joi, a warm and thoughtful AI companion."

    topic_info = topic_title
    if topic_content:
        topic_info += f" — {topic_content}"

    # Pull relevant context via FTS (no extra LLM calls — pure SQL/BM25)
    facts_ctx = memory.get_facts_as_context(
        query=topic_title,
        max_tokens=FACTS_FTS_MAX_TOKENS,
        min_confidence=0.6,
        conversation_id=conversation_id,
    )
    summaries_ctx = memory.get_summaries_as_context(
        query=topic_title,
        max_tokens=SUMMARIES_FTS_MAX_TOKENS,
        days=30,
        conversation_id=conversation_id,
    )

    # Build system prompt
    system_parts = [base_prompt]
    if facts_ctx:
        system_parts.append(f"\n\n{facts_ctx}")
    system_prompt = "".join(system_parts)

    # Build context block for user prompt
    context_parts = []
    if summaries_ctx:
        context_parts.append(f"Relevant context from our history:\n{summaries_ctx}")
    context_block = "\n\n".join(context_parts) + "\n\n" if context_parts else ""

    user_prompt = (
        f"{context_block}"
        f"You want to bring something up: {topic_info}\n\n"
        "Write one short message — the kind you'd fire off without overthinking it. "
        "Could be a question, a passing observation, or something that crossed your mind. "
        "No greeting. No philosophical warm-up. Get straight to the point. Just the message."
    )

    try:
        # Use chat with system prompt for better personality consistency
        response = llm.chat(
            messages=[{"role": "user", "content": user_prompt}],
            system=system_prompt,
        )
        if response.error or not response.text:
            logger.warning("Wind: LLM generation failed", extra={"error": response.error})
            return None

        text = response.text.strip()

        # Validate output
        is_valid, text = validate_output(text)
        if not is_valid:
            logger.warning("Wind: generated message failed validation", extra={"action": "wind_validate"})
            return None

        # Length check (proactive messages should be short)
        if len(text) > 500:
            logger.warning("Wind: generated message too long, truncating", extra={"length": len(text)})
            text = text[:500]

        logger.info("Wind: generated message", extra={
            "length": len(text),
            "topic": topic_title[:30],
            "action": "wind_generate"
        })
        return text

    except Exception as e:
        logger.error("Wind: message generation error", extra={"error": str(e)})
        return None


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

    # Validate output (even short responses)
    is_valid, text = validate_output(text)
    if not is_valid:
        return None

    return text


def _write_brain_debug(message_id: str, conversation_id: str, model: str,
                       context_size: int, system: str, messages: list,
                       debug_components: dict) -> None:
    """Write full LLM call details to a YAML file (JOI_BRAIN_DEBUG=1 only)."""
    import yaml
    import pathlib
    now = datetime.now()
    dirpath = pathlib.Path(BRAIN_DEBUG_DIR)
    dirpath.mkdir(parents=True, exist_ok=True)
    ts_str = now.strftime("%Y-%m-%d_%H-%M-%S-") + f"{now.microsecond // 1000:03d}"
    filename = dirpath / f"{ts_str}_{message_id[:8]}.yaml"
    data = {
        "timestamp": now.isoformat(),
        "message_id": message_id,
        "conversation_id": conversation_id,
        "model": model,
        "message_count": len(messages),
        "context_size": context_size,
        "facts": debug_components.get("facts"),
        "summaries": debug_components.get("summaries"),
        "rag": debug_components.get("rag"),
        "system_prompt": system,
        "messages": messages,
    }
    try:
        with open(filename, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False,
                      default_style=None, width=120)
    except Exception as e:
        logger.warning("brain_debug write failed", extra={"error": str(e)})


def _build_chat_messages(messages: List, is_group: bool = False) -> List[Dict[str, str]]:
    """Convert stored messages to LLM chat format.

    For group conversations, includes sender name prefix so Joi knows who said what.
    """
    # Storage prefixes used by Wind/reminder scheduler — strip before sending to LLM
    _STORAGE_PREFIXES = ("[JOI-WIND] ", "[REMINDER] ")

    chat_messages = []
    for msg in messages:
        if msg.content_text:
            role = "user" if msg.direction == "inbound" else "assistant"

            content = msg.content_text
            # Strip internal storage prefixes from outbound messages
            if role == "assistant":
                for prefix in _STORAGE_PREFIXES:
                    if content.startswith(prefix):
                        content = content[len(prefix):]
                        break

            if role == "user" and is_group:
                # For group messages, prefix with sender name/id
                sender = msg.sender_name or msg.sender_id or "Unknown"
                content = f"[{sender}]: {content}"

            chat_messages.append({"role": role, "content": content})
    return chat_messages


def _build_enriched_prompt(
    base_prompt: str,
    user_message: Optional[str] = None,
    conversation_id: Optional[str] = None,
    knowledge_scopes: Optional[List[str]] = None,
    _debug: Optional[dict] = None,
) -> str:
    """Build system prompt enriched with user facts, summaries, and RAG context for this conversation."""
    parts = [base_prompt]

    # Add user facts for this conversation (FTS search with fallback)
    facts_text = None
    if FACTS_FTS_ENABLED and user_message:
        facts_text = memory.get_facts_as_context(
            user_message,
            max_tokens=FACTS_FTS_MAX_TOKENS,
            min_confidence=0.6,
            conversation_id=conversation_id,
        )
        if facts_text:
            parts.append("\n\n" + facts_text)
            logger.info("Facts FTS: added context", extra={"chars": len(facts_text)})
        else:
            # Fallback: load all facts if FTS returns nothing
            facts_text = memory.get_facts_as_text(min_confidence=0.6, conversation_id=conversation_id)
            if facts_text:
                parts.append("\n\n" + facts_text)
                logger.debug("Facts FTS: fallback to all facts", extra={"chars": len(facts_text)})
    else:
        # FTS disabled: original behavior
        facts_text = memory.get_facts_as_text(min_confidence=0.6, conversation_id=conversation_id)
        if facts_text:
            parts.append("\n\n" + facts_text)
    if _debug is not None:
        _debug["facts"] = {"chars": len(facts_text), "content": facts_text} if facts_text else None

    # Add recently-expired temporal facts (last 7 days) so Joi can answer
    # "what was my schedule yesterday?" and similar temporal queries.
    if conversation_id:
        expired_facts = memory.get_recently_expired_facts(days=7, conversation_id=conversation_id)
        if expired_facts:
            now_ms_ctx = int(time.time() * 1000)
            exp_lines = ["Past events (last 7 days):"]
            for ef in expired_facts:
                age_h = (now_ms_ctx - ef.expires_at) / 3_600_000  # type: ignore[operator]
                if age_h < 24:
                    when = f"expired {int(age_h)}h ago"
                elif age_h < 48:
                    when = "expired yesterday"
                else:
                    when = f"expired {int(age_h / 24)} days ago"
                mentioned = ""
                if ef.detected_at:
                    det_h = (now_ms_ctx - ef.detected_at) / 3_600_000
                    if det_h < 24:
                        mentioned = f", mentioned {int(det_h)}h ago"
                    elif det_h < 48:
                        mentioned = ", mentioned yesterday"
                    else:
                        mentioned = f", mentioned {int(det_h / 24)} days ago"
                exp_lines.append(f"  - {ef.key}: {ef.value} ({when}{mentioned})")
            parts.append("\n\n" + "\n".join(exp_lines))

    # Add recent conversation summaries for this conversation (FTS search with fallback)
    summaries_text = None
    if SUMMARIES_FTS_ENABLED and user_message:
        summaries_text = memory.get_summaries_as_context(
            user_message,
            max_tokens=SUMMARIES_FTS_MAX_TOKENS,
            days=100,
            conversation_id=conversation_id,
        )
        if summaries_text:
            parts.append("\n\n" + summaries_text)
            logger.info("Summaries FTS: added context", extra={"chars": len(summaries_text)})
        else:
            # Fallback: load recent summaries if FTS returns nothing
            summaries_text = memory.get_summaries_as_text(days=10, conversation_id=conversation_id, max_chars=SUMMARIES_FTS_MAX_TOKENS * 4)
            if summaries_text:
                parts.append("\n\n" + summaries_text)
                logger.debug("Summaries FTS: fallback to recent", extra={"chars": len(summaries_text)})
    else:
        # FTS disabled: original behavior
        summaries_text = memory.get_summaries_as_text(days=10, conversation_id=conversation_id, max_chars=SUMMARIES_FTS_MAX_TOKENS * 4)
        if summaries_text:
            parts.append("\n\n" + summaries_text)
    if _debug is not None:
        _debug["summaries"] = {"chars": len(summaries_text), "content": summaries_text} if summaries_text else None

    # Add RAG context if enabled and user message provided
    rag_context = None
    if RAG_ENABLED and user_message:
        logger.debug("RAG lookup", extra={"query": user_message[:50], "scopes": knowledge_scopes})
        rag_context = memory.get_knowledge_as_context(
            user_message,
            max_tokens=RAG_MAX_TOKENS,
            scopes=knowledge_scopes,
        )
        if rag_context:
            parts.append("\n\n" + rag_context)
            logger.info("RAG: added context", extra={"chars": len(rag_context)})
        else:
            logger.info("RAG: no matches for query")
    if _debug is not None:
        _debug["rag"] = {"chars": len(rag_context), "content": rag_context} if rag_context else None

    # Add current datetime if time awareness is enabled
    if TIME_AWARENESS_ENABLED:
        try:
            tz = ZoneInfo(TIME_AWARENESS_TIMEZONE)
        except Exception:
            tz = ZoneInfo("UTC")
        now = datetime.now(tz)
        # Format: "Wednesday, February 26, 2026, 14:30, Europe/Bratislava"
        human_datetime = now.strftime('%A, %B %d, %Y, %H:%M') + f", {TIME_AWARENESS_TIMEZONE}"
        parts.append(f"\n\nCurrent date and time: {human_datetime}")

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
        logger.error("Consolidation error", extra={"error": str(e)})


def _start_typing_refresh(msg: InboundMessage, interval: float = 10.0):
    """
    Start a background thread that re-sends the typing indicator every interval seconds.

    Signal typing indicators expire after ~15s. Call the returned stop function
    when the response is ready to cancel the refresh loop.

    Returns a stop callable.
    """
    stop_event = threading.Event()

    def _loop():
        while not stop_event.wait(interval):
            _send_typing_indicator(msg)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return stop_event.set


def _send_typing_indicator(msg: InboundMessage) -> None:
    """Send a typing indicator to Signal via Mesh. Fire-and-forget."""
    url = f"{settings.mesh_url}/api/v1/typing"
    payload = {
        "recipient": {
            "id": msg.sender.id,
            "transport_id": msg.sender.transport_id,
        },
        "delivery": {
            "target": msg.conversation.type,
            "group_id": msg.conversation.id if msg.conversation.type == "group" else None,
        },
    }
    try:
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        current_secret = _get_current_hmac_secret()
        if current_secret:
            hmac_headers = create_request_headers(body, current_secret)
            headers.update(hmac_headers)
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(url, content=body, headers=headers)
        if resp.status_code != 200:
            logger.warning("Typing indicator rejected by mesh", extra={
                "status_code": resp.status_code,
                "action": "typing_indicator",
            })
        else:
            logger.debug("Typing indicator sent", extra={"action": "typing_indicator"})
    except Exception as exc:
        logger.warning("Typing indicator failed", extra={"error": str(exc), "action": "typing_indicator"})


def _handle_wind_snooze_command(text: str, conversation_id: str) -> Optional[str]:
    """Return a confirmation string if text is a Wind snooze/clear command, else None."""
    if len(text.split()) > 8:
        return None

    if _SNOOZE_CLEAR.search(text):
        wind_orchestrator.clear_snooze(conversation_id)
        return "Wind resumed."

    if not _SNOOZE_TRIGGER.search(text):
        return None

    now = datetime.now()  # naive local, consistent with Wind's internal datetime convention

    if _DURATION_TONIGHT.search(text):
        tz = ZoneInfo(wind_orchestrator.config.timezone)
        end_hour = wind_orchestrator.config.quiet_hours_end
        now_aware = datetime.now(tz)
        candidate = now_aware.replace(hour=end_hour, minute=0, second=0, microsecond=0)
        if candidate <= now_aware:
            candidate += timedelta(days=1)
        # convert to server-local naive so Wind's naive comparisons work
        until = candidate.astimezone().replace(tzinfo=None)
    elif m := _DURATION_HOURS.search(text):
        until = now + timedelta(hours=min(int(m.group(1)), 168))
    elif m := _DURATION_MINS.search(text):
        until = now + timedelta(minutes=max(5, min(int(m.group(1)), 10080)))
    elif m := _DURATION_DAYS.search(text):
        until = now + timedelta(days=min(int(m.group(1)), 7))
    else:
        until = now + timedelta(hours=4)

    wind_orchestrator.snooze(conversation_id, until=until)

    delta = until - now
    total_minutes = int(delta.total_seconds() / 60)
    if total_minutes >= 1440:
        label = f"{total_minutes // 1440}d"
    elif total_minutes >= 60:
        label = f"{total_minutes // 60}h"
    else:
        label = f"{total_minutes}m"

    return f"Wind snoozed for {label}."


def _handle_reminder_snooze_command(text: str, conversation_id: str) -> Optional[str]:
    """
    Return a confirmation string if text is a post-fire reminder snooze, else None.

    Only matches if a reminder fired recently (within 2h) — avoids stealing
    new reminder creation requests like "remind me in 1h".
    """
    if not _REMINDER_SNOOZE_TRIGGER.search(text):
        return None

    last = reminder_manager.get_last_fired(conversation_id)
    if not last or last.fired_at is None:
        return None
    if (datetime.now() - last.fired_at).total_seconds() > 7200:
        return None

    now = datetime.now()
    if m := _DURATION_HOURS.search(text):
        new_due = now + timedelta(hours=min(int(m.group(1)), 168))
    elif m := _DURATION_MINS.search(text):
        new_due = now + timedelta(minutes=max(5, min(int(m.group(1)), 10080)))
    elif m := _DURATION_DAYS.search(text):
        new_due = now + timedelta(days=min(int(m.group(1)), 7))
    else:
        new_due = now + timedelta(hours=1)

    reminder_manager.snooze(last.id, new_due)

    delta = new_due - now
    total_minutes = int(delta.total_seconds() / 60)
    if total_minutes >= 1440:
        label = f"{total_minutes // 1440}d"
    elif total_minutes >= 60:
        label = f"{total_minutes // 60}h"
    else:
        label = f"{total_minutes}m"

    return f"Reminder snoozed for {label}. I'll remind you about \"{last.title}\" then."


def _handle_reminder_command(text: str, conversation_id: str) -> Optional[str]:
    """
    Return a confirmation string if text is a reminder command, else None.

    Recognizes: "remind me in 5m to check the oven"
                "remind me tonight to call mom"
                "remind me in 2h about the meeting"
    """
    if not _REMINDER_TRIGGER.search(text):
        return None

    if len(text.split()) > 25:
        return None

    now = datetime.now(timezone.utc)
    time_end = -1

    if m := _DURATION_TONIGHT.search(text):
        tz = ZoneInfo(TIME_AWARENESS_TIMEZONE)
        now_local = now.astimezone(tz)
        # "Tonight" = 9pm local time
        candidate = now_local.replace(hour=21, minute=0, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
        due_at = candidate.astimezone(timezone.utc)
        time_end = m.end()
    elif m := _DURATION_HOURS.search(text):
        due_at = now + timedelta(hours=min(int(m.group(1)), 168))
        time_end = m.end()
    elif m := _DURATION_MINS.search(text):
        due_at = now + timedelta(minutes=max(1, min(int(m.group(1)), 10080)))
        time_end = m.end()
    elif m := _DURATION_DAYS.search(text):
        due_at = now + timedelta(days=min(int(m.group(1)), 365))
        time_end = m.end()
    else:
        return None  # No time expression found

    # Find "to/about" keyword after the time expression to extract the title
    about_match = _REMINDER_ABOUT.search(text, pos=max(0, time_end))
    if not about_match:
        # Fall back: try anywhere in the text
        about_match = _REMINDER_ABOUT.search(text)
        if not about_match:
            return None

    title = text[about_match.end():].strip().strip("., ")
    if not title:
        return None

    # Set reminder with 24h expiry for one-shots
    expires_at = now + timedelta(hours=24)
    reminder_manager.add(
        conversation_id=conversation_id,
        title=title,
        due_at=due_at,
        expires_at=expires_at,
    )

    delta = due_at - now
    total_minutes = int(delta.total_seconds() / 60)
    if total_minutes >= 1440:
        label = f"{total_minutes // 1440}d"
    elif total_minutes >= 60:
        label = f"{total_minutes // 60}h"
    else:
        label = f"{total_minutes}m"

    return f"Reminder set for {label}."


def _handle_reschedule_intent(text: str, conversation_id: str) -> bool:
    """
    Detect and handle a reschedule intent in a user message.

    Uses a focused LLM extraction call to identify the fact to reschedule and
    the new time (as ttl_hours). Updates the DB if a match is found.

    Returns True if a fact was successfully rescheduled (DB updated), False otherwise.
    Falls through on any error — the main LLM response handles clarification.
    """
    if not _RESCHEDULE_TRIGGER.search(text):
        return False

    # Get current temporal facts for this conversation
    temporal_facts = [
        f for f in memory.get_facts(conversation_id=conversation_id, include_expired=True)
        if f.expires_at is not None
    ]
    if not temporal_facts:
        return False

    facts_list = "\n".join(
        f"  - id={f.id} key={f.key}: {f.value}" for f in temporal_facts
    )

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    extraction_prompt = f"""The user wants to reschedule something. Current time: {now_iso}

User message: "{text}"

Current temporal facts:
{facts_list}

If the user is rescheduling one of these facts, respond with JSON:
{{"fact_id": <id>, "ttl_hours": <hours from now until new time>}}

If the new time is ambiguous or no fact matches, respond with: null

Only valid JSON or null. No explanation."""

    try:
        response = llm.generate(prompt=extraction_prompt)
        if response.error or not response.text:
            return False

        raw = response.text.strip()
        if raw.lower() == "null" or not raw.startswith("{"):
            return False

        import json as _json
        data = _json.loads(raw)
        fact_id = int(data["fact_id"])
        ttl_hours = float(data["ttl_hours"])
        if ttl_hours <= 0 or ttl_hours > 8760:  # cap at 1 year
            return False

        rescheduled = memory.reschedule_fact(fact_id=fact_id, conversation_id=conversation_id, ttl_hours=ttl_hours)
        if rescheduled:
            logger.info("Rescheduled fact via NLU", extra={
                "fact_id": fact_id,
                "ttl_hours": ttl_hours,
                "action": "fact_rescheduled",
            })
        return rescheduled
    except Exception as e:
        logger.debug("Reschedule intent extraction failed", extra={"error": str(e)})
        return False


def _generate_reminder_message(
    title: str,
    conversation_id: str,
    is_recurring: bool = False,
    snooze_count: int = 0,
) -> Optional[str]:
    """
    Generate a reminder message using an injection-safe prompt.

    The reminder title is user-supplied data and is wrapped in triple-quotes
    to prevent prompt injection.
    """
    base_prompt = "You are Joi, an AI assistant."

    facts_text = memory.get_facts_as_text(min_confidence=0.6, conversation_id=conversation_id)
    system_parts = [base_prompt]
    if facts_text:
        system_parts.append(f"\n\n{facts_text}")
    system_prompt = "".join(system_parts)

    # Build context notes
    context_notes = []
    if is_recurring:
        context_notes.append("This is a recurring reminder.")
    if snooze_count > 0:
        context_notes.append(f"The user snoozed this {snooze_count} time(s) before.")
    context_line = (" " + " ".join(context_notes)) if context_notes else ""

    user_prompt = f"""The user asked you to remind them about this at this time:
\"\"\"
{title}
\"\"\"

Deliver the reminder now in 1-2 sentences.{context_line}
Rules:
- Say it's time / they asked you to remind them — keep it simple and direct
- Do NOT say you forgot, apologize, or reference shared experiences
- Do NOT add relationship commentary or emotional context
- The tone can be warm, dry, or light — match what you know about the user
- No greetings like "Hey!" or "Hi!"

Just the message, nothing else."""

    try:
        response = llm.chat(
            messages=[{"role": "user", "content": user_prompt}],
            system=system_prompt,
        )
        if response.error or not response.text:
            logger.warning("Reminder: LLM generation failed", extra={"error": response.error})
            return None

        text = response.text.strip()

        is_valid, text = validate_output(text)
        if not is_valid:
            logger.warning("Reminder: generated message failed validation")
            return None

        if len(text) > 500:
            text = text[:500]

        logger.info("Reminder: generated message", extra={
            "length": len(text),
            "title": title[:30],
            "action": "reminder_generate",
        })
        return text

    except Exception as e:
        logger.error("Reminder: message generation error", extra={"error": str(e)})
        return None


def _send_to_mesh(
    recipient_id: str,
    recipient_transport_id: str,
    conversation: InboundConversation,
    text: str,
    reply_to: Optional[str] = None,
    is_critical: bool = False,
) -> bool:
    """Send a message back to mesh for delivery via Signal."""
    # Check outbound rate limit (critical messages bypass)
    allowed, reason = outbound_limiter.check_and_record(is_critical=is_critical)
    if not allowed:
        logger.warning("Outbound blocked by rate limit", extra={"reason": reason})
        return False

    # Enforce cooldown between sends to same conversation
    # Uses per-conversation locks so different conversations don't block each other
    convo_id = conversation.id
    cooldown = RESPONSE_COOLDOWN_GROUP_SECONDS if conversation.type == "group" else RESPONSE_COOLDOWN_DM_SECONDS
    now = time.time()
    with _get_send_lock(convo_id):
        last_send = _last_send_times.get(convo_id, 0)
        elapsed = now - last_send
        if elapsed < cooldown:
            wait_time = cooldown - elapsed
            logger.debug("Cooldown: waiting before sending", extra={"wait_seconds": round(wait_time, 1), "conversation_id": convo_id})
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
            logger.info("Sent response to mesh successfully", extra={"action": "mesh_send"})
            # Record outbound for Wind state tracking
            wind_orchestrator.record_outbound(conversation.id)
            return True
        else:
            logger.error("Mesh returned error", extra={"error": data.get("error")})
            return False

    except Exception as exc:
        logger.error("Failed to send to mesh", extra={"error": str(exc)})
        return False


# --- Main ---

def _validate_models():
    configured = {"JOI_OLLAMA_MODEL": settings.ollama_model}
    for var in ("JOI_CONSOLIDATION_MODEL", "JOI_ENGAGEMENT_MODEL",
                "JOI_CURIOSITY_MODEL", "JOI_EMBEDDING_MODEL"):
        val = os.getenv(var)
        if val:
            configured[var] = val

    available = llm.list_models()  # raises RuntimeError if Ollama unreachable

    missing = [f"{var}={name}" for var, name in configured.items() if name not in available]
    if missing:
        raise ValueError(
            f"Ollama model(s) not found: {', '.join(missing)}. "
            f"Available: {sorted(available)}"
        )


def main():
    import uvicorn

    from config.prompts import PROMPTS_DIR

    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logger.info("Starting Joi API", extra={
        "bind_host": settings.bind_host,
        "bind_port": settings.bind_port,
        "log_level": settings.log_level,
        "action": "main_start"
    })
    logger.info("Ollama config", extra={
        "url": settings.ollama_url,
        "model": settings.ollama_model,
        "num_ctx": settings.ollama_num_ctx if settings.ollama_num_ctx > 0 else None
    })
    logger.info("Service config", extra={
        "mesh_url": settings.mesh_url,
        "memory_db": os.getenv("JOI_MEMORY_DB", "/var/lib/joi/memory.db"),
        "context_messages": CONTEXT_MESSAGE_COUNT,
        "prompts_dir": str(PROMPTS_DIR),
        "cooldown_dm": RESPONSE_COOLDOWN_DM_SECONDS,
        "cooldown_group": RESPONSE_COOLDOWN_GROUP_SECONDS
    })

    if BRAIN_DEBUG:
        logger.warning("=" * 70)
        logger.warning("  *** JOI_BRAIN_DEBUG IS ENABLED ***")
        logger.warning("  Writing full LLM payloads (unredacted) to: %s", BRAIN_DEBUG_DIR)
        logger.warning("  DISABLE THIS IN PRODUCTION")
        logger.warning("=" * 70)

    _validate_models()

    uvicorn.run(
        app,
        host=settings.bind_host,
        port=settings.bind_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
