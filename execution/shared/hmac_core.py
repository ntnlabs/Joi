"""Core HMAC authentication utilities.

Shared between joi and mesh services. Defense-in-depth layer over Nebula VPN.

Header format:
    X-Nonce: <uuid4>
    X-Timestamp: <unix-epoch-ms>
    X-HMAC-SHA256: HMAC-SHA256(nonce + timestamp + body, shared_secret)
"""
import hashlib
import hmac
import time
import uuid
from typing import Tuple


# Default timestamp tolerance: 5 minutes (300,000 ms)
DEFAULT_TIMESTAMP_TOLERANCE_MS = 300_000

# Nonce retention: 15 minutes (must be > 2x timestamp tolerance)
NONCE_RETENTION_MS = 15 * 60 * 1000


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
