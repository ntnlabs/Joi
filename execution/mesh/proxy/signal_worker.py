import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request

from config import load_settings
from forwarder import forward_to_joi
from hmac_auth import (
    InMemoryNonceStore,
    get_shared_secret,
    verify_hmac,
    verify_timestamp,
    DEFAULT_TIMESTAMP_TOLERANCE_MS,
)
from jsonrpc_stdio import JsonRpcStdioClient
from policy import MeshPolicy


logger = logging.getLogger("mesh.signal_worker")

# HMAC authentication
_hmac_secret = get_shared_secret()
_hmac_enabled = _hmac_secret is not None
_nonce_store = InMemoryNonceStore() if _hmac_enabled else None
_hmac_timestamp_tolerance = int(os.getenv("MESH_HMAC_TIMESTAMP_TOLERANCE_MS", str(DEFAULT_TIMESTAMP_TOLERANCE_MS)))


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
            logger.debug("Tracking message ts=%d to %s", timestamp, recipient)

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


# Global RPC client (shared between receiver thread and HTTP server)
_rpc: Optional[JsonRpcStdioClient] = None
_rpc_lock = threading.Lock()
_account: str = ""
_dedupe_cache = MessageDedupeCache()


# --- Flask app for outbound API ---
flask_app = Flask("mesh-outbound")
flask_app.logger.setLevel(logging.WARNING)  # Quiet Flask logs


@flask_app.before_request
def verify_hmac_auth():
    """Verify HMAC authentication for incoming requests from Joi."""
    # Skip health endpoint
    if request.path == "/health":
        return None

    # Skip if HMAC not configured
    if not _hmac_enabled:
        return None

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

    # Verify HMAC signature
    if not verify_hmac(nonce, timestamp, body, signature, _hmac_secret):
        logger.warning("HMAC auth failed: invalid signature")
        return jsonify({"status": "error", "error": {"code": "hmac_invalid_signature", "message": "Invalid HMAC signature"}}), 401

    logger.debug("HMAC auth passed for %s", request.path)
    return None


@flask_app.route("/health", methods=["GET"])
def health():
    hmac_status = "enabled" if _hmac_enabled else "disabled"
    return jsonify({"status": "ok", "mode": "worker", "hmac": hmac_status})


@flask_app.route("/api/v1/delivery/status", methods=["GET"])
def delivery_status():
    """Query delivery status for a message by timestamp."""
    ts_str = request.args.get("timestamp")
    if not ts_str:
        # Return all tracked messages (for debugging)
        return jsonify({
            "status": "ok",
            "data": {str(k): v for k, v in _delivery_tracker.get_all_status().items()}
        })

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


@flask_app.route("/api/v1/message/outbound", methods=["POST"])
def send_outbound():
    """Handle outbound messages from Joi."""
    global _rpc, _account

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

    logger.info("Sent message to %s (ts=%s)", transport_id or group_id, sent_at)
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

    receipt_type = receipt.get("type", "").upper()
    timestamps = receipt.get("timestamps", [])

    if not timestamps:
        return True  # It's a receipt but no timestamps to process

    if not isinstance(timestamps, list):
        timestamps = [timestamps]

    # Convert to ints
    timestamps = [int(ts) for ts in timestamps if isinstance(ts, (int, float))]

    if receipt_type == "DELIVERY":
        count = _delivery_tracker.mark_delivered(timestamps)
        if count > 0:
            logger.info("Processed %d delivery receipt(s)", count)
    elif receipt_type == "READ":
        count = _delivery_tracker.mark_read(timestamps)
        if count > 0:
            logger.info("Processed %d read receipt(s)", count)
    else:
        logger.debug("Unknown receipt type: %s", receipt_type)

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


def _check_bot_mentioned(data_message: Dict[str, Any], bot_account: str) -> bool:
    """Check if the bot is mentioned in the message."""
    mentions = data_message.get("mentions")
    if not isinstance(mentions, list):
        return False
    for mention in mentions:
        if isinstance(mention, dict):
            # Signal uses 'number' field for phone-based mentions
            number = mention.get("number")
            if isinstance(number, str) and number == bot_account:
                return True
    return False


