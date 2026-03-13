"""
Wind orchestrator - main entry point for proactive messaging.
"""

import logging
from datetime import datetime
from typing import Callable, List, Optional, Tuple

from .config import WindConfig
from .state import WindStateManager
from .topics import TopicManager, PendingTopic
from .logging import WindDecisionLogger
from .impulse import ImpulseEngine, ImpulseResult

logger = logging.getLogger("joi.wind.orchestrator")


class WindOrchestrator:
    """
    Main orchestrator for Wind proactive messaging.

    Coordinates:
    - State manager (per-conversation state)
    - Topic manager (topic queue)
    - Impulse engine (gates + scoring)
    - Decision logger (observability)

    Phase 2: Live sends enabled. Shadow mode available via config for testing.
    """

    def __init__(
        self,
        db_connection_factory: Callable,
        config: Optional[WindConfig] = None,
    ):
        """
        Initialize WindOrchestrator.

        Args:
            db_connection_factory: Callable that returns a database connection
            config: Wind configuration (defaults to disabled)
        """
        self.config = config or WindConfig()
        self.state_manager = WindStateManager(db_connection_factory)
        self.topic_manager = TopicManager(db_connection_factory)
        self.decision_logger = WindDecisionLogger(db_connection_factory)
        self.impulse_engine = ImpulseEngine(
            config=self.config,
            state_manager=self.state_manager,
            topic_manager=self.topic_manager,
        )

    def update_config(self, config: WindConfig) -> None:
        """Update Wind configuration."""
        old = self.config
        config_changed = (
            old.enabled != config.enabled or
            old.shadow_mode != config.shadow_mode or
            old.allowlist != config.allowlist
        )

        self.config = config
        self.impulse_engine.config = config

        if config_changed:
            logger.info(
                "Wind config updated: enabled=%s shadow_mode=%s allowlist=%d",
                config.enabled, config.shadow_mode, len(config.allowlist)
            )

    def check_impulse(
        self,
        conversation_id: str,
        now: Optional[datetime] = None,
    ) -> Tuple[bool, Optional[str], Optional[PendingTopic], float]:
        """
        Check impulse for a single conversation.

        Returns:
            (should_send, skip_reason, selected_topic, impulse_score)
        """
        if now is None:
            now = datetime.now()

        # Calculate impulse
        result = self.impulse_engine.calculate_impulse(conversation_id, now)

        # If not eligible (failed gate), log and return
        if not result.eligible:
            self.decision_logger.log_decision(
                conversation_id=conversation_id,
                eligible=False,
                decision="skip",
                gate_result=result.gate_result.to_dict(),
                impulse_score=0.0,
                threshold=result.threshold,
                skip_reason=result.gate_result.failed_gate,
            )
            return False, result.gate_result.failed_gate, None, 0.0

        # Check if above threshold
        if not result.above_threshold:
            self.decision_logger.log_decision(
                conversation_id=conversation_id,
                eligible=True,
                decision="skip",
                gate_result=result.gate_result.to_dict(),
                impulse_score=result.score,
                threshold=result.threshold,
                factor_breakdown=result.factors,
                skip_reason="below_threshold",
                threshold_offset=result.threshold_offset,
                accumulated_impulse=result.accumulated_impulse,
            )
            return False, "below_threshold", None, result.score

        # Select topic
        topic = self.topic_manager.get_best_topic(conversation_id)
        if not topic:
            self.decision_logger.log_decision(
                conversation_id=conversation_id,
                eligible=True,
                decision="skip",
                gate_result=result.gate_result.to_dict(),
                impulse_score=result.score,
                threshold=result.threshold,
                factor_breakdown=result.factors,
                skip_reason="no_viable_topic",
                threshold_offset=result.threshold_offset,
                accumulated_impulse=result.accumulated_impulse,
            )
            return False, "no_viable_topic", None, result.score

        # Shadow mode: log decision but don't send
        if self.config.shadow_mode:
            self.decision_logger.log_decision(
                conversation_id=conversation_id,
                eligible=True,
                decision="shadow_logged",
                gate_result=result.gate_result.to_dict(),
                impulse_score=result.score,
                threshold=result.threshold,
                factor_breakdown=result.factors,
                selected_topic_id=topic.id,
                draft_message=f"[Shadow] Would send topic: {topic.title}",
                threshold_offset=result.threshold_offset,
                accumulated_impulse=result.accumulated_impulse,
            )
            logger.info(
                "Wind shadow: would send to %s (score=%.2f, threshold=%.2f, accum=%.2f, topic=#%d: %s)",
                conversation_id, result.score, result.threshold, result.accumulated_impulse,
                topic.id, topic.title
            )
            return False, "shadow_mode", topic, result.score

        # Live mode: signal that we should send
        # Caller (scheduler) handles actual LLM generation and sending
        logger.info(
            "Wind live: triggering send to %s (score=%.2f, threshold=%.2f, accum=%.2f, topic=#%d: %s)",
            conversation_id, result.score, result.threshold, result.accumulated_impulse,
            topic.id, topic.title
        )
        return True, None, topic, result.score

    def check_impulse_all(
        self,
        now: Optional[datetime] = None,
    ) -> List[Tuple[str, bool, Optional[str], Optional[PendingTopic], float]]:
        """
        Check impulse for all eligible conversations.

        Only processes conversations in the allowlist.

        Returns:
            List of (conversation_id, should_send, skip_reason, topic, impulse_score)
        """
        if now is None:
            now = datetime.now()

        if not self.config.enabled:
            logger.debug("Wind disabled, skipping check_impulse_all")
            return []

        results = []

        # Only check conversations in allowlist
        for conversation_id in self.config.allowlist:
            try:
                should_send, skip_reason, topic, score = self.check_impulse(conversation_id, now)
                results.append((conversation_id, should_send, skip_reason, topic, score))
            except Exception as e:
                logger.error(
                    "Error checking impulse for %s: %s",
                    conversation_id, e
                )
                results.append((conversation_id, False, f"error: {e}", None, 0.0))

        # Expire stale topics while we're at it
        self.topic_manager.expire_stale_topics()

        return results

    def tick(
        self,
        now: Optional[datetime] = None,
    ) -> List[Tuple[str, bool, Optional[str], Optional[PendingTopic], float]]:
        """
        Scheduler tick entry point.

        Called by the scheduler every interval (default 60s).

        Returns:
            List of (conversation_id, should_send, skip_reason, topic, impulse_score)
            Caller (scheduler) handles actual sends for should_send=True.
        """
        if now is None:
            now = datetime.now()

        if not self.config.enabled:
            return []

        logger.debug("Wind tick", extra={"timestamp": now.isoformat()})

        results = self.check_impulse_all(now)

        # Log summary
        checked = len(results)
        to_send = sum(1 for _, should_send, _, _, _ in results if should_send)

        if checked > 0:
            logger.debug(
                "Wind tick: checked %d conversations, %d to send",
                checked, to_send
            )

        return results

    # --- Utility methods for external use ---

    def record_user_interaction(self, conversation_id: str) -> None:
        """Record that a user sent a message (resets unanswered counter)."""
        self.state_manager.record_user_interaction(conversation_id)

    def record_outbound(self, conversation_id: str) -> None:
        """Record that any outbound message was sent."""
        self.state_manager.record_outbound(conversation_id)

    def add_topic(
        self,
        conversation_id: str,
        topic_type: str,
        title: str,
        content: Optional[str] = None,
        priority: int = 50,
        **kwargs,
    ) -> int:
        """Add a topic to the queue. Returns topic ID."""
        return self.topic_manager.add_topic(
            conversation_id=conversation_id,
            topic_type=topic_type,
            title=title,
            content=content,
            priority=priority,
            **kwargs,
        )

    def get_decision_stats(
        self,
        conversation_id: Optional[str] = None,
        days: int = 7,
    ) -> dict:
        """Get decision statistics."""
        return self.decision_logger.get_decision_stats(conversation_id, days)

    def snooze(self, conversation_id: str, until: datetime) -> None:
        """Snooze Wind for a conversation until specified time."""
        self.state_manager.set_snooze(conversation_id, until)

    def clear_snooze(self, conversation_id: str) -> None:
        """Clear Wind snooze for a conversation."""
        self.state_manager.clear_snooze(conversation_id)

    def record_proactive_sent(
        self,
        conversation_id: str,
        topic: PendingTopic,
        impulse_score: float,
        message_text: str,
    ) -> None:
        """
        Record that a proactive message was successfully sent.

        Called by scheduler after successful _send_to_mesh().
        Updates state and marks topic as mentioned.
        """
        # Log the successful send
        self.decision_logger.log_decision(
            conversation_id=conversation_id,
            eligible=True,
            decision="send",
            impulse_score=impulse_score,
            threshold=self.config.impulse_threshold,
            selected_topic_id=topic.id,
            draft_message=message_text,
        )

        # Mark topic as mentioned
        self.topic_manager.mark_mentioned(topic.id)

        # Update proactive state
        self.state_manager.record_proactive_sent(conversation_id)

        logger.info(
            "Wind sent to %s: topic=#%d '%s'",
            conversation_id, topic.id, topic.title
        )
