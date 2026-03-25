"""HMAC authentication for joi → mesh and mesh → backend requests.

Defense-in-depth layer over Nebula VPN. See api-contracts.md for spec.
"""
import logging
import os
import threading
from pathlib import Path
from typing import Optional, Tuple

from shared.hmac_core import (
    DEFAULT_TIMESTAMP_TOLERANCE_MS,
    NONCE_RETENTION_MS,
    compute_hmac,
    create_request_headers,
    generate_nonce,
    get_timestamp_ms,
    verify_hmac,
    verify_timestamp,
)

logger = logging.getLogger("mesh.hmac_auth")

# Writable secret file (for rotation persistence across restart)
HMAC_SECRET_FILE = Path(os.getenv("MESH_HMAC_SECRET_FILE", "/var/lib/signal-cli/hmac.secret"))

# Re-export for convenience
__all__ = [
    "DEFAULT_TIMESTAMP_TOLERANCE_MS",
    "NONCE_RETENTION_MS",
    "compute_hmac",
    "create_request_headers",
    "generate_nonce",
    "get_timestamp_ms",
    "verify_hmac",
    "verify_timestamp",
    "get_shared_secret",
    "get_shared_secret_for_backend",
    "save_shared_secret",
    "InMemoryNonceStore",
    "HMAC_SECRET_FILE",
]


def get_shared_secret() -> Optional[bytes]:
    """Get the shared secret from file or environment.

    Priority:
    1. Secret file (persisted after rotation)
    2. Environment variable (initial setup / fallback)
    """
    # Try file first (supports rotation persistence)
    if HMAC_SECRET_FILE.exists():
        try:
            secret = HMAC_SECRET_FILE.read_text().strip()
            if secret:
                return bytes.fromhex(secret)
        except Exception as e:
            logger.warning("Failed to read HMAC secret file", extra={"error": str(e)})

    # Fall back to environment
    secret = os.getenv("MESH_HMAC_SECRET")
    if secret:
        try:
            return bytes.fromhex(secret)
        except ValueError:
            logger.critical("HMAC secret is not valid hex — refusing to use weak secret", extra={"action": "hmac_config_error"})
            return None
    return None


def get_shared_secret_for_backend(backend_name: str) -> Optional[bytes]:
    """Get HMAC secret for a specific backend.

    Looks up MESH_HMAC_SECRET_{BACKEND} env var (uppercase).
    No fallback - each backend must have its own secret configured.

    Args:
        backend_name: Backend identifier (e.g., "joi", "leeloo")

    Returns:
        Secret bytes if configured, None otherwise
    """
    env_name = f"MESH_HMAC_SECRET_{backend_name.upper()}"
    secret = os.getenv(env_name)
    if secret:
        try:
            return bytes.fromhex(secret)
        except ValueError:
            logger.critical("HMAC secret is not valid hex — refusing to use weak secret", extra={"action": "hmac_config_error", "env_var": env_name})
            return None

    # No fallback - fail-closed for security
    logger.warning("No HMAC secret for backend", extra={
        "backend": backend_name,
        "env_var": env_name
    })
    return None


def save_shared_secret(secret_hex: str) -> bool:
    """Persist rotated secret to file for restart recovery.

    Called by ConfigState when receiving HMAC rotation from Joi.
    """
    try:
        HMAC_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_file = HMAC_SECRET_FILE.with_suffix(".tmp")
        temp_file.write_text(secret_hex + "\n")
        temp_file.chmod(0o600)
        temp_file.rename(HMAC_SECRET_FILE)
        logger.info("Persisted rotated HMAC secret", extra={
            "action": "secret_persisted",
            "path": str(HMAC_SECRET_FILE)
        })
        return True
    except Exception as e:
        logger.error("Failed to persist HMAC secret", extra={"error": str(e)})
        return False


class InMemoryNonceStore:
    """Thread-safe in-memory nonce storage for replay protection.

    Note: Nonces are lost on restart. For mesh side, this is acceptable
    since the primary security is on the Joi side.
    """

    def __init__(self, retention_ms: int = NONCE_RETENTION_MS, max_size: int = 10000):
        self._nonces: dict = {}  # nonce -> expires_at
        self._lock = threading.Lock()
        self._retention_ms = retention_ms
        self._max_size = max_size
        self._last_cleanup = 0

    def check_and_store(self, nonce: str, source: str = "joi") -> Tuple[bool, str]:
        """Check if nonce is new and store it.

        Args:
            nonce: The nonce to check
            source: Request source identifier (for logging)

        Returns:
            Tuple of (is_new, error_reason)
        """
        now = get_timestamp_ms()
        expires_at = now + self._retention_ms

        with self._lock:
            # Cleanup expired nonces periodically (every 60s)
            if now - self._last_cleanup > 60000:
                self._cleanup(now)
                self._last_cleanup = now

            if nonce in self._nonces:
                logger.warning("Replay detected", extra={
                    "nonce": nonce[:8],
                    "source": source,
                    "action": "replay_blocked"
                })
                return False, "replay_detected"

            self._nonces[nonce] = expires_at
            return True, ""

    def _cleanup(self, now_ms: int):
        """Remove expired nonces."""
        expired = [k for k, v in self._nonces.items() if v < now_ms]
        for k in expired:
            del self._nonces[k]

        # If still too large, remove oldest
        if len(self._nonces) > self._max_size:
            sorted_items = sorted(self._nonces.items(), key=lambda x: x[1])
            for k, _ in sorted_items[:len(self._nonces) - self._max_size]:
                del self._nonces[k]
