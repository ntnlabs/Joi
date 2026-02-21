"""
Policy Manager for Joi.

Manages the canonical mesh policy that gets pushed to the mesh service.
Joi is the single source of truth for policy configuration.
"""

import copy
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

DEFAULT_POLICY_PATH = "/var/lib/joi/policy/mesh-policy.json"

DEFAULT_POLICY = {
    "version": 1,
    "mode": "companion",  # "companion" (default) or "business"
    "dm_group_knowledge": False,  # Only applies in business mode
    "identity": {
        "bot_name": "Joi",
        "allowed_senders": [],
        "groups": {},
    },
    "rate_limits": {
        "inbound": {
            "max_per_hour": 120,
            "max_per_minute": 20,
        }
    },
    "validation": {
        "max_text_length": 1500,
        "max_timestamp_skew_ms": 300000,
    },
    "security": {
        "privacy_mode": True,
        "kill_switch": False,
    },
}


class PolicyManager:
    """
    Manages canonical mesh policy on Joi side.

    Thread-safe storage and modification of policy configuration.
    Persists to disk and computes hash for sync verification.
    """

    def __init__(self, policy_path: Optional[str] = None):
        self._path = Path(policy_path or os.getenv("JOI_MESH_POLICY_PATH", DEFAULT_POLICY_PATH))
        self._config: Dict[str, Any] = {}
        self._config_hash: str = ""
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        """Load policy from disk, or create default if not exists."""
        with self._lock:
            if self._path.exists():
                try:
                    with open(self._path, "r", encoding="utf-8") as f:
                        self._config = json.load(f)
                    logger.info("Loaded policy from %s", self._path)
                except (json.JSONDecodeError, IOError) as e:
                    logger.error("Failed to load policy from %s: %s", self._path, e)
                    self._config = copy.deepcopy(DEFAULT_POLICY)
            else:
                logger.warning("Policy file not found at %s, using defaults", self._path)
                self._config = copy.deepcopy(DEFAULT_POLICY)
                self._save_unlocked()

            self._update_hash_unlocked()

    def _save_unlocked(self) -> None:
        """Save policy to disk (caller must hold lock)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._config, f, indent=2)
        logger.info("Saved policy to %s", self._path)

    def _save(self) -> None:
        """Save policy to disk."""
        with self._lock:
            self._save_unlocked()

    def _update_hash_unlocked(self) -> None:
        """Update config hash (caller must hold lock)."""
        # Normalize JSON for consistent hashing
        normalized = json.dumps(self._config, sort_keys=True, separators=(",", ":"))
        self._config_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def get_config(self) -> Dict[str, Any]:
        """Get current config for push (deep copy)."""
        with self._lock:
            return json.loads(json.dumps(self._config))

    def get_config_hash(self) -> str:
        """Get SHA256 hash of current config."""
        with self._lock:
            return self._config_hash

    def get_config_for_push(self) -> Dict[str, Any]:
        """Get config with timestamp for pushing to mesh."""
        config = self.get_config()
        config["timestamp_ms"] = int(time.time() * 1000)
        return config

    # --- Identity Section ---

    def get_bot_name(self) -> str:
        """Get current bot name."""
        with self._lock:
            return self._config.get("identity", {}).get("bot_name", "Joi")

    def update_bot_name(self, name: str) -> None:
        """Update bot display name."""
        with self._lock:
            if "identity" not in self._config:
                self._config["identity"] = {}
            self._config["identity"]["bot_name"] = name
            self._update_hash_unlocked()
            self._save_unlocked()
        logger.info("Updated bot_name to: %s", name)

    def get_allowed_senders(self) -> List[str]:
        """Get list of allowed senders."""
        with self._lock:
            return list(self._config.get("identity", {}).get("allowed_senders", []))

    def update_allowed_senders(self, senders: List[str]) -> None:
        """Update allowed senders list."""
        with self._lock:
            if "identity" not in self._config:
                self._config["identity"] = {}
            self._config["identity"]["allowed_senders"] = list(senders)
            self._update_hash_unlocked()
            self._save_unlocked()
        logger.info("Updated allowed_senders: %d entries", len(senders))

    def add_allowed_sender(self, sender: str) -> bool:
        """Add a sender to allowed list. Returns True if added (wasn't already present)."""
        with self._lock:
            if "identity" not in self._config:
                self._config["identity"] = {}
            senders = self._config["identity"].get("allowed_senders", [])
            if sender not in senders:
                senders.append(sender)
                self._config["identity"]["allowed_senders"] = senders
                self._update_hash_unlocked()
                self._save_unlocked()
                logger.info("Added allowed sender: %s", sender)
                return True
            return False

    def remove_allowed_sender(self, sender: str) -> bool:
        """Remove a sender from allowed list. Returns True if removed."""
        with self._lock:
            if "identity" not in self._config:
                return False
            senders = self._config["identity"].get("allowed_senders", [])
            if sender in senders:
                senders.remove(sender)
                self._config["identity"]["allowed_senders"] = senders
                self._update_hash_unlocked()
                self._save_unlocked()
                logger.info("Removed allowed sender: %s", sender)
                return True
            return False

    # --- Groups Section ---

    def get_groups(self) -> Dict[str, Dict[str, Any]]:
        """Get all group configurations."""
        with self._lock:
            return dict(self._config.get("identity", {}).get("groups", {}))

    def update_group(
        self,
        group_id: str,
        participants: List[str],
        names: Optional[List[str]] = None,
    ) -> None:
        """Add or update a group configuration."""
        with self._lock:
            if "identity" not in self._config:
                self._config["identity"] = {}
            if "groups" not in self._config["identity"]:
                self._config["identity"]["groups"] = {}
            self._config["identity"]["groups"][group_id] = {
                "participants": list(participants),
                "names": list(names) if names else [],
            }
            self._update_hash_unlocked()
            self._save_unlocked()
        logger.info("Updated group: %s (%d participants)", group_id[:16], len(participants))

    def remove_group(self, group_id: str) -> bool:
        """Remove a group. Returns True if removed."""
        with self._lock:
            groups = self._config.get("identity", {}).get("groups", {})
            if group_id in groups:
                del groups[group_id]
                self._update_hash_unlocked()
                self._save_unlocked()
                logger.info("Removed group: %s", group_id[:16])
                return True
            return False

    # --- Rate Limits Section ---

    def get_rate_limits(self) -> Dict[str, int]:
        """Get current rate limits."""
        with self._lock:
            inbound = self._config.get("rate_limits", {}).get("inbound", {})
            return {
                "max_per_hour": inbound.get("max_per_hour", 120),
                "max_per_minute": inbound.get("max_per_minute", 20),
            }

    def update_rate_limits(self, max_per_hour: int, max_per_minute: int) -> None:
        """Update rate limit settings."""
        with self._lock:
            if "rate_limits" not in self._config:
                self._config["rate_limits"] = {}
            self._config["rate_limits"]["inbound"] = {
                "max_per_hour": max_per_hour,
                "max_per_minute": max_per_minute,
            }
            self._update_hash_unlocked()
            self._save_unlocked()
        logger.info("Updated rate limits: %d/hour, %d/minute", max_per_hour, max_per_minute)

    # --- Validation Section ---

    def get_validation(self) -> Dict[str, int]:
        """Get validation settings."""
        with self._lock:
            validation = self._config.get("validation", {})
            return {
                "max_text_length": validation.get("max_text_length", 1500),
                "max_timestamp_skew_ms": validation.get("max_timestamp_skew_ms", 300000),
            }

    def update_validation(
        self,
        max_text_length: Optional[int] = None,
        max_timestamp_skew_ms: Optional[int] = None,
    ) -> None:
        """Update validation settings."""
        with self._lock:
            if "validation" not in self._config:
                self._config["validation"] = {}
            if max_text_length is not None:
                self._config["validation"]["max_text_length"] = max_text_length
            if max_timestamp_skew_ms is not None:
                self._config["validation"]["max_timestamp_skew_ms"] = max_timestamp_skew_ms
            self._update_hash_unlocked()
            self._save_unlocked()
        logger.info("Updated validation settings")

    # --- Security Section ---

    def get_security(self) -> Dict[str, bool]:
        """Get security settings."""
        with self._lock:
            security = self._config.get("security", {})
            return {
                "privacy_mode": bool(security.get("privacy_mode", False)),
                "kill_switch": bool(security.get("kill_switch", False)),
            }

    def is_privacy_mode(self) -> bool:
        """Check if privacy mode is enabled."""
        with self._lock:
            return bool(self._config.get("security", {}).get("privacy_mode", False))

    def is_kill_switch_active(self) -> bool:
        """Check if kill switch is active."""
        with self._lock:
            return bool(self._config.get("security", {}).get("kill_switch", False))

    def set_privacy_mode(self, enabled: bool) -> None:
        """Enable or disable privacy mode."""
        with self._lock:
            if "security" not in self._config:
                self._config["security"] = {}
            self._config["security"]["privacy_mode"] = enabled
            self._update_hash_unlocked()
            self._save_unlocked()
        logger.info("Privacy mode %s", "enabled" if enabled else "disabled")

    def set_kill_switch(self, active: bool) -> None:
        """
        Activate or deactivate kill switch.

        When active, mesh will not forward messages to Joi.
        Use in emergencies to immediately stop message processing.
        """
        with self._lock:
            if "security" not in self._config:
                self._config["security"] = {}
            self._config["security"]["kill_switch"] = active
            self._update_hash_unlocked()
            self._save_unlocked()
        if active:
            logger.warning("KILL SWITCH ACTIVATED - mesh forwarding will be disabled")
        else:
            logger.info("Kill switch deactivated")

    # --- Full Config Update ---

    def set_config(self, config: Dict[str, Any]) -> None:
        """Replace entire config (used for initial migration)."""
        with self._lock:
            self._config = config
            if "version" not in self._config:
                self._config["version"] = 1
            self._update_hash_unlocked()
            self._save_unlocked()
        logger.info("Replaced full config, hash=%s", self._config_hash[:16])

    def reload(self) -> None:
        """Reload config from disk."""
        self._load()
        logger.info("Reloaded policy from disk, hash=%s", self._config_hash[:16])

    # --- Mode Configuration ---

    def get_mode(self) -> str:
        """Get current mode ('companion' or 'business')."""
        with self._lock:
            return self._config.get("mode", "companion")

    def is_business_mode(self) -> bool:
        """Check if running in business mode."""
        return self.get_mode() == "business"

    def is_dm_group_knowledge_enabled(self) -> bool:
        """
        Check if DM group knowledge access is enabled.

        Companion mode: always returns False (hardcoded security)
        Business mode: returns the dm_group_knowledge config value
        """
        with self._lock:
            mode = self._config.get("mode", "companion")
            if mode != "business":
                return False  # Companion mode = hardcoded OFF
            return bool(self._config.get("dm_group_knowledge", False))

    def set_mode(self, mode: str) -> None:
        """Set operating mode ('companion' or 'business')."""
        if mode not in ("companion", "business"):
            raise ValueError(f"Invalid mode: {mode}. Must be 'companion' or 'business'")
        with self._lock:
            self._config["mode"] = mode
            self._update_hash_unlocked()
            self._save_unlocked()
        logger.info("Mode set to: %s", mode)

    def set_dm_group_knowledge(self, enabled: bool) -> None:
        """Enable or disable DM group knowledge access (only effective in business mode)."""
        with self._lock:
            self._config["dm_group_knowledge"] = enabled
            self._update_hash_unlocked()
            self._save_unlocked()
        logger.info("DM group knowledge %s", "enabled" if enabled else "disabled")
