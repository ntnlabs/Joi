from typing import Any, Dict, Optional
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

    # Fire-and-forget: don't block signal worker
    thread = threading.Thread(target=_forward_async, args=(url, payload), daemon=True)
    thread.start()
