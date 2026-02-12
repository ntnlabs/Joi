from typing import Any, Dict
import logging
import os
import threading

import httpx

logger = logging.getLogger("mesh.forwarder")

# Reusable client for connection pooling
_client: httpx.Client = None
_client_lock = threading.Lock()


def _get_client() -> httpx.Client:
    """Get or create a reusable HTTP client."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                timeout = float(os.getenv("MESH_FORWARD_TIMEOUT", "120"))
                _client = httpx.Client(timeout=timeout)
    return _client


def _forward_async(url: str, payload: Dict[str, Any]) -> None:
    """Forward message in background thread."""
    try:
        client = _get_client()
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        logger.debug("Forwarded message_id=%s to Joi", payload.get("message_id"))
    except Exception as e:
        logger.error("Forward to Joi failed: %s", e)


def forward_to_joi(payload: Dict[str, Any]) -> None:
    """Forward message to Joi asynchronously (fire-and-forget)."""
    if os.getenv("MESH_ENABLE_FORWARD", "0") != "1":
        return

    url = os.getenv("MESH_JOI_INBOUND_URL", "http://joi:8443/api/v1/message/inbound")

    # Fire-and-forget: don't block signal worker
    thread = threading.Thread(target=_forward_async, args=(url, payload), daemon=True)
    thread.start()
