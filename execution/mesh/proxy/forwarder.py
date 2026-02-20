from typing import Any, Dict, Optional
from concurrent.futures import ThreadPoolExecutor
import atexit
import base64
import json
import logging
import os
import threading

import httpx

from hmac_auth import create_request_headers, get_shared_secret

logger = logging.getLogger("mesh.forwarder")

# Reference to ConfigState - set by worker at startup to avoid module import issues
_config_state_ref: Optional[Any] = None

# Cache the shared secret (fallback if config_state not set)
_hmac_secret: Optional[bytes] = None
_hmac_secret_loaded = False

# Reusable client for connection pooling
_client: httpx.Client = None
_client_lock = threading.Lock()

# Bounded thread pool for async forwarding (prevents unbounded thread spawning)
_executor: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="forward")
atexit.register(_executor.shutdown, wait=False)


def set_config_state(config_state) -> None:
    """Set reference to ConfigState from worker (avoids module import issues)."""
    global _config_state_ref
    _config_state_ref = config_state
    logger.debug("Forwarder config_state reference set")


def _get_client() -> httpx.Client:
    """Get or create a reusable HTTP client."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                timeout = float(os.getenv("MESH_FORWARD_TIMEOUT", "120"))
                _client = httpx.Client(timeout=timeout)
    return _client


def _get_hmac_secret() -> Optional[bytes]:
    """
    Get current HMAC secret for signing.

    Gets live secret from config_state (supports rotation),
    falls back to env if config_state not ready.
    """
    global _hmac_secret, _hmac_secret_loaded

    # Try to get live secret from config_state (supports rotation)
    if _config_state_ref is not None:
        try:
            current, _ = _config_state_ref.get_hmac_secrets()
            if current:
                return current
        except Exception:
            pass

    # Fall back to env-loaded secret
    if not _hmac_secret_loaded:
        _hmac_secret = get_shared_secret()
        _hmac_secret_loaded = True
        if _hmac_secret:
            logger.info("HMAC authentication enabled for Joi forwarding")
        else:
            logger.warning("MESH_HMAC_SECRET not set - forwarding without HMAC")
    return _hmac_secret


def _get_config_hash() -> Optional[str]:
    """Get current config hash for sync verification."""
    if _config_state_ref is not None:
        try:
            config_hash = _config_state_ref.get_hash()
            return config_hash if config_hash else None
        except Exception:
            pass
    return None


def _forward_async(url: str, payload: Dict[str, Any]) -> None:
    """Forward message in background thread with HMAC signing."""
    try:
        client = _get_client()

        # Serialize payload to JSON bytes
        body = json.dumps(payload).encode("utf-8")

        # Build headers with HMAC if secret is configured
        headers = {"Content-Type": "application/json"}
        secret = _get_hmac_secret()
        if secret:
            hmac_headers = create_request_headers(body, secret)
            headers.update(hmac_headers)

        # Add config hash for sync verification
        config_hash = _get_config_hash()
        if config_hash:
            headers["X-Config-Hash"] = config_hash

        resp = client.post(url, content=body, headers=headers)
        resp.raise_for_status()
        logger.debug("Forwarded message_id=%s to Joi", payload.get("message_id"))
    except Exception as e:
        logger.error("Forward to Joi failed: %s", e)


def forward_to_joi(payload: Dict[str, Any]) -> None:
    """Forward message to Joi asynchronously (fire-and-forget)."""
    if os.getenv("MESH_ENABLE_FORWARD", "0") != "1":
        return

    url = os.getenv("MESH_JOI_INBOUND_URL", "http://joi:8443/api/v1/message/inbound")

    # Submit to bounded thread pool (max 4 concurrent forwards)
    try:
        _executor.submit(_forward_async, url, payload)
    except RuntimeError:
        # Executor shutdown - log but don't crash
        logger.warning("Forwarder executor shutdown, dropping message")


def _forward_document_sync(
    url: str,
    filename: str,
    content: bytes,
    content_type: str,
    scope: str,
    sender_id: str,
) -> bool:
    """Forward document to Joi synchronously. Returns True on success."""
    try:
        client = _get_client()

        # Build payload with base64-encoded content
        payload = {
            "filename": filename,
            "content_base64": base64.b64encode(content).decode("ascii"),
            "content_type": content_type,
            "scope": scope,
            "sender_id": sender_id,
        }
        body = json.dumps(payload).encode("utf-8")

        # Build headers with HMAC if secret is configured
        headers = {"Content-Type": "application/json"}
        secret = _get_hmac_secret()
        if secret:
            hmac_headers = create_request_headers(body, secret)
            headers.update(hmac_headers)

        # Add config hash for sync verification
        config_hash = _get_config_hash()
        if config_hash:
            headers["X-Config-Hash"] = config_hash

        resp = client.post(url, content=body, headers=headers)
        resp.raise_for_status()
        logger.info("Forwarded document %s to Joi", filename)
        return True
    except Exception as e:
        logger.error("Forward document to Joi failed: %s", e)
        return False


def forward_document_to_joi(
    filename: str,
    content: bytes,
    content_type: str,
    scope: str,
    sender_id: str,
) -> bool:
    """
    Forward document to Joi for ingestion.

    Unlike message forwarding, this is synchronous so we know if it succeeded
    before deleting the attachment file.

    Args:
        filename: Original filename
        content: File content as bytes
        content_type: MIME type
        scope: Ingestion scope (conversation_id)
        sender_id: Sender's transport ID

    Returns:
        True if successfully forwarded, False otherwise
    """
    if os.getenv("MESH_ENABLE_FORWARD", "0") != "1":
        return False

    url = os.getenv("MESH_JOI_DOCUMENT_URL", "http://joi:8443/api/v1/document/ingest")

    return _forward_document_sync(url, filename, content, content_type, scope, sender_id)
