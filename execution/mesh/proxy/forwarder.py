from typing import Any, Dict, List, Optional
import atexit
import base64
import json
import logging
import os
import queue
import threading
import time

import httpx

from hmac_auth import create_request_headers, get_shared_secret, get_shared_secret_for_backend

logger = logging.getLogger("mesh.forwarder")

# Joi API base URL - single config point for all Joi endpoints
# Set MESH_JOI_URL to your Joi VM's address (e.g., http://10.42.0.2:8443)
MESH_JOI_URL = os.getenv("MESH_JOI_URL", "http://joi:8443")

# Reference to ConfigState - set by worker at startup to avoid module import issues
_config_state_ref: Optional[Any] = None

# Reference to RoutingState - set by worker at startup for multi-backend routing
_routing_state: Optional[Any] = None


def set_routing_state(state: Any) -> None:
    """Set reference to RoutingState from worker (avoids module import issues)."""
    global _routing_state
    _routing_state = state
    logger.debug("Forwarder routing_state reference set", extra={"action": "init"})

# Cache the shared secret (fallback if config_state not set)
_hmac_secret: Optional[bytes] = None
_hmac_secret_loaded = False

# Reusable client for connection pooling
_client: httpx.Client = None
_client_lock = threading.Lock()

# Fixed worker queues for causal ordering (same conversation always goes to same worker)
# Configurable via environment for high-traffic scenarios
_NUM_WORKERS = int(os.getenv("MESH_FORWARD_WORKERS", "4"))
_MAX_PENDING = int(os.getenv("MESH_FORWARD_MAX_PENDING", "20"))
_worker_queues: List[queue.Queue] = []
_workers: List[threading.Thread] = []
_workers_started = False
_workers_lock = threading.Lock()


def _cleanup_client():
    """Cleanup HTTP client on shutdown."""
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
        _client = None


atexit.register(_cleanup_client)


def set_config_state(config_state) -> None:
    """Set reference to ConfigState from worker (avoids module import issues)."""
    global _config_state_ref
    _config_state_ref = config_state
    logger.debug("Forwarder config_state reference set", extra={"action": "init"})


