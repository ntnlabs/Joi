"""
HMAC Key Rotator for Joi.

Handles automatic and manual HMAC key rotation between Joi and Mesh.
Supports grace period during rotation to handle in-flight requests.

Push-first protocol: Never persist new secret until mesh confirms receipt.
This prevents permanent lockout from crashes during rotation.
"""

import json
import logging
import secrets
import threading
import time
import uuid
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

# Pending state file for crash recovery (separate from main state)
PENDING_STATE_FILE = Path("/var/lib/joi/hmac-rotation-pending.json")

# Abandon pending rotations older than this
PENDING_ROTATION_MAX_AGE_SECONDS = 3600  # 1 hour


class HMACRotator:
    """
    Handles HMAC key rotation with grace period support.

    Push-first rotation flow (crash-safe):
    1. Generate new secret, hold in memory as "pending"
    2. Save pending state to file (for crash recovery)
    3. Sign push request with CURRENT secret (mesh knows this)
    4. Push to mesh with new secret
    5. Mesh responds OK - NOW safe to persist to disk
    6. Update memory: current->old, pending->current
    7. Clean up pending state
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
        self._lock = threading.RLock()

        # Key states - explicit, not derived from file
        self._active_secret: Optional[bytes] = None      # Current key for signing
        self._pending_secret: Optional[bytes] = None     # New key awaiting mesh confirmation
        self._old_secret: Optional[bytes] = None         # Previous key (grace period)
        self._old_secret_expires: float = 0

        # Pending rotation tracking
        self._pending_rotation_id: Optional[str] = None
        self._pending_started_at: Optional[float] = None

        # Track last rotation time
        self._last_rotation_time: Optional[float] = None
        self._state_file = Path("/var/lib/joi/hmac-rotation-state.json")

        # Track failed rotations for retry logic
        self._failed_rotation_count: int = 0
        self._last_failed_rotation: Optional[float] = None

        self._load_state()
        self._recover_pending_state()

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
                logger.warning("Failed to load rotation state", extra={"error": str(e)})

    def _save_state(self) -> None:
        """Save rotation state to disk atomically."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: write to temp file, then rename
            tmp_file = self._state_file.with_suffix(".tmp")
            with open(tmp_file, "w") as f:
                json.dump({
                    "last_rotation_time": self._last_rotation_time,
                    "failed_rotation_count": self._failed_rotation_count,
                    "last_failed_rotation": self._last_failed_rotation,
                }, f)
            tmp_file.rename(self._state_file)
        except Exception as e:
            logger.error("Failed to save rotation state", extra={"error": str(e)})

    def _save_pending_state(self, rotation_id: str, secret_hex: str) -> bool:
        """Save pending rotation for crash recovery.

        This is written BEFORE pushing to mesh, so if we crash after mesh accepts
        but before local persist, we can recover on restart.
        """
        try:
            PENDING_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "rotation_id": rotation_id,
                "secret_hex": secret_hex,
                "started_at": time.time(),
            }
            # Atomic write
            tmp = PENDING_STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(state))
            tmp.rename(PENDING_STATE_FILE)
            return True
        except Exception as e:
            logger.error("Failed to save pending state", extra={"error": str(e)})
            return False

    def _clear_pending_state(self) -> None:
        """Clear pending state after success or abandon."""
        try:
            PENDING_STATE_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        self._pending_secret = None
        self._pending_rotation_id = None
        self._pending_started_at = None

    def _recover_pending_state(self) -> None:
        """Called on startup - check for interrupted rotation.

        If pending state exists:
        - If too old (>1 hour), abandon it
        - Otherwise, load pending secret so we can accept it during verification
          (mesh might have started using it)
        """
        if not PENDING_STATE_FILE.exists():
            return

        try:
            state = json.loads(PENDING_STATE_FILE.read_text())
            age = time.time() - state.get("started_at", 0)

            if age > PENDING_ROTATION_MAX_AGE_SECONDS:
                logger.warning("Abandoning stale pending rotation", extra={
                    "action": "pending_abandoned",
                    "age_seconds": int(age),
                    "rotation_id": state.get("rotation_id", "")[:8]
                })
                self._clear_pending_state()
                return

            # Recent pending rotation - mesh might have it
            # Add to valid secrets for verification
            self._pending_secret = bytes.fromhex(state["secret_hex"])
            self._pending_rotation_id = state["rotation_id"]
            self._pending_started_at = state.get("started_at")

            logger.info("Recovered pending rotation state", extra={
                "action": "pending_recovered",
                "rotation_id": state["rotation_id"][:8],
                "age_seconds": int(age)
            })

        except Exception as e:
            logger.warning("Failed to recover pending state", extra={"error": str(e)})
            self._clear_pending_state()

    def rotate(self, use_grace_period: bool = True) -> tuple[bool, str]:
        """
        Execute HMAC key rotation with push-first protocol.

        Push-first flow (crash-safe):
        1. Generate new secret, hold in memory only
        2. Save pending state to file (for crash recovery)
        3. Push to mesh using CURRENT secret for signing
        4. Mesh responds OK - NOW safe to persist to disk
        5. Update memory: current->old, pending->current
        6. Clean up pending state

        This ensures we never write a secret to disk that mesh doesn't know,
        preventing permanent lockout from crashes during rotation.

        Args:
            use_grace_period: If True, old key remains valid for grace period.
                              If False (incident response), immediate switchover.

        Returns:
            (success, message)
        """
        with self._lock:
            rotation_id = str(uuid.uuid4())
            logger.info("Starting HMAC key rotation", extra={
                "action": "rotation_start",
                "rotation_id": rotation_id[:8],
                "grace_period": use_grace_period
            })

            # 1. Generate new secret (don't write to disk yet!)
            new_secret = secrets.token_bytes(32)
            new_secret_hex = new_secret.hex()

            # 2. Get current secret for signing (from memory or file)
            current_secret = self._get_signing_secret()
            if not current_secret:
                logger.error("No current secret available for rotation", extra={
                    "action": "rotation_failed",
                    "reason": "no_current_secret"
                })
                return False, "no_current_secret"

            # 3. Save pending state for crash recovery
            # If we crash after mesh accepts but before local persist, we can recover
            if not self._save_pending_state(rotation_id, new_secret_hex):
                self._record_failed_rotation("pending_state_failed")
                return False, "pending_state_failed"

            self._pending_secret = new_secret
            self._pending_rotation_id = rotation_id
            self._pending_started_at = time.time()

            # 4. Push to mesh using CURRENT secret (mesh knows this one)
            success, error = self._push_to_mesh(new_secret_hex, current_secret, use_grace_period)

            if not success:
                # Mesh didn't accept - clear pending, keep using old key
                # This is safe: we never wrote the new key to disk
                logger.warning("Rotation push failed, clearing pending state", extra={
                    "action": "rotation_failed",
                    "rotation_id": rotation_id[:8],
                    "error": error
                })
                self._clear_pending_state()
                self._record_failed_rotation(error)
                return False, error

            # 5. SUCCESS - NOW safe to persist to disk
            # Mesh has confirmed receipt, so even if we crash now, mesh has the key
            if not self._persist_secret(new_secret_hex):
                # Disk write failed but mesh has the key
                # This is a critical state but not a lockout - mesh will work
                logger.critical("Mesh accepted new key but local persist failed", extra={
                    "action": "rotation_persist_failed",
                    "rotation_id": rotation_id[:8]
                })
                # Don't return failure - mesh has the key, we can recover on restart

            # 6. Update memory state
            grace_ms = self._grace_period_ms if use_grace_period else 0
            if use_grace_period:
                self._old_secret = self._active_secret
                self._old_secret_expires = time.time() + (grace_ms / 1000)
                logger.info("Old key valid during grace period", extra={
                    "action": "grace_period_active",
                    "expires_in_seconds": grace_ms / 1000
                })

            self._active_secret = new_secret

            # Clear failed rotation tracking
            self._failed_rotation_count = 0
            self._last_failed_rotation = None

            # Update state
            self._last_rotation_time = time.time()
            self._save_state()

            # 7. Clean up pending state
            self._clear_pending_state()

            logger.info("HMAC rotation complete", extra={
                "action": "rotation_complete",
                "rotation_id": rotation_id[:8],
                "status": "success"
            })
            return True, "ok"

    def _get_signing_secret(self) -> Optional[bytes]:
        """Get secret for signing outbound requests.

        Priority:
        1. Active secret in memory (set after successful rotation)
        2. File on disk (persisted from previous rotation)
        3. Environment variable (initial setup)

        NEVER returns pending secret - that's not confirmed yet.
        """
        if self._active_secret:
            return self._active_secret
        return get_shared_secret()  # File -> env fallback

    def _push_to_mesh(
        self,
        new_secret_hex: str,
        signing_secret: bytes,
        use_grace_period: bool
    ) -> tuple[bool, str]:
        """Push new secret to mesh, signed with current secret.

        Returns:
            (success, error_message)
        """
        grace_ms = self._grace_period_ms if use_grace_period else 0
        config = self._policy.get_config_for_push()
        config["hmac_rotation"] = {
            "new_secret": new_secret_hex,
            "effective_at_ms": int(time.time() * 1000) + grace_ms,
            "grace_period_ms": grace_ms,
        }

        try:
            body = json.dumps(config).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            # Sign with current secret (mesh knows this one)
            hmac_headers = create_request_headers(body, signing_secret)
            headers.update(hmac_headers)

            url = f"{self._mesh_url}{MESH_ROTATION_ENDPOINT}"
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, content=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()

            if data.get("status") == "ok":
                return True, ""

            error = data.get("error", "unknown")
            logger.error("Mesh rejected rotation", extra={
                "action": "rotation_rejected",
                "error": error
            })
            return False, error

        except httpx.HTTPStatusError as e:
            error = f"http_error_{e.response.status_code}"
            logger.error("Rotation HTTP error", extra={
                "action": "rotation_failed",
                "status_code": e.response.status_code
            })
            return False, error
        except Exception as e:
            logger.error("Rotation push failed", extra={
                "action": "rotation_failed",
                "error": str(e)
            })
            return False, str(e)

    def _persist_secret(self, secret_hex: str) -> bool:
        """Persist secret to disk after mesh confirms.

        This is called AFTER mesh has confirmed receipt, so it's safe
        to write - mesh already knows the key.
        """
        try:
            HMAC_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)

            # Atomic write: temp file then rename
            temp_file = HMAC_SECRET_FILE.with_suffix(".tmp")
            temp_file.write_text(secret_hex + "\n")
            temp_file.rename(HMAC_SECRET_FILE)

            # Set restrictive permissions
            HMAC_SECRET_FILE.chmod(0o600)

            logger.info("Persisted HMAC secret to disk", extra={
                "action": "secret_persisted",
                "path": str(HMAC_SECRET_FILE)
            })
            return True

        except Exception as e:
            logger.error("Failed to persist secret", extra={
                "path": str(HMAC_SECRET_FILE),
                "error": str(e)
            })
            return False

    def _record_failed_rotation(self, reason: str) -> None:
        """Record a failed rotation attempt."""
        self._failed_rotation_count += 1
        self._last_failed_rotation = time.time()
        self._save_state()
        logger.warning("Rotation failed", extra={
            "action": "rotation_failed",
            "attempt": self._failed_rotation_count,
            "reason": reason
        })

    def get_current_secret(self) -> Optional[bytes]:
        """Get current secret for signing outbound requests.

        Alias for _get_signing_secret() for backward compatibility.
        """
        return self._get_signing_secret()

    def get_valid_secrets(self) -> list[bytes]:
        """Get secrets valid for INBOUND verification.

        Accepts:
        - Active secret (current)
        - Old secret (during grace period)
        - Pending secret (mesh might have started using it)

        Used for HMAC verification to accept all potentially valid keys.
        """
        secrets_list = []

        with self._lock:
            # Active secret (current confirmed key)
            if self._active_secret:
                secrets_list.append(self._active_secret)
            else:
                # Fall back to file/env if no rotation has happened
                env_secret = get_shared_secret()
                if env_secret:
                    secrets_list.append(env_secret)

            # Old secret during grace period
            if self._old_secret and time.time() < self._old_secret_expires:
                secrets_list.append(self._old_secret)

            # Pending secret (mesh might have accepted and started using it)
            # This handles the case where we crashed after mesh accepted
            # but before we updated our active secret
            if self._pending_secret:
                secrets_list.append(self._pending_secret)

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
                logger.info("Rotation retry due", extra={
                    "action": "rotation_retry",
                    "attempt": self._failed_rotation_count + 1,
                    "backoff_seconds": retry_after
                })
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

            # Use HMAC-protected ping endpoint (not /health which is exempt from auth)
            url = f"{self._mesh_url}/hmac/ping"
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(url, headers=headers)

                if resp.status_code == 401:
                    logger.error("HMAC sync check failed", extra={
                        "action": "sync_check",
                        "status": "hmac_mismatch"
                    })
                    return False, "hmac_mismatch"

                if resp.status_code == 200:
                    logger.debug("HMAC sync check passed", extra={"action": "sync_check", "status": "ok"})
                    return True, "ok"

                logger.warning("HMAC sync check: unexpected status", extra={
                    "action": "sync_check",
                    "status_code": resp.status_code,
                })
                return False, f"unexpected_status_{resp.status_code}"

        except Exception as e:
            logger.warning("HMAC sync check failed", extra={
                "action": "sync_check",
                "error": str(e)
            })
            return False, f"network_error: {e}"

    def _verify_mesh_accepts(self, secret: bytes) -> bool:
        """Check if mesh accepts a specific secret.

        Used during startup to verify if mesh accepted a pending key.
        """
        try:
            body = b""
            headers = {"Content-Type": "application/json"}
            hmac_headers = create_request_headers(body, secret)
            headers.update(hmac_headers)

            url = f"{self._mesh_url}/hmac/ping"
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(url, headers=headers)
                return resp.status_code == 200
        except Exception:
            return False

    def startup_sync_check(self) -> bool:
        """Verify Joi and Mesh are in sync on startup.

        If we have a pending rotation (interrupted by crash), try to complete
        or abandon it. Then verify current key works with mesh.

        Returns:
            True if in sync (or mesh unreachable - benefit of doubt)
            False if definite HMAC mismatch
        """
        with self._lock:
            # If pending state exists, check if mesh accepted it
            if self._pending_secret:
                rotation_id = self._pending_rotation_id or "unknown"
                logger.info("Checking interrupted rotation on startup", extra={
                    "action": "startup_pending_check",
                    "rotation_id": rotation_id[:8]
                })

                if self._verify_mesh_accepts(self._pending_secret):
                    # Mesh has the new key - complete the rotation
                    logger.info("Mesh accepted pending key, completing rotation", extra={
                        "action": "startup_rotation_completed",
                        "rotation_id": rotation_id[:8]
                    })

                    # Persist to disk (safe - mesh already has it)
                    self._persist_secret(self._pending_secret.hex())

                    # Update memory state
                    self._old_secret = self._active_secret
                    if self._old_secret:
                        self._old_secret_expires = time.time() + (self._grace_period_ms / 1000)

                    self._active_secret = self._pending_secret
                    self._last_rotation_time = time.time()
                    self._save_state()
                    self._clear_pending_state()

                else:
                    # Mesh doesn't have it - abandon pending
                    # This is safe: we still have our old key
                    logger.info("Mesh rejected pending key, abandoning", extra={
                        "action": "startup_pending_abandoned",
                        "rotation_id": rotation_id[:8]
                    })
                    self._clear_pending_state()

        # Now verify current key works
        in_sync, msg = self.check_mesh_sync()
        if not in_sync:
            if msg == "hmac_mismatch":
                logger.error("Startup sync check: HMAC mismatch with mesh", extra={
                    "action": "startup_sync_failed"
                })
                return False
            else:
                logger.warning("Startup sync check: could not confirm sync", extra={
                    "action": "startup_sync_uncertain", "reason": msg
                })
                # Don't abort — mesh may be restarting; HMAC will be verified on first real request

        logger.info("Startup sync check passed", extra={
            "action": "startup_sync_ok",
            "has_pending": self._pending_secret is not None
        })
        return True

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
                "has_active_secret": self._active_secret is not None,
                "has_pending_secret": self._pending_secret is not None,
                "pending_rotation_id": (
                    self._pending_rotation_id[:8]
                    if self._pending_rotation_id else None
                ),
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
