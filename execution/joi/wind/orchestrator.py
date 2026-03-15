"""
Wind orchestrator - main entry point for proactive messaging.
"""

import logging
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Tuple

from .config import WindConfig
from .state import WindStateManager
from .topics import TopicManager, PendingTopic
from .logging import WindDecisionLogger
from .impulse import ImpulseEngine, ImpulseResult
from .engagement import EngagementClassifier, EngagementResult
from .feedback import TopicFeedbackManager, normalize_topic_family

logger = logging.getLogger("joi.wind.orchestrator")

# Topic type lifecycle configuration
# After Engaged | After Ignored | After Deflected
LIFECYCLE_RULES = {
    "tension": {"engaged": "resolve", "ignored": "retry_1", "deflected": "dismiss"},
    "affinity": {"engaged": "mark_engaged", "ignored": "retry_2", "deflected": "cooldown_3"},
    "discovery": {"engaged": "convert_affinity", "ignored": "expire", "deflected": "cooldown_7"},
    "reminder": {"engaged": "complete", "ignored": "retry_ttl", "deflected": "defer_1"},
    "followup": {"engaged": "complete", "ignored": "retry_1", "deflected": "dismiss"},
    # Phase 4b: Ghost probe — rare re-check for deeply-rejected families
    "ghost": {"engaged": "mark_engaged", "ignored": "cooldown_90", "deflected": "undertaker_promote"},
}

