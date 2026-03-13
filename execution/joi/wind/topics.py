"""
Topic management for Wind proactive messaging.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger("joi.wind.topics")


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
    due_at: Optional[datetime] = None  # For reminders: when to trigger
    mentioned_at: Optional[datetime] = None
    novelty_key: Optional[str] = None
    source_event_id: Optional[str] = None
    # Engagement tracking fields (Phase 4a)
    outcome: Optional[str] = None  # 'engaged', 'ignored', 'deflected'
    outcome_at: Optional[datetime] = None
    retry_count: int = 0
    last_retry_at: Optional[datetime] = None
    sent_message_id: Optional[str] = None  # Links to message.message_id


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse ISO format datetime string."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _format_datetime(dt: Optional[datetime]) -> Optional[str]:
    """Format datetime to ISO string."""
    if not dt:
        return None
    return dt.isoformat()


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
        now = datetime.now().isoformat()

        cursor = conn.execute(
            """
            SELECT id, conversation_id, topic_type, title, content, priority,
                   status, created_at, expires_at, due_at, mentioned_at, novelty_key,
                   source_event_id, outcome, outcome_at, retry_count, last_retry_at,
                   sent_message_id
            FROM pending_topics
            WHERE conversation_id = ?
              AND status = ?
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY priority DESC, created_at ASC
            LIMIT ?
            """,
            (conversation_id, self.STATUS_PENDING, now, limit)
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
                   sent_message_id
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
        now = datetime.now()
        conn = self._connect()

        # Check for duplicate by novelty_key
        if novelty_key:
            cursor = conn.execute(
                """
                SELECT id FROM pending_topics
                WHERE conversation_id = ? AND novelty_key = ? AND status = ?
                """,
                (conversation_id, novelty_key, self.STATUS_PENDING)
            )
            if cursor.fetchone():
                logger.debug(
                    "Skipping duplicate topic: %s (novelty_key=%s)",
                    title, novelty_key
                )
                return 0

        cursor = conn.execute(
            """
            INSERT INTO pending_topics (
                conversation_id, topic_type, title, content, priority,
                status, created_at, expires_at, due_at, novelty_key, source_event_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            )
        )
        conn.commit()

        topic_id = cursor.lastrowid or 0
        logger.info(
            "Added topic #%d for %s: type=%s, title=%s, priority=%d",
            topic_id, conversation_id, topic_type, title[:50], priority
        )
        return topic_id

    def get_due_reminders(self, now: Optional[datetime] = None) -> List[PendingTopic]:
        """
        Get all reminders that are due (due_at <= now).

        Returns topics with status='pending' and due_at in the past.
        """
        if now is None:
            now = datetime.now()
        now_iso = now.isoformat()
        conn = self._connect()

        cursor = conn.execute(
            """
            SELECT id, conversation_id, topic_type, title, content, priority,
                   status, created_at, expires_at, due_at, mentioned_at, novelty_key,
                   source_event_id, outcome, outcome_at, retry_count, last_retry_at,
                   sent_message_id
            FROM pending_topics
            WHERE status = ?
              AND due_at IS NOT NULL
              AND due_at <= ?
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY due_at ASC, priority DESC
            """,
            (self.STATUS_PENDING, now_iso, now_iso)
        )

        return [self._row_to_topic(row) for row in cursor.fetchall()]

    def mark_mentioned(self, topic_id: int) -> None:
        """
        Mark a topic as mentioned (used in a proactive message).

        Updates status to 'mentioned' and sets mentioned_at.
        """
        now = datetime.now()
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
        now = datetime.now().isoformat()
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

    def count_pending(self, conversation_id: str) -> int:
        """Count pending topics for a conversation."""
        conn = self._connect()
        now = datetime.now().isoformat()
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
        now = datetime.now()
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
        now = datetime.now()
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

    def requeue_for_retry(self, topic_id: int) -> bool:
        """
        Requeue a topic for retry after ignored/deflected outcome.

        Increments retry_count and resets status to 'pending'.
        Returns False if max retries exceeded (caller should expire).
        """
        now = datetime.now()
        conn = self._connect()

        # Get current topic to check retry count
        topic = self.get_topic_by_id(topic_id)
        if not topic:
            return False

        conn.execute(
            """
            UPDATE pending_topics
            SET status = ?, retry_count = retry_count + 1, last_retry_at = ?,
                outcome = NULL, outcome_at = NULL, sent_message_id = NULL
            WHERE id = ?
            """,
            (self.STATUS_PENDING, _format_datetime(now), topic_id)
        )
        conn.commit()
        logger.info("Requeued topic for retry", extra={
            "topic_id": topic_id,
            "retry_count": topic.retry_count + 1
        })
        return True

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
                   sent_message_id
            FROM pending_topics
            WHERE conversation_id = ?
              AND status = ?
              AND outcome IS NULL
            ORDER BY mentioned_at ASC
            """,
            (conversation_id, self.STATUS_AWAITING_RESPONSE)
        )

        return [self._row_to_topic(row) for row in cursor.fetchall()]

    def get_topic_by_message_id(self, message_id: str) -> Optional[PendingTopic]:
        """Get topic by the message_id of the Wind message that mentioned it."""
        conn = self._connect()
        cursor = conn.execute(
            """
            SELECT id, conversation_id, topic_type, title, content, priority,
                   status, created_at, expires_at, due_at, mentioned_at, novelty_key,
                   source_event_id, outcome, outcome_at, retry_count, last_retry_at,
                   sent_message_id
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
                       sent_message_id
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
                       sent_message_id
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
