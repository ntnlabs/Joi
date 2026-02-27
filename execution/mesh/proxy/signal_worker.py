import hashlib
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, request

from config import load_settings
from forwarder import forward_to_joi, forward_document_to_joi, set_config_state
from hmac_auth import (
    InMemoryNonceStore,
    get_shared_secret,
    save_shared_secret,
    verify_hmac,
    verify_timestamp,
    DEFAULT_TIMESTAMP_TOLERANCE_MS,
)
from jsonrpc_stdio import JsonRpcStdioClient
from policy import MeshPolicy


logger = logging.getLogger("mesh.signal_worker")

# HMAC authentication
_hmac_secret = get_shared_secret()  # Initial secret from env (fallback)
_nonce_store = InMemoryNonceStore()  # Always create - needed when HMAC becomes available
_hmac_timestamp_tolerance = int(os.getenv("MESH_HMAC_TIMESTAMP_TOLERANCE_MS", str(DEFAULT_TIMESTAMP_TOLERANCE_MS)))

# Document handling configuration
# Note: Only extensions supported by ingestion.py (txt, md)
# Extension-based filtering (MIME types from Signal are unreliable)
# UTF-8 validation provides the real security check
ALLOWED_DOCUMENT_EXTENSIONS = {".txt", ".md"}
EXTENSION_TO_MIME = {".txt": "text/plain", ".md": "text/markdown"}
MAX_DOCUMENT_SIZE_BYTES = int(os.getenv("MESH_MAX_DOCUMENT_SIZE", str(1 * 1024 * 1024)))  # 1MB default
SIGNAL_ATTACHMENTS_DIR = Path(os.getenv("SIGNAL_ATTACHMENTS_DIR", "/var/lib/signal-cli/attachments"))


def _redact_pii(value: str, pii_type: str = "phone") -> str:
    """
    Redact PII when privacy mode is enabled.

    Args:
        value: The value to potentially redact
        pii_type: Type of PII ("phone", "group", "uuid")

    Returns:
        Redacted value if privacy mode, original otherwise
    """
    # Import here to avoid circular dependency at module load
    # _config_state may not be initialized yet
    try:
        if _config_state.is_privacy_mode():
            if pii_type == "phone":
                # Show last 4 digits: +1234567890 -> +***7890
                if len(value) > 4:
                    return f"+***{value[-4:]}"
                return "***"
            elif pii_type == "group":
                # Show first 8 chars of group ID
                return f"[GRP:{value[:8]}...]" if len(value) > 8 else "[GRP:***]"
            elif pii_type == "uuid":
                # Show first 8 chars of UUID
                return f"{value[:8]}..." if len(value) > 8 else "***"
            else:
                return "***"
    except Exception:
        pass  # _config_state not ready yet
    return value


# --- Dedupe Cache ---

class MessageDedupeCache:
    """Thread-safe cache to prevent duplicate message processing."""

    def __init__(self, ttl_seconds: int = 3600, max_size: int = 10000):
        self._cache: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._last_prune = time.time()

    def check_and_add(self, message_id: str) -> bool:
        """
        Check if message_id is a duplicate.
        Returns True if it's a NEW message (not seen before).
        Returns False if it's a DUPLICATE (already processed).
        """
        now = time.time()
        with self._lock:
            # Prune old entries periodically (every 5 minutes)
            if now - self._last_prune > 300:
                self._prune(now)
                self._last_prune = now

            if message_id in self._cache:
                return False  # Duplicate

            self._cache[message_id] = now
            return True  # New message

    def _prune(self, now: float) -> None:
        """Remove entries older than TTL."""
        cutoff = now - self._ttl
        expired = [k for k, v in self._cache.items() if v < cutoff]
        for k in expired:
            del self._cache[k]

        # If still too large, remove oldest entries
        if len(self._cache) > self._max_size:
            sorted_items = sorted(self._cache.items(), key=lambda x: x[1])
            to_remove = len(self._cache) - self._max_size
            for k, _ in sorted_items[:to_remove]:
                del self._cache[k]

        if expired:
            logger.debug("Pruned %d expired dedupe entries", len(expired))


# --- Delivery Tracker ---

