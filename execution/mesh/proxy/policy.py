from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Set

from rate_limiter import InboundRateLimiter


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    store_only: bool = False  # True = store for context, but don't respond


class MeshPolicy:
    def __init__(self, policy_path: str):
        self.policy_path = policy_path
        self._config = self._load_config(policy_path)

        identity = self._config.get("identity", {})
        self.allowed_senders: Set[str] = set(identity.get("allowed_senders", []))

        groups = identity.get("groups", {})
        self.group_participants: Dict[str, Set[str]] = {}
        if isinstance(groups, dict):
            for group_id, group_cfg in groups.items():
                if not isinstance(group_cfg, dict):
                    continue
                participants = group_cfg.get("participants", [])
                if isinstance(group_id, str) and isinstance(participants, list):
                    self.group_participants[group_id] = {
                        p for p in participants if isinstance(p, str)
                    }

        inbound_limits = self._config.get("rate_limits", {}).get("inbound", {})
        max_per_hour = int(inbound_limits.get("max_per_hour", 120))
        max_per_minute = int(inbound_limits.get("max_per_minute", 20))
        self.rate_limiter = InboundRateLimiter(max_per_hour=max_per_hour, max_per_minute=max_per_minute)

        validation = self._config.get("validation", {})
        self.max_text_length = int(validation.get("max_text_length", 1500))
        self.max_timestamp_skew_ms = int(validation.get("max_timestamp_skew_ms", 300_000))

    @staticmethod
    def _load_config(path: str) -> Dict[str, Any]:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Policy config file not found: {path}")
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("Policy config must be a JSON object")
        return data

    def evaluate_inbound(self, payload: Dict[str, Any]) -> PolicyDecision:
        sender = self._sender_transport_id(payload)
        if not sender:
            return PolicyDecision(False, "invalid_sender")

        if sender not in self.allowed_senders:
            return PolicyDecision(False, "unknown_sender")

        conversation = payload.get("conversation", {})
        if not isinstance(conversation, dict):
            return PolicyDecision(False, "invalid_conversation")

        convo_type = conversation.get("type")
        convo_id = conversation.get("id")
        if convo_type not in {"direct", "group"} or not isinstance(convo_id, str) or not convo_id:
            return PolicyDecision(False, "invalid_conversation")

        if convo_type == "group":
            allowed_participants = self.group_participants.get(convo_id)
            if allowed_participants is None:
                return PolicyDecision(False, "group_not_allowed")
            # For groups: allow all messages from the group (for context)
            # but mark non-allowed senders as store_only (Joi won't respond)
            sender_allowed = sender in allowed_participants
            if not sender_allowed:
                # Forward for context but don't respond
                return PolicyDecision(True, "store_only", store_only=True)

        validation_result = self._validate_content(payload)
        if not validation_result.allowed:
            return validation_result

        timestamp = payload.get("timestamp")
        if not isinstance(timestamp, int):
            return PolicyDecision(False, "invalid_timestamp")
        now_ms = int(time.time() * 1000)
        if abs(now_ms - timestamp) > self.max_timestamp_skew_ms:
            return PolicyDecision(False, "timestamp_out_of_window")

        limit_result = self.rate_limiter.check_and_add(f"inbound:{sender}", now_ms=now_ms)
        if not limit_result.allowed:
            return PolicyDecision(False, limit_result.reason)

        return PolicyDecision(True, "ok")

    @staticmethod
    def _sender_transport_id(payload: Dict[str, Any]) -> Optional[str]:
        sender = payload.get("sender", {})
        if not isinstance(sender, dict):
            return None
        transport_id = sender.get("transport_id")
        if not isinstance(transport_id, str) or not transport_id:
            return None
        return transport_id

    def _validate_content(self, payload: Dict[str, Any]) -> PolicyDecision:
        content = payload.get("content", {})
        if not isinstance(content, dict):
            return PolicyDecision(False, "invalid_content")

        content_type = content.get("type")
        if content_type not in {"text", "reaction"}:
            return PolicyDecision(False, "unsupported_content_type")

        if content_type == "text":
            text = content.get("text")
            if not isinstance(text, str) or not text:
                return PolicyDecision(False, "invalid_text")
            if len(text) > self.max_text_length:
                return PolicyDecision(False, "text_too_long")

        if content_type == "reaction":
            reaction = content.get("reaction")
            if not isinstance(reaction, str) or not reaction:
                return PolicyDecision(False, "invalid_reaction")

        return PolicyDecision(True, "ok")
