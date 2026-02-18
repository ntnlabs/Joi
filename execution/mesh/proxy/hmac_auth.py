"""HMAC authentication for mesh ↔ joi requests.

Defense-in-depth layer over Nebula VPN. See api-contracts.md for spec.

Header format:
    X-Nonce: <uuid4>
    X-Timestamp: <unix-epoch-ms>
    X-HMAC-SHA256: HMAC-SHA256(nonce + timestamp + body, shared_secret)
"""
import hashlib
import hmac
import logging
import os
import threading
import time
import uuid
from typing import Optional, Tuple

logger = logging.getLogger("mesh.hmac_auth")

# Default timestamp tolerance: 5 minutes (300,000 ms)
DEFAULT_TIMESTAMP_TOLERANCE_MS = 300_000

# Nonce retention: 15 minutes
NONCE_RETENTION_MS = 15 * 60 * 1000


def get_shared_secret() -> Optional[bytes]:
    """
    Get the shared secret from environment or key file.

    Checks:
    1. MESH_HMAC_SECRET environment variable
    2. /var/lib/mesh-proxy/hmac.key file (for rotated keys)
    """
    # First check environment
    secret = os.getenv("MESH_HMAC_SECRET")
    if secret:
        return secret.encode("utf-8")

    # Fall back to key file (written by config push rotation)
    key_file = "/var/lib/mesh-proxy/hmac.key"
    try:
        with open(key_file, "r") as f:
            secret = f.read().strip()
            if secret:
                return bytes.fromhex(secret)
    except (FileNotFoundError, ValueError):
        pass

    return None


def generate_nonce() -> str:
    """Generate a new UUID v4 nonce."""
    return str(uuid.uuid4())


def get_timestamp_ms() -> int:
    """Get current timestamp in milliseconds."""
    return int(time.time() * 1000)


def compute_hmac(nonce: str, timestamp: int, body: bytes, secret: bytes) -> str:
    """Compute HMAC-SHA256 for request signing.

    Args:
        nonce: UUID v4 nonce string
        timestamp: Unix timestamp in milliseconds
        body: Raw request body bytes
        secret: Shared secret bytes

    Returns:
        Hex-encoded HMAC signature
    """
    message = f"{nonce}{timestamp}".encode("utf-8") + body
    signature = hmac.new(secret, message, hashlib.sha256)
    return signature.hexdigest()


def create_request_headers(body: bytes, secret: bytes) -> dict:
    """Create HMAC authentication headers for a request.

    Args:
        body: Request body bytes
        secret: Shared secret bytes

    Returns:
        Dict with X-Nonce, X-Timestamp, X-HMAC-SHA256 headers
    """
    nonce = generate_nonce()
    timestamp = get_timestamp_ms()
    signature = compute_hmac(nonce, timestamp, body, secret)

    return {
        "X-Nonce": nonce,
        "X-Timestamp": str(timestamp),
        "X-HMAC-SHA256": signature,
    }


def verify_hmac(nonce: str, timestamp: int, body: bytes, signature: str, secret: bytes) -> bool:
    """Verify HMAC signature.

    Args:
        nonce: UUID v4 nonce from X-Nonce header
        timestamp: Timestamp from X-Timestamp header
        body: Raw request body bytes
        signature: HMAC from X-HMAC-SHA256 header
        secret: Shared secret bytes

    Returns:
        True if signature is valid
    """
    expected = compute_hmac(nonce, timestamp, body, secret)
    return hmac.compare_digest(expected, signature)


def verify_timestamp(timestamp: int, tolerance_ms: int = DEFAULT_TIMESTAMP_TOLERANCE_MS) -> Tuple[bool, str]:
    """Verify timestamp is within tolerance.

    Args:
        timestamp: Timestamp from X-Timestamp header (ms)
        tolerance_ms: Maximum allowed skew in milliseconds

    Returns:
        Tuple of (is_valid, error_reason)
    """
    now = get_timestamp_ms()
    skew = abs(now - timestamp)

    if skew > tolerance_ms:
        direction = "future" if timestamp > now else "past"
        return False, f"timestamp_skew_{direction}"

    return True, ""


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
                logger.warning("Replay detected: nonce=%s source=%s", nonce[:8], source)
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
