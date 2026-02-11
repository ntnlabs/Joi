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
from jsonrpc_stdio import JsonRpcStdioClient
from policy import MeshPolicy


logger = logging.getLogger("mesh.signal_worker")


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


# Global RPC client (shared between receiver thread and HTTP server)
_rpc: Optional[JsonRpcStdioClient] = None
_rpc_lock = threading.Lock()
_account: str = ""
_dedupe_cache = MessageDedupeCache()


# --- Flask app for outbound API ---
flask_app = Flask("mesh-outbound")
flask_app.logger.setLevel(logging.WARNING)  # Quiet Flask logs


@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "mode": "worker"})


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

    logger.info("Sent message to %s", transport_id or group_id)
    return jsonify({
        "status": "ok",
        "data": {
            "message_id": str(sent_at) if sent_at else None,
            "transport": "signal",
            "sent_at": sent_at,
            "delivered": False,
        }
    })


# --- Helper functions ---

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


def _normalize_signal_message(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    envelope = _as_dict(raw.get("envelope"))
    if not envelope:
        return None

    data_message = _as_dict(envelope.get("dataMessage"))
    reaction = _as_dict(data_message.get("reaction"))
    message_text = _extract_message_text(data_message)

    content_type = "text"
    content_reaction: Optional[str] = None
    if reaction:
        content_type = "reaction"
        emoji = reaction.get("emoji")
        if isinstance(emoji, str):
            content_reaction = emoji
    elif message_text is None:
        return None

    source = envelope.get("source")
    if not isinstance(source, str) or not source:
        source = "unknown"

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
    }


def run_http_server(port: int):
    """Run Flask server in a thread."""
    # Use werkzeug directly to avoid Flask dev server warnings
    from werkzeug.serving import make_server
    server = make_server("0.0.0.0", port, flask_app, threaded=True)
    logger.info("HTTP server listening on port %d", port)
    server.serve_forever()


def main() -> None:
    global _rpc, _account

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
                    logger.info("Received %d message(s) from Signal", len(messages))
                for msg in messages:
                    payload = _normalize_signal_message(msg)
                    if payload is None:
                        logger.info("Skipping unsupported Signal event")
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
                        else:
                            logger.warning("Dropping sender=%s reason=%s", sender, decision.reason)
                        continue

                    # Add store_only flag to payload for Joi
                    if decision.store_only:
                        payload["store_only"] = True
                        logger.info("Forwarding message_id=%s to Joi (store_only)", payload.get("message_id"))
                    else:
                        logger.info("Forwarding message_id=%s to Joi", payload.get("message_id"))

                    forward_to_joi(payload)
            except Exception as exc:  # noqa: BLE001
                logger.error("signal_worker error: %s", exc)
                time.sleep(1)
    finally:
        _rpc.close()


if __name__ == "__main__":
    main()
