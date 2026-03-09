"""
Wind state management for per-conversation proactive messaging state.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger("joi.wind.state")


@dataclass
class WindState:
    """Per-conversation Wind state."""

    conversation_id: str
    last_user_interaction_at: Optional[datetime] = None
    last_outbound_at: Optional[datetime] = None
    last_proactive_sent_at: Optional[datetime] = None
    last_impulse_check_at: Optional[datetime] = None
    proactive_sent_today: int = 0
    proactive_day_bucket: Optional[str] = None  # YYYY-MM-DD
    unanswered_proactive_count: int = 0
    wind_snooze_until: Optional[datetime] = None
    updated_at: Optional[datetime] = None


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


class WindStateManager:
    """
    Manages per-conversation Wind state in the database.

    Uses the wind_state table for persistence.
    """

    def __init__(self, db_connection_factory):
        """
        Initialize WindStateManager.

        Args:
            db_connection_factory: Callable that returns a database connection
                                   (typically memory._connect)
        """
        self._connect = db_connection_factory

    def get_state(self, conversation_id: str) -> Optional[WindState]:
        """
        Get Wind state for a conversation.

        Returns None if no state exists yet.
        """
        conn = self._connect()
        cursor = conn.execute(
            """
            SELECT conversation_id, last_user_interaction_at, last_outbound_at,
                   last_proactive_sent_at, last_impulse_check_at, proactive_sent_today,
                   proactive_day_bucket, unanswered_proactive_count, wind_snooze_until,
                   updated_at
            FROM wind_state
            WHERE conversation_id = ?
            """,
            (conversation_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None

        return WindState(
            conversation_id=row["conversation_id"],
            last_user_interaction_at=_parse_datetime(row["last_user_interaction_at"]),
            last_outbound_at=_parse_datetime(row["last_outbound_at"]),
            last_proactive_sent_at=_parse_datetime(row["last_proactive_sent_at"]),
            last_impulse_check_at=_parse_datetime(row["last_impulse_check_at"]),
            proactive_sent_today=row["proactive_sent_today"] or 0,
            proactive_day_bucket=row["proactive_day_bucket"],
            unanswered_proactive_count=row["unanswered_proactive_count"] or 0,
            wind_snooze_until=_parse_datetime(row["wind_snooze_until"]),
            updated_at=_parse_datetime(row["updated_at"]),
        )

    def get_or_create_state(self, conversation_id: str) -> WindState:
        """
        Get Wind state for a conversation, creating default if not exists.
        """
        state = self.get_state(conversation_id)
        if state:
            return state

        # Create default state
        now = datetime.now()
        conn = self._connect()
        conn.execute(
            """
            INSERT INTO wind_state (conversation_id, updated_at)
            VALUES (?, ?)
            """,
            (conversation_id, _format_datetime(now))
        )
        conn.commit()

        return WindState(
            conversation_id=conversation_id,
            updated_at=now,
        )

    def update_state(self, conversation_id: str, **updates) -> None:
        """
        Update Wind state fields for a conversation.

        Args:
            conversation_id: Conversation to update
            **updates: Field names and values to update
        """
        if not updates:
            return

        # Ensure state exists
        self.get_or_create_state(conversation_id)

        # Build SET clause
        now = datetime.now()
        updates["updated_at"] = now

        set_clauses = []
        params = []

        for key, value in updates.items():
            set_clauses.append(f"{key} = ?")
            if isinstance(value, datetime):
                params.append(_format_datetime(value))
            else:
                params.append(value)

        params.append(conversation_id)

        conn = self._connect()
        conn.execute(
            f"""
            UPDATE wind_state
            SET {', '.join(set_clauses)}
            WHERE conversation_id = ?
            """,
            params
        )
        conn.commit()
        logger.debug("Updated wind_state for %s: %s", conversation_id, list(updates.keys()))

    def record_proactive_sent(self, conversation_id: str) -> None:
        """
        Record that a proactive message was sent.

        Updates:
        - last_proactive_sent_at
        - proactive_sent_today (incremented, or reset if new day)
        - proactive_day_bucket
        - unanswered_proactive_count (incremented)
        """
        now = datetime.now()
        today_bucket = now.strftime("%Y-%m-%d")

        state = self.get_or_create_state(conversation_id)

        # Check if we need to reset daily counter
        if state.proactive_day_bucket != today_bucket:
            new_count = 1
        else:
            new_count = state.proactive_sent_today + 1

        self.update_state(
            conversation_id,
            last_proactive_sent_at=now,
            proactive_sent_today=new_count,
            proactive_day_bucket=today_bucket,
            unanswered_proactive_count=state.unanswered_proactive_count + 1,
        )
        logger.info(
            "Recorded proactive sent for %s (today: %d, unanswered: %d)",
            conversation_id, new_count, state.unanswered_proactive_count + 1
        )

    def record_user_interaction(self, conversation_id: str) -> None:
        """
        Record a user interaction.

        Updates:
        - last_user_interaction_at
        - unanswered_proactive_count (reset to 0)
        """
        now = datetime.now()

        self.update_state(
            conversation_id,
            last_user_interaction_at=now,
            unanswered_proactive_count=0,
        )
        logger.debug("Recorded user interaction for %s", conversation_id)

    def record_outbound(self, conversation_id: str) -> None:
        """
        Record an outbound message (any type).

        Updates:
        - last_outbound_at
        """
        now = datetime.now()
        self.update_state(
            conversation_id,
            last_outbound_at=now,
        )

    def record_impulse_check(self, conversation_id: str) -> None:
        """
        Record that an impulse check was performed.

        Updates:
        - last_impulse_check_at
        """
        now = datetime.now()
        self.update_state(
            conversation_id,
            last_impulse_check_at=now,
        )

    def reset_daily_counters(self, conversation_id: str) -> None:
        """
        Reset daily counters for a conversation.

        Called when day bucket changes.
        """
        today_bucket = datetime.now().strftime("%Y-%m-%d")
        self.update_state(
            conversation_id,
            proactive_sent_today=0,
            proactive_day_bucket=today_bucket,
        )
        logger.debug("Reset daily counters for %s", conversation_id)

    def set_snooze(self, conversation_id: str, until: datetime) -> None:
        """
        Snooze Wind for a conversation until specified time.
        """
        self.update_state(
            conversation_id,
            wind_snooze_until=until,
        )
        logger.info("Snoozed Wind for %s until %s", conversation_id, until)

    def clear_snooze(self, conversation_id: str) -> None:
        """
        Clear Wind snooze for a conversation.
        """
        self.update_state(
            conversation_id,
            wind_snooze_until=None,
        )
        logger.debug("Cleared Wind snooze for %s", conversation_id)

    def get_all_conversation_ids(self) -> list[str]:
        """
        Get all conversation IDs with Wind state.
        """
        conn = self._connect()
        cursor = conn.execute("SELECT conversation_id FROM wind_state")
        return [row["conversation_id"] for row in cursor.fetchall()]
