"""
Reminder manager - standalone reminder subsystem, separate from Wind.

Reminders are deterministic, user-requested, and available in all modes.
They do not go through Wind's engagement tracking or lifecycle pipeline.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

logger = logging.getLogger("joi.reminders")


@dataclass
class Reminder:
    """A reminder entry."""

    id: int
    conversation_id: str
    title: str
    due_at: Optional[datetime]
    status: str
    recurrence: Optional[str]
    created_at: Optional[datetime]
    fired_at: Optional[datetime]
    expires_at: Optional[datetime]
    snooze_count: int


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse ISO format datetime string."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _fmt_dt(dt: Optional[datetime]) -> Optional[str]:
    """Format datetime to ISO string."""
    if not dt:
        return None
    return dt.isoformat()


_RECURRENCE_RE = re.compile(r"^(\d+)([dhm])$")


def parse_recurrence_interval(recurrence: str) -> Optional[timedelta]:
    """
    Parse a recurrence interval string to a timedelta.

    Supported formats: '1d', '7d', '2h', '30m'.
    Returns None if the string cannot be parsed.
    """
    if not recurrence:
        return None
    m = _RECURRENCE_RE.match(recurrence.strip())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "d":
        return timedelta(days=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "m":
        return timedelta(minutes=n)
    return None


class ReminderManager:
    """
    Manages user reminders.

    Uses the reminders table for persistence.
    Completely separate from Wind's pending_topics pipeline.
    """

    def __init__(self, db_connection_factory):
        """
        Initialize ReminderManager.

        Args:
            db_connection_factory: Callable that returns a database connection
        """
        self._connect = db_connection_factory

    def _row_to_reminder(self, row) -> Reminder:
        """Convert a database row to Reminder object."""
        return Reminder(
            id=row["id"],
            conversation_id=row["conversation_id"],
            title=row["title"],
            due_at=_parse_dt(row["due_at"]),
            status=row["status"],
            recurrence=row["recurrence"],
            created_at=_parse_dt(row["created_at"]),
            fired_at=_parse_dt(row["fired_at"]),
            expires_at=_parse_dt(row["expires_at"]),
            snooze_count=row["snooze_count"] or 0,
        )

    def add(
        self,
        conversation_id: str,
        title: str,
        due_at: datetime,
        expires_at: Optional[datetime] = None,
        recurrence: Optional[str] = None,
    ) -> int:
        """
        Add a new reminder.

        Args:
            conversation_id: Target conversation
            title: What to remind about (user-supplied text)
            due_at: When to fire
            expires_at: Give-up time for one-shots; None = never expire
            recurrence: Interval string e.g. '7d', '1d', '2h'; None = one-time

        Returns:
            Reminder ID
        """
        now = datetime.now()
        conn = self._connect()
        cursor = conn.execute(
            """
            INSERT INTO reminders (
                conversation_id, title, due_at, status, recurrence, created_at, expires_at
            ) VALUES (?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                conversation_id,
                title,
                _fmt_dt(due_at),
                recurrence,
                _fmt_dt(now),
                _fmt_dt(expires_at),
            ),
        )
        conn.commit()
        reminder_id = cursor.lastrowid or 0
        logger.info("Reminder added", extra={
            "reminder_id": reminder_id,
            "conversation_id": conversation_id,
            "due_at": _fmt_dt(due_at),
            "recurrence": recurrence,
            "action": "reminder_add",
        })
        return reminder_id

    def get_due(self, now: Optional[datetime] = None) -> List[Reminder]:
        """
        Get all reminders that are due (due_at <= now, not expired).

        Returns reminders with status='pending' whose due_at is in the past.
        """
        if now is None:
            now = datetime.now()
        now_iso = now.isoformat()
        conn = self._connect()
        cursor = conn.execute(
            """
            SELECT id, conversation_id, title, due_at, status, recurrence,
                   created_at, fired_at, expires_at, snooze_count
            FROM reminders
            WHERE status = 'pending'
              AND due_at <= ?
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY due_at ASC
            """,
            (now_iso, now_iso),
        )
        return [self._row_to_reminder(row) for row in cursor.fetchall()]

    def mark_fired(self, reminder_id: int) -> None:
        """Mark a one-time reminder as fired. Sets status='fired', fired_at=now."""
        now = datetime.now()
        conn = self._connect()
        conn.execute(
            "UPDATE reminders SET status = 'fired', fired_at = ? WHERE id = ?",
            (_fmt_dt(now), reminder_id),
        )
        conn.commit()
        logger.debug("Reminder marked fired", extra={"reminder_id": reminder_id})

    def reschedule(self, reminder_id: int, new_due_at: datetime) -> None:
        """
        Reschedule a recurring reminder after firing.

        Sets due_at to new_due_at, resets fired_at, keeps status='pending'.
        """
        conn = self._connect()
        conn.execute(
            """
            UPDATE reminders
            SET due_at = ?, status = 'pending', fired_at = NULL
            WHERE id = ?
            """,
            (_fmt_dt(new_due_at), reminder_id),
        )
        conn.commit()
        logger.debug("Reminder rescheduled", extra={
            "reminder_id": reminder_id,
            "new_due_at": _fmt_dt(new_due_at),
        })

    def cancel(self, reminder_id: int) -> None:
        """Cancel a reminder."""
        conn = self._connect()
        conn.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE id = ?",
            (reminder_id,),
        )
        conn.commit()
        logger.debug("Reminder cancelled", extra={"reminder_id": reminder_id})

    def snooze(self, reminder_id: int, new_due_at: datetime) -> None:
        """
        Snooze a reminder: reschedule and increment snooze_count.

        Used when user replies to a fired reminder with a snooze-style message.
        """
        conn = self._connect()
        conn.execute(
            """
            UPDATE reminders
            SET due_at = ?, status = 'pending', snooze_count = snooze_count + 1
            WHERE id = ?
            """,
            (_fmt_dt(new_due_at), reminder_id),
        )
        conn.commit()
        logger.debug("Reminder snoozed", extra={"reminder_id": reminder_id})

    def get_last_fired(self, conversation_id: str) -> Optional[Reminder]:
        """
        Get the most recently fired reminder for a conversation.

        Used to handle post-fire snooze ("remind me again in 1h").
        """
        conn = self._connect()
        cursor = conn.execute(
            """
            SELECT id, conversation_id, title, due_at, status, recurrence,
                   created_at, fired_at, expires_at, snooze_count
            FROM reminders
            WHERE conversation_id = ? AND status = 'fired'
            ORDER BY fired_at DESC
            LIMIT 1
            """,
            (conversation_id,),
        )
        row = cursor.fetchone()
        return self._row_to_reminder(row) if row else None

    def list_pending(self, conversation_id: str) -> List[Reminder]:
        """List pending reminders for a conversation, ordered by due_at."""
        conn = self._connect()
        cursor = conn.execute(
            """
            SELECT id, conversation_id, title, due_at, status, recurrence,
                   created_at, fired_at, expires_at, snooze_count
            FROM reminders
            WHERE conversation_id = ? AND status = 'pending'
            ORDER BY due_at ASC
            """,
            (conversation_id,),
        )
        return [self._row_to_reminder(row) for row in cursor.fetchall()]
