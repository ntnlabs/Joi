"""
Wind configuration dataclass.
"""

from dataclasses import dataclass, field
from typing import List


def _parse_quiet_minutes(value, default: int) -> int:
    """Accept int (hours), int (already minutes >= 24 treated as minutes), or 'HH:MM' string."""
    if isinstance(value, str) and ":" in value:
        h, m = value.split(":", 1)
        return int(h) * 60 + int(m)
    val = int(value)
    if val < 24:          # old-style: bare hour
        return val * 60
    return val            # already minutes


@dataclass
class WindConfig:
    """Configuration for Wind proactive messaging system."""

    # Global toggles
    enabled: bool = False
    shadow_mode: bool = True  # Phase 1: log only, no sends

    # Hard gates
    quiet_hours_start: int = 1380  # 23:00 local time (minutes since midnight)
    quiet_hours_end: int = 420    # 07:00 local time (minutes since midnight)
    min_cooldown_minutes: int = 60   # 1 hour between proactives
    daily_cap: int = 3  # Max proactive messages per day
    daily_cap_boost_moderate_minutes: int = 60  # EMA below this → cap +1
    daily_cap_boost_active_minutes: int = 15    # EMA below this → cap +2
    max_unanswered_streak: int = 2  # Stop after N unanswered proactives
    min_silence_minutes: int = 30   # 30 min since last user msg

    # Hot conversation suppression (Phase 5)
    active_convo_gap_minutes: int = 2    # EMA <= this → heated conversation (2 min)
    active_convo_hot_gap_minutes: int = 3  # EMA <= this → hot conversation (3 min)
    active_convo_silence_minutes: int = 60  # required silence when hot/heated (1 hour)
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
    mood_weight: float = 0.15       # Phase 4d: max impulse contribution from mood (±0.15 at intensity 1.0)

    # Phase 4a: Engagement tracking
    ignore_timeout_hours: float = 12.0  # Hours before topic is considered ignored

    # Phase 4b: Symmetric decay + novelty bonus
    interest_decay_rate: float = 0.02   # 2%/day decay for interest_weight (slower than rejection's 5%)
    novelty_weight: float = 0.1         # Impulse bonus when best pending topic is from unexplored family

    # Phase 4b: Affinity bonus
    affinity_weight: float = 0.15       # Max impulse boost from high-interest topic families

    # Phase 4b: Pursuit back-off (retry delays per attempt)
    pursuit_backoff_hours: List[int] = field(default_factory=lambda: [4, 12, 24])

    # Phase 4b: Cooldown anti-periodicity
    cooldown_days: int = 9              # Center of cooldown window (±jitter)
    cooldown_jitter_days: int = 2       # ±N random days → actual cooldown is 7–11 days

    # Phase 4b: Undertaker (permanent block for deeply rejected families)
    undertaker_threshold: float = 2.0   # rejection_weight required to auto-promote (via lifecycle action)

    # Phase 4b: Ghost probe (rare re-check after deep rejection + long silence)
    ghost_probe_days: int = 60          # Days of silence before ghost probe fires
    ghost_probe_priority: int = 20      # Very low priority — surfaces only when nothing else pending

    # Undertaker poke: autonomous challenge of blocked families (0 = disabled)
    undertaker_poke_days: int = 30

    # Curiosity mining: max pending tension/discovery topics before mining is skipped
    max_pending_mined_topics: int = 1   # Raise to 3+ for faster testing

    # End-of-day LLM dedup pass for near-duplicate pending topics
    topic_dedup_enabled: bool = True

    # Phase 5: Topic priority decay (end-of-day)
    topic_priority_decay_points: int = 4   # Base decay points/day; scales up with queue depth via sqrt
    topic_priority_decay_reference: int = 8  # Queue depth at which decay equals base_points exactly (0 = disable)

    # Minimum silence before daily housekeeping tasks fire (separate from min_silence_minutes)
    daily_tasks_silence_minutes: int = 30

    # Clock time (minutes since midnight) at which end-of-day tasks are eligible to fire
    end_of_day_time: int = 180  # 03:00 local time

    # Emotional depth mining (end-of-day)
    emotional_mining_enabled: bool = True
    emotional_day_char_limit: int = 8000   # max chars of day context fed to LLM
    emotional_min_message_chars: int = 20  # skip messages shorter than this

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
            quiet_hours_start=_parse_quiet_minutes(data.get("quiet_hours_start", 1380), 1380),
            quiet_hours_end=_parse_quiet_minutes(data.get("quiet_hours_end", 420), 420),
            min_cooldown_minutes=data.get("min_cooldown_minutes", 60),
            daily_cap=data.get("daily_cap", 3),
            daily_cap_boost_moderate_minutes=data.get("daily_cap_boost_moderate_minutes", 60),
            daily_cap_boost_active_minutes=data.get("daily_cap_boost_active_minutes", 15),
            max_unanswered_streak=data.get("max_unanswered_streak", 2),
            min_silence_minutes=data.get("min_silence_minutes", 30),
            active_convo_gap_minutes=data.get("active_convo_gap_minutes", 2),
            active_convo_hot_gap_minutes=data.get("active_convo_hot_gap_minutes", 3),
            active_convo_silence_minutes=data.get("active_convo_silence_minutes", 60),
            active_convo_ema_alpha=data.get("active_convo_ema_alpha", 0.3),
            impulse_threshold=data.get("impulse_threshold", 0.6),
            base_impulse=data.get("base_impulse", 0.1),
            silence_weight=data.get("silence_weight", 0.3),
            silence_cap_hours=data.get("silence_cap_hours", 24.0),
            topic_pressure_weight=data.get("topic_pressure_weight", 0.2),
            fatigue_weight=data.get("fatigue_weight", 0.3),
            engagement_weight=data.get("engagement_weight", 0.2),
            mood_weight=data.get("mood_weight", 0.15),
            ignore_timeout_hours=data.get("ignore_timeout_hours", 12.0),
            interest_decay_rate=data.get("interest_decay_rate", 0.02),
            novelty_weight=data.get("novelty_weight", 0.1),
            affinity_weight=data.get("affinity_weight", 0.15),
            pursuit_backoff_hours=list(data.get("pursuit_backoff_hours", [4, 12, 24])),
            cooldown_days=data.get("cooldown_days", 9),
            cooldown_jitter_days=data.get("cooldown_jitter_days", 2),
            undertaker_threshold=data.get("undertaker_threshold", 2.0),
            ghost_probe_days=data.get("ghost_probe_days", 60),
            ghost_probe_priority=data.get("ghost_probe_priority", 20),
            undertaker_poke_days=data.get("undertaker_poke_days", 30),
            max_pending_mined_topics=data.get("max_pending_mined_topics", 1),
            topic_dedup_enabled=data.get("topic_dedup_enabled", True),
            topic_priority_decay_points=int(data.get("topic_priority_decay_points", 4)),
            topic_priority_decay_reference=int(data.get("topic_priority_decay_reference", 8)),
            daily_tasks_silence_minutes=data.get("daily_tasks_silence_minutes", 30),
            end_of_day_time=_parse_quiet_minutes(data.get("end_of_day_time", 180), 180),
            emotional_mining_enabled=data.get("emotional_mining_enabled", True),
            emotional_day_char_limit=int(data.get("emotional_day_char_limit", 8000)),
            emotional_min_message_chars=int(data.get("emotional_min_message_chars", 20)),
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
            "min_cooldown_minutes": self.min_cooldown_minutes,
            "daily_cap": self.daily_cap,
            "daily_cap_boost_moderate_minutes": self.daily_cap_boost_moderate_minutes,
            "daily_cap_boost_active_minutes": self.daily_cap_boost_active_minutes,
            "max_unanswered_streak": self.max_unanswered_streak,
            "min_silence_minutes": self.min_silence_minutes,
            "active_convo_gap_minutes": self.active_convo_gap_minutes,
            "active_convo_hot_gap_minutes": self.active_convo_hot_gap_minutes,
            "active_convo_silence_minutes": self.active_convo_silence_minutes,
            "active_convo_ema_alpha": self.active_convo_ema_alpha,
            "impulse_threshold": self.impulse_threshold,
            "base_impulse": self.base_impulse,
            "silence_weight": self.silence_weight,
            "silence_cap_hours": self.silence_cap_hours,
            "topic_pressure_weight": self.topic_pressure_weight,
            "fatigue_weight": self.fatigue_weight,
            "engagement_weight": self.engagement_weight,
            "mood_weight": self.mood_weight,
            "ignore_timeout_hours": self.ignore_timeout_hours,
            "interest_decay_rate": self.interest_decay_rate,
            "novelty_weight": self.novelty_weight,
            "affinity_weight": self.affinity_weight,
            "pursuit_backoff_hours": list(self.pursuit_backoff_hours),
            "cooldown_days": self.cooldown_days,
            "cooldown_jitter_days": self.cooldown_jitter_days,
            "undertaker_threshold": self.undertaker_threshold,
            "ghost_probe_days": self.ghost_probe_days,
            "ghost_probe_priority": self.ghost_probe_priority,
            "undertaker_poke_days": self.undertaker_poke_days,
            "max_pending_mined_topics": self.max_pending_mined_topics,
            "topic_dedup_enabled": self.topic_dedup_enabled,
            "topic_priority_decay_points": self.topic_priority_decay_points,
            "topic_priority_decay_reference": self.topic_priority_decay_reference,
            "daily_tasks_silence_minutes": self.daily_tasks_silence_minutes,
            "end_of_day_time": self.end_of_day_time,
            "emotional_mining_enabled": self.emotional_mining_enabled,
            "emotional_day_char_limit": self.emotional_day_char_limit,
            "emotional_min_message_chars": self.emotional_min_message_chars,
            "threshold_drift_min": self.threshold_drift_min,
            "threshold_drift_max": self.threshold_drift_max,
            "threshold_drift_step": self.threshold_drift_step,
            "threshold_mean_reversion": self.threshold_mean_reversion,
            "soft_trigger_steepness": self.soft_trigger_steepness,
            "allowlist": list(self.allowlist),
            "timezone": self.timezone,
        }
