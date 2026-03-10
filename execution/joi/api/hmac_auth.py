"""HMAC authentication for mesh → joi requests.

Defense-in-depth layer over Nebula VPN. See api-contracts.md for spec.
"""
import logging
import os
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

logger = logging.getLogger("joi.hmac_auth")

# Writable secret file (for rotation persistence)
HMAC_SECRET_FILE = Path(os.getenv("JOI_HMAC_SECRET_FILE", "/var/lib/joi/hmac.secret"))

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
    "NonceStore",
    "HMAC_SECRET_FILE",
]


def get_shared_secret() -> Optional[bytes]:
    """Get the shared secret from file or environment.

    Priority:
    1. Secret file (writable, updated by rotation)
    2. Environment variable (initial setup / fallback)
    """
    # Try file first (supports rotation)
    if HMAC_SECRET_FILE.exists():
        try:
            secret = HMAC_SECRET_FILE.read_text().strip()
            if secret:
                # Secret file contains hex-encoded bytes
                return bytes.fromhex(secret)
        except Exception as e:
            logger.warning("Failed to read HMAC secret file", extra={"error": str(e)})

    # Fall back to environment
    secret = os.getenv("JOI_HMAC_SECRET")
    if not secret:
        return None
    # Env var may be hex or raw string - try hex first
    try:
        return bytes.fromhex(secret)
    except ValueError:
        return secret.encode("utf-8")


class NonceStore:
    """SQLite-backed nonce storage for replay protection.

    Nonces are stored for 15 minutes to prevent replay attacks.
    Cleanup runs periodically to remove expired entries.

    Thread-safe: All database operations are serialized via lock.
    """

    def __init__(self, db_path: str):
        """Initialize nonce store with database path.

        Args:
            db_path: Path to SQLite database file
        """
        import sqlite3
        import threading
        self._lock = threading.Lock()
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
        """Check if nonce is new and store it atomically.

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

        with self._lock:
            # First, cleanup expired nonces (every call, lightweight)
            self._cleanup_expired(now)

            # Atomic check-and-store: INSERT OR IGNORE returns 0 rows if duplicate
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO replay_nonces (nonce, source, received_at, expires_at) VALUES (?, ?, ?, ?)",
                (nonce, source, now, expires_at)
            )
            self._conn.commit()

            if cursor.rowcount == 0:
                # Nonce already existed - replay attack
                logger.warning("Replay detected", extra={
                    "nonce": nonce[:8],
                    "source": source,
                    "action": "replay_blocked"
                })
                return False, "replay_detected"

            return True, ""

    def _cleanup_expired(self, now_ms: int):
        """Remove expired nonces. Caller must hold lock."""
        self._conn.execute(
            "DELETE FROM replay_nonces WHERE expires_at < ?",
            (now_ms,)
        )
        # Don't commit here - will be committed with the nonce insert

    def cleanup_expired(self) -> int:
        """Public method to cleanup expired nonces. Returns count deleted."""
        now = get_timestamp_ms()
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM replay_nonces WHERE expires_at < ?",
                (now,)
            )
            deleted = cursor.rowcount
            self._conn.commit()
        if deleted > 0:
            logger.debug("Cleaned up expired nonces", extra={"count": deleted})
        return deleted

    def close(self):
        """Close the database connection."""
        with self._lock:
            self._conn.close()