def _normalize_signal_message(raw: Dict[str, Any], bot_account: str = "") -> Optional[Dict[str, Any]]:
    envelope = _as_dict(raw.get("envelope"))
    if not envelope:
        return None

    # Debug: log envelope source fields (useful for UUID vs phone troubleshooting)
    logger.debug("Envelope source fields: source=%s sourceNumber=%s sourceUuid=%s",
                 envelope.get("source"), envelope.get("sourceNumber"), envelope.get("sourceUuid"))

    data_message = _as_dict(envelope.get("dataMessage"))
    reaction = _as_dict(data_message.get("reaction"))
    message_text = _extract_message_text(data_message)
    bot_mentioned = _check_bot_mentioned(data_message, bot_account) if bot_account else False

    content_type = "text"
    content_reaction: Optional[str] = None
    if reaction:
        content_type = "reaction"
        emoji = reaction.get("emoji")
        if isinstance(emoji, str):
            content_reaction = emoji
    elif message_text is None:
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
        logger.debug("Skipping rate limit notice to %s (cooldown)", sender)
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
            logger.info("Sent rate limit notice to %s", sender)
        except Exception as exc:
            logger.error("Failed to send rate limit notice: %s", exc)


def run_http_server(port: int):
    """Run Flask server in a thread."""
    # Use werkzeug directly to avoid Flask dev server warnings
    from werkzeug.serving import make_server
    server = make_server("0.0.0.0", port, flask_app, threaded=True)
    logger.info("HTTP server listening on port %d", port)
    server.serve_forever()


def main() -> None:
    global _rpc, _account

    log_level = os.getenv("MESH_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    _account = os.getenv("SIGNAL_ACCOUNT", "")
    if not _account:
        raise SystemExit("SIGNAL_ACCOUNT not set")

    http_port = int(os.getenv("MESH_WORKER_HTTP_PORT", "8444"))
    notification_wait_seconds = float(os.getenv("MESH_SIGNAL_POLL_SECONDS", "5"))
    signal_cli_bin = os.getenv("SIGNAL_CLI_BIN", "/usr/local/bin/signal-cli")
    signal_cli_config_dir = os.getenv("SIGNAL_CLI_CONFIG_DIR", "/var/lib/signal-cli")
    policy_file = os.getenv("MESH_POLICY_FILE", "/etc/mesh-proxy/policy.json")

    if not Path(signal_cli_bin).exists():
        raise SystemExit(f"SIGNAL_CLI_BIN not found: {signal_cli_bin}")
    if not Path(signal_cli_config_dir).exists():
        raise SystemExit(f"SIGNAL_CLI_CONFIG_DIR not found: {signal_cli_config_dir}")
    if not Path(policy_file).exists():
        raise SystemExit(f"MESH_POLICY_FILE not found: {policy_file}")

    policy = MeshPolicy(policy_file)

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
    logger.info("Policy loaded from %s", policy_file)
    if _hmac_enabled:
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

                    payload = _normalize_signal_message(msg, bot_account=_account)
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
                            logger.info("Dropping unknown sender=%s", sender)
                        elif decision.reason.startswith("rate_limited"):
                            logger.warning("Rate limited sender=%s reason=%s", sender, decision.reason)
                            _send_rate_limit_notice(payload)
                        else:
                            logger.warning("Dropping sender=%s reason=%s", sender, decision.reason)
                        continue

                    # Add store_only flag to payload for Joi
                    if decision.store_only:
                        payload["store_only"] = True
                        logger.info("Forwarding message_id=%s to Joi (store_only)", payload.get("message_id"))
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

                    forward_to_joi(payload)
            except Exception as exc:  # noqa: BLE001
                logger.error("signal_worker error: %s", exc)
                time.sleep(1)
    finally:
        _rpc.close()


if __name__ == "__main__":
    main()