# Default lifecycle for unknown types
DEFAULT_LIFECYCLE = {"engaged": "mark_engaged", "ignored": "retry_1", "deflected": "dismiss"}


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
        llm_client: Optional[Callable] = None,
    ):
        """
        Initialize WindOrchestrator.

        Args:
            db_connection_factory: Callable that returns a database connection
            config: Wind configuration (defaults to disabled)
            llm_client: Optional LLM client for engagement classification
        """
        self.config = config or WindConfig()
        self._db_connect = db_connection_factory
        self.state_manager = WindStateManager(db_connection_factory)
        self.topic_manager = TopicManager(db_connection_factory)
        self.decision_logger = WindDecisionLogger(db_connection_factory)
        # Phase 4a+4b: Feedback manager (constructed before ImpulseEngine to wire in)
        self.feedback_manager = TopicFeedbackManager(
            db_connection_factory,
            cooldown_days=self.config.cooldown_days if hasattr(self.config, 'cooldown_days') else 9,
            cooldown_jitter_days=self.config.cooldown_jitter_days if hasattr(self.config, 'cooldown_jitter_days') else 2,
            interest_decay_rate=self.config.interest_decay_rate if hasattr(self.config, 'interest_decay_rate') else 0.02,
            undertaker_threshold=self.config.undertaker_threshold if hasattr(self.config, 'undertaker_threshold') else 2.0,
        )
        self.impulse_engine = ImpulseEngine(
            config=self.config,
            state_manager=self.state_manager,
            topic_manager=self.topic_manager,
            feedback_manager=self.feedback_manager,
        )
        # Phase 4a: Engagement tracking
        self.engagement_classifier = EngagementClassifier(
            llm_client=llm_client,
            timeout_hours=self.config.ignore_timeout_hours if hasattr(self.config, 'ignore_timeout_hours') else 12.0,
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

        # Phase 4b: Generate ghost probes for deeply rejected families
        for conversation_id in self.config.allowlist:
            self._generate_ghost_probes(conversation_id, now)

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

    def record_user_interaction(
        self,
        conversation_id: str,
        message_text: Optional[str] = None,
        reply_to_id: Optional[str] = None,
    ) -> None:
        """
        Record that a user sent a message (resets unanswered counter).

        Also evaluates pending topics awaiting response for engagement tracking.

        Args:
            conversation_id: Conversation ID
            message_text: User's message text (for engagement classification)
            reply_to_id: Message ID being replied to (for direct reply detection)
        """
        self.state_manager.record_user_interaction(
            conversation_id,
            ema_alpha=self.config.active_convo_ema_alpha,
        )

        # Phase 4a: Evaluate pending topics
        if message_text:
            self._evaluate_pending_topics(
                conversation_id=conversation_id,
                user_message=message_text,
                reply_to_id=reply_to_id,
            )
            # Phase 4b: Check if user mentioned a cooled-down topic (cooldown break)
            self._scan_for_cooldown_breaks(conversation_id, message_text)

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
        message_id: Optional[str] = None,
    ) -> None:
        """
        Record that a proactive message was successfully sent.

        Called by scheduler after successful _send_to_mesh().
        Updates state and marks topic as sent (awaiting response).

        Args:
            conversation_id: Conversation ID
            topic: Topic that was mentioned
            impulse_score: Impulse score at time of send
            message_text: Message text sent
            message_id: Message ID of the sent message (for reply tracking)
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

        # Phase 4a: Mark topic as sent with message_id for tracking
        if message_id:
            self.topic_manager.mark_sent(topic.id, message_id)
        else:
            # Fallback to old behavior if no message_id
            self.topic_manager.mark_mentioned(topic.id)

        # Update proactive state
        self.state_manager.record_proactive_sent(conversation_id)

        logger.info(
            "Wind sent to %s: topic=#%d '%s' (msg_id=%s)",
            conversation_id, topic.id, topic.title, message_id or "none"
        )

    # --- Phase 4a: Engagement evaluation ---

    def set_llm_client(self, llm_client: Callable) -> None:
        """Set the LLM client for engagement classification."""
        self.engagement_classifier.set_llm_client(llm_client)

    def _evaluate_pending_topics(
        self,
        conversation_id: str,
        user_message: str,
        reply_to_id: Optional[str] = None,
    ) -> None:
        """
        Evaluate topics awaiting response against user's message.

        For each pending topic:
        1. Try direct reply detection (Signal quote timestamp match)
        2. If not direct reply, use LLM classification
        3. Apply outcome and lifecycle rules
        """
        # Direct reply fast path — if user quoted a message, find the matching topic
        directly_replied_topic_id = None
        if reply_to_id:
            try:
                signal_ts = int(reply_to_id)
                matched = self.topic_manager.get_topic_by_signal_timestamp(
                    conversation_id, signal_ts
                )
                if matched:
                    directly_replied_topic_id = matched.id
                    logger.info("Direct reply matched Wind topic", extra={
                        "conversation_id": conversation_id,
                        "topic_id": matched.id,
                        "signal_ts": signal_ts,
                    })
                    result = EngagementResult(
                        outcome=EngagementClassifier.OUTCOME_ENGAGED,
                        confidence=1.0,
                        quality=0.8,
                        method="direct_reply",
                    )
                    self._apply_engagement_outcome(matched, result)
            except (ValueError, TypeError):
                pass  # reply_to_id not a valid Signal timestamp

        # LLM/timeout path for remaining pending topics
        topics = self.topic_manager.get_topics_awaiting_response(conversation_id)
        for topic in topics:
            if topic.id == directly_replied_topic_id:
                continue  # already handled above

            # Skip if no sent_message_id (shouldn't happen, but defensive)
            if not topic.sent_message_id:
                continue

            # Get the original Wind message text for classification
            wind_message = self._get_wind_message_text(topic)

            # Classify engagement
            result = self.engagement_classifier.classify(
                wind_message=wind_message,
                wind_message_id=topic.sent_message_id,
                mentioned_at=topic.mentioned_at or datetime.now(),
                user_response=user_message,
                user_response_reply_to=None,  # direct reply handled above
            )

            if result:
                self._apply_engagement_outcome(topic, result)

    def _get_wind_message_text(self, topic: PendingTopic) -> str:
        """Get the Wind message text for a topic (from content or title)."""
        # In practice, the Wind message is generated from topic title/content
        # For classification, we use whatever is available
        if topic.content:
            return topic.content
        return topic.title

    def _apply_engagement_outcome(
        self,
        topic: PendingTopic,
        result: EngagementResult,
    ) -> None:
        """
        Apply engagement outcome to topic and update feedback.

        Args:
            topic: The topic that received a response
            result: Engagement classification result
        """
        # Record outcome on topic
        self.topic_manager.mark_outcome(topic.id, result.outcome)

        # Update conversation engagement stats
        self.state_manager.record_engagement(
            conversation_id=topic.conversation_id,
            outcome=result.outcome,
            quality=result.quality,
        )

        # Update topic family feedback
        topic_family = normalize_topic_family(topic.topic_type, topic.title)
        if result.outcome == "engaged":
            self.feedback_manager.record_engagement(
                topic.conversation_id,
                topic_family,
                result.quality,
            )
        elif result.outcome == "ignored":
            self.feedback_manager.record_ignore(topic.conversation_id, topic_family)
        elif result.outcome == "deflected":
            self.feedback_manager.record_deflection(topic.conversation_id, topic_family)

        # Apply lifecycle rules
        self._apply_lifecycle_rules(topic, result.outcome)

        logger.info("Engagement outcome applied", extra={
            "topic_id": topic.id,
            "conversation_id": topic.conversation_id,
            "outcome": result.outcome,
            "confidence": result.confidence,
            "quality": result.quality,
            "method": result.method,
            "topic_family": topic_family,
        })

    def _apply_lifecycle_rules(
        self,
        topic: PendingTopic,
        outcome: str,
    ) -> None:
        """
        Apply topic type lifecycle rules based on outcome.

        Rules determine what happens to the topic:
        - resolve/complete/dismiss: Mark as mentioned (done)
        - retry_N: Requeue for retry (up to N times)
        - expire: Mark as expired
        - cooldown_N: Apply N-day cooldown to topic family
        - convert_affinity: Change topic_type to affinity
        - defer_N: Postpone by N days
        """
        rules = LIFECYCLE_RULES.get(topic.topic_type, DEFAULT_LIFECYCLE)
        action = rules.get(outcome, "dismiss")

        if action in ("resolve", "complete", "dismiss", "mark_engaged"):
            # Topic is done - status already updated by mark_outcome
            pass

        elif action.startswith("retry_"):
            # Retry logic with pursuit back-off
            max_retries = self._parse_retry_count(action, topic)
            if topic.retry_count < max_retries:
                backoff_list = self.config.pursuit_backoff_hours if hasattr(self.config, 'pursuit_backoff_hours') else [4, 12, 24]
                backoff_h = backoff_list[min(topic.retry_count, len(backoff_list) - 1)]
                due_after = datetime.now() + timedelta(hours=backoff_h)
                self.topic_manager.requeue_for_retry(topic.id, due_after=due_after)
            else:
                self.topic_manager.mark_expired(topic.id)

        elif action == "undertaker_promote":
            # Permanently block this topic family (ghost probe was deflected)
            topic_family = normalize_topic_family(topic.topic_type, topic.title)
            self.feedback_manager.mark_undertaker(topic.conversation_id, topic_family)
            self.topic_manager.mark_dismissed(topic.id)
            logger.info("Undertaker promotion: family blocked permanently", extra={
                "conversation_id": topic.conversation_id,
                "topic_family": topic_family,
            })

        elif action == "expire":
            self.topic_manager.mark_expired(topic.id)

        elif action.startswith("cooldown_"):
            # Apply cooldown to topic family
            try:
                days = int(action.split("_")[1])
            except (ValueError, IndexError):
                days = 7
            topic_family = normalize_topic_family(topic.topic_type, topic.title)
            self._apply_family_cooldown(topic.conversation_id, topic_family, days)
            self.topic_manager.mark_dismissed(topic.id)

        elif action == "convert_affinity":
            # Convert discovery to affinity topic (not implemented in schema)
            # For now, just mark as mentioned
            pass

        elif action.startswith("defer_"):
            # Defer reminder by N days
            try:
                days = int(action.split("_")[1])
            except (ValueError, IndexError):
                days = 1
            self._defer_topic(topic.id, days)

    def _parse_retry_count(self, action: str, topic: PendingTopic) -> int:
        """Parse max retry count from action string."""
        if action == "retry_ttl":
            # For reminders: retry until TTL (use expires_at)
            return 999  # Effectively unlimited, TTL handles expiry
        try:
            return int(action.split("_")[1])
        except (ValueError, IndexError):
            return 1

    def _apply_family_cooldown(
        self,
        conversation_id: str,
        topic_family: str,
        days: int,
    ) -> None:
        """Apply cooldown to a topic family with jitter."""
        import random
        now = datetime.now()
        jitter = getattr(self.config, 'cooldown_jitter_days', 2)
        actual_days = max(1, days + random.randint(-jitter, jitter))
        cooldown_until = now + timedelta(days=actual_days)

        conn = self._db_connect()
        # Ensure feedback entry exists
        self.feedback_manager.get_or_create_feedback(conversation_id, topic_family)

        conn.execute(
            """
            UPDATE topic_feedback
            SET cooldown_until = ?, updated_at = ?
            WHERE conversation_id = ? AND topic_family = ?
            """,
            (cooldown_until.isoformat(), now.isoformat(), conversation_id, topic_family)
        )
        conn.commit()

        logger.info("Applied topic family cooldown", extra={
            "conversation_id": conversation_id,
            "topic_family": topic_family,
            "cooldown_days": actual_days,
            "cooldown_until": cooldown_until.isoformat(),
        })

    def _defer_topic(self, topic_id: int, days: int) -> None:
        """Defer a topic's due_at by N days."""
        now = datetime.now()
        new_due = now + timedelta(days=days)

        conn = self._db_connect()
        conn.execute(
            """
            UPDATE pending_topics
            SET due_at = ?, status = 'pending', outcome = NULL, outcome_at = NULL,
                sent_message_id = NULL
            WHERE id = ?
            """,
            (new_due.isoformat(), topic_id)
        )
        conn.commit()

        logger.info("Deferred topic", extra={
            "topic_id": topic_id,
            "defer_days": days,
            "new_due": new_due.isoformat(),
        })

    def _scan_for_cooldown_breaks(self, conversation_id: str, message_text: str) -> None:
        """
        Check if user message mentions any cooled-down topic family.

        If a user spontaneously brings up a topic that Wind has cooled down,
        treat it as a weak positive signal: exit cooldown early.
        """
        active_cooldowns = self.feedback_manager.get_active_cooldowns(conversation_id)
        if not active_cooldowns:
            return

        msg_lower = message_text.lower()
        for feedback in active_cooldowns:
            if feedback.undertaker:
                continue  # Never break undertaker via keyword match
            # Simple keyword match: family name words vs message text
            family_keywords = feedback.topic_family.replace("_", " ").lower().split()
            if any(kw in msg_lower for kw in family_keywords if len(kw) > 3):
                logger.info("Cooldown break: user mentioned cooled-down family", extra={
                    "conversation_id": conversation_id,
                    "topic_family": feedback.topic_family,
                    "action": "cooldown_break",
                })
                self.feedback_manager.record_user_initiated_mention(conversation_id, feedback.topic_family)

    def _generate_ghost_probes(self, conversation_id: str, now: Optional[datetime] = None) -> None:
        """
        Generate gentle re-probe topics for deeply rejected families after long silence.

        Called each tick. Topics are deduplicated via novelty_key so only one ghost
        probe per family per month is ever queued.
        """
        if now is None:
            now = datetime.now()

        ghost_probe_days = getattr(self.config, 'ghost_probe_days', 60)
        ghost_probe_priority = getattr(self.config, 'ghost_probe_priority', 20)
        undertaker_threshold = getattr(self.config, 'undertaker_threshold', 2.0)

        inactive_since = now - timedelta(days=ghost_probe_days)
        # Look for families above normal cooldown threshold but below undertaker
        deeply_rejected = self.feedback_manager.get_deeply_rejected_families(
            conversation_id,
            min_rejection=self.feedback_manager._cooldown_threshold,
            max_rejection=undertaker_threshold,
            inactive_since=inactive_since,
        )

        for family in deeply_rejected:
            novelty_key = f"ghost_{family}_{now.strftime('%Y-%m')}"
            self.topic_manager.add_topic(
                conversation_id=conversation_id,
                topic_type="ghost",
                title=f"Re-check: {family}",
                priority=ghost_probe_priority,
                novelty_key=novelty_key,
            )
            logger.debug("Generated ghost probe", extra={
                "conversation_id": conversation_id,
                "topic_family": family,
                "novelty_key": novelty_key,
            })

    def check_timeout_topics(self, now: Optional[datetime] = None) -> int:
        """
        Check all topics awaiting response for timeout.

        Called periodically (e.g., by scheduler) to classify topics
        that have exceeded the ignore timeout with no user response.

        Returns:
            Number of topics timed out
        """
        if now is None:
            now = datetime.now()

        timed_out = 0

        # Get all conversations with wind state
        for conv_id in self.state_manager.get_all_conversation_ids():
            topics = self.topic_manager.get_topics_awaiting_response(conv_id)

            for topic in topics:
                if not topic.mentioned_at:
                    continue

                # Check timeout
                result = self.engagement_classifier.classify_timeout(
                    mentioned_at=topic.mentioned_at,
                    now=now,
                )

                if result:
                    self._apply_engagement_outcome(topic, result)
                    timed_out += 1

        if timed_out > 0:
            logger.info("Timed out topics", extra={"count": timed_out})

        return timed_out
