"""
Topic feedback management for Wind proactive messaging.

Tracks per-topic-family preferences: rejection/interest weights,
cooldowns, and engagement history. All tracking is per-conversation.
"""

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, List, Optional

logger = logging.getLogger("joi.wind.feedback")

# Default configuration
DEFAULT_COOLDOWN_THRESHOLD = 0.7  # rejection_weight >= this triggers cooldown
DEFAULT_COOLDOWN_DAYS = 9   # Days to cooldown a topic family (center of jitter window)
DEFAULT_DECAY_RATE = 0.05   # 5% decay per day for rejection weight
DEFAULT_INTEREST_DECAY_RATE = 0.02  # 2% decay per day for interest_weight
DEFAULT_COOLDOWN_JITTER_DAYS = 2    # ±N days random jitter on cooldown duration
DEFAULT_UNDERTAKER_THRESHOLD = 2.0  # rejection_weight to auto-promote to undertaker


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
    undertaker: bool = False  # Phase 4b: permanently blocked family
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
        interest_decay_rate: float = DEFAULT_INTEREST_DECAY_RATE,
        cooldown_jitter_days: int = DEFAULT_COOLDOWN_JITTER_DAYS,
        undertaker_threshold: float = DEFAULT_UNDERTAKER_THRESHOLD,
    ):
        """
        Initialize TopicFeedbackManager.

        Args:
            db_connection_factory: Callable that returns a database connection
            cooldown_threshold: Rejection weight that triggers cooldown
            cooldown_days: Center of cooldown window in days (actual = days ± jitter)
            decay_rate: Daily decay rate for rejection weight (0.05 = 5%)
            interest_decay_rate: Daily decay rate for interest_weight (0.02 = 2%)
            cooldown_jitter_days: Random ±N days applied to cooldown duration
            undertaker_threshold: rejection_weight at which family is auto-promoted to undertaker
        """
        self._connect = db_connection_factory
        self._cooldown_threshold = cooldown_threshold
        self._cooldown_days = cooldown_days
        self._decay_rate = decay_rate
        self._interest_decay_rate = interest_decay_rate
        self._cooldown_jitter_days = cooldown_jitter_days
        self._undertaker_threshold = undertaker_threshold

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
                   last_positive_at, last_negative_at, cooldown_until, undertaker, updated_at
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
            undertaker=bool(row["undertaker"] or 0),
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
            # Apply jitter to cooldown duration (anti-periodicity)
            jitter = random.randint(-self._cooldown_jitter_days, self._cooldown_jitter_days)
            actual_days = max(1, self._cooldown_days + jitter)
            now = datetime.now()
            cooldown_until = now + timedelta(days=actual_days)

            # Check for undertaker promotion
            promote_undertaker = feedback.rejection_weight >= self._undertaker_threshold

            conn = self._connect()
            conn.execute(
                """
                UPDATE topic_feedback
                SET cooldown_until = ?, undertaker = ?, updated_at = ?
                WHERE conversation_id = ? AND topic_family = ?
                """,
                (_format_datetime(cooldown_until), 1 if promote_undertaker else (1 if feedback.undertaker else 0),
                 _format_datetime(now), conversation_id, topic_family)
            )
            conn.commit()

            if promote_undertaker:
                logger.info("Topic family promoted to undertaker", extra={
                    "conversation_id": conversation_id,
                    "topic_family": topic_family,
                    "rejection_weight": feedback.rejection_weight,
                })
            else:
                logger.info("Topic family put in cooldown", extra={
                    "conversation_id": conversation_id,
                    "topic_family": topic_family,
                    "rejection_weight": feedback.rejection_weight,
                    "cooldown_days": actual_days,
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
        if not feedback:
            return False

        # Undertaker = permanently blocked
        if feedback.undertaker:
            return True

        if not feedback.cooldown_until:
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

        # Undertaker = strongly negative (never surface)
        if feedback.undertaker:
            return -1.0

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
        rejection_multiplier = 1.0 - self._decay_rate
        interest_multiplier = 1.0 - self._interest_decay_rate

        if conversation_id:
            cursor = conn.execute(
                """
                UPDATE topic_feedback
                SET rejection_weight = CASE WHEN rejection_weight > 0
                        THEN rejection_weight * ? ELSE rejection_weight END,
                    interest_weight = CASE WHEN interest_weight > 0
                        THEN interest_weight * ? ELSE interest_weight END,
                    updated_at = ?
                WHERE conversation_id = ? AND (rejection_weight > 0 OR interest_weight > 0)
                """,
                (rejection_multiplier, interest_multiplier, _format_datetime(now), conversation_id)
            )
        else:
            cursor = conn.execute(
                """
                UPDATE topic_feedback
                SET rejection_weight = CASE WHEN rejection_weight > 0
                        THEN rejection_weight * ? ELSE rejection_weight END,
                    interest_weight = CASE WHEN interest_weight > 0
                        THEN interest_weight * ? ELSE interest_weight END,
                    updated_at = ?
                WHERE rejection_weight > 0 OR interest_weight > 0
                """,
                (rejection_multiplier, interest_multiplier, _format_datetime(now))
            )

        conn.commit()
        count = cursor.rowcount

        if count > 0:
            logger.info("Applied weight decay", extra={
                "conversation_id": conversation_id or "all",
                "records_updated": count,
                "rejection_decay_rate": self._decay_rate,
                "interest_decay_rate": self._interest_decay_rate,
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
                   last_positive_at, last_negative_at, cooldown_until, undertaker, updated_at
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
                undertaker=bool(row["undertaker"] or 0),
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
                   last_positive_at, last_negative_at, cooldown_until, undertaker, updated_at
            FROM topic_feedback
            WHERE conversation_id = ? AND (cooldown_until > ? OR undertaker = 1)
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
                undertaker=bool(row["undertaker"] or 0),
                updated_at=_parse_datetime(row["updated_at"]),
            )
            for row in cursor.fetchall()
        ]


    def mark_undertaker(
        self,
        conversation_id: str,
        topic_family: str,
    ) -> None:
        """
        Permanently block a topic family (undertaker state).

        Called by lifecycle action 'undertaker_promote' when a ghost probe is deflected.
        """
        now = datetime.now()
        conn = self._connect()
        conn.execute(
            """
            UPDATE topic_feedback
            SET undertaker = 1, updated_at = ?
            WHERE conversation_id = ? AND topic_family = ?
            """,
            (_format_datetime(now), conversation_id, topic_family)
        )
        conn.commit()

        logger.info("Topic family marked as undertaker", extra={
            "conversation_id": conversation_id,
            "topic_family": topic_family,
            "action": "undertaker_promote",
        })

    def record_user_initiated_mention(
        self,
        conversation_id: str,
        topic_family: str,
    ) -> None:
        """
        User spontaneously mentioned a cooled-down topic — weak positive signal.

        Reduces rejection_weight by 0.1 and clears the cooldown, allowing Wind
        to bring the topic back sooner. Does not affect undertaker families.
        """
        now = datetime.now()
        conn = self._connect()
        conn.execute(
            """
            UPDATE topic_feedback
            SET rejection_weight = MAX(0.0, rejection_weight - 0.1),
                cooldown_until = NULL,
                updated_at = ?
            WHERE conversation_id = ? AND topic_family = ?
              AND cooldown_until IS NOT NULL AND undertaker = 0
            """,
            (_format_datetime(now), conversation_id, topic_family)
        )
        conn.commit()

        logger.info("Cooldown break: user initiated mention", extra={
            "conversation_id": conversation_id,
            "topic_family": topic_family,
            "action": "cooldown_break",
        })

    def get_deeply_rejected_families(
        self,
        conversation_id: str,
        min_rejection: float,
        max_rejection: float,
        inactive_since: datetime,
    ) -> List[str]:
        """
        Return topic families with rejection in (min, max) range and no activity since cutoff.

        Used by ghost probe scheduler to find candidates for re-check after long silence.
        Excludes undertaker families.
        """
        conn = self._connect()
        cursor = conn.execute(
            """
            SELECT topic_family
            FROM topic_feedback
            WHERE conversation_id = ?
              AND rejection_weight > ?
              AND rejection_weight < ?
              AND undertaker = 0
              AND (last_negative_at IS NULL OR last_negative_at < ?)
            ORDER BY rejection_weight DESC
            """,
            (conversation_id, min_rejection, max_rejection, _format_datetime(inactive_since))
        )
        return [row["topic_family"] for row in cursor.fetchall()]


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