def _get_client() -> httpx.Client:
    """Get or create a reusable HTTP client."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                timeout = float(os.getenv("MESH_FORWARD_TIMEOUT", "180"))
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
            logger.info("HMAC authentication enabled for Joi forwarding", extra={"action": "init", "hmac_enabled": True})
        else:
            logger.warning("HMAC not configured for forwarding", extra={"action": "init", "hmac_enabled": False})
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


def _worker_loop(worker_id: int) -> None:
    """Process messages for this worker sequentially (ensures causal ordering)."""
    q = _worker_queues[worker_id]
    while True:
        try:
            work_item = q.get()
            if work_item is None:  # Shutdown signal
                break
            url, payload, backend_name, secret = work_item
            _forward_sync(url, payload, backend_name, secret)
            q.task_done()
        except Exception as e:
            logger.error("Worker loop error", extra={"worker": worker_id, "error": str(e)})


def _init_workers() -> None:
    """Initialize worker queues and threads (lazy, on first forward)."""
    global _worker_queues, _workers, _workers_started
    with _workers_lock:
        if _workers_started:
            return
        _worker_queues = [queue.Queue(maxsize=_MAX_PENDING) for _ in range(_NUM_WORKERS)]
        for i in range(_NUM_WORKERS):
            t = threading.Thread(target=_worker_loop, args=(i,), name=f"forward-{i}", daemon=True)
            t.start()
            _workers.append(t)
        _workers_started = True
        logger.info("Forward workers initialized", extra={"workers": _NUM_WORKERS, "action": "init"})


def _shutdown_workers() -> None:
    """Shutdown worker threads gracefully."""
    global _workers_started
    with _workers_lock:
        if not _workers_started:
            return
        # Send shutdown signal to all workers
        for q in _worker_queues:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
        _workers_started = False


atexit.register(_shutdown_workers)


def _forward_sync(url: str, payload: Dict[str, Any], backend_name: Optional[str], secret: Optional[bytes]) -> None:
    """Forward message synchronously with retry. Called by worker threads."""
    message_id = payload.get("message_id", "unknown")

    def do_forward() -> bool:
        """Attempt to forward. Returns True on success."""
        client = _get_client()

        # Serialize payload to JSON bytes
        body = json.dumps(payload).encode("utf-8")

        # Build headers with HMAC if secret is configured
        headers = {"Content-Type": "application/json"}
        if secret:
            hmac_headers = create_request_headers(body, secret)
            headers.update(hmac_headers)

        # Add config hash for sync verification
        config_hash = _get_config_hash()
        if config_hash:
            headers["X-Config-Hash"] = config_hash

        resp = client.post(url, content=body, headers=headers)
        resp.raise_for_status()
        return True

    log_extra = {"message_id": message_id, "action": "forward"}
    if backend_name:
        log_extra["backend"] = backend_name

    try:
        do_forward()
        logger.debug("Forwarded message", extra=log_extra)
    except Exception as e:
        logger.warning("Forward failed, will retry", extra={
            **log_extra,
            "error": str(e),
            "retry_in_seconds": 10
        })
        # Single retry after 10 seconds for transient network issues
        time.sleep(10)
        try:
            do_forward()
            log_extra["action"] = "forward_retry"
            logger.info("Forward retry succeeded", extra=log_extra)
        except Exception as e2:
            log_extra["action"] = "forward_dropped"
            log_extra["error"] = str(e2)
            logger.error("Forward failed after retry, dropping message", extra=log_extra)


def _get_conversation_id(payload: Dict[str, Any]) -> str:
    """Extract conversation ID for worker routing (group_id or sender)."""
    # Group messages use group_id, DMs use sender
    return payload.get("group_id") or payload.get("sender", {}).get("transport_id", "unknown")


def forward_to_backend(payload: Dict[str, Any], backend_name: str, backend_url: str) -> None:
    """Forward message to specified backend with causal ordering."""
    if os.getenv("MESH_ENABLE_FORWARD", "0") != "1":
        return

    _init_workers()

    # Fail-closed: reject if no secret configured for this backend
    secret = get_shared_secret_for_backend(backend_name)
    if not secret:
        logger.error("Forward rejected: no HMAC secret configured (fail-closed)", extra={
            "backend": backend_name,
            "env_var": f"MESH_HMAC_SECRET_{backend_name.upper()}",
            "action": "forward_rejected"
        })
        return

    url = f"{backend_url}/api/v1/message/inbound"

    # Hash conversation to worker - same convo always goes to same worker
    convo_id = _get_conversation_id(payload)
    worker_idx = hash(convo_id) % _NUM_WORKERS

    try:
        _worker_queues[worker_idx].put_nowait((url, payload, backend_name, secret))
    except queue.Full:
        logger.warning("Forward queue full, dropping message", extra={
            "backend": backend_name,
            "worker": worker_idx,
            "action": "forward_dropped"
        })


def forward_to_joi(payload: Dict[str, Any]) -> None:
    """Forward message to Joi with causal ordering per conversation."""
    if os.getenv("MESH_ENABLE_FORWARD", "0") != "1":
        return

    # Check if routing is enabled and use multi-backend routing
    if _routing_state and _routing_state.is_enabled():
        backend_name, backend_url = _routing_state.get_backend_for_payload(payload)
        forward_to_backend(payload, backend_name, backend_url)
        return

    _init_workers()

    # Legacy behavior - use MESH_JOI_URL with default HMAC secret
    url = f"{MESH_JOI_URL}/api/v1/message/inbound"
    secret = _get_hmac_secret()
    if not secret:
        logger.error("Forward rejected: no HMAC secret configured (fail-closed)", extra={"action": "forward_rejected"})
        return

    # Hash conversation to worker - same convo always goes to same worker
    convo_id = _get_conversation_id(payload)
    worker_idx = hash(convo_id) % _NUM_WORKERS

    try:
        _worker_queues[worker_idx].put_nowait((url, payload, None, secret))
    except queue.Full:
        logger.warning("Forward queue full, dropping message", extra={
            "worker": worker_idx,
            "action": "forward_dropped"
        })


def forward_typing(sender: str, conversation_id: str) -> None:
    """
    Forward a typing indicator to Joi. Best-effort, fire-and-forget.

    Called when Signal delivers a typingMessage with action=STARTED.
    Joi uses this to suppress Wind proactive messages while the user is composing.
    """
    if os.getenv("MESH_ENABLE_FORWARD", "0") != "1":
        return

    url = f"{MESH_JOI_URL}/api/v1/typing/inbound"
    payload = {"sender": sender, "conversation_id": conversation_id}
    body = json.dumps(payload).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    secret = _get_hmac_secret()
    if not secret:
        logger.error("Forward rejected: no HMAC secret configured (fail-closed)", extra={"action": "forward_rejected"})
        return
    if secret:
        hmac_headers = create_request_headers(body, secret)
        headers.update(hmac_headers)

    try:
        client = _get_client()
        client.post(url, content=body, headers=headers, timeout=3.0)
    except Exception as e:
        logger.debug("Typing forward failed (non-critical)", extra={"error": str(e)})


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
        if not secret:
            logger.error("Forward rejected: no HMAC secret configured (fail-closed)", extra={"action": "forward_rejected"})
            return False
        hmac_headers = create_request_headers(body, secret)
        headers.update(hmac_headers)

        # Add config hash for sync verification
        config_hash = _get_config_hash()
        if config_hash:
            headers["X-Config-Hash"] = config_hash

        resp = client.post(url, content=body, headers=headers)
        resp.raise_for_status()
        logger.info("Forwarded document to Joi", extra={
            "filename": filename,
            "action": "document_forward"
        })
        return True
    except Exception as e:
        logger.error("Forward document to Joi failed", extra={
            "filename": filename,
            "error": str(e),
            "action": "document_forward_failed"
        })
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

    url = f"{MESH_JOI_URL}/api/v1/document/ingest"

    return _forward_document_sync(url, filename, content, content_type, scope, sender_id)
