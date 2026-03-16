"""
Impulse engine for Wind proactive messaging.

Handles hard gates (fast fail) and impulse score calculation.
"""

import logging
import math
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from .config import WindConfig
from .feedback import TopicFeedbackManager
from .state import WindStateManager, WindState
from .topics import TopicManager

logger = logging.getLogger("joi.wind.impulse")


@dataclass
class GateResult:
    """Result of hard gate checks."""

    passed: bool
    failed_gate: Optional[str] = None
    gate_details: Dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary for logging."""
        return {
            "passed": self.passed,
            "failed_gate": self.failed_gate,
            "gate_details": self.gate_details,
        }


@dataclass
class ImpulseResult:
    """Result of impulse calculation."""

    eligible: bool  # Passed all gates
    gate_result: GateResult
    score: float = 0.0
    threshold: float = 0.6
    above_threshold: bool = False
    factors: Dict[str, float] = field(default_factory=dict)
    # WindMood fields
    threshold_offset: float = 0.0
    accumulated_impulse: float = 0.0

    @property
    def should_send(self) -> bool:
        """Whether we should send a proactive message."""
        return self.eligible and self.above_threshold


class ImpulseEngine:
    """
    Engine for calculating Wind impulse and checking hard gates.

    Hard gates (fast fail order):
    1. Wind globally enabled?
    2. Conversation in allowlist?
    3. Not snoozed?
    4. Not in quiet hours?
    5. Cooldown satisfied?
    6. Daily cap not exceeded?
    7. Unanswered streak OK?
    8. Sufficient silence since last user interaction?
    """

    def __init__(
        self,
        config: WindConfig,
        state_manager: WindStateManager,
        topic_manager: TopicManager,
        feedback_manager: Optional[TopicFeedbackManager] = None,
    ):
        """
        Initialize ImpulseEngine.

        Args:
            config: Wind configuration
            state_manager: Wind state manager
            topic_manager: Topic manager
            feedback_manager: Optional feedback manager for affinity/novelty factors (Phase 4b)
        """
        self.config = config
        self.state_manager = state_manager
        self.topic_manager = topic_manager
        self.feedback_manager = feedback_manager

    def check_gates(
        self,
        conversation_id: str,
        now: Optional[datetime] = None,
    ) -> GateResult:
        """
        Check all hard gates for a conversation.

        Returns GateResult with pass/fail status and details.
        """
        if now is None:
            now = datetime.now()

        gates: Dict[str, bool] = {}

        # Gate 1: Wind globally enabled
        gates["wind_enabled"] = self.config.enabled
        if not gates["wind_enabled"]:
            return GateResult(
                passed=False,
                failed_gate="wind_disabled",
                gate_details=gates,
            )

        # Gate 2: Conversation in allowlist
        gates["in_allowlist"] = conversation_id in self.config.allowlist
        if not gates["in_allowlist"]:
            return GateResult(
                passed=False,
                failed_gate="not_in_allowlist",
                gate_details=gates,
            )

        # Get conversation state
        state = self.state_manager.get_state(conversation_id)

        # Gate 3: Not snoozed
        if state and state.wind_snooze_until:
            gates["not_snoozed"] = now > state.wind_snooze_until
        else:
            gates["not_snoozed"] = True
        if not gates["not_snoozed"]:
            return GateResult(
                passed=False,
                failed_gate="snoozed",
                gate_details=gates,
            )

        # Gate 4: Not in quiet hours
        gates["not_quiet_hours"] = self._check_not_quiet_hours(now)
        if not gates["not_quiet_hours"]:
            return GateResult(
                passed=False,
                failed_gate="quiet_hours",
                gate_details=gates,
            )

        # Gate 5: Cooldown satisfied
        gates["cooldown_ok"] = self._check_cooldown(state, now)
        if not gates["cooldown_ok"]:
            return GateResult(
                passed=False,
                failed_gate="cooldown",
                gate_details=gates,
            )

        # Gate 6: Daily cap not exceeded
        gates["daily_cap_ok"] = self._check_daily_cap(state, now)
        if not gates["daily_cap_ok"]:
            return GateResult(
                passed=False,
                failed_gate="daily_cap_exceeded",
                gate_details=gates,
            )

        # Gate 7: Unanswered streak OK
        if state:
            gates["unanswered_ok"] = state.unanswered_proactive_count < self.config.max_unanswered_streak
        else:
            gates["unanswered_ok"] = True
        if not gates["unanswered_ok"]:
            return GateResult(
                passed=False,
                failed_gate="unanswered_streak",
                gate_details=gates,
            )

        # Gate 8: Sufficient silence since last user interaction
        gates["silence_ok"] = self._check_silence(state, now)
        if not gates["silence_ok"]:
            return GateResult(
                passed=False,
                failed_gate="insufficient_silence",
                gate_details=gates,
            )

        # All gates passed
        return GateResult(passed=True, gate_details=gates)

    def _check_not_quiet_hours(self, now: datetime) -> bool:
        """Check if we're outside quiet hours."""
        # Wind uses naive local datetimes throughout; now.hour is already local time.
        hour = now.hour
        start = self.config.quiet_hours_start
        end = self.config.quiet_hours_end

        # Handle overnight quiet hours (e.g., 23:00 to 07:00)
        if start > end:
            # Quiet if hour >= start OR hour < end
            in_quiet = hour >= start or hour < end
        else:
            # Quiet if hour >= start AND hour < end
            in_quiet = start <= hour < end

        return not in_quiet

    def _check_cooldown(self, state: Optional[WindState], now: datetime) -> bool:
        """Check if cooldown since last proactive is satisfied."""
        if not state or not state.last_proactive_sent_at:
            return True

        elapsed = (now - state.last_proactive_sent_at).total_seconds()
        return elapsed >= self.config.min_cooldown_minutes * 60

    def _check_daily_cap(self, state: Optional[WindState], now: datetime) -> bool:
        """Check if daily cap is not exceeded."""
        if not state:
            return True

        today_bucket = now.strftime("%Y-%m-%d")

        # Reset count if different day
        if state.proactive_day_bucket != today_bucket:
            return True

        return state.proactive_sent_today < self.config.daily_cap

    def _check_silence(self, state: Optional[WindState], now: datetime) -> bool:
        """Check if sufficient silence since last user interaction.

        Hot conversations (low gap EMA) require longer silence before Wind fires.
        """
        if not state or not state.last_user_interaction_at:
            return True

        elapsed = (now - state.last_user_interaction_at).total_seconds()

        if (
            state.convo_gap_ema_seconds is not None
            and state.convo_gap_ema_seconds <= self.config.active_convo_gap_minutes * 60
        ):
            required = self.config.active_convo_silence_minutes * 60
        else:
            required = self.config.min_silence_minutes * 60

        return elapsed >= required

    def calculate_impulse(
        self,
        conversation_id: str,
        now: Optional[datetime] = None,
    ) -> ImpulseResult:
        """
        Calculate full impulse result for a conversation.

        Checks gates first, then calculates score if eligible.
        Uses WindMood for natural variance:
        - Bounded random walk for threshold drift
        - Accumulated impulse across ticks
        - Soft probability for final trigger decision
        """
        if now is None:
            now = datetime.now()

        # Check hard gates first
        gate_result = self.check_gates(conversation_id, now)

        if not gate_result.passed:
            return ImpulseResult(
                eligible=False,
                gate_result=gate_result,
                score=0.0,
                threshold=self.config.impulse_threshold,
                above_threshold=False,
                factors={},
            )

        # Get current state (includes threshold_offset, accumulated_impulse)
        state = self.state_manager.get_state(conversation_id)

        # Calculate current threshold with drift
        current_threshold = self._get_current_threshold(state)

        # Calculate impulse score
        factors = self._calculate_factors(state, conversation_id, now)
        score = sum(factors.values())
        score = round(max(0.0, min(1.0, score)), 2)  # Clamp to [0, 1], round to 2 decimals

        # Accumulate impulse
        prev_accumulated = state.accumulated_impulse if state else 0.0
        new_accumulated = round(prev_accumulated + score, 2)

        # Drift threshold (random walk with mean reversion)
        new_offset = self._drift_threshold(state)

        # Save updated state
        self.state_manager.update_state(
            conversation_id,
            threshold_offset=new_offset,
            accumulated_impulse=new_accumulated,
        )

        # Check if accumulated crosses threshold
        crossed_threshold = new_accumulated >= current_threshold

        # Soft probability if above threshold
        should_trigger = False
        if crossed_threshold:
            trigger_probability = self._soft_trigger_probability(new_accumulated, current_threshold)
            should_trigger = random.random() < trigger_probability

            if should_trigger:
                # Reset accumulator on trigger
                self.state_manager.update_state(conversation_id, accumulated_impulse=0.0)
                new_accumulated = 0.0

        # Record impulse check
        self.state_manager.record_impulse_check(conversation_id)

        return ImpulseResult(
            eligible=True,
            gate_result=gate_result,
            score=score,
            threshold=current_threshold,
            above_threshold=should_trigger,
            factors=factors,
            threshold_offset=new_offset,
            accumulated_impulse=new_accumulated,
        )

    def _get_current_threshold(self, state: Optional[WindState]) -> float:
        """Get current threshold with drift offset."""
        base = self.config.impulse_threshold
        if state and state.threshold_offset is not None:
            return round(base + state.threshold_offset, 3)
        return base

    def _drift_threshold(self, state: Optional[WindState]) -> float:
        """Apply random walk with mean reversion to threshold offset."""
        current_offset = 0.0
        if state and state.threshold_offset is not None:
            current_offset = state.threshold_offset

        # Random step
        step = random.uniform(
            -self.config.threshold_drift_step,
            self.config.threshold_drift_step
        )

        # Mean reversion (pull toward 0)
        reversion = -current_offset * self.config.threshold_mean_reversion

        new_offset = current_offset + step + reversion

        # Clamp to bounds
        new_offset = max(
            self.config.threshold_drift_min,
            min(self.config.threshold_drift_max, new_offset)
        )

        return round(new_offset, 3)

    def _soft_trigger_probability(self, accumulated: float, threshold: float) -> float:
        """Sigmoid probability based on how far accumulated exceeds threshold."""
        excess = accumulated - threshold
        # Sigmoid: 0.5 at threshold, approaches 1.0 as excess grows
        return 1.0 / (1.0 + math.exp(-self.config.soft_trigger_steepness * excess))

    def _calculate_factors(
        self,
        state: Optional[WindState],
        conversation_id: str,
        now: datetime,
    ) -> Dict[str, float]:
        """
        Calculate individual factor contributions to impulse score.

        Returns dict of factor name -> contribution.
        """
        factors: Dict[str, float] = {}

        # Base impulse
        factors["base"] = self.config.base_impulse

        # Silence factor (increases with time since last user msg)
        silence_contribution = 0.0
        if state and state.last_user_interaction_at:
            elapsed_hours = (now - state.last_user_interaction_at).total_seconds() / 3600
            # Cap at configured max
            capped_hours = min(elapsed_hours, self.config.silence_cap_hours)
            # Linear scale: 0 at min_silence, 1.0 at silence_cap_hours
            min_hours = self.config.min_silence_minutes / 60
            if capped_hours > min_hours:
                silence_contribution = (
                    (capped_hours - min_hours) / (self.config.silence_cap_hours - min_hours)
                ) * self.config.silence_weight
        factors["silence"] = silence_contribution

        # Topic pressure factor
        topic_pressure = self.topic_manager.get_topic_pressure(conversation_id)
        factors["topic_pressure"] = topic_pressure * self.config.topic_pressure_weight

        # Fatigue damper (negative, based on recent proactives)
        fatigue_damper = 0.0
        if state and state.proactive_sent_today > 0:
            # More proactives today = more fatigue
            fatigue_ratio = state.proactive_sent_today / self.config.daily_cap
            fatigue_damper = -fatigue_ratio * self.config.fatigue_weight
        factors["fatigue"] = fatigue_damper

        # Phase 4a: Engagement factor (boost/dampen based on engagement score)
        # engagement_score: 0.5 = neutral, >0.5 = boost, <0.5 = dampen
        engagement_contribution = 0.0
        if state:
            # Center around 0.5: score 0.5 = 0 contribution, 1.0 = +weight, 0.0 = -weight
            engagement_deviation = state.engagement_score - 0.5
            engagement_contribution = engagement_deviation * self.config.engagement_weight * 2
        factors["engagement"] = engagement_contribution

        # Phase 4b: Affinity + novelty factors (require feedback_manager)
        affinity_contribution = 0.0
        novelty_contribution = 0.0
        if self.feedback_manager:
            best = self.topic_manager.get_best_topic(conversation_id)
            if best:
                from .feedback import normalize_topic_family
                family = normalize_topic_family(best.topic_type, best.title)
                fb = self.feedback_manager.get_feedback(conversation_id, family)

                # Affinity: high interest_weight boosts impulse for this topic
                if fb and fb.interest_weight > 0:
                    affinity_contribution = fb.interest_weight * self.config.affinity_weight

                # Novelty: unexplored families (never engaged) get a small bonus
                if not fb or fb.engagement_count == 0:
                    novelty_contribution = self.config.novelty_weight

        factors["affinity"] = affinity_contribution
        factors["novelty"] = novelty_contribution

        logger.debug(
            "Impulse factors for %s: base=%.2f silence=%.2f pressure=%.2f "
            "fatigue=%.2f engagement=%.2f affinity=%.2f novelty=%.2f",
            conversation_id,
            factors["base"],
            factors["silence"],
            factors["topic_pressure"],
            factors["fatigue"],
            factors["engagement"],
            factors["affinity"],
            factors["novelty"],
        )

        return factors
