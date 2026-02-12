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
import time
import uuid
from typing import Optional, Tuple

logger = logging.getLogger("joi.hmac_auth")

# Default timestamp tolerance: 5 minutes (300,000 ms)
DEFAULT_TIMESTAMP_TOLERANCE_MS = 300_000

# Nonce retention: 15 minutes (must be > 2x timestamp tolerance)
NONCE_RETENTION_MS = 15 * 60 * 1000


def get_shared_secret() -> Optional[bytes]:
    """Get the shared secret from environment."""
    secret = os.getenv("JOI_HMAC_SECRET")
    if not secret:
        return None
    return secret.encode("utf-8")


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
    # Concatenate: nonce + timestamp + body
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


class NonceStore:
    """SQLite-backed nonce storage for replay protection.

    Nonces are stored for 15 minutes to prevent replay attacks.
    Cleanup runs periodically to remove expired entries.
    """

    def __init__(self, db_path: str):
        """Initialize nonce store with database path.

        Args:
            db_path: Path to SQLite database file
        """
        import sqlite3
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._ensure_table()

    def _ensure_table(self):
        """Create replay_nonces table if it doesn't exist."""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS replay_nonces (
                nonce TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                received_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_nonces_expires ON replay_nonces(expires_at)
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_nonces_source ON replay_nonces(source, received_at)
        """)
        self._conn.commit()

    def check_and_store(self, nonce: str, source: str = "mesh") -> Tuple[bool, str]:
        """Check if nonce is new and store it.

        Args:
            nonce: The nonce to check
            source: Request source identifier

        Returns:
            Tuple of (is_new, error_reason)
            - (True, "") if nonce is new and was stored
            - (False, "replay_detected") if nonce was seen before
        """
        now = get_timestamp_ms()
        expires_at = now + NONCE_RETENTION_MS

        # First, cleanup expired nonces (every call, lightweight)
        self._cleanup_expired(now)

        # Check if nonce exists
        cursor = self._conn.execute(
            "SELECT 1 FROM replay_nonces WHERE nonce = ?",
            (nonce,)
        )
        if cursor.fetchone() is not None:
            logger.warning("Replay detected: nonce=%s source=%s", nonce[:8], source)
            return False, "replay_detected"

        # Store new nonce
        try:
            self._conn.execute(
                "INSERT INTO replay_nonces (nonce, source, received_at, expires_at) VALUES (?, ?, ?, ?)",
                (nonce, source, now, expires_at)
            )
            self._conn.commit()
            return True, ""
        except Exception as e:
            # Race condition: another thread may have inserted
            logger.warning("Nonce insert failed (possible race): %s", e)
            return False, "replay_detected"

    def _cleanup_expired(self, now_ms: int):
        """Remove expired nonces."""
        self._conn.execute(
            "DELETE FROM replay_nonces WHERE expires_at < ?",
            (now_ms,)
        )
        # Don't commit here - will be committed with the nonce insert


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
