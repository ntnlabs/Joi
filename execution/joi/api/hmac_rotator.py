"""
HMAC Key Rotator for Joi.

Handles automatic and manual HMAC key rotation between Joi and Mesh.
Supports grace period during rotation to handle in-flight requests.
"""

import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import httpx

from hmac_auth import create_request_headers, get_shared_secret, HMAC_SECRET_FILE

if TYPE_CHECKING:
    from policy_manager import PolicyManager

logger = logging.getLogger(__name__)

# Grace period: old key remains valid for this duration after rotation
DEFAULT_GRACE_PERIOD_MS = 60_000  # 60 seconds

MESH_ROTATION_ENDPOINT = "/config/sync"


class HMACRotator:
    """
    Handles HMAC key rotation with grace period support.

    Rotation flow:
    1. Generate new 32-byte secret
    2. Push to mesh with grace period info
    3. After mesh confirms, update local env file
    4. Old key remains valid for grace period
    """

    def __init__(
        self,
        mesh_url: str,
        policy_manager: "PolicyManager",
        grace_period_ms: int = DEFAULT_GRACE_PERIOD_MS,
    ):
        self._mesh_url = mesh_url
        self._policy = policy_manager
        self._grace_period_ms = grace_period_ms
        self._lock = threading.RLock()  # Reentrant - rotate() calls get_current_secret()

        # Live secret (updated after rotation, used for signing)
        self._current_secret: Optional[bytes] = None

        # Track old secret during grace period (for verification)
        self._old_secret: Optional[bytes] = None
        self._old_secret_expires: float = 0

        # Track last rotation time
        self._last_rotation_time: Optional[float] = None
        self._state_file = Path("/var/lib/joi/hmac-rotation-state.json")

        # Track failed rotations for retry logic
        self._failed_rotation_count: int = 0
        self._last_failed_rotation: Optional[float] = None

        self._load_state()

    def _load_state(self) -> None:
        """Load rotation state from disk."""
        if self._state_file.exists():
            try:
                with open(self._state_file, "r") as f:
                    state = json.load(f)
                self._last_rotation_time = state.get("last_rotation_time")
                self._failed_rotation_count = state.get("failed_rotation_count", 0)
                self._last_failed_rotation = state.get("last_failed_rotation")
            except Exception as e:
                logger.warning("Failed to load rotation state: %s", e)

    def _save_state(self) -> None:
        """Save rotation state to disk."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._state_file, "w") as f:
                json.dump({
                    "last_rotation_time": self._last_rotation_time,
                    "failed_rotation_count": self._failed_rotation_count,
                    "last_failed_rotation": self._last_failed_rotation,
                }, f)
        except Exception as e:
            logger.error("Failed to save rotation state: %s", e)

    def rotate(self, use_grace_period: bool = True) -> tuple[bool, str]:
        """
        Execute HMAC key rotation with two-phase commit.

        Flow:
        1. Save backup of current secret file
        2. Write new secret to local file
        3. Push to mesh (mesh accepts both old and new during grace period)
        4. If push fails, rollback local file
        5. Update memory and state

        Args:
            use_grace_period: If True, old key remains valid for grace period.
                              If False (incident response), immediate switchover.

        Returns:
            (success, message)
        """
        with self._lock:
            logger.info("Starting HMAC key rotation (grace_period=%s)", use_grace_period)

            # Generate new secret
            new_secret = secrets.token_bytes(32)
            new_secret_hex = new_secret.hex()

            # Get current secret for signing the rotation request
            current_secret = self.get_current_secret()
            if not current_secret:
                return False, "no_current_secret"
            current_secret_hex = current_secret.hex()

            # Phase 1: Write new secret to local file (with backup for rollback)
            backup_created = self._backup_secret_file()
            if not self._update_secret_file(new_secret_hex):
                self._restore_secret_file()
                self._record_failed_rotation("local_write_failed")
                return False, "failed_to_update_secret_file"

            # Phase 2: Push to mesh
            grace_ms = self._grace_period_ms if use_grace_period else 0
            config = self._policy.get_config_for_push()
            config["hmac_rotation"] = {
                "new_secret": new_secret_hex,
                "effective_at_ms": int(time.time() * 1000) + grace_ms,
                "grace_period_ms": grace_ms,
            }

            push_success = False
            push_error = ""
            try:
                body = json.dumps(config).encode("utf-8")
                headers = {"Content-Type": "application/json"}
                # Sign with CURRENT secret (mesh will accept it during grace period)
                hmac_headers = create_request_headers(body, current_secret)
                headers.update(hmac_headers)

                url = f"{self._mesh_url}{MESH_ROTATION_ENDPOINT}"
                with httpx.Client(timeout=10.0) as client:
                    resp = client.post(url, content=body, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()

                if data.get("status") == "ok":
                    push_success = True
                else:
                    push_error = data.get("error", "unknown")
                    logger.error("Mesh rejected rotation: %s", push_error)

            except httpx.HTTPStatusError as e:
                push_error = f"http_error_{e.response.status_code}"
                logger.error("Rotation HTTP error: %s", e)
            except Exception as e:
                push_error = str(e)
                logger.error("Rotation push failed: %s", e)

            # Phase 3: Commit or rollback
            if not push_success:
                # Rollback local file
                logger.warning("Rotation failed, rolling back local secret file")
                if backup_created:
                    self._restore_secret_file()
                else:
                    # No backup - restore by writing current secret
                    self._update_secret_file(current_secret_hex)
                self._record_failed_rotation(push_error)
                return False, push_error

            # Success - update memory and state
            if use_grace_period:
                self._old_secret = current_secret
                self._old_secret_expires = time.time() + (grace_ms / 1000)
                logger.info("Old key valid until: %s", time.ctime(self._old_secret_expires))

            # Store new secret in memory for immediate use
            self._current_secret = new_secret

            # Clear failed rotation tracking
            self._failed_rotation_count = 0
            self._last_failed_rotation = None

            # Update state
            self._last_rotation_time = time.time()
            self._save_state()

            # Clean up backup file
            self._cleanup_backup()

            logger.info("HMAC rotation complete, new key active")
            return True, "ok"

    def _record_failed_rotation(self, reason: str) -> None:
        """Record a failed rotation attempt."""
        self._failed_rotation_count += 1
        self._last_failed_rotation = time.time()
        self._save_state()
        logger.warning(
            "Rotation failed (attempt %d): %s",
            self._failed_rotation_count, reason
        )

    def _backup_secret_file(self) -> bool:
        """Create backup of current secret file."""
        backup_path = HMAC_SECRET_FILE.with_suffix(".backup")
        try:
            if HMAC_SECRET_FILE.exists():
                import shutil
                shutil.copy2(HMAC_SECRET_FILE, backup_path)
                return True
        except Exception as e:
            logger.warning("Failed to backup secret file: %s", e)
        return False

    def _restore_secret_file(self) -> bool:
        """Restore secret file from backup."""
        backup_path = HMAC_SECRET_FILE.with_suffix(".backup")
        try:
            if backup_path.exists():
                import shutil
                shutil.copy2(backup_path, HMAC_SECRET_FILE)
                logger.info("Restored secret file from backup")
                return True
        except Exception as e:
            logger.error("Failed to restore secret file: %s", e)
        return False

    def _cleanup_backup(self) -> None:
        """Remove backup file after successful rotation."""
        backup_path = HMAC_SECRET_FILE.with_suffix(".backup")
        try:
            if backup_path.exists():
                backup_path.unlink()
        except Exception as e:
            logger.warning("Failed to cleanup backup file: %s", e)

    def _update_secret_file(self, new_secret_hex: str) -> bool:
        """Update HMAC secret in writable secret file.

        Writes to /var/lib/joi/hmac.secret (or JOI_HMAC_SECRET_FILE env).
        This location is writable by the joi user, unlike /etc/default/.
        """
        try:
            # Ensure parent directory exists
            HMAC_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)

            # Atomic write: temp file then rename
            temp_file = HMAC_SECRET_FILE.with_suffix(".tmp")
            temp_file.write_text(new_secret_hex + "\n")
            temp_file.rename(HMAC_SECRET_FILE)

            # Set restrictive permissions (owner read/write only)
            HMAC_SECRET_FILE.chmod(0o600)

            logger.info("Updated HMAC secret in %s", HMAC_SECRET_FILE)
            return True

        except Exception as e:
            logger.error("Failed to update secret file %s: %s", HMAC_SECRET_FILE, e)
            return False

    def get_current_secret(self) -> Optional[bytes]:
        """
        Get current secret for signing outbound requests.

        Returns in-memory secret if rotated, otherwise falls back to env.
        """
        with self._lock:
            if self._current_secret:
                return self._current_secret
        return get_shared_secret()

    def get_valid_secrets(self) -> list[bytes]:
        """
        Get list of valid secrets (current + old during grace period).

        Used for HMAC verification to accept both keys during rotation.
        """
        secrets_list = []

        # Use in-memory secret if available, otherwise env
        current = self.get_current_secret()
        if current:
            secrets_list.append(current)

        with self._lock:
            if self._old_secret and time.time() < self._old_secret_expires:
                secrets_list.append(self._old_secret)

        return secrets_list

    def get_last_rotation_time(self) -> Optional[float]:
        """Get timestamp of last rotation."""
        return self._last_rotation_time

    def should_rotate(
        self,
        interval_seconds: int = 7 * 24 * 3600,
        retry_interval_seconds: int = 3600,
    ) -> bool:
        """
        Check if rotation is due based on interval (default: weekly).

        Also returns True if there are failed rotations that need retry
        (retries every hour by default, with exponential backoff).
        """
        # Check for failed rotation retry
        if self._failed_rotation_count > 0 and self._last_failed_rotation:
            # Exponential backoff: 1h, 2h, 4h, max 24h
            backoff_multiplier = min(2 ** (self._failed_rotation_count - 1), 24)
            retry_after = retry_interval_seconds * backoff_multiplier
            if (time.time() - self._last_failed_rotation) >= retry_after:
                logger.info(
                    "Rotation retry due (attempt %d, backoff %ds)",
                    self._failed_rotation_count + 1, retry_after
                )
                return True

        # Normal interval-based rotation
        if self._last_rotation_time is None:
            return False  # Never rotated - initial setup is manual
        return (time.time() - self._last_rotation_time) >= interval_seconds

    def check_mesh_sync(self) -> tuple[bool, str]:
        """
        Verify that mesh accepts our current HMAC secret.

        Makes a lightweight authenticated request to mesh to detect key mismatch.
        Call this on startup to catch sync issues early.

        Returns:
            (in_sync, message)
        """
        current_secret = self.get_current_secret()
        if not current_secret:
            return False, "no_local_secret"

        try:
            # Use a lightweight endpoint - config sync with GET (read-only)
            body = b""
            headers = {"Content-Type": "application/json"}
            hmac_headers = create_request_headers(body, current_secret)
            headers.update(hmac_headers)

            # Use health endpoint if available, or a simple authenticated GET
            url = f"{self._mesh_url}/health"
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(url, headers=headers)

                if resp.status_code == 401:
                    logger.error("HMAC sync check failed: mesh rejected our secret")
                    return False, "hmac_mismatch"

                if resp.status_code == 200:
                    logger.debug("HMAC sync check passed")
                    return True, "ok"

                # Other errors might be network issues, not HMAC
                logger.warning("HMAC sync check: unexpected status %d", resp.status_code)
                return True, f"status_{resp.status_code}"  # Assume OK if not 401

        except Exception as e:
            logger.warning("HMAC sync check failed: %s", e)
            return False, f"network_error: {e}"

    def get_rotation_status(self) -> dict:
        """
        Get current rotation status for diagnostics.

        Returns dict with rotation state info.
        """
        with self._lock:
            return {
                "enabled": True,
                "last_rotation_time": self._last_rotation_time,
                "last_rotation_ago_seconds": (
                    time.time() - self._last_rotation_time
                    if self._last_rotation_time else None
                ),
                "failed_rotation_count": self._failed_rotation_count,
                "last_failed_rotation": self._last_failed_rotation,
                "has_in_memory_secret": self._current_secret is not None,
                "old_secret_active": (
                    self._old_secret is not None
                    and time.time() < self._old_secret_expires
                ),
                "old_secret_expires_in": (
                    self._old_secret_expires - time.time()
                    if self._old_secret and time.time() < self._old_secret_expires
                    else None
                ),
            }
