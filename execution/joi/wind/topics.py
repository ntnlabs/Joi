"""
Topic management for Wind proactive messaging.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .feedback import normalize_topic_family

logger = logging.getLogger("joi.wind.topics")

REDISCOVERY_DAYS = 30  # Days before a mentioned discovery topic can be re-created


@dataclass
class PendingTopic:
    """A pending topic in the queue."""

    id: int
    conversation_id: str
    topic_type: str
    title: str
    content: Optional[str] = None
    priority: int = 50
    status: str = "pending"
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    due_at: Optional[datetime] = None
    mentioned_at: Optional[datetime] = None
    novelty_key: Optional[str] = None
    source_event_id: Optional[str] = None
    # Engagement tracking fields (Phase 4a)
    outcome: Optional[str] = None  # 'engaged', 'ignored', 'deflected'
    outcome_at: Optional[datetime] = None
    retry_count: int = 0
    last_retry_at: Optional[datetime] = None
    sent_message_id: Optional[str] = None  # Links to message.message_id
    # Outcome curiosity emotional context (Phase 4c)
    emotional_context: Optional[str] = None


from .utils import _parse_datetime, _format_datetime


class TopicManager:
    """
    Manages pending topics for Wind proactive messaging.

    Uses the pending_topics table for persistence.
    """

    # Topic statuses
    STATUS_PENDING = "pending"
    STATUS_MENTIONED = "mentioned"
    STATUS_EXPIRED = "expired"
    STATUS_DISMISSED = "dismissed"
    STATUS_AWAITING_RESPONSE = "awaiting_response"  # Phase 4a: sent, waiting for user

    # Engagement outcomes
    OUTCOME_ENGAGED = "engaged"
    OUTCOME_IGNORED = "ignored"
    OUTCOME_DEFLECTED = "deflected"

    def __init__(self, db_connection_factory):
        """
        Initialize TopicManager.

        Args:
            db_connection_factory: Callable that returns a database connection
        """
        self._connect = db_connection_factory

    def _row_to_topic(self, row) -> PendingTopic:
        """Convert a database row to PendingTopic object."""
        return PendingTopic(
            id=row["id"],
            conversation_id=row["conversation_id"],
            topic_type=row["topic_type"],
            title=row["title"],
            content=row["content"],
            priority=row["priority"] or 50,
            status=row["status"] or self.STATUS_PENDING,
            created_at=_parse_datetime(row["created_at"]),
            expires_at=_parse_datetime(row["expires_at"]),
            due_at=_parse_datetime(row["due_at"]),
            mentioned_at=_parse_datetime(row["mentioned_at"]),
            novelty_key=row["novelty_key"],
            source_event_id=row["source_event_id"],
            outcome=row["outcome"] if "outcome" in row.keys() else None,
            outcome_at=_parse_datetime(row["outcome_at"]) if "outcome_at" in row.keys() else None,
            retry_count=row["retry_count"] if "retry_count" in row.keys() else 0,
            last_retry_at=_parse_datetime(row["last_retry_at"]) if "last_retry_at" in row.keys() else None,
            sent_message_id=row["sent_message_id"] if "sent_message_id" in row.keys() else None,
            emotional_context=row["emotional_context"] if "emotional_context" in row.keys() else None,
        )

    def get_pending_topics(
        self,
        conversation_id: str,
        limit: int = 10,
    ) -> List[PendingTopic]:
        """
        Get pending topics for a conversation, ordered by priority.

        Returns only non-expired topics with status='pending'.
        """
        conn = self._connect()
        now = datetime.now(timezone.utc).isoformat()

        cursor = conn.execute(
            """
            SELECT id, conversation_id, topic_type, title, content, priority,
                   status, created_at, expires_at, due_at, mentioned_at, novelty_key,
                   source_event_id, outcome, outcome_at, retry_count, last_retry_at,
                   sent_message_id, emotional_context
            FROM pending_topics
            WHERE conversation_id = ?
              AND status = ?
              AND (expires_at IS NULL OR expires_at > ?)
              AND (due_at IS NULL OR due_at <= ?)
            ORDER BY priority DESC, created_at ASC
            LIMIT ?
            """,
            (conversation_id, self.STATUS_PENDING, now, now, limit)
        )

        return [self._row_to_topic(row) for row in cursor.fetchall()]

    def get_topic_by_id(self, topic_id: int) -> Optional[PendingTopic]:
        """Get a specific topic by ID."""
        conn = self._connect()
        cursor = conn.execute(
            """
            SELECT id, conversation_id, topic_type, title, content, priority,
                   status, created_at, expires_at, due_at, mentioned_at, novelty_key,
                   source_event_id, outcome, outcome_at, retry_count, last_retry_at,
                   sent_message_id, emotional_context
            FROM pending_topics
            WHERE id = ?
            """,
            (topic_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None

        return self._row_to_topic(row)

    def add_topic(
        self,
        conversation_id: str,
        topic_type: str,
        title: str,
        content: Optional[str] = None,
        priority: int = 50,
        expires_at: Optional[datetime] = None,
        due_at: Optional[datetime] = None,
        novelty_key: Optional[str] = None,
        source_event_id: Optional[str] = None,
        emotional_context: Optional[str] = None,
    ) -> int:
        """
        Add a new topic to the queue.

        Args:
            conversation_id: Target conversation
            topic_type: Type of topic (e.g., 'followup', 'reminder', 'curiosity')
            title: Short topic title
            content: Optional detailed content
            priority: Priority (0-100, higher = more urgent)
            expires_at: When topic expires and should be skipped
            due_at: When to trigger (for reminders)
            novelty_key: Key for deduplication
            source_event_id: Reference to source event

        Returns:
            Topic ID
        """
        now = datetime.now(timezone.utc)
        conn = self._connect()

        # Check for existing topic by novelty_key (any status)
        if novelty_key:
            cursor = conn.execute(
                """
                SELECT id, status, priority, mentioned_at FROM pending_topics
                WHERE conversation_id = ? AND novelty_key = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (conversation_id, novelty_key)
            )
            row = cursor.fetchone()
            if row:
                existing_id, existing_status, existing_priority = row[0], row[1], row[2]
                if existing_status in (self.STATUS_PENDING, self.STATUS_AWAITING_RESPONSE):
                    # Already active — boost priority as re-trigger is an engagement signal
                    new_priority = min(existing_priority + priority, 75)
                    conn.execute(
                        "UPDATE pending_topics SET priority = ? WHERE id = ?",
                        (new_priority, existing_id)
                    )
                    conn.commit()
                    logger.debug("Boosted active topic priority", extra={
                        "topic_id": existing_id, "old_priority": existing_priority,
                        "new_priority": new_priority, "novelty_key": novelty_key
                    })
                    return existing_id
                elif existing_status == self.STATUS_DISMISSED:
                    # Dismissed before — re-trigger is an engagement signal, revive with boost
                    new_priority = min(existing_priority + priority, 75)
                    conn.execute(
                        """
                        UPDATE pending_topics
                        SET status = ?, priority = ?, outcome = NULL, outcome_at = NULL
                        WHERE id = ?
                        """,
                        (self.STATUS_PENDING, new_priority, existing_id)
                    )
                    conn.commit()
                    logger.info("Revived dismissed topic with priority boost", extra={
                        "topic_id": existing_id, "old_priority": existing_priority,
                        "new_priority": new_priority, "novelty_key": novelty_key
                    })
                    return existing_id
                elif existing_status == self.STATUS_MENTIONED:
                    # Topic already served — suppress re-creation for REDISCOVERY_DAYS
                    mentioned_at_str = row[3]  # mentioned_at from query
                    if mentioned_at_str:
                        try:
                            mentioned_dt = datetime.fromisoformat(mentioned_at_str)
                            if (now - mentioned_dt) < timedelta(days=REDISCOVERY_DAYS):
                                logger.debug("Suppressed duplicate discovery: recently mentioned", extra={
                                    "topic_id": existing_id, "novelty_key": novelty_key,
                                    "mentioned_at": mentioned_at_str,
                                })
                                return existing_id
                        except Exception:
                            pass
                    # Mentioned long ago — fall through to create fresh (re-share after cooldown)

        cursor = conn.execute(
            """
            INSERT INTO pending_topics (
                conversation_id, topic_type, title, content, priority,
                status, created_at, expires_at, due_at, novelty_key, source_event_id,
                emotional_context
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                topic_type,
                title,
                content,
                priority,
                self.STATUS_PENDING,
                _format_datetime(now),
                _format_datetime(expires_at),
                _format_datetime(due_at),
                novelty_key,
                source_event_id,
                emotional_context,
            )
        )
        conn.commit()

        topic_id = cursor.lastrowid or 0
        logger.info(
            "Added topic #%d for %s: type=%s, title=%s, priority=%d",
            topic_id, conversation_id, topic_type, title[:50], priority
        )
        return topic_id

    def mark_mentioned(self, topic_id: int) -> None:
        """
        Mark a topic as mentioned (used in a proactive message).

        Updates status to 'mentioned' and sets mentioned_at.
        """
        now = datetime.now(timezone.utc)
        conn = self._connect()
        conn.execute(
            """
            UPDATE pending_topics
            SET status = ?, mentioned_at = ?
            WHERE id = ?
            """,
            (self.STATUS_MENTIONED, _format_datetime(now), topic_id)
        )
        conn.commit()
        logger.debug("Marked topic as mentioned", extra={"topic_id": topic_id})

    def mark_expired(self, topic_id: int) -> None:
        """Mark a topic as expired."""
        conn = self._connect()
        conn.execute(
            """
            UPDATE pending_topics
            SET status = ?
            WHERE id = ?
            """,
            (self.STATUS_EXPIRED, topic_id)
        )
        conn.commit()
        logger.debug("Marked topic as expired", extra={"topic_id": topic_id})

    def mark_dismissed(self, topic_id: int) -> None:
        """Mark a topic as dismissed (user indicated not interested)."""
        conn = self._connect()
        conn.execute(
            """
            UPDATE pending_topics
            SET status = ?
            WHERE id = ?
            """,
            (self.STATUS_DISMISSED, topic_id)
        )
        conn.commit()
        logger.debug("Marked topic as dismissed", extra={"topic_id": topic_id})

    def expire_stale_topics(self) -> int:
        """
        Expire all topics past their expires_at time.

        Returns:
            Number of topics expired
        """
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        cursor = conn.execute(
            """
            UPDATE pending_topics
            SET status = ?
            WHERE status = ?
              AND expires_at IS NOT NULL
              AND expires_at <= ?
            """,
            (self.STATUS_EXPIRED, self.STATUS_PENDING, now)
        )
        conn.commit()

        expired_count = cursor.rowcount
        if expired_count > 0:
            logger.info("Expired stale topics", extra={"count": expired_count})
        return expired_count

    def get_topic_pressure(self, conversation_id: str) -> float:
        """
        Calculate topic pressure for a conversation.

        Returns weighted sum of pending topics normalized to [0, 1].
        Higher priority topics contribute more.
        """
        topics = self.get_pending_topics(conversation_id, limit=20)
        if not topics:
            return 0.0

        # Weight by priority: higher priority = more pressure
        # Normalize: max possible = 20 topics * priority 100 = 2000
        total_weight = sum(t.priority for t in topics)
        normalized = min(1.0, total_weight / 500.0)  # Cap at 500 weighted points

        logger.debug(
            "Topic pressure for %s: %.2f (%d topics, %d total weight)",
            conversation_id, normalized, len(topics), total_weight
        )
        return normalized

    def get_best_topic(self, conversation_id: str) -> Optional[PendingTopic]:
        """
        Get the best topic for proactive messaging.

        Returns the highest priority pending topic.
        """
        topics = self.get_pending_topics(conversation_id, limit=1)
        return topics[0] if topics else None

    def apply_priority_decay(self, conversation_id: str, base_points: int = 4, reference_count: int = 8) -> int:
        """
        Decay priority of all pending topics each day.

        Decay rate scales with queue depth (sqrt): larger queues decay faster so
        neglected topics sink and engaged ones float to the top naturally.
        Formula: points = max(base_points, round(base_points * sqrt(pending / reference_count)))

        Topics created today are excluded so freshly mined topics are not immediately penalised.
        Topics floor at 0. Only STATUS_PENDING, non-expired topics are touched.
        """
        import math
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        now_str = now.isoformat()
        today_start_str = today_start.isoformat()
        conn = self._connect()

        row = conn.execute(
            """
            SELECT COUNT(*) FROM pending_topics
            WHERE conversation_id = ?
              AND status = ?
              AND (expires_at IS NULL OR expires_at > ?)
              AND created_at < ?
            """,
            (conversation_id, self.STATUS_PENDING, now_str, today_start_str),
        ).fetchone()
        pending_count = row[0] if row else 0

        if pending_count == 0:
            return 0

        ref = max(1, reference_count)
        points = max(base_points, round(base_points * math.sqrt(pending_count / ref)))

        cursor = conn.execute(
            """
            UPDATE pending_topics
            SET priority = MAX(0, priority - ?)
            WHERE conversation_id = ?
              AND status = ?
              AND (expires_at IS NULL OR expires_at > ?)
              AND created_at < ?
            """,
            (points, conversation_id, self.STATUS_PENDING, now_str, today_start_str),
        )
        conn.commit()
        count = cursor.rowcount
        if count > 0:
            logger.info("Applied topic priority decay", extra={
                "conversation_id": conversation_id,
                "pending_count": pending_count,
                "points": points,
                "topics_updated": count,
            })
        return points

    def apply_affinity_protection(
        self,
        conversation_id: str,
        decayed_points: int,
        feedback_manager,
        affinity_factor: float = 0.5,
        undertaker_release_threshold: float = 0.5,
    ) -> int:
        """
        Partially restore priority for topics from families the user likes.

        Runs after apply_priority_decay(). Topics from families with positive preference
        (interest_weight - rejection_weight > 0) get back a fraction of the decayed points,
        so well-liked topics resist decay and float to the top naturally.

        If a family is in the undertaker but preference has climbed above
        undertaker_release_threshold (user started engaging with it organically),
        the family is released from the undertaker — user-driven signal trumps the block.

        Restore formula: round(decayed_points × affinity_factor × min(1.0, preference_score))
        """
        if decayed_points <= 0 or affinity_factor <= 0:
            return 0

        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        now_str = now.isoformat()
        today_start_str = today_start.isoformat()
        conn = self._connect()

        rows = conn.execute(
            """
            SELECT id, topic_type, title FROM pending_topics
            WHERE conversation_id = ?
              AND status = ?
              AND (expires_at IS NULL OR expires_at > ?)
              AND created_at < ?
            """,
            (conversation_id, self.STATUS_PENDING, now_str, today_start_str),
        ).fetchall()

        restored = 0
        undertaker_released = 0
        for row in rows:
            family = normalize_topic_family(row["topic_type"], row["title"])
            fb = feedback_manager.get_feedback(conversation_id, family)
            if fb is None:
                continue
            preference = max(0.0, fb.interest_weight - fb.rejection_weight)
            if preference <= 0:
                continue

            # If family is in undertaker but user has started engaging with it organically,
            # release it — user-driven signal trumps the permanent block.
            if fb.undertaker and preference >= undertaker_release_threshold:
                feedback_manager.restore_from_undertaker(conversation_id, family)
                logger.info("Undertaker family released via organic user engagement", extra={
                    "conversation_id": conversation_id,
                    "topic_family": family,
                    "preference_score": round(preference, 3),
                })
                undertaker_released += 1
                # Fall through — also restore priority on this topic

            restore = round(decayed_points * affinity_factor * min(1.0, preference))
            if restore <= 0:
                continue
            conn.execute(
                "UPDATE pending_topics SET priority = MIN(100, priority + ?) WHERE id = ?",
                (restore, row["id"]),
            )
            restored += 1

        if restored > 0 or undertaker_released > 0:
            conn.commit()
            logger.info("Applied topic affinity protection", extra={
                "conversation_id": conversation_id,
                "topics_restored": restored,
                "undertaker_released": undertaker_released,
                "decayed_points": decayed_points,
                "affinity_factor": affinity_factor,
            })
        return restored

    def count_pending(self, conversation_id: str) -> int:
        """Count pending topics for a conversation."""
        conn = self._connect()
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            """
            SELECT COUNT(*) FROM pending_topics
            WHERE conversation_id = ?
              AND status = ?
              AND (expires_at IS NULL OR expires_at > ?)
            """,
            (conversation_id, self.STATUS_PENDING, now)
        )
        return cursor.fetchone()[0]

    def count_pending_by_type(self, conversation_id: str, topic_type: str) -> int:
        """Count active (pending or awaiting_response) topics of a specific type for a conversation."""
        conn = self._connect()
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            """
            SELECT COUNT(*) FROM pending_topics
            WHERE conversation_id = ?
              AND topic_type = ?
              AND status IN (?, ?)
              AND (expires_at IS NULL OR expires_at > ?)
            """,
            (conversation_id, topic_type, self.STATUS_PENDING, self.STATUS_AWAITING_RESPONSE, now)
        )
        return cursor.fetchone()[0]

    def delete_topic(self, topic_id: int) -> bool:
        """Delete a topic by ID. Returns True if deleted."""
        conn = self._connect()
        cursor = conn.execute(
            "DELETE FROM pending_topics WHERE id = ?",
            (topic_id,)
        )
        conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.debug("Deleted topic", extra={"topic_id": topic_id})
        return deleted

    def boost_priority(self, topic_id: int, delta: int = 10) -> None:
        """Boost a topic's priority by delta, capped at 100."""
        conn = self._connect()
        conn.execute(
            "UPDATE pending_topics SET priority = MIN(100, priority + ?) WHERE id = ?",
            (delta, topic_id)
        )
        conn.commit()

    def update_topic_content(self, topic_id: int, title: str, content: Optional[str] = None) -> None:
        """Update a topic's title and content (used when merging near-duplicates)."""
        conn = self._connect()
        conn.execute(
            "UPDATE pending_topics SET title = ?, content = ? WHERE id = ?",
            (title, content, topic_id)
        )
        conn.commit()

    def clear_conversation_topics(self, conversation_id: str) -> int:
        """Delete all topics for a conversation. Returns count deleted."""
        conn = self._connect()
        cursor = conn.execute(
            "DELETE FROM pending_topics WHERE conversation_id = ?",
            (conversation_id,)
        )
        conn.commit()
        deleted = cursor.rowcount
        if deleted > 0:
            logger.info("Cleared topics", extra={"count": deleted, "conversation_id": conversation_id})
        return deleted

    # --- Phase 4a: Engagement tracking methods ---

    def mark_sent(self, topic_id: int, message_id: str) -> None:
        """
        Mark a topic as sent and awaiting response.

        Updates status to 'awaiting_response' and stores the message_id
        for later correlation with user responses.
        """
        now = datetime.now(timezone.utc)
        conn = self._connect()
        conn.execute(
            """
            UPDATE pending_topics
            SET status = ?, mentioned_at = ?, sent_message_id = ?
            WHERE id = ?
            """,
            (self.STATUS_AWAITING_RESPONSE, _format_datetime(now), message_id, topic_id)
        )
        conn.commit()
        logger.debug("Marked topic as sent", extra={
            "topic_id": topic_id,
            "message_id": message_id
        })

    def mark_outcome(
        self,
        topic_id: int,
        outcome: str,
        final_status: Optional[str] = None,
    ) -> None:
        """
        Mark a topic with its engagement outcome.

        Args:
            topic_id: Topic to update
            outcome: One of 'engaged', 'ignored', 'deflected'
            final_status: Final status (default: 'mentioned' for engaged, keep current for others)
        """
        now = datetime.now(timezone.utc)
        conn = self._connect()

        # Determine final status based on outcome if not specified
        if final_status is None:
            if outcome == self.OUTCOME_ENGAGED:
                final_status = self.STATUS_MENTIONED
            else:
                # For ignored/deflected, keep awaiting_response to allow retry logic
                final_status = self.STATUS_AWAITING_RESPONSE

        conn.execute(
            """
            UPDATE pending_topics
            SET outcome = ?, outcome_at = ?, status = ?
            WHERE id = ?
            """,
            (outcome, _format_datetime(now), final_status, topic_id)
        )
        conn.commit()
        logger.info("Marked topic outcome", extra={
            "topic_id": topic_id,
            "outcome": outcome,
            "status": final_status
        })

    def requeue_for_retry(self, topic_id: int, due_after: Optional[datetime] = None) -> bool:
        """
        Requeue a topic for retry after ignored/deflected outcome.

        Increments retry_count and resets status to 'pending'.
        Returns False if max retries exceeded (caller should expire).

        Args:
            topic_id: Topic to requeue
            due_after: If set, topic won't surface until this time (pursuit back-off)
        """
        now = datetime.now(timezone.utc)
        conn = self._connect()

        # Get current topic to check retry count
        topic = self.get_topic_by_id(topic_id)
        if not topic:
            return False

        conn.execute(
            """
            UPDATE pending_topics
            SET status = ?, retry_count = retry_count + 1, last_retry_at = ?,
                due_at = ?,
                outcome = NULL, outcome_at = NULL, sent_message_id = NULL
            WHERE id = ?
            """,
            (self.STATUS_PENDING, _format_datetime(now), _format_datetime(due_after), topic_id)
        )
        conn.commit()
        logger.info("Requeued topic for retry", extra={
            "topic_id": topic_id,
            "retry_count": topic.retry_count + 1,
            "due_after": due_after.isoformat() if due_after else None,
        })
        return True

    def defer_topic(self, topic_id: int, due_at: datetime) -> bool:
        """
        Defer a topic to a new due_at time, preserving outcome data.

        Unlike requeue_for_retry, this does not increment retry_count and
        does not wipe outcome/outcome_at/sent_message_id.
        Returns True if topic was found and updated.
        """
        conn = self._connect()
        cursor = conn.execute(
            """
            UPDATE pending_topics
            SET due_at = ?, status = ?
            WHERE id = ?
            """,
            (_format_datetime(due_at), self.STATUS_PENDING, topic_id)
        )
        conn.commit()
        if cursor.rowcount > 0:
            logger.info("Deferred topic", extra={
                "topic_id": topic_id,
                "new_due": due_at.isoformat(),
            })
        return cursor.rowcount > 0

    def get_topics_awaiting_response(
        self,
        conversation_id: str,
        timeout_hours: float = 12.0,
    ) -> List[PendingTopic]:
        """
        Get topics that were sent and are awaiting user response.

        Args:
            conversation_id: Conversation to check
            timeout_hours: Hours after which topic is considered ignored (default: 12)

        Returns:
            List of topics with status='awaiting_response' and no outcome yet
        """
        conn = self._connect()

        cursor = conn.execute(
            """
            SELECT id, conversation_id, topic_type, title, content, priority,
                   status, created_at, expires_at, due_at, mentioned_at, novelty_key,
                   source_event_id, outcome, outcome_at, retry_count, last_retry_at,
                   sent_message_id, emotional_context
            FROM pending_topics
            WHERE conversation_id = ?
              AND status = ?
              AND outcome IS NULL
            ORDER BY mentioned_at ASC
            """,
            (conversation_id, self.STATUS_AWAITING_RESPONSE)
        )

        return [self._row_to_topic(row) for row in cursor.fetchall()]

    def get_topic_by_signal_timestamp(
        self,
        conversation_id: str,
        signal_timestamp_ms: int,
        tolerance_ms: int = 5000,
    ) -> Optional[PendingTopic]:
        """
        Find a pending topic whose sent message timestamp matches a Signal quote ID.

        Signal quote IDs are the sender's envelope timestamp (ms). We match against
        the stored message timestamp via JOIN with messages table.
        Tolerance of 5s covers any delay between store_message() and signal-cli send.
        """
        conn = self._connect()
        row = conn.execute(
            """
            SELECT pt.* FROM pending_topics pt
            JOIN messages m ON m.message_id = pt.sent_message_id
            WHERE pt.conversation_id = ?
              AND pt.status = ?
              AND ABS(m.timestamp - ?) <= ?
            LIMIT 1
            """,
            (conversation_id, self.STATUS_AWAITING_RESPONSE, signal_timestamp_ms, tolerance_ms),
        ).fetchone()
        return self._row_to_topic(row) if row else None

    def get_topic_by_message_id(self, message_id: str) -> Optional[PendingTopic]:
        """Get topic by the message_id of the Wind message that mentioned it."""
        conn = self._connect()
        cursor = conn.execute(
            """
            SELECT id, conversation_id, topic_type, title, content, priority,
                   status, created_at, expires_at, due_at, mentioned_at, novelty_key,
                   source_event_id, outcome, outcome_at, retry_count, last_retry_at,
                   sent_message_id, emotional_context
            FROM pending_topics
            WHERE sent_message_id = ?
            """,
            (message_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_topic(row)

    def get_recent_topics(
        self,
        conversation_id: str,
        limit: int = 20,
        include_all_statuses: bool = True,
    ) -> List[PendingTopic]:
        """
        Get recent topics for a conversation (for history/admin).

        Args:
            conversation_id: Conversation to query
            limit: Max topics to return
            include_all_statuses: Include expired/dismissed topics

        Returns:
            List of topics ordered by created_at descending
        """
        conn = self._connect()

        if include_all_statuses:
            cursor = conn.execute(
                """
                SELECT id, conversation_id, topic_type, title, content, priority,
                       status, created_at, expires_at, due_at, mentioned_at, novelty_key,
                       source_event_id, outcome, outcome_at, retry_count, last_retry_at,
                       sent_message_id, emotional_context
                FROM pending_topics
                WHERE conversation_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (conversation_id, limit)
            )
        else:
            cursor = conn.execute(
                """
                SELECT id, conversation_id, topic_type, title, content, priority,
                       status, created_at, expires_at, due_at, mentioned_at, novelty_key,
                       source_event_id, outcome, outcome_at, retry_count, last_retry_at,
                       sent_message_id, emotional_context
                FROM pending_topics
                WHERE conversation_id = ?
                  AND status IN (?, ?, ?)
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (conversation_id, self.STATUS_PENDING, self.STATUS_MENTIONED,
                 self.STATUS_AWAITING_RESPONSE, limit)
            )

        return [self._row_to_topic(row) for row in cursor.fetchall()]
