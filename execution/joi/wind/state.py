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
    # WindMood: threshold drift and accumulator
    threshold_offset: Optional[float] = None  # NULL = use baseline from config
    accumulated_impulse: float = 0.0
    # Engagement tracking (Phase 4a)
    engagement_score: float = 0.5  # Running EMA of response quality (0.0-1.0)
    total_proactives_sent: int = 0
    total_engaged: int = 0
    total_ignored: int = 0
    total_deflected: int = 0
    last_engaged_at: Optional[datetime] = None
    last_deflected_at: Optional[datetime] = None
    # Hot conversation suppression (Phase 5)
    convo_gap_ema_seconds: Optional[float] = None


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
                   updated_at, threshold_offset, accumulated_impulse,
                   engagement_score, total_proactives_sent, total_engaged,
                   total_ignored, total_deflected, last_engaged_at, last_deflected_at,
                   convo_gap_ema_seconds
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
            threshold_offset=row["threshold_offset"],  # NULL preserved as None
            accumulated_impulse=row["accumulated_impulse"] or 0.0,
            engagement_score=row["engagement_score"] if row["engagement_score"] is not None else 0.5,
            total_proactives_sent=row["total_proactives_sent"] or 0,
            total_engaged=row["total_engaged"] or 0,
            total_ignored=row["total_ignored"] or 0,
            total_deflected=row["total_deflected"] or 0,
            last_engaged_at=_parse_datetime(row["last_engaged_at"]),
            last_deflected_at=_parse_datetime(row["last_deflected_at"]),
            convo_gap_ema_seconds=row["convo_gap_ema_seconds"],
        )

    def get_or_create_state(self, conversation_id: str) -> WindState:
        """
        Get Wind state for a conversation, creating default if not exists.

        Uses INSERT OR IGNORE for atomicity - avoids TOCTOU race where
        concurrent threads could both see "not exists" and both INSERT.
        """
        now = datetime.now()
        conn = self._connect()

        # Atomic insert - silently ignores if already exists
        conn.execute(
            """
            INSERT OR IGNORE INTO wind_state (conversation_id, updated_at)
            VALUES (?, ?)
            """,
            (conversation_id, _format_datetime(now))
        )
        conn.commit()

        # Now fetch the state (guaranteed to exist)
        return self.get_state(conversation_id)

    _VALID_STATE_COLUMNS = frozenset({
        "last_user_interaction_at", "last_outbound_at", "last_proactive_sent_at",
        "last_impulse_check_at", "proactive_sent_today", "proactive_day_bucket",
        "unanswered_proactive_count", "wind_snooze_until", "updated_at",
        "threshold_offset", "accumulated_impulse",
        "engagement_score", "total_proactives_sent", "total_engaged",
        "total_ignored", "total_deflected", "last_engaged_at", "last_deflected_at",
        "convo_gap_ema_seconds",
    })

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
            if key not in self._VALID_STATE_COLUMNS:
                raise ValueError(f"Invalid wind_state column: {key!r}")
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
        logger.debug("Updated wind_state", extra={"conversation_id": conversation_id, "keys": list(updates.keys())})

    def record_proactive_sent(self, conversation_id: str) -> None:
        """
        Record that a proactive message was sent.

        Updates:
        - last_proactive_sent_at
        - proactive_sent_today (incremented, or reset if new day)
        - proactive_day_bucket
        - unanswered_proactive_count (incremented)
        - total_proactives_sent (incremented)

        Uses atomic SQL update to avoid read-modify-write race with
        record_user_interaction (which resets unanswered_proactive_count).
        """
        now = datetime.now()
        today_bucket = now.strftime("%Y-%m-%d")

        # Ensure state exists (atomic)
        self.get_or_create_state(conversation_id)

        conn = self._connect()

        # Atomic update with conditional reset and increment
        # If day bucket changed, reset to 1; otherwise increment
        # unanswered_proactive_count always increments atomically
        conn.execute(
            """
            UPDATE wind_state
            SET last_proactive_sent_at = ?,
                proactive_sent_today = CASE
                    WHEN proactive_day_bucket != ? OR proactive_day_bucket IS NULL
                    THEN 1
                    ELSE proactive_sent_today + 1
                END,
                proactive_day_bucket = ?,
                unanswered_proactive_count = unanswered_proactive_count + 1,
                total_proactives_sent = COALESCE(total_proactives_sent, 0) + 1,
                updated_at = ?
            WHERE conversation_id = ?
            """,
            (_format_datetime(now), today_bucket, today_bucket,
             _format_datetime(now), conversation_id)
        )
        conn.commit()

        # Log with fresh state
        state = self.get_state(conversation_id)
        logger.info("Recorded proactive sent", extra={
            "conversation_id": conversation_id,
            "today": state.proactive_sent_today,
            "unanswered": state.unanswered_proactive_count,
            "total": state.total_proactives_sent
        })

    def record_user_interaction(self, conversation_id: str, ema_alpha: float = 0.3) -> None:
        """
        Record a user interaction.

        Updates:
        - last_user_interaction_at
        - unanswered_proactive_count (reset to 0)
        - convo_gap_ema_seconds (EMA of inter-message gaps)
        """
        now = datetime.now()
        state = self.get_state(conversation_id)

        updates: dict = {
            "last_user_interaction_at": now,
            "unanswered_proactive_count": 0,
        }

        if state and state.last_user_interaction_at:
            gap = min(
                (now - state.last_user_interaction_at).total_seconds(),
                4 * 3600,  # cap at 4 hours so stale gaps don't permanently inflate EMA
            )
            old_ema = state.convo_gap_ema_seconds
            if old_ema is None:
                new_ema = gap  # bootstrap from first observed gap
            else:
                new_ema = (1 - ema_alpha) * old_ema + ema_alpha * gap
            updates["convo_gap_ema_seconds"] = round(new_ema, 1)

        self.update_state(conversation_id, **updates)
        logger.debug("Recorded user interaction", extra={"conversation_id": conversation_id})

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
        logger.debug("Reset daily counters", extra={"conversation_id": conversation_id})

    def set_snooze(self, conversation_id: str, until: datetime) -> None:
        """
        Snooze Wind for a conversation until specified time.
        """
        self.update_state(
            conversation_id,
            wind_snooze_until=until,
        )
        logger.info("Snoozed Wind", extra={"conversation_id": conversation_id, "until": str(until)})

    def clear_snooze(self, conversation_id: str) -> None:
        """
        Clear Wind snooze for a conversation.
        """
        self.update_state(
            conversation_id,
            wind_snooze_until=None,
        )
        logger.debug("Cleared Wind snooze", extra={"conversation_id": conversation_id})

    def get_all_conversation_ids(self) -> list[str]:
        """
        Get all conversation IDs with Wind state.
        """
        conn = self._connect()
        cursor = conn.execute("SELECT conversation_id FROM wind_state")
        return [row["conversation_id"] for row in cursor.fetchall()]

    def reset_windmood(self, conversation_id: Optional[str] = None) -> int:
        """
        Reset WindMood values to defaults.

        Sets threshold_offset=NULL (use baseline) and accumulated_impulse=0.

        Args:
            conversation_id: Reset one conversation, or None for all

        Returns:
            Number of conversations reset
        """
        conn = self._connect()

        if conversation_id:
            cursor = conn.execute(
                """
                UPDATE wind_state
                SET threshold_offset = NULL, accumulated_impulse = 0.0, updated_at = ?
                WHERE conversation_id = ?
                """,
                (_format_datetime(datetime.now()), conversation_id)
            )
        else:
            cursor = conn.execute(
                """
                UPDATE wind_state
                SET threshold_offset = NULL, accumulated_impulse = 0.0, updated_at = ?
                """,
                (_format_datetime(datetime.now()),)
            )

        conn.commit()
        count = cursor.rowcount
        logger.info("Reset WindMood", extra={
            "conversation_id": conversation_id or "all",
            "count": count
        })
        return count

    def get_windmood_states(self) -> list[dict]:
        """
        Get WindMood state for all conversations.

        Returns list of dicts with conversation_id, threshold_offset, accumulated_impulse.
        """
        conn = self._connect()
        cursor = conn.execute(
            """
            SELECT conversation_id, threshold_offset, accumulated_impulse
            FROM wind_state
            ORDER BY conversation_id
            """
        )
        return [
            {
                "conversation_id": row["conversation_id"],
                "threshold_offset": row["threshold_offset"],
                "accumulated_impulse": row["accumulated_impulse"] or 0.0,
            }
            for row in cursor.fetchall()
        ]

    # --- Phase 4a: Engagement tracking methods ---

    def record_engagement(
        self,
        conversation_id: str,
        outcome: str,
        quality: float = 0.0,
        ema_alpha: float = 0.2,
    ) -> None:
        """
        Record an engagement outcome and update running engagement score.

        Args:
            conversation_id: Conversation to update
            outcome: 'engaged', 'ignored', or 'deflected'
            quality: Response quality 0.0-1.0 (only meaningful for 'engaged')
            ema_alpha: EMA weight for new value (default 0.2 = slow adaptation)
        """
        now = datetime.now()
        self.get_or_create_state(conversation_id)
        conn = self._connect()

        # Determine counter to increment and quality for EMA
        if outcome == "engaged":
            counter_col = "total_engaged"
            timestamp_col = "last_engaged_at"
            ema_input = quality  # Use actual quality
        elif outcome == "ignored":
            counter_col = "total_ignored"
            timestamp_col = None
            ema_input = 0.0  # Ignored = 0 quality
        elif outcome == "deflected":
            counter_col = "total_deflected"
            timestamp_col = "last_deflected_at"
            ema_input = 0.0  # Deflected = 0 quality
        else:
            logger.warning("Unknown engagement outcome", extra={"outcome": outcome})
            return

        # Build dynamic SQL for the update
        # EMA formula: new_score = (1 - alpha) * old_score + alpha * new_value
        # counter_col and timestamp_col are validated by the if/elif block above.
        set_parts = [
            f"{counter_col} = COALESCE({counter_col}, 0) + 1",
            "engagement_score = ? * COALESCE(engagement_score, 0.5) + ? * ?",
            "updated_at = ?",
        ]
        params = [1.0 - ema_alpha, ema_alpha, ema_input, _format_datetime(now)]

        if timestamp_col:
            set_parts.append(f"{timestamp_col} = ?")
            params.append(_format_datetime(now))

        params.append(conversation_id)

        sql = f"""
            UPDATE wind_state
            SET {', '.join(set_parts)}
            WHERE conversation_id = ?
        """

        conn.execute(sql, params)
        conn.commit()

        # Log with fresh state
        state = self.get_state(conversation_id)
        logger.info("Recorded engagement", extra={
            "conversation_id": conversation_id,
            "outcome": outcome,
            "quality": quality,
            "engagement_score": round(state.engagement_score, 3) if state else 0.5,
        })

    def get_engagement_stats(self, conversation_id: str) -> dict:
        """
        Get engagement statistics for a conversation.

        Returns dict with all engagement metrics.
        """
        state = self.get_state(conversation_id)
        if not state:
            return {
                "conversation_id": conversation_id,
                "engagement_score": 0.5,
                "total_proactives_sent": 0,
                "total_engaged": 0,
                "total_ignored": 0,
                "total_deflected": 0,
                "engagement_rate": 0.0,
                "last_engaged_at": None,
                "last_deflected_at": None,
            }

        total_responses = state.total_engaged + state.total_ignored + state.total_deflected
        engagement_rate = state.total_engaged / total_responses if total_responses > 0 else 0.0

        return {
            "conversation_id": conversation_id,
            "engagement_score": round(state.engagement_score, 3),
            "total_proactives_sent": state.total_proactives_sent,
            "total_engaged": state.total_engaged,
            "total_ignored": state.total_ignored,
            "total_deflected": state.total_deflected,
            "engagement_rate": round(engagement_rate, 3),
            "last_engaged_at": state.last_engaged_at.isoformat() if state.last_engaged_at else None,
            "last_deflected_at": state.last_deflected_at.isoformat() if state.last_deflected_at else None,
        }

    def get_all_engagement_stats(self) -> list[dict]:
        """Get engagement stats for all conversations."""
        conn = self._connect()
        cursor = conn.execute(
            """
            SELECT conversation_id, engagement_score, total_proactives_sent,
                   total_engaged, total_ignored, total_deflected,
                   last_engaged_at, last_deflected_at
            FROM wind_state
            ORDER BY conversation_id
            """
        )
        results = []
        for row in cursor.fetchall():
            total_responses = (row["total_engaged"] or 0) + (row["total_ignored"] or 0) + (row["total_deflected"] or 0)
            engagement_rate = (row["total_engaged"] or 0) / total_responses if total_responses > 0 else 0.0
            results.append({
                "conversation_id": row["conversation_id"],
                "engagement_score": round(row["engagement_score"] or 0.5, 3),
                "total_proactives_sent": row["total_proactives_sent"] or 0,
                "total_engaged": row["total_engaged"] or 0,
                "total_ignored": row["total_ignored"] or 0,
                "total_deflected": row["total_deflected"] or 0,
                "engagement_rate": round(engagement_rate, 3),
                "last_engaged_at": row["last_engaged_at"],
                "last_deflected_at": row["last_deflected_at"],
            })
        return results