class DeliveryTracker:
    """Track sent messages and their delivery status."""

    def __init__(self, ttl_seconds: int = 86400, max_size: int = 10000):
        # timestamp -> {message_id, recipient, sent_at, delivered_at, read_at}
        self._messages: Dict[int, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._max_size = max_size

    def register_sent(self, timestamp: int, recipient: str, message_id: Optional[str] = None) -> None:
        """Register a sent message for delivery tracking."""
        with self._lock:
            self._prune()
            self._messages[timestamp] = {
                "message_id": message_id,
                "recipient": recipient,
                "sent_at": int(time.time() * 1000),
                "delivered_at": None,
                "read_at": None,
            }
            logger.debug("Tracking message ts=%d to %s", timestamp, _redact_pii(recipient, "phone"))

    def mark_delivered(self, timestamps: List[int]) -> int:
        """Mark messages as delivered. Returns count of newly marked."""
        count = 0
        now = int(time.time() * 1000)
        with self._lock:
            for ts in timestamps:
                if ts in self._messages and self._messages[ts]["delivered_at"] is None:
                    self._messages[ts]["delivered_at"] = now
                    count += 1
                    logger.info("Message ts=%d delivered", ts)
        return count

    def mark_read(self, timestamps: List[int]) -> int:
        """Mark messages as read. Returns count of newly marked."""
        count = 0
        now = int(time.time() * 1000)
        with self._lock:
            for ts in timestamps:
                if ts in self._messages and self._messages[ts]["read_at"] is None:
                    self._messages[ts]["read_at"] = now
                    # Also mark as delivered if not already
                    if self._messages[ts]["delivered_at"] is None:
                        self._messages[ts]["delivered_at"] = now
                    count += 1
                    logger.info("Message ts=%d read", ts)
        return count

    def get_status(self, timestamp: int) -> Optional[Dict[str, Any]]:
        """Get delivery status for a message."""
        with self._lock:
            return self._messages.get(timestamp)

    def get_all_status(self) -> Dict[int, Dict[str, Any]]:
        """Get all tracked messages (for debugging)."""
        with self._lock:
            return dict(self._messages)

    def _prune(self) -> None:
        """Remove old entries."""
        now = time.time()
        cutoff = int((now - self._ttl) * 1000)
        expired = [ts for ts, data in self._messages.items() if data["sent_at"] < cutoff]
        for ts in expired:
            del self._messages[ts]

        if len(self._messages) > self._max_size:
            sorted_items = sorted(self._messages.items(), key=lambda x: x[1]["sent_at"])
            to_remove = len(self._messages) - self._max_size
            for ts, _ in sorted_items[:to_remove]:
                del self._messages[ts]


_delivery_tracker = DeliveryTracker()


# --- Config State for Joi-pushed config (memory-only, no disk) ---


class ConfigState:
    """
    Thread-safe config state management for pushed config from Joi.

    Memory-only - no disk persistence. Mesh is stateless, always waits for Joi push.

    SECURITY: Joi is authoritative. Mesh starts clean, receives config from Joi.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._config: Dict[str, Any] = {}
        self._config_hash: str = ""
        self._last_update: float = 0

        # HMAC rotation support (in-memory only)
        self._old_hmac_secret: Optional[bytes] = None
        self._old_hmac_expires: float = 0
        self._new_hmac_secret: Optional[bytes] = None

        # Reference to MeshPolicy instance (set by main)
        self._mesh_policy: Optional[MeshPolicy] = None

        # Security flags (pushed from Joi)
        self._privacy_mode: bool = False
        self._kill_switch: bool = False

    def set_mesh_policy(self, policy: MeshPolicy) -> None:
        """Set reference to MeshPolicy instance for reloading."""
        self._mesh_policy = policy

    def apply_config(self, config: Dict[str, Any]) -> str:
        """
        Apply new config from Joi (memory only, no disk persistence).

        Returns config hash.
        """
        with self._lock:
            # Handle HMAC rotation if present
            rotation = config.pop("hmac_rotation", None)
            if rotation:
                self._handle_hmac_rotation(rotation)

            # Handle security flags (extract before storing)
            security = config.get("security", {})
            old_kill_switch = self._kill_switch
            self._privacy_mode = bool(security.get("privacy_mode", False))
            self._kill_switch = bool(security.get("kill_switch", False))

            # Log security flag changes
            if self._kill_switch and not old_kill_switch:
                logger.warning("KILL SWITCH ACTIVATED - forwarding to Joi disabled")
            elif not self._kill_switch and old_kill_switch:
                logger.info("Kill switch deactivated - forwarding resumed")

            if self._privacy_mode:
                logger.info("Privacy mode enabled - PII will be redacted in logs")

            # Remove timestamp_ms before storing (it's metadata, not config)
            config.pop("timestamp_ms", None)

            self._config = config
            self._config_hash = self._compute_hash(config)
            self._last_update = time.time()

            # Update MeshPolicy in memory (no disk)
            if self._mesh_policy:
                self._mesh_policy.update_from_config(config)

            return self._config_hash

    def is_kill_switch_active(self) -> bool:
        """Check if kill switch is active (forwarding disabled)."""
        with self._lock:
            return self._kill_switch

    def is_privacy_mode(self) -> bool:
        """Check if privacy mode is enabled (PII redaction)."""
        with self._lock:
            return self._privacy_mode

    def _handle_hmac_rotation(self, rotation: Dict[str, Any]) -> None:
        """Handle HMAC key rotation from config push."""
        new_secret_hex = rotation.get("new_secret")
        grace_period_ms = rotation.get("grace_period_ms", 60000)

        if new_secret_hex:
            # Save current secret as old (for grace period)
            # Use in-memory key if set (from previous rotation), otherwise env
            self._old_hmac_secret = self._new_hmac_secret if self._new_hmac_secret else get_shared_secret()
            self._old_hmac_expires = time.time() + (grace_period_ms / 1000)

            # Store new secret in memory for immediate use
            self._new_hmac_secret = bytes.fromhex(new_secret_hex)

            # Persist to file for restart recovery
            save_shared_secret(new_secret_hex)

            logger.info(
                "HMAC rotation: new key active, old key valid for %ds",
                grace_period_ms // 1000,
            )

    def _compute_hash(self, config: Dict[str, Any]) -> str:
        """Compute SHA256 hash of config."""
        normalized = json.dumps(config, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def get_hash(self) -> str:
        """Get current config hash."""
        with self._lock:
            return self._config_hash

    def get_hmac_secrets(self) -> Tuple[bytes, Optional[bytes]]:
        """
        Get HMAC secrets for verification.

        Returns (current_secret, old_secret_if_in_grace_period).
        During rotation, both keys are valid.
        """
        with self._lock:
            # If we have a new secret that hasn't been loaded into module-level yet
            current = self._new_hmac_secret if self._new_hmac_secret else get_shared_secret()

            # Check if old secret is still valid (grace period)
            if self._old_hmac_secret and time.time() < self._old_hmac_expires:
                return current, self._old_hmac_secret

            return current, None


_config_state = ConfigState()


# Global RPC client (shared between receiver thread and HTTP server)
_rpc: Optional[JsonRpcStdioClient] = None
_rpc_lock = threading.Lock()
_account: str = ""
_dedupe_cache = MessageDedupeCache()


# --- Flask app for outbound API ---
flask_app = Flask("mesh-outbound")
flask_app.logger.setLevel(logging.WARNING)  # Quiet Flask logs


def _is_hmac_available() -> bool:
    """Check if HMAC authentication is available (dynamic check)."""
    # Check config_state for rotated secret
    try:
        current, _ = _config_state.get_hmac_secrets()
        if current:
            return True
    except Exception:
        pass

    # Fall back to module-level or env
    return get_shared_secret() is not None


@flask_app.before_request
def verify_hmac_auth():
    """Verify HMAC authentication for incoming requests from Joi."""
    # Skip health and read-only status endpoints
    if request.path in ("/health", "/config/status"):
        return None

    # Check if HMAC is available (dynamic check - not just startup state)
    if not _is_hmac_available():
        # Fail-closed: reject if no HMAC configured
        logger.error("HMAC auth failed: no secret configured (fail-closed)")
        return jsonify({"status": "error", "error": {"code": "hmac_not_configured", "message": "HMAC authentication not configured"}}), 503

    # Extract headers
    nonce = request.headers.get("X-Nonce")
    timestamp_str = request.headers.get("X-Timestamp")
    signature = request.headers.get("X-HMAC-SHA256")

    # Check all required headers present
    if not all([nonce, timestamp_str, signature]):
        logger.warning("HMAC auth failed: missing headers")
        return jsonify({"status": "error", "error": {"code": "hmac_missing_headers", "message": "Missing authentication headers"}}), 401

    # Parse timestamp
    try:
        timestamp = int(timestamp_str)
    except ValueError:
        logger.warning("HMAC auth failed: invalid timestamp format")
        return jsonify({"status": "error", "error": {"code": "hmac_invalid_timestamp", "message": "Invalid timestamp format"}}), 401

    # Verify timestamp freshness
    ts_valid, ts_error = verify_timestamp(timestamp, _hmac_timestamp_tolerance)
    if not ts_valid:
        logger.warning("HMAC auth failed: %s", ts_error)
        return jsonify({"status": "error", "error": {"code": ts_error, "message": "Request timestamp out of tolerance"}}), 401

    # Verify nonce not replayed
    nonce_valid, nonce_error = _nonce_store.check_and_store(nonce, source="joi")
    if not nonce_valid:
        logger.warning("HMAC auth failed: %s nonce=%s", nonce_error, nonce[:8])
        return jsonify({"status": "error", "error": {"code": nonce_error, "message": "Nonce already used"}}), 401

    # Get raw body for HMAC verification
    body = request.get_data()

    # Verify HMAC signature - try current key first, then old key during grace period
    current_secret, old_secret = _config_state.get_hmac_secrets()

    # Use module-level secret if config state doesn't have one yet
    if current_secret is None:
        current_secret = _hmac_secret

    if verify_hmac(nonce, timestamp, body, signature, current_secret):
        logger.debug("HMAC auth passed for %s (current key)", request.path)
        return None

    # Try old key during grace period
    if old_secret and verify_hmac(nonce, timestamp, body, signature, old_secret):
        logger.info("HMAC auth passed for %s (grace period key)", request.path)
        return None

    logger.warning("HMAC auth failed: invalid signature")
    return jsonify({"status": "error", "error": {"code": "hmac_invalid_signature", "message": "Invalid HMAC signature"}}), 401


@flask_app.route("/health", methods=["GET"])
def health():
    hmac_status = "enabled" if _is_hmac_available() else "disabled"
    return jsonify({"status": "ok", "mode": "worker", "hmac": hmac_status})


@flask_app.route("/api/v1/delivery/status", methods=["GET"])
def delivery_status():
    """Query delivery status for a message by timestamp."""
    ts_str = request.args.get("timestamp")
    if not ts_str:
        return jsonify({"status": "error", "error": "missing_timestamp"}), 400

    try:
        ts = int(ts_str)
    except ValueError:
        return jsonify({"status": "error", "error": "invalid_timestamp"}), 400

    status = _delivery_tracker.get_status(ts)
    if status is None:
        return jsonify({"status": "ok", "data": None, "message": "not_tracked"})

    return jsonify({
        "status": "ok",
        "data": {
            "timestamp": ts,
            "delivered": status["delivered_at"] is not None,
            "read": status["read_at"] is not None,
            "delivered_at": status["delivered_at"],
            "read_at": status["read_at"],
            "sent_at": status["sent_at"],
        }
    })


@flask_app.route("/config/sync", methods=["POST"])
def config_sync():
    """
    Receive config push from Joi.

    Joi is authoritative for policy config. This endpoint accepts
    pushed config, persists it, and reloads MeshPolicy.

    HMAC authentication is required (verified by before_request middleware).
    """
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "invalid_json"}), 400

    # Validate required fields
    if "identity" not in data:
        return jsonify({"status": "error", "error": "missing_identity"}), 400

    # Apply config and get hash
    try:
        config_hash = _config_state.apply_config(data)
    except Exception as e:
        logger.error("Config sync failed: %s", e)
        return jsonify({"status": "error", "error": "apply_failed"}), 500

    logger.info("Config sync applied, hash=%s", config_hash[:16])

    return jsonify({
        "status": "ok",
        "data": {
            "config_hash": config_hash,
            "applied_at": int(time.time() * 1000),
        }
    })


@flask_app.route("/config/status", methods=["GET"])
def config_status():
    """Get current config sync status."""
    return jsonify({
        "status": "ok",
        "data": {
            "config_hash": _config_state.get_hash(),
        }
    })


def _list_groups() -> List[Dict]:
    """Query signal-cli for all groups and their members."""
    global _rpc
    with _rpc_lock:
        if _rpc is None:
            logger.warning("Cannot list groups: RPC not ready")
            return []
        try:
            result = _rpc.call("listGroups", {}, timeout=30.0)
            if "error" in result:
                logger.warning("listGroups error: %s", result["error"])
                return []
            return result.get("result", [])
        except Exception as exc:
            logger.error("listGroups failed: %s", exc)
            return []


@flask_app.route("/groups/members", methods=["GET"])
def get_group_members():
    """Return all groups with their member lists.

    Returns both phone numbers and UUIDs for each member to handle
    ID format mismatches between signal-cli and message sender IDs.
    """
    groups = _list_groups()
    result = {}
    for g in groups:
        group_id = g.get("id")
        members = g.get("members", [])
        if group_id and members:
            # Extract ALL member identifiers (both number and uuid)
            # This ensures matching works regardless of ID format
            member_ids = set()  # Use set to deduplicate
            for m in members:
                if isinstance(m, dict):
                    # Add both number and uuid if available
                    number = m.get("number")
                    uuid_id = m.get("uuid")
                    if number:
                        member_ids.add(number)
                    if uuid_id:
                        member_ids.add(uuid_id)
                elif isinstance(m, str) and m:
                    member_ids.add(m)
            if member_ids:
                result[group_id] = list(member_ids)
    return jsonify({"status": "ok", "data": result})


@flask_app.route("/api/v1/message/outbound", methods=["POST"])
def send_outbound():
    """Handle outbound messages from Joi."""
    global _rpc, _account

    # Check kill switch - block all message sending when active
    if _config_state.is_kill_switch_active():
        logger.warning("Kill switch active - blocking outbound message")
        return jsonify({"status": "error", "error": "kill_switch_active"}), 503

    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "invalid_json"}), 400

    # Extract fields from request (per api-contracts.md)
    recipient = data.get("recipient", {})
    transport_id = recipient.get("transport_id")
    content = data.get("content", {})
    text = content.get("text")
    delivery = data.get("delivery", {})
    target = delivery.get("target", "direct")
    group_id = delivery.get("group_id")

    if not transport_id and target != "group":
        return jsonify({"status": "error", "error": "missing_recipient"}), 400
    if not text:
        return jsonify({"status": "error", "error": "missing_text"}), 400

    # Build signal-cli payload
    payload: Dict[str, Any] = {
        "account": _account,
        "message": text,
    }

    if target == "group":
        if not group_id:
            return jsonify({"status": "error", "error": "missing_group_id"}), 400
        payload["groupId"] = group_id
    else:
        payload["recipients"] = [transport_id]

    # Send via signal-cli
    with _rpc_lock:
        if _rpc is None:
            return jsonify({"status": "error", "error": "rpc_not_ready"}), 503
        try:
            result = _rpc.call("send", payload, timeout=30.0)
        except Exception as exc:
            logger.error("Signal send failed: %s", exc)
            return jsonify({"status": "error", "error": str(exc)}), 500

    if "error" in result:
        logger.warning("Signal send error: %s", result["error"])
        return jsonify({"status": "error", "error": str(result["error"])}), 500

    # Extract timestamp from result
    sent_at = None
    res = result.get("result")
    if isinstance(res, dict):
        sent_at = res.get("timestamp")
    elif isinstance(res, list) and res:
        sent_at = res[0].get("timestamp")

    # Track for delivery confirmation
    if sent_at:
        recipient_id = group_id if target == "group" else transport_id
        _delivery_tracker.register_sent(sent_at, recipient_id)

    recipient_display = _redact_pii(transport_id, "phone") if transport_id else _redact_pii(group_id, "group")
    logger.info("Sent message to %s (ts=%s)", recipient_display, sent_at)
    return jsonify({
        "status": "ok",
        "data": {
            "message_id": str(sent_at) if sent_at else None,
            "transport": "signal",
            "sent_at": sent_at,
            "delivered": False,  # Will be updated async via receipts
        }
    })


# --- Helper functions ---

def _handle_receipt_message(raw: Dict[str, Any]) -> bool:
    """
    Handle receipt messages (delivery/read confirmations).
    Returns True if this was a receipt message, False otherwise.
    """
    envelope = _as_dict(raw.get("envelope"))
    if not envelope:
        return False

    receipt = _as_dict(envelope.get("receiptMessage"))
    if not receipt:
        return False

    # Signal-cli uses boolean fields: isDelivery, isRead, isViewed
    is_delivery = receipt.get("isDelivery", False)
    is_read = receipt.get("isRead", False)
    is_viewed = receipt.get("isViewed", False)  # For media messages

    timestamps = receipt.get("timestamps", [])

    logger.debug(
        "Receipt: isDelivery=%s isRead=%s isViewed=%s timestamps=%s",
        is_delivery, is_read, is_viewed, timestamps
    )

    if not timestamps:
        return True  # It's a receipt but no timestamps to process

    if not isinstance(timestamps, list):
        timestamps = [timestamps]

    # Convert to ints
    timestamps = [int(ts) for ts in timestamps if isinstance(ts, (int, float))]

    if is_delivery:
        count = _delivery_tracker.mark_delivered(timestamps)
        if count > 0:
            logger.info("Processed %d delivery receipt(s)", count)
    if is_read or is_viewed:
        count = _delivery_tracker.mark_read(timestamps)
        if count > 0:
            logger.info("Processed %d read receipt(s)", count)

    return True


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list_of_dicts(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    return []


def _extract_messages(notifications: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    for item in notifications:
        if item.get("method") != "receive":
            continue
        params = item.get("params")
        if isinstance(params, dict):
            result = params.get("result")
            if isinstance(result, list):
                messages.extend(_as_list_of_dicts(result))
            elif "envelope" in params:
                messages.append(params)
        elif isinstance(params, list):
            messages.extend(_as_list_of_dicts(params))
    return messages


def _extract_message_text(data_message: Dict[str, Any]) -> Optional[str]:
    text = data_message.get("message")
    if isinstance(text, str):
        stripped = text.strip()
        if stripped:
            return stripped
    return None


def _check_bot_mentioned(data_message: Dict[str, Any], bot_account: str, bot_uuid: str = "") -> bool:
    """Check if the bot is mentioned in the message.

    Checks both phone number and UUID since Signal mentions may use either.
    """
    mentions = data_message.get("mentions")
    message_text = data_message.get("message", "")

    # Debug: if U+FFFC exists, dump entire dataMessage structure to find where mentions are
    if "\ufffc" in str(message_text):
        logger.info("U+FFFC detected! Full dataMessage keys: %s", list(data_message.keys()))
        logger.info("dataMessage content: %s", {k: v for k, v in data_message.items() if k != "message"})

    # Log what we got for debugging
    logger.info("Mentions check: mentions=%s, bot_uuid=%s", mentions, bot_uuid[:8] if bot_uuid else None)
    if not isinstance(mentions, list):
        return False
    for mention in mentions:
        if isinstance(mention, dict):
            logger.info("Checking mention: %s", mention)
            # Check phone number
            number = mention.get("number")
            if number and isinstance(number, str) and number == bot_account:
                return True
            # Check UUID (Signal autocomplete uses UUID)
            uuid = mention.get("uuid")
            if uuid and bot_uuid and isinstance(uuid, str) and uuid == bot_uuid:
                return True
    return False


def _process_attachments(
    data_message: Dict[str, Any],
    sender_id: str,
    conversation_type: str,
    conversation_id: str,
) -> None:
    """
    Process document attachments from a Signal message.

    Validates type/size, reads content, forwards to Joi for ingestion,
    then deletes the attachment file.
    """
    attachments = data_message.get("attachments")
    if not isinstance(attachments, list) or not attachments:
        return

    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue

        filename = attachment.get("filename", "")
        file_size = attachment.get("size", 0)
        attachment_id = attachment.get("id")

        # Check if filename has allowed extension
        # Extension-based filtering is more reliable than MIME type from Signal
        if not filename:
            logger.debug("Skipping attachment without filename")
            continue

        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
            logger.debug("Skipping attachment with unsupported extension: %s", ext)
            continue

        # Check file size
        if file_size > MAX_DOCUMENT_SIZE_BYTES:
            logger.warning(
                "Attachment too large: %d bytes (max %d)",
                file_size, MAX_DOCUMENT_SIZE_BYTES
            )
            continue

        # Find the attachment file
        if not attachment_id:
            logger.warning("Attachment missing ID, cannot locate file")
            continue

        attachment_path = SIGNAL_ATTACHMENTS_DIR / attachment_id
        if not attachment_path.exists():
            logger.warning("Attachment file not found: %s", attachment_path)
            continue

        # Read file content
        try:
            with open(attachment_path, "rb") as f:
                content = f.read()
        except Exception as e:
            logger.error("Failed to read attachment %s: %s", attachment_path, e)
            continue

        # Validate content is valid UTF-8 text (security: extension alone isn't enough)
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning(
                "Attachment rejected: not valid UTF-8 text (filename=%s)",
                filename
            )
            # Delete the invalid file
            try:
                attachment_path.unlink()
            except Exception:
                pass
            continue

        # Determine scope (same logic as fact_key)
        scope = conversation_id  # group_id for groups, sender phone for DMs

        logger.info(
            "Processing document: %s (%d bytes) for scope %s",
            filename, len(content), _redact_pii(scope, "group" if conversation_type == "group" else "phone")
        )

        # Forward to Joi for ingestion
        content_type = EXTENSION_TO_MIME.get(ext, "text/plain")
        try:
            success = forward_document_to_joi(
                filename=filename,
                content=content,
                content_type=content_type,
                scope=scope,
                sender_id=sender_id,
            )
            if not success:
                logger.warning("Document forward to Joi returned failure: %s", filename)
                continue
            logger.info("Document forwarded to Joi: %s", filename)
        except Exception as e:
            logger.error("Failed to forward document to Joi: %s", e)
            continue

        # Delete attachment after successful forward
        try:
            attachment_path.unlink()
            logger.debug("Deleted attachment file: %s", attachment_path)
        except Exception as e:
            logger.warning("Failed to delete attachment %s: %s", attachment_path, e)


def _normalize_signal_message(raw: Dict[str, Any], bot_account: str = "", bot_uuid: str = "") -> Optional[Dict[str, Any]]:
    envelope = _as_dict(raw.get("envelope"))
    if not envelope:
        return None

    # Debug: log envelope source fields (useful for UUID vs phone troubleshooting)
    logger.debug("Envelope source fields: source=%s sourceNumber=%s sourceUuid=%s",
                 envelope.get("source"), envelope.get("sourceNumber"), envelope.get("sourceUuid"))

    data_message = _as_dict(envelope.get("dataMessage"))
    reaction = _as_dict(data_message.get("reaction"))
    message_text = _extract_message_text(data_message)
    bot_mentioned = _check_bot_mentioned(data_message, bot_account, bot_uuid) if bot_account else False

    content_type = "text"
    content_reaction: Optional[str] = None
    if reaction:
        content_type = "reaction"
        emoji = reaction.get("emoji")
        if isinstance(emoji, str):
            content_reaction = emoji
    elif message_text is None:
        # Check if there are attachments - pass through for document processing
        attachments = data_message.get("attachments")
        if isinstance(attachments, list) and attachments:
            content_type = "attachment"
            message_text = ""  # Empty text, but we'll process attachments
        else:
            return None

    # Prefer phone number over UUID for sender identification
    # signal-cli may use "source" (phone), "sourceNumber" (phone), or "sourceUuid" (UUID)
    source = envelope.get("sourceNumber") or envelope.get("source")
    if not isinstance(source, str) or not source:
        # Fallback to UUID if no phone number
        source = envelope.get("sourceUuid") or "unknown"

    timestamp = envelope.get("timestamp")
    if not isinstance(timestamp, int):
        timestamp = int(time.time() * 1000)

    message_id = envelope.get("serverGuid")
    if not isinstance(message_id, str) or not message_id:
        message_id = str(uuid.uuid4())

    group_info = _as_dict(data_message.get("groupInfo"))
    group_id = group_info.get("groupId")
    if isinstance(group_id, str) and group_id:
        conversation_type = "group"
        conversation_id = group_id
    else:
        conversation_type = "direct"
        conversation_id = source

    quote_data = _as_dict(data_message.get("quote"))
    quote: Optional[Dict[str, Any]] = None
    quote_id = quote_data.get("id")
    if isinstance(quote_id, int):
        quote = {"message_id": str(quote_id), "text": None}

    return {
        "transport": "signal",
        "message_id": message_id,
        "sender": {
            "id": "owner",
            "transport_id": source,
            "display_name": envelope.get("sourceName"),
        },
        "conversation": {
            "type": conversation_type,
            "id": conversation_id,
        },
        "priority": "normal",
        "content": {
            "type": content_type,
            "text": message_text,
            "voice_transcription": None,
            "voice_transcription_failed": False,
            "voice_failure_reason": None,
            "voice_duration_ms": None,
            "caption": None,
            "media_url": None,
            "reaction": content_reaction,
            "transport_native": raw,
        },
        "metadata": {
            "mesh_received_at": int(time.time() * 1000),
            "original_format": content_type,
        },
        "timestamp": timestamp,
        "quote": quote,
        "bot_mentioned": bot_mentioned,
    }


_rate_limit_notice_sent: Dict[str, float] = {}  # sender -> timestamp
_rate_limit_notice_cooldown = 60.0  # Only send notice once per minute per sender


def _send_rate_limit_notice(payload: Dict[str, Any]) -> None:
    """Send a rate limit notice back to the user (max once per minute)."""
    global _rpc, _account

    sender = payload.get("sender", {}).get("transport_id")
    conversation = payload.get("conversation", {})
    convo_type = conversation.get("type")
    convo_id = conversation.get("id")

    if not sender:
        return

    # Check cooldown - don't spam rate limit notices
    now = time.time()
    last_sent = _rate_limit_notice_sent.get(sender, 0)
    if now - last_sent < _rate_limit_notice_cooldown:
        logger.debug("Skipping rate limit notice to %s (cooldown)", _redact_pii(sender, "phone"))
        return
    _rate_limit_notice_sent[sender] = now

    notice_text = "You're sending messages too quickly. Please slow down a bit."

    send_payload: Dict[str, Any] = {
        "account": _account,
        "message": notice_text,
    }

    if convo_type == "group" and convo_id:
        send_payload["groupId"] = convo_id
    else:
        send_payload["recipients"] = [sender]

    with _rpc_lock:
        if _rpc is None:
            logger.warning("Cannot send rate limit notice: RPC not ready")
            return
        try:
            _rpc.call("send", send_payload, timeout=10.0)
            logger.info("Sent rate limit notice to %s", _redact_pii(sender, "phone"))
        except Exception as exc:
            logger.error("Failed to send rate limit notice: %s", exc)


def run_http_server(port: int):
    """Run Flask server in a thread."""
    # Use werkzeug directly to avoid Flask dev server warnings
    from werkzeug.serving import make_server
    server = make_server("0.0.0.0", port, flask_app, threaded=True)
    logger.info("HTTP server listening on port %d", port)
    server.serve_forever()


_account_uuid: str = ""  # Bot's UUID, fetched at startup


def main() -> None:
    global _rpc, _account, _account_uuid

    log_level = os.getenv("MESH_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    _account = os.getenv("SIGNAL_ACCOUNT", "")
    if not _account:
        raise SystemExit("SIGNAL_ACCOUNT not set")
    _account_uuid = os.getenv("SIGNAL_BOT_UUID", "")  # Optional, will try to fetch if not set

    http_port = int(os.getenv("MESH_WORKER_HTTP_PORT", "8444"))
    notification_wait_seconds = float(os.getenv("MESH_SIGNAL_POLL_SECONDS", "5"))
    signal_cli_bin = os.getenv("SIGNAL_CLI_BIN", "/usr/local/bin/signal-cli")
    signal_cli_config_dir = os.getenv("SIGNAL_CLI_CONFIG_DIR", "/var/lib/signal-cli")
    if not Path(signal_cli_bin).exists():
        raise SystemExit(f"SIGNAL_CLI_BIN not found: {signal_cli_bin}")
    if not Path(signal_cli_config_dir).exists():
        raise SystemExit(f"SIGNAL_CLI_CONFIG_DIR not found: {signal_cli_config_dir}")

    # Mesh is stateless - always start with empty policy, wait for Joi push
    policy = MeshPolicy()

    # Set up config state for Joi-pushed config (memory-only)
    _config_state.set_mesh_policy(policy)
    set_config_state(_config_state)  # Share with forwarder to avoid module import issues

    _rpc = JsonRpcStdioClient(
        [
            signal_cli_bin,
            "--config",
            signal_cli_config_dir,
            "jsonRpc",
            "--receive-mode=on-connection",
        ]
    )

    logger.info("Signal worker started (on-connection notifications)")
    logger.info("Waiting for config push from Joi (denying all messages)")
    if _is_hmac_available():
        logger.info("HMAC authentication enabled")
    else:
        logger.warning("HMAC authentication DISABLED - set MESH_HMAC_SECRET")

    # Start HTTP server in background thread
    http_thread = threading.Thread(target=run_http_server, args=(http_port,), daemon=True)
    http_thread.start()

    try:
        while True:
            try:
                notification = _rpc.pop_notification(timeout=notification_wait_seconds)
                if notification is None:
                    continue

                messages = _extract_messages([notification])

                if messages:
                    logger.debug("Received %d raw message(s) from Signal", len(messages))
                for msg in messages:
                    # Check for delivery/read receipts first
                    if _handle_receipt_message(msg):
                        continue  # Receipt handled, no further processing needed

                    payload = _normalize_signal_message(msg, bot_account=_account, bot_uuid=_account_uuid)
                    if payload is None:
                        logger.debug("Skipping unsupported Signal event")
                        continue

                    # Dedupe check - drop if we've seen this message_id before
                    message_id = payload.get("message_id")
                    if not _dedupe_cache.check_and_add(message_id):
                        logger.info("Dropping duplicate message_id=%s", message_id)
                        continue

                    decision = policy.evaluate_inbound(payload)
                    if not decision.allowed:
                        sender = payload.get("sender", {}).get("transport_id", "unknown")
                        if decision.reason == "unknown_sender":
                            # Don't redact unknown senders - admin needs full ID to add them
                            logger.info("Dropping unknown sender=%s", sender)
                        elif decision.reason.startswith("rate_limited"):
                            sender_display = _redact_pii(sender, "phone")
                            logger.warning("Rate limited sender=%s reason=%s", sender_display, decision.reason)
                            _send_rate_limit_notice(payload)
                        else:
                            sender_display = _redact_pii(sender, "phone")
                            logger.warning("Dropping sender=%s reason=%s", sender_display, decision.reason)
                        continue

                    # Add store_only flag to payload for Joi
                    if decision.store_only:
                        payload["store_only"] = True
                        # Show full sender for store_only - admin needs ID to add them to participants
                        sender = payload.get("sender", {}).get("transport_id", "unknown")
                        logger.info("Forwarding message_id=%s to Joi (store_only, sender=%s)", payload.get("message_id"), sender)
                    else:
                        logger.info("Forwarding message_id=%s to Joi", payload.get("message_id"))

                    # Add group_names for @mention detection
                    convo = payload.get("conversation", {})
                    if convo.get("type") == "group":
                        group_id = convo.get("id")
                        # Start with bot_name, add per-group names on top
                        names = []
                        bot_name = policy.get_bot_name()
                        if bot_name:
                            names.append(bot_name)
                        group_names = policy.get_group_names(group_id)
                        if group_names:
                            names.extend(n for n in group_names if n not in names)
                        if names:
                            payload["group_names"] = names
                            logger.debug("Group names for addressing: %s", names)
                        else:
                            logger.debug("No group names found (bot_name=%s, group_id=%s)", bot_name, group_id[:8] if group_id else None)

                    # Check kill switch before forwarding
                    if _config_state.is_kill_switch_active():
                        logger.warning("Kill switch active - dropping message (not forwarding to Joi)")
                        continue

                    # Process document attachments (only for allowed senders, not store_only)
                    content_type = payload.get("content", {}).get("type", "")
                    if not decision.store_only:
                        raw_native = payload.get("content", {}).get("transport_native", {})
                        envelope = _as_dict(raw_native.get("envelope"))
                        data_message = _as_dict(envelope.get("dataMessage")) if envelope else {}
                        if data_message:
                            _process_attachments(
                                data_message=data_message,
                                sender_id=payload.get("sender", {}).get("transport_id", ""),
                                conversation_type=convo.get("type", "direct"),
                                conversation_id=convo.get("id", ""),
                            )

                    # Skip Joi forwarding for attachment-only messages (nothing to respond to)
                    if content_type == "attachment":
                        logger.info("Attachment-only message processed, skipping Joi forward")
                        continue

                    forward_to_joi(payload)
            except Exception as exc:  # noqa: BLE001
                logger.error("signal_worker error: %s", exc)
                time.sleep(1)
    finally:
        _rpc.close()


if __name__ == "__main__":
    main()
