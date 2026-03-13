"""
Topic feedback management for Wind proactive messaging.

Tracks per-topic-family preferences: rejection/interest weights,
cooldowns, and engagement history. All tracking is per-conversation.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, List, Optional

logger = logging.getLogger("joi.wind.feedback")

# Default configuration
DEFAULT_COOLDOWN_THRESHOLD = 0.7  # rejection_weight >= this triggers cooldown
DEFAULT_COOLDOWN_DAYS = 7  # Days to cooldown a topic family
DEFAULT_DECAY_RATE = 0.05  # 5% decay per day for rejection weight


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


@dataclass
class TopicFeedback:
    """Feedback state for a topic family in a conversation."""

    id: int
    conversation_id: str
    topic_family: str  # Normalized: 'weather', 'health', etc.
    rejection_weight: float = 0.0
    interest_weight: float = 0.0
    engagement_count: int = 0
    ignore_count: int = 0
    deflection_count: int = 0
    last_positive_at: Optional[datetime] = None
    last_negative_at: Optional[datetime] = None
    cooldown_until: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class TopicFeedbackManager:
    """
    Manages per-topic-family feedback for conversations.

    Features:
    - Rejection/interest weight tracking
    - Automatic cooldown when rejection threshold exceeded
    - Weight decay over time (forgiveness)
    - Engagement history per family
    """

    def __init__(
        self,
        db_connection_factory: Callable,
        cooldown_threshold: float = DEFAULT_COOLDOWN_THRESHOLD,
        cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
        decay_rate: float = DEFAULT_DECAY_RATE,
    ):
        """
        Initialize TopicFeedbackManager.

        Args:
            db_connection_factory: Callable that returns a database connection
            cooldown_threshold: Rejection weight that triggers cooldown
            cooldown_days: Days to cooldown a topic family
            decay_rate: Daily decay rate for rejection weight (0.05 = 5%)
        """
        self._connect = db_connection_factory
        self._cooldown_threshold = cooldown_threshold
        self._cooldown_days = cooldown_days
        self._decay_rate = decay_rate

    def get_feedback(
        self,
        conversation_id: str,
        topic_family: str,
    ) -> Optional[TopicFeedback]:
        """Get feedback for a specific topic family in a conversation."""
        conn = self._connect()
        cursor = conn.execute(
            """
            SELECT id, conversation_id, topic_family, rejection_weight, interest_weight,
                   engagement_count, ignore_count, deflection_count,
                   last_positive_at, last_negative_at, cooldown_until, updated_at
            FROM topic_feedback
            WHERE conversation_id = ? AND topic_family = ?
            """,
            (conversation_id, topic_family)
        )
        row = cursor.fetchone()
        if not row:
            return None

        return TopicFeedback(
            id=row["id"],
            conversation_id=row["conversation_id"],
            topic_family=row["topic_family"],
            rejection_weight=row["rejection_weight"] or 0.0,
            interest_weight=row["interest_weight"] or 0.0,
            engagement_count=row["engagement_count"] or 0,
            ignore_count=row["ignore_count"] or 0,
            deflection_count=row["deflection_count"] or 0,
            last_positive_at=_parse_datetime(row["last_positive_at"]),
            last_negative_at=_parse_datetime(row["last_negative_at"]),
            cooldown_until=_parse_datetime(row["cooldown_until"]),
            updated_at=_parse_datetime(row["updated_at"]),
        )

    def get_or_create_feedback(
        self,
        conversation_id: str,
        topic_family: str,
    ) -> TopicFeedback:
        """Get or create feedback entry for a topic family."""
        existing = self.get_feedback(conversation_id, topic_family)
        if existing:
            return existing

        now = datetime.now()
        conn = self._connect()
        cursor = conn.execute(
            """
            INSERT INTO topic_feedback (conversation_id, topic_family, updated_at)
            VALUES (?, ?, ?)
            """,
            (conversation_id, topic_family, _format_datetime(now))
        )
        conn.commit()

        return TopicFeedback(
            id=cursor.lastrowid or 0,
            conversation_id=conversation_id,
            topic_family=topic_family,
            updated_at=now,
        )

    def record_engagement(
        self,
        conversation_id: str,
        topic_family: str,
        quality: float = 0.5,
    ) -> None:
        """
        Record positive engagement with a topic family.

        Args:
            conversation_id: Conversation ID
            topic_family: Normalized topic family name
            quality: Engagement quality 0.0-1.0
        """
        now = datetime.now()
        self.get_or_create_feedback(conversation_id, topic_family)

        conn = self._connect()
        # Increase interest, decrease rejection
        conn.execute(
            """
            UPDATE topic_feedback
            SET engagement_count = engagement_count + 1,
                interest_weight = MIN(1.0, interest_weight + ?),
                rejection_weight = MAX(0.0, rejection_weight - 0.1),
                last_positive_at = ?,
                cooldown_until = NULL,
                updated_at = ?
            WHERE conversation_id = ? AND topic_family = ?
            """,
            (quality * 0.2, _format_datetime(now), _format_datetime(now),
             conversation_id, topic_family)
        )
        conn.commit()

        logger.debug("Recorded engagement", extra={
            "conversation_id": conversation_id,
            "topic_family": topic_family,
            "quality": quality,
        })

    def record_ignore(
        self,
        conversation_id: str,
        topic_family: str,
    ) -> None:
        """Record that a topic in this family was ignored."""
        now = datetime.now()
        self.get_or_create_feedback(conversation_id, topic_family)

        conn = self._connect()
        # Mild increase in rejection
        conn.execute(
            """
            UPDATE topic_feedback
            SET ignore_count = ignore_count + 1,
                rejection_weight = MIN(1.0, rejection_weight + 0.1),
                last_negative_at = ?,
                updated_at = ?
            WHERE conversation_id = ? AND topic_family = ?
            """,
            (_format_datetime(now), _format_datetime(now),
             conversation_id, topic_family)
        )
        conn.commit()

        # Check if cooldown should be triggered
        self._check_cooldown(conversation_id, topic_family)

        logger.debug("Recorded ignore", extra={
            "conversation_id": conversation_id,
            "topic_family": topic_family,
        })

    def record_deflection(
        self,
        conversation_id: str,
        topic_family: str,
    ) -> None:
        """Record that a topic in this family was deflected (explicit rejection)."""
        now = datetime.now()
        self.get_or_create_feedback(conversation_id, topic_family)

        conn = self._connect()
        # Stronger increase in rejection for explicit deflection
        conn.execute(
            """
            UPDATE topic_feedback
            SET deflection_count = deflection_count + 1,
                rejection_weight = MIN(1.0, rejection_weight + 0.3),
                interest_weight = MAX(0.0, interest_weight - 0.1),
                last_negative_at = ?,
                updated_at = ?
            WHERE conversation_id = ? AND topic_family = ?
            """,
            (_format_datetime(now), _format_datetime(now),
             conversation_id, topic_family)
        )
        conn.commit()

        # Check if cooldown should be triggered
        self._check_cooldown(conversation_id, topic_family)

        logger.debug("Recorded deflection", extra={
            "conversation_id": conversation_id,
            "topic_family": topic_family,
        })

    def _check_cooldown(
        self,
        conversation_id: str,
        topic_family: str,
    ) -> None:
        """Check if topic family should be put in cooldown."""
        feedback = self.get_feedback(conversation_id, topic_family)
        if not feedback:
            return

        if feedback.rejection_weight >= self._cooldown_threshold:
            cooldown_until = datetime.now() + timedelta(days=self._cooldown_days)
            conn = self._connect()
            conn.execute(
                """
                UPDATE topic_feedback
                SET cooldown_until = ?, updated_at = ?
                WHERE conversation_id = ? AND topic_family = ?
                """,
                (_format_datetime(cooldown_until), _format_datetime(datetime.now()),
                 conversation_id, topic_family)
            )
            conn.commit()

            logger.info("Topic family put in cooldown", extra={
                "conversation_id": conversation_id,
                "topic_family": topic_family,
                "rejection_weight": feedback.rejection_weight,
                "cooldown_until": cooldown_until.isoformat(),
            })

    def is_in_cooldown(
        self,
        conversation_id: str,
        topic_family: str,
        now: Optional[datetime] = None,
    ) -> bool:
        """Check if a topic family is currently in cooldown."""
        if now is None:
            now = datetime.now()

        feedback = self.get_feedback(conversation_id, topic_family)
        if not feedback or not feedback.cooldown_until:
            return False

        return now < feedback.cooldown_until

    def get_topic_preference(
        self,
        conversation_id: str,
        topic_family: str,
    ) -> float:
        """
        Get preference score for a topic family (-1.0 to 1.0).

        Positive = interested, negative = avoiding.
        Used to adjust impulse score for topics in this family.
        """
        feedback = self.get_feedback(conversation_id, topic_family)
        if not feedback:
            return 0.0  # Neutral

        # preference = interest - rejection
        return max(-1.0, min(1.0, feedback.interest_weight - feedback.rejection_weight))

    def apply_daily_decay(self, conversation_id: Optional[str] = None) -> int:
        """
        Apply daily decay to rejection weights (forgiveness over time).

        Args:
            conversation_id: Specific conversation, or None for all

        Returns:
            Number of records updated
        """
        now = datetime.now()
        conn = self._connect()

        # Decay formula: new_weight = old_weight * (1 - decay_rate)
        decay_multiplier = 1.0 - self._decay_rate

        if conversation_id:
            cursor = conn.execute(
                """
                UPDATE topic_feedback
                SET rejection_weight = rejection_weight * ?,
                    updated_at = ?
                WHERE conversation_id = ? AND rejection_weight > 0
                """,
                (decay_multiplier, _format_datetime(now), conversation_id)
            )
        else:
            cursor = conn.execute(
                """
                UPDATE topic_feedback
                SET rejection_weight = rejection_weight * ?,
                    updated_at = ?
                WHERE rejection_weight > 0
                """,
                (decay_multiplier, _format_datetime(now))
            )

        conn.commit()
        count = cursor.rowcount

        if count > 0:
            logger.info("Applied rejection weight decay", extra={
                "conversation_id": conversation_id or "all",
                "records_updated": count,
                "decay_rate": self._decay_rate,
            })

        return count

    def clear_cooldown(
        self,
        conversation_id: str,
        topic_family: Optional[str] = None,
    ) -> int:
        """
        Clear cooldown for topic family(s).

        Args:
            conversation_id: Conversation ID
            topic_family: Specific family, or None for all families

        Returns:
            Number of records updated
        """
        now = datetime.now()
        conn = self._connect()

        if topic_family:
            cursor = conn.execute(
                """
                UPDATE topic_feedback
                SET cooldown_until = NULL, updated_at = ?
                WHERE conversation_id = ? AND topic_family = ?
                """,
                (_format_datetime(now), conversation_id, topic_family)
            )
        else:
            cursor = conn.execute(
                """
                UPDATE topic_feedback
                SET cooldown_until = NULL, updated_at = ?
                WHERE conversation_id = ?
                """,
                (_format_datetime(now), conversation_id)
            )

        conn.commit()
        count = cursor.rowcount

        if count > 0:
            logger.info("Cleared cooldown", extra={
                "conversation_id": conversation_id,
                "topic_family": topic_family or "all",
                "records_updated": count,
            })

        return count

    def get_all_feedback(
        self,
        conversation_id: str,
    ) -> List[TopicFeedback]:
        """Get all topic feedback for a conversation."""
        conn = self._connect()
        cursor = conn.execute(
            """
            SELECT id, conversation_id, topic_family, rejection_weight, interest_weight,
                   engagement_count, ignore_count, deflection_count,
                   last_positive_at, last_negative_at, cooldown_until, updated_at
            FROM topic_feedback
            WHERE conversation_id = ?
            ORDER BY topic_family
            """,
            (conversation_id,)
        )

        return [
            TopicFeedback(
                id=row["id"],
                conversation_id=row["conversation_id"],
                topic_family=row["topic_family"],
                rejection_weight=row["rejection_weight"] or 0.0,
                interest_weight=row["interest_weight"] or 0.0,
                engagement_count=row["engagement_count"] or 0,
                ignore_count=row["ignore_count"] or 0,
                deflection_count=row["deflection_count"] or 0,
                last_positive_at=_parse_datetime(row["last_positive_at"]),
                last_negative_at=_parse_datetime(row["last_negative_at"]),
                cooldown_until=_parse_datetime(row["cooldown_until"]),
                updated_at=_parse_datetime(row["updated_at"]),
            )
            for row in cursor.fetchall()
        ]

    def get_active_cooldowns(
        self,
        conversation_id: str,
        now: Optional[datetime] = None,
    ) -> List[TopicFeedback]:
        """Get all topic families currently in cooldown."""
        if now is None:
            now = datetime.now()

        conn = self._connect()
        cursor = conn.execute(
            """
            SELECT id, conversation_id, topic_family, rejection_weight, interest_weight,
                   engagement_count, ignore_count, deflection_count,
                   last_positive_at, last_negative_at, cooldown_until, updated_at
            FROM topic_feedback
            WHERE conversation_id = ? AND cooldown_until > ?
            ORDER BY cooldown_until
            """,
            (conversation_id, _format_datetime(now))
        )

        return [
            TopicFeedback(
                id=row["id"],
                conversation_id=row["conversation_id"],
                topic_family=row["topic_family"],
                rejection_weight=row["rejection_weight"] or 0.0,
                interest_weight=row["interest_weight"] or 0.0,
                engagement_count=row["engagement_count"] or 0,
                ignore_count=row["ignore_count"] or 0,
                deflection_count=row["deflection_count"] or 0,
                last_positive_at=_parse_datetime(row["last_positive_at"]),
                last_negative_at=_parse_datetime(row["last_negative_at"]),
                cooldown_until=_parse_datetime(row["cooldown_until"]),
                updated_at=_parse_datetime(row["updated_at"]),
            )
            for row in cursor.fetchall()
        ]


def normalize_topic_family(topic_type: str, title: str) -> str:
    """
    Normalize topic type/title to a topic family.

    Uses topic_type if it's a known category, otherwise extracts
    a simplified family from the title.

    Args:
        topic_type: Type of topic (e.g., 'reminder', 'followup', 'curiosity')
        title: Topic title

    Returns:
        Normalized topic family string (lowercase, simplified)
    """
    # Known topic types that are their own family
    known_families = {
        "reminder", "followup", "tension", "affinity", "discovery",
        "health", "weather", "schedule", "work", "family", "hobby",
    }

    topic_type_lower = topic_type.lower()
    if topic_type_lower in known_families:
        return topic_type_lower

    # For unknown types, use the first significant word from title
    title_lower = title.lower()

    # Remove common stop words and punctuation
    stop_words = {"the", "a", "an", "is", "are", "was", "were", "about", "for", "with"}
    words = title_lower.replace("?", "").replace("!", "").replace(".", "").split()
    significant = [w for w in words if w not in stop_words and len(w) > 2]

    if significant:
        return significant[0]

    # Fallback to topic_type
    return topic_type_lower
