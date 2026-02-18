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

from hmac_auth import create_request_headers, get_shared_secret

if TYPE_CHECKING:
    from policy_manager import PolicyManager

logger = logging.getLogger(__name__)

# Grace period: old key remains valid for this duration after rotation
DEFAULT_GRACE_PERIOD_MS = 60_000  # 60 seconds

# Environment file where HMAC secret is stored
JOI_ENV_FILE = Path("/etc/default/joi-api")
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
        self._lock = threading.Lock()

        # Live secret (updated after rotation, used for signing)
        self._current_secret: Optional[bytes] = None

        # Track old secret during grace period (for verification)
        self._old_secret: Optional[bytes] = None
        self._old_secret_expires: float = 0

        # Track last rotation time
        self._last_rotation_time: Optional[float] = None
        self._state_file = Path("/var/lib/joi/hmac-rotation-state.json")
        self._load_state()

    def _load_state(self) -> None:
        """Load rotation state from disk."""
        if self._state_file.exists():
            try:
                with open(self._state_file, "r") as f:
                    state = json.load(f)
                self._last_rotation_time = state.get("last_rotation_time")
            except Exception as e:
                logger.warning("Failed to load rotation state: %s", e)

    def _save_state(self) -> None:
        """Save rotation state to disk."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._state_file, "w") as f:
                json.dump({
                    "last_rotation_time": self._last_rotation_time,
                }, f)
        except Exception as e:
            logger.error("Failed to save rotation state: %s", e)

    def rotate(self, use_grace_period: bool = True) -> tuple[bool, str]:
        """
        Execute HMAC key rotation.

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

            # Get current secret for signing the rotation request (use live rotated key, not env)
            current_secret = self.get_current_secret()
            if not current_secret:
                return False, "no_current_secret"

            # Build rotation payload
            grace_ms = self._grace_period_ms if use_grace_period else 0
            config = self._policy.get_config_for_push()
            config["hmac_rotation"] = {
                "new_secret": new_secret_hex,
                "effective_at_ms": int(time.time() * 1000) + grace_ms,
                "grace_period_ms": grace_ms,
            }

            # Push to mesh
            try:
                body = json.dumps(config).encode("utf-8")
                headers = {"Content-Type": "application/json"}
                hmac_headers = create_request_headers(body, current_secret)
                headers.update(hmac_headers)

                url = f"{self._mesh_url}{MESH_ROTATION_ENDPOINT}"
                with httpx.Client(timeout=10.0) as client:
                    resp = client.post(url, content=body, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()

                if data.get("status") != "ok":
                    error = data.get("error", "unknown")
                    logger.error("Mesh rejected rotation: %s", error)
                    return False, f"mesh_rejected: {error}"

            except httpx.HTTPStatusError as e:
                logger.error("Rotation HTTP error: %s", e)
                return False, f"http_error_{e.response.status_code}"
            except Exception as e:
                logger.error("Rotation failed: %s", e)
                return False, str(e)

            # Mesh accepted - now update local secret
            if use_grace_period:
                self._old_secret = current_secret
                self._old_secret_expires = time.time() + (grace_ms / 1000)
                logger.info("Old key valid until: %s", time.ctime(self._old_secret_expires))

            # Store new secret in memory for immediate use
            self._current_secret = new_secret

            # Update local env file (for persistence across restarts)
            if not self._update_env_file(new_secret_hex):
                return False, "failed_to_update_env"

            # Update state
            self._last_rotation_time = time.time()
            self._save_state()

            logger.info("HMAC rotation complete, new key active in memory")
            return True, "ok"

    def _update_env_file(self, new_secret_hex: str) -> bool:
        """Update HMAC secret in env file."""
        if not JOI_ENV_FILE.exists():
            logger.warning("Env file not found: %s", JOI_ENV_FILE)
            return False

        try:
            content = JOI_ENV_FILE.read_text()
            lines = content.splitlines()
            new_lines = []
            found = False

            for line in lines:
                if line.startswith("JOI_HMAC_SECRET="):
                    new_lines.append(f'JOI_HMAC_SECRET="{new_secret_hex}"')
                    found = True
                else:
                    new_lines.append(line)

            if not found:
                new_lines.append(f'JOI_HMAC_SECRET="{new_secret_hex}"')

            JOI_ENV_FILE.write_text("\n".join(new_lines) + "\n")
            logger.info("Updated HMAC secret in %s", JOI_ENV_FILE)

            # Note: The new secret takes effect on next restart.
            # For immediate effect, we'd need to reload the module-level HMAC_SECRET.
            # Since we track _old_secret for grace period, this is handled.

            return True

        except Exception as e:
            logger.error("Failed to update env file: %s", e)
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

    def should_rotate(self, interval_seconds: int = 7 * 24 * 3600) -> bool:
        """Check if rotation is due based on interval (default: weekly)."""
        if self._last_rotation_time is None:
            return False  # Never rotated - initial setup is manual
        return (time.time() - self._last_rotation_time) >= interval_seconds
