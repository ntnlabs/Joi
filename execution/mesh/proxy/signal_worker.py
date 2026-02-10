import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from config import load_settings
from forwarder import forward_to_joi
from jsonrpc_stdio import JsonRpcStdioClient


logger = logging.getLogger("mesh.signal_worker")


def _receive_messages(rpc: JsonRpcStdioClient, account: str, timeout_s: int) -> List[Dict[str, Any]]:
    params = {"account": account, "timeout": timeout_s}
    result = rpc.call("receive", params)
    if "error" in result:
        raise RuntimeError(result["error"])
    rpc_result = result.get("result")
    if isinstance(rpc_result, list):
        return rpc_result
    return []


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
            # signal-cli can send a single event dict or {"result": [events]}
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
        # For now we only forward text/reaction. Other types are logged and skipped.
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    account = os.getenv("SIGNAL_ACCOUNT", "")
    if not account:
        raise SystemExit("SIGNAL_ACCOUNT not set")

    poll_seconds = int(os.getenv("MESH_SIGNAL_POLL_SECONDS", "5"))
    timeout_s = int(os.getenv("MESH_SIGNAL_TIMEOUT", "10"))

    rpc = JsonRpcStdioClient(
        [
            "/usr/local/bin/signal-cli",
            "--config",
            "/var/lib/signal-cli",
            "jsonRpc",
            "--receive-mode=manual",
        ]
    )

    logger.info("Signal worker started (manual receive)")

    try:
        while True:
            try:
                inline_messages = _receive_messages(rpc, account, timeout_s)
                notification_messages = _extract_messages(rpc.pop_all_notifications())
                messages = inline_messages + notification_messages

                if messages:
                    logger.info("Received %d message(s) from Signal", len(messages))
                for msg in messages:
                    payload = _normalize_signal_message(msg)
                    if payload is None:
                        logger.info("Skipping unsupported Signal event")
                        continue
                    logger.info("Forwarding message_id=%s to Joi", payload.get("message_id"))
                    forward_to_joi(payload)
            except Exception as exc:  # noqa: BLE001
                logger.error("signal_worker error: %s", exc)

            time.sleep(poll_seconds)
    finally:
        rpc.close()


if __name__ == "__main__":
    main()
