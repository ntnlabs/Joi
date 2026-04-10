"""
Wind decision logging for observability.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

logger = logging.getLogger("joi.wind.logging")


@dataclass
class WindDecision:
    """A Wind decision record."""

    id: Optional[int] = None
    conversation_id: str = ""
    timestamp: Optional[datetime] = None
    eligible: bool = False
    gate_result: Optional[str] = None  # JSON string of gate results
    impulse_score: Optional[float] = None
    threshold: Optional[float] = None
    factor_breakdown: Optional[str] = None  # JSON string of factors
    selected_topic_id: Optional[int] = None
    decision: str = ""  # 'send', 'skip', 'shadow_logged'
    skip_reason: Optional[str] = None
    draft_message: Optional[str] = None
    # WindMood fields
    threshold_offset: Optional[float] = None
    accumulated_impulse: Optional[float] = None


def _format_datetime(dt: Optional[datetime]) -> Optional[str]:
    """Format datetime to ISO string."""
    if not dt:
        return None
    return dt.isoformat()


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse ISO format datetime string."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _fmt_conv_id(conversation_id: str) -> str:
    """Format conversation_id for logging without over-truncating group IDs."""
    if not conversation_id:
        return "?"
    if conversation_id.startswith("+"):
        # Phone number (DM) — show last 4 digits
        return f"+***{conversation_id[-4:]}" if len(conversation_id) >= 4 else "+***"
    else:
        # Group ID (base64) — show first 8 chars
        return f"[GRP:{conversation_id[:8]}...]" if len(conversation_id) >= 8 else f"[GRP:{conversation_id}]"


class WindDecisionLogger:
    """
    Logs Wind decisions for observability and debugging.

    Uses the wind_decision_log table for persistence.
    """

    def __init__(self, db_connection_factory):
        """
        Initialize WindDecisionLogger.

        Args:
            db_connection_factory: Callable that returns a database connection
        """
        self._connect = db_connection_factory
        # Cache last state per conversation: {conv_id: (decision, score_rounded, skip_reason)}
        self._last_state: dict[str, tuple[str, float, str | None]] = {}

    def log_decision(
        self,
        conversation_id: str,
        eligible: bool,
        decision: str,
        gate_result: Optional[Dict[str, Any]] = None,
        impulse_score: Optional[float] = None,
        threshold: Optional[float] = None,
        factor_breakdown: Optional[Dict[str, float]] = None,
        selected_topic_id: Optional[int] = None,
        skip_reason: Optional[str] = None,
        draft_message: Optional[str] = None,
        threshold_offset: Optional[float] = None,
        accumulated_impulse: Optional[float] = None,
    ) -> int:
        """
        Log a Wind decision.

        Args:
            conversation_id: Conversation evaluated
            eligible: Whether conversation was eligible
            decision: Decision made ('send', 'skip', 'shadow_logged')
            gate_result: Dict of gate check results
            impulse_score: Calculated impulse score
            threshold: Threshold used (with drift applied)
            factor_breakdown: Dict of factor contributions
            selected_topic_id: ID of topic selected (if any)
            skip_reason: Reason for skipping (if applicable)
            draft_message: Draft message (in shadow mode)
            threshold_offset: WindMood threshold drift offset
            accumulated_impulse: WindMood accumulated impulse

        Returns:
            Log entry ID
        """
        now = datetime.now(timezone.utc)
        conn = self._connect()

        # Serialize dicts to JSON
        gate_result_json = json.dumps(gate_result) if gate_result else None
        factor_breakdown_json = json.dumps(factor_breakdown) if factor_breakdown else None

        cursor = conn.execute(
            """
            INSERT INTO wind_decision_log (
                conversation_id, timestamp, eligible, gate_result, impulse_score,
                threshold, factor_breakdown, selected_topic_id, decision,
                skip_reason, draft_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                _format_datetime(now),
                1 if eligible else 0,
                gate_result_json,
                impulse_score,
                threshold,
                factor_breakdown_json,
                selected_topic_id,
                decision,
                skip_reason,
                draft_message,
            )
        )
        conn.commit()

        log_id = cursor.lastrowid or 0

        # Check if state changed (score is already rounded in impulse engine)
        current_state = (decision, impulse_score or 0.0, skip_reason)
        last_state = self._last_state.get(conversation_id)

        # Only log INFO if state changed (suppresses identical repeats)
        if last_state != current_state:
            # Include WindMood fields in log if available
            offset_str = f" offset={threshold_offset:+.3f}" if threshold_offset is not None else ""
            accum_str = f" accum={accumulated_impulse:.2f}" if accumulated_impulse is not None else ""
            logger.info(
                "Wind decision #%d: conv=%s score=%.2f threshold=%.2f%s%s decision=%s reason=%s",
                log_id,
                _fmt_conv_id(conversation_id),
                impulse_score or 0.0,
                threshold or 0.0,
                offset_str,
                accum_str,
                decision,
                skip_reason or "-",
            )
            self._last_state[conversation_id] = current_state

        return log_id

    def get_recent_decisions(
        self,
        conversation_id: Optional[str] = None,
        limit: int = 20,
    ) -> list[WindDecision]:
        """
        Get recent Wind decisions.

        Args:
            conversation_id: Filter by conversation (None = all)
            limit: Maximum number of entries

        Returns:
            List of WindDecision objects
        """
        conn = self._connect()

        if conversation_id:
            cursor = conn.execute(
                """
                SELECT id, conversation_id, timestamp, eligible, gate_result,
                       impulse_score, threshold, factor_breakdown, selected_topic_id,
                       decision, skip_reason, draft_message
                FROM wind_decision_log
                WHERE conversation_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (conversation_id, limit)
            )
        else:
            cursor = conn.execute(
                """
                SELECT id, conversation_id, timestamp, eligible, gate_result,
                       impulse_score, threshold, factor_breakdown, selected_topic_id,
                       decision, skip_reason, draft_message
                FROM wind_decision_log
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,)
            )

        return [
            WindDecision(
                id=row["id"],
                conversation_id=row["conversation_id"],
                timestamp=_parse_datetime(row["timestamp"]),
                eligible=bool(row["eligible"]),
                gate_result=row["gate_result"],
                impulse_score=row["impulse_score"],
                threshold=row["threshold"],
                factor_breakdown=row["factor_breakdown"],
                selected_topic_id=row["selected_topic_id"],
                decision=row["decision"],
                skip_reason=row["skip_reason"],
                draft_message=row["draft_message"],
            )
            for row in cursor.fetchall()
        ]

    def get_decision_stats(
        self,
        conversation_id: Optional[str] = None,
        days: int = 7,
    ) -> Dict[str, Any]:
        """
        Get decision statistics for a conversation or globally.

        Returns:
            Dict with counts and averages
        """
        conn = self._connect()

        # Calculate cutoff
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        if conversation_id:
            cursor = conn.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN decision = 'send' THEN 1 ELSE 0 END) as sent,
                    SUM(CASE WHEN decision = 'skip' THEN 1 ELSE 0 END) as skipped,
                    SUM(CASE WHEN decision = 'shadow_logged' THEN 1 ELSE 0 END) as shadow,
                    AVG(impulse_score) as avg_score
                FROM wind_decision_log
                WHERE conversation_id = ? AND timestamp > ?
                """,
                (conversation_id, cutoff)
            )
        else:
            cursor = conn.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN decision = 'send' THEN 1 ELSE 0 END) as sent,
                    SUM(CASE WHEN decision = 'skip' THEN 1 ELSE 0 END) as skipped,
                    SUM(CASE WHEN decision = 'shadow_logged' THEN 1 ELSE 0 END) as shadow,
                    AVG(impulse_score) as avg_score
                FROM wind_decision_log
                WHERE timestamp > ?
                """,
                (cutoff,)
            )

        row = cursor.fetchone()
        return {
            "total_decisions": row["total"] or 0,
            "sent": row["sent"] or 0,
            "skipped": row["skipped"] or 0,
            "shadow_logged": row["shadow"] or 0,
            "avg_impulse_score": row["avg_score"] or 0.0,
            "days": days,
        }

    def cleanup_old_logs(self, days: int = 30) -> int:
        """
        Delete logs older than specified days.

        Returns:
            Number of logs deleted
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        conn = self._connect()
        cursor = conn.execute(
            "DELETE FROM wind_decision_log WHERE timestamp < ?",
            (cutoff,)
        )
        conn.commit()

        deleted = cursor.rowcount
        if deleted > 0:
            logger.info("Cleaned up wind decision logs", extra={"count": deleted, "days": days})
        return deleted
