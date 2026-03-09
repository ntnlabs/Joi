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

    def __init__(self, db_connection_factory):
        """
        Initialize TopicManager.

        Args:
            db_connection_factory: Callable that returns a database connection
        """
        self._connect = db_connection_factory

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
                   source_event_id
            FROM pending_topics
            WHERE conversation_id = ?
              AND status = ?
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY priority DESC, created_at ASC
            LIMIT ?
            """,
            (conversation_id, self.STATUS_PENDING, now, limit)
        )

        return [
            PendingTopic(
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
            )
            for row in cursor.fetchall()
        ]

    def get_topic_by_id(self, topic_id: int) -> Optional[PendingTopic]:
        """Get a specific topic by ID."""
        conn = self._connect()
        cursor = conn.execute(
            """
            SELECT id, conversation_id, topic_type, title, content, priority,
                   status, created_at, expires_at, due_at, mentioned_at, novelty_key,
                   source_event_id
            FROM pending_topics
            WHERE id = ?
            """,
            (topic_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None

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
        )

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
                   source_event_id
            FROM pending_topics
            WHERE status = ?
              AND due_at IS NOT NULL
              AND due_at <= ?
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY due_at ASC, priority DESC
            """,
            (self.STATUS_PENDING, now_iso, now_iso)
        )

        return [
            PendingTopic(
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
            )
            for row in cursor.fetchall()
        ]

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
        logger.debug("Marked topic #%d as mentioned", topic_id)

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
        logger.debug("Marked topic #%d as expired", topic_id)

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
        logger.debug("Marked topic #%d as dismissed", topic_id)

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
            logger.info("Expired %d stale topics", expired_count)
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
            logger.debug("Deleted topic #%d", topic_id)
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
            logger.info("Cleared %d topics for %s", deleted, conversation_id)
        return deleted
