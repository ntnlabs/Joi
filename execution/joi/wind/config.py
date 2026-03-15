"""
Wind configuration dataclass.
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class WindConfig:
    """Configuration for Wind proactive messaging system."""

    # Global toggles
    enabled: bool = False
    shadow_mode: bool = True  # Phase 1: log only, no sends

    # Hard gates
    quiet_hours_start: int = 23  # 23:00 local time
    quiet_hours_end: int = 7  # 07:00 local time
    min_cooldown_seconds: int = 3600  # 1 hour between proactives
    daily_cap: int = 3  # Max proactive messages per day
    max_unanswered_streak: int = 2  # Stop after N unanswered proactives
    min_silence_seconds: int = 1800  # 30 min since last user msg

    # Hot conversation suppression (Phase 5)
    active_convo_gap_seconds: int = 120    # EMA <= this → hot conversation (2 min)
    active_convo_silence_seconds: int = 3600  # required silence when hot (1 hour)
    active_convo_ema_alpha: float = 0.3    # EMA smoothing factor

    # Impulse thresholds
    impulse_threshold: float = 0.6  # Minimum score to trigger

    # Factor weights for impulse calculation
    base_impulse: float = 0.1
    silence_weight: float = 0.3
    silence_cap_hours: float = 24.0
    topic_pressure_weight: float = 0.2
    fatigue_weight: float = 0.3
    engagement_weight: float = 0.2  # Phase 4a: boost/dampen based on engagement score

    # Phase 4a: Engagement tracking
    ignore_timeout_hours: float = 12.0  # Hours before topic is considered ignored

    # WindMood: threshold drift bounds (random walk)
    threshold_drift_min: float = -0.1  # Can drift 0.1 below baseline
    threshold_drift_max: float = 0.1   # Can drift 0.1 above baseline
    threshold_drift_step: float = 0.01  # Max change per tick
    threshold_mean_reversion: float = 0.01  # 1% pull toward baseline per tick

    # WindMood: soft probability settings
    soft_trigger_steepness: float = 10.0  # Sigmoid steepness (higher = sharper)

    # Allowlist (conversation IDs eligible for Wind)
    allowlist: List[str] = field(default_factory=list)

    # Timezone for quiet hours (IANA format)
    timezone: str = "Europe/Bratislava"

    @classmethod
    def from_dict(cls, data: dict) -> "WindConfig":
        """Create WindConfig from dictionary (e.g., from policy)."""
        return cls(
            enabled=data.get("enabled", False),
            shadow_mode=data.get("shadow_mode", True),
            quiet_hours_start=data.get("quiet_hours_start", 23),
            quiet_hours_end=data.get("quiet_hours_end", 7),
            min_cooldown_seconds=data.get("min_cooldown_seconds", 3600),
            daily_cap=data.get("daily_cap", 3),
            max_unanswered_streak=data.get("max_unanswered_streak", 2),
            min_silence_seconds=data.get("min_silence_seconds", 1800),
            active_convo_gap_seconds=data.get("active_convo_gap_seconds", 120),
            active_convo_silence_seconds=data.get("active_convo_silence_seconds", 3600),
            active_convo_ema_alpha=data.get("active_convo_ema_alpha", 0.3),
            impulse_threshold=data.get("impulse_threshold", 0.6),
            base_impulse=data.get("base_impulse", 0.1),
            silence_weight=data.get("silence_weight", 0.3),
            silence_cap_hours=data.get("silence_cap_hours", 24.0),
            topic_pressure_weight=data.get("topic_pressure_weight", 0.2),
            fatigue_weight=data.get("fatigue_weight", 0.3),
            engagement_weight=data.get("engagement_weight", 0.2),
            ignore_timeout_hours=data.get("ignore_timeout_hours", 12.0),
            threshold_drift_min=data.get("threshold_drift_min", -0.1),
            threshold_drift_max=data.get("threshold_drift_max", 0.1),
            threshold_drift_step=data.get("threshold_drift_step", 0.01),
            threshold_mean_reversion=data.get("threshold_mean_reversion", 0.01),
            soft_trigger_steepness=data.get("soft_trigger_steepness", 10.0),
            allowlist=list(data.get("allowlist", [])),
            timezone=data.get("timezone", "Europe/Bratislava"),
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "enabled": self.enabled,
            "shadow_mode": self.shadow_mode,
            "quiet_hours_start": self.quiet_hours_start,
            "quiet_hours_end": self.quiet_hours_end,
            "min_cooldown_seconds": self.min_cooldown_seconds,
            "daily_cap": self.daily_cap,
            "max_unanswered_streak": self.max_unanswered_streak,
            "min_silence_seconds": self.min_silence_seconds,
            "active_convo_gap_seconds": self.active_convo_gap_seconds,
            "active_convo_silence_seconds": self.active_convo_silence_seconds,
            "active_convo_ema_alpha": self.active_convo_ema_alpha,
            "impulse_threshold": self.impulse_threshold,
            "base_impulse": self.base_impulse,
            "silence_weight": self.silence_weight,
            "silence_cap_hours": self.silence_cap_hours,
            "topic_pressure_weight": self.topic_pressure_weight,
            "fatigue_weight": self.fatigue_weight,
            "engagement_weight": self.engagement_weight,
            "ignore_timeout_hours": self.ignore_timeout_hours,
            "threshold_drift_min": self.threshold_drift_min,
            "threshold_drift_max": self.threshold_drift_max,
            "threshold_drift_step": self.threshold_drift_step,
            "threshold_mean_reversion": self.threshold_mean_reversion,
            "soft_trigger_steepness": self.soft_trigger_steepness,
            "allowlist": list(self.allowlist),
            "timezone": self.timezone,
        }
