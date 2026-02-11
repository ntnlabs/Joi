"""
Joi Memory Store - SQLite/SQLCipher database for conversation and state.

See memory-store-schema.md for full schema documentation.
"""

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("joi.memory")


@dataclass
class Message:
    """A stored message."""
    id: int
    message_id: str
    direction: str  # 'inbound' or 'outbound'
    channel: str
    content_type: str
    content_text: Optional[str]
    conversation_id: Optional[str]
    reply_to_id: Optional[str]
    timestamp: int
    created_at: int
    archived: bool = False


@dataclass
class UserFact:
    """A fact about the user."""
    id: int
    category: str  # 'personal', 'preference', 'relationship', etc.
    key: str
    value: str
    confidence: float
    source: str  # 'stated', 'inferred', 'configured'
    learned_at: int
    last_verified_at: Optional[int]


@dataclass
class ContextSummary:
    """A summarized conversation period."""
    id: int
    summary_type: str  # 'conversation', 'daily', 'weekly'
    period_start: int
    period_end: int
    summary_text: str
    message_count: int
    created_at: int


# Schema version for migrations
SCHEMA_VERSION = 2

# SQL for creating tables
SCHEMA_SQL = """
-- Messages table (conversation history)
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE NOT NULL,
    direction TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'direct',
    content_type TEXT NOT NULL,
    content_text TEXT,
    content_media_path TEXT,
    conversation_id TEXT,
    reply_to_id TEXT,
    timestamp INTEGER NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000),
    processed INTEGER NOT NULL DEFAULT 0,
    escalated INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (reply_to_id) REFERENCES messages(message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_messages_direction ON messages(direction, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_messages_archived ON messages(archived, timestamp DESC);

-- System state table (operational state)
CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000)
);

-- Initialize default system state if not exists
INSERT OR IGNORE INTO system_state (key, value) VALUES
    ('schema_version', '2'),
    ('last_interaction_at', '0'),
    ('last_impulse_check_at', '0'),
    ('messages_sent_this_hour', '0'),
    ('messages_sent_hour_start', '0'),
    ('current_conversation_topic', ''),
    ('agent_state', '"idle"'),
    ('last_context_cleanup_at', '0'),
    ('last_memory_consolidation_at', '0');

-- User facts table (long-term memory about user)
CREATE TABLE IF NOT EXISTS user_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.8,
    source TEXT NOT NULL,
    source_message_id TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    learned_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000),
    last_referenced_at INTEGER,
    last_verified_at INTEGER,
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000),
    UNIQUE(category, key, active)
);

CREATE INDEX IF NOT EXISTS idx_facts_category ON user_facts(category, active);
CREATE INDEX IF NOT EXISTS idx_facts_active ON user_facts(active, confidence DESC);

-- Context summaries table (compressed conversation history)
CREATE TABLE IF NOT EXISTS context_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_type TEXT NOT NULL,
    period_start INTEGER NOT NULL,
    period_end INTEGER NOT NULL,
    summary_text TEXT NOT NULL,
    key_points_json TEXT,
    message_count INTEGER,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000)
);

CREATE INDEX IF NOT EXISTS idx_summaries_period ON context_summaries(summary_type, period_end DESC);
"""


class MemoryStore:
    """
    SQLite-based memory store for Joi.

    Thread-safe via connection-per-thread pattern.
    Can be upgraded to SQLCipher by changing _connect().
    """

    def __init__(self, db_path: str, encryption_key: Optional[str] = None):
        """
        Initialize memory store.

        Args:
            db_path: Path to SQLite database file
            encryption_key: Optional SQLCipher encryption key (for future use)
        """
        self._db_path = db_path
        self._encryption_key = encryption_key
        self._local = threading.local()

        # Ensure directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        # Initialize schema
        self._init_schema()
        logger.info("Memory store initialized: %s", db_path)

    def _connect(self) -> sqlite3.Connection:
        """Get or create a connection for the current thread."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row

            # Performance settings
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")

            # SQLCipher would be configured here:
            # if self._encryption_key:
            #     conn.execute(f"PRAGMA key = '{self._encryption_key}'")
            #     conn.execute("PRAGMA cipher_page_size = 4096")
            #     conn.execute("PRAGMA kdf_iter = 256000")

            self._local.conn = conn
        return self._local.conn

    def _init_schema(self) -> None:
        """Initialize database schema."""
        conn = self._connect()
        conn.executescript(SCHEMA_SQL)
        conn.commit()

    def close(self) -> None:
        """Close the database connection for this thread."""
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    # --- Message Operations ---

    def store_message(
        self,
        message_id: str,
        direction: str,
        content_type: str,
        content_text: Optional[str],
        timestamp: int,
        channel: str = "direct",
        conversation_id: Optional[str] = None,
        reply_to_id: Optional[str] = None,
        content_media_path: Optional[str] = None,
    ) -> int:
        """
        Store a message in the database.

        Args:
            message_id: Unique message identifier (from Signal)
            direction: 'inbound' or 'outbound'
            content_type: 'text', 'reaction', etc.
            content_text: Message text content
            timestamp: Unix epoch milliseconds
            channel: 'direct' or 'critical'
            conversation_id: Conversation/thread ID
            reply_to_id: Message ID being replied to
            content_media_path: Local path if media attachment

        Returns:
            Database row ID of inserted message
        """
        conn = self._connect()
        now_ms = int(time.time() * 1000)

        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO messages (
                message_id, direction, channel, content_type, content_text,
                content_media_path, conversation_id, reply_to_id, timestamp, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id, direction, channel, content_type, content_text,
                content_media_path, conversation_id, reply_to_id, timestamp, now_ms
            )
        )
        conn.commit()

        # Update last_interaction_at for inbound messages
        if direction == "inbound":
            self.set_state("last_interaction_at", str(now_ms))

        logger.debug("Stored %s message: %s", direction, message_id)
        return cursor.lastrowid or 0

    def get_recent_messages(
        self,
        limit: int = 20,
        conversation_id: Optional[str] = None,
        content_type: str = "text",
    ) -> List[Message]:
        """
        Get recent messages for LLM context.

        Args:
            limit: Maximum number of messages to return
            conversation_id: Filter by conversation (optional)
            content_type: Filter by content type (default: text)

        Returns:
            List of Message objects, oldest first (for context building)
        """
        conn = self._connect()

        if conversation_id:
            cursor = conn.execute(
                """
                SELECT id, message_id, direction, channel, content_type,
                       content_text, conversation_id, reply_to_id, timestamp, created_at, archived
                FROM messages
                WHERE content_type = ? AND conversation_id = ? AND archived = 0
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (content_type, conversation_id, limit)
            )
        else:
            cursor = conn.execute(
                """
                SELECT id, message_id, direction, channel, content_type,
                       content_text, conversation_id, reply_to_id, timestamp, created_at, archived
                FROM messages
                WHERE content_type = ? AND archived = 0
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (content_type, limit)
            )

        rows = cursor.fetchall()

        # Convert to Message objects and reverse to get oldest-first order
        messages = [
            Message(
                id=row["id"],
                message_id=row["message_id"],
                direction=row["direction"],
                channel=row["channel"],
                content_type=row["content_type"],
                content_text=row["content_text"],
                conversation_id=row["conversation_id"],
                reply_to_id=row["reply_to_id"],
                timestamp=row["timestamp"],
                created_at=row["created_at"],
                archived=bool(row["archived"]),
            )
            for row in rows
        ]

        return list(reversed(messages))

    def get_message_count(self, direction: Optional[str] = None, since_ms: Optional[int] = None, include_archived: bool = False) -> int:
        """Count messages, optionally filtered by direction and time."""
        conn = self._connect()

        query = "SELECT COUNT(*) FROM messages WHERE 1=1"
        params: List[Any] = []

        if not include_archived:
            query += " AND archived = 0"

        if direction:
            query += " AND direction = ?"
            params.append(direction)

        if since_ms:
            query += " AND timestamp > ?"
            params.append(since_ms)

        cursor = conn.execute(query, params)
        return cursor.fetchone()[0]

    # --- System State Operations ---

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a system state value."""
        conn = self._connect()
        cursor = conn.execute(
            "SELECT value FROM system_state WHERE key = ?",
            (key,)
        )
        row = cursor.fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        """Set a system state value."""
        conn = self._connect()
        now_ms = int(time.time() * 1000)
        conn.execute(
            """
            INSERT INTO system_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?
            """,
            (key, value, now_ms, value, now_ms)
        )
        conn.commit()

    def get_last_interaction_ms(self) -> int:
        """Get timestamp of last user interaction."""
        val = self.get_state("last_interaction_at", "0")
        return int(val) if val else 0

    # --- User Facts Operations ---

    def store_fact(
        self,
        category: str,
        key: str,
        value: str,
        confidence: float = 0.8,
        source: str = "inferred",
        source_message_id: Optional[str] = None,
    ) -> int:
        """
        Store or update a fact about the user.

        If fact with same category+key exists, updates it.
        """
        conn = self._connect()
        now_ms = int(time.time() * 1000)

        # Try to update existing active fact
        cursor = conn.execute(
            """
            UPDATE user_facts
            SET value = ?, confidence = ?, source = ?, source_message_id = ?,
                last_verified_at = ?, updated_at = ?
            WHERE category = ? AND key = ? AND active = 1
            """,
            (value, confidence, source, source_message_id, now_ms, now_ms, category, key)
        )

        if cursor.rowcount == 0:
            # Insert new fact
            cursor = conn.execute(
                """
                INSERT INTO user_facts (
                    category, key, value, confidence, source, source_message_id,
                    learned_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (category, key, value, confidence, source, source_message_id, now_ms, now_ms)
            )

        conn.commit()
        logger.debug("Stored fact: %s.%s = %s (confidence: %.2f)", category, key, value, confidence)
        return cursor.lastrowid or 0

    def get_facts(
        self,
        min_confidence: float = 0.5,
        category: Optional[str] = None,
        limit: int = 50,
    ) -> List[UserFact]:
        """Get active user facts, optionally filtered by category."""
        conn = self._connect()

        if category:
            cursor = conn.execute(
                """
                SELECT id, category, key, value, confidence, source,
                       learned_at, last_verified_at
                FROM user_facts
                WHERE active = 1 AND confidence >= ? AND category = ?
                ORDER BY confidence DESC
                LIMIT ?
                """,
                (min_confidence, category, limit)
            )
        else:
            cursor = conn.execute(
                """
                SELECT id, category, key, value, confidence, source,
                       learned_at, last_verified_at
                FROM user_facts
                WHERE active = 1 AND confidence >= ?
                ORDER BY category, confidence DESC
                LIMIT ?
                """,
                (min_confidence, limit)
            )

        return [
            UserFact(
                id=row["id"],
                category=row["category"],
                key=row["key"],
                value=row["value"],
                confidence=row["confidence"],
                source=row["source"],
                learned_at=row["learned_at"],
                last_verified_at=row["last_verified_at"],
            )
            for row in cursor.fetchall()
        ]

    def get_facts_as_text(self, min_confidence: float = 0.5) -> str:
        """Get facts formatted as text for LLM context."""
        facts = self.get_facts(min_confidence=min_confidence)
        if not facts:
            return ""

        lines = ["Known facts about the user:"]
        by_category: Dict[str, List[UserFact]] = {}
        for fact in facts:
            by_category.setdefault(fact.category, []).append(fact)

        for category, cat_facts in sorted(by_category.items()):
            lines.append(f"\n{category.title()}:")
            for fact in cat_facts:
                lines.append(f"  - {fact.key}: {fact.value}")

        return "\n".join(lines)

    # --- Context Summaries Operations ---

    def store_summary(
        self,
        summary_type: str,
        period_start: int,
        period_end: int,
        summary_text: str,
        message_count: int = 0,
        key_points_json: Optional[str] = None,
    ) -> int:
        """Store a conversation summary."""
        conn = self._connect()
        now_ms = int(time.time() * 1000)

        cursor = conn.execute(
            """
            INSERT INTO context_summaries (
                summary_type, period_start, period_end, summary_text,
                key_points_json, message_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (summary_type, period_start, period_end, summary_text,
             key_points_json, message_count, now_ms)
        )
        conn.commit()

        logger.info("Stored %s summary for period %d-%d (%d messages)",
                    summary_type, period_start, period_end, message_count)
        return cursor.lastrowid or 0

    def get_recent_summaries(
        self,
        summary_type: str = "conversation",
        days: int = 7,
        limit: int = 10,
    ) -> List[ContextSummary]:
        """Get recent summaries within the last N days."""
        conn = self._connect()
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - (days * 24 * 60 * 60 * 1000)

        cursor = conn.execute(
            """
            SELECT id, summary_type, period_start, period_end, summary_text,
                   message_count, created_at
            FROM context_summaries
            WHERE summary_type = ? AND period_end > ?
            ORDER BY period_end DESC
            LIMIT ?
            """,
            (summary_type, cutoff_ms, limit)
        )

        return [
            ContextSummary(
                id=row["id"],
                summary_type=row["summary_type"],
                period_start=row["period_start"],
                period_end=row["period_end"],
                summary_text=row["summary_text"],
                message_count=row["message_count"] or 0,
                created_at=row["created_at"],
            )
            for row in cursor.fetchall()
        ]

    def get_summaries_as_text(self, days: int = 7) -> str:
        """Get summaries formatted as text for LLM context."""
        summaries = self.get_recent_summaries(days=days)
        if not summaries:
            return ""

        lines = ["Recent conversation history:"]
        for summary in reversed(summaries):  # Oldest first
            # Format timestamp as date
            from datetime import datetime
            date_str = datetime.fromtimestamp(summary.period_end / 1000).strftime("%Y-%m-%d")
            lines.append(f"\n[{date_str}]")
            lines.append(summary.summary_text)

        return "\n".join(lines)

    # --- Messages for Summarization ---

    def get_messages_for_summarization(
        self,
        older_than_ms: int,
        limit: int = 200,
    ) -> List[Message]:
        """Get old non-archived messages that should be summarized."""
        conn = self._connect()

        cursor = conn.execute(
            """
            SELECT id, message_id, direction, channel, content_type,
                   content_text, conversation_id, reply_to_id, timestamp, created_at, archived
            FROM messages
            WHERE content_type = 'text' AND timestamp < ? AND archived = 0
            ORDER BY timestamp ASC
            LIMIT ?
            """,
            (older_than_ms, limit)
        )

        return [
            Message(
                id=row["id"],
                message_id=row["message_id"],
                direction=row["direction"],
                channel=row["channel"],
                content_type=row["content_type"],
                content_text=row["content_text"],
                conversation_id=row["conversation_id"],
                reply_to_id=row["reply_to_id"],
                timestamp=row["timestamp"],
                created_at=row["created_at"],
                archived=bool(row["archived"]),
            )
            for row in cursor.fetchall()
        ]

    def archive_messages_before(self, before_ms: int) -> int:
        """Archive (soft-delete) messages older than timestamp."""
        conn = self._connect()

        cursor = conn.execute(
            "UPDATE messages SET archived = 1 WHERE timestamp < ? AND archived = 0",
            (before_ms,)
        )
        conn.commit()

        archived = cursor.rowcount
        if archived > 0:
            logger.info("Archived %d messages before %d", archived, before_ms)
        return archived

    def delete_messages_before(self, before_ms: int) -> int:
        """Hard delete messages older than timestamp (use archive_messages_before for soft delete)."""
        conn = self._connect()

        cursor = conn.execute(
            "DELETE FROM messages WHERE timestamp < ?",
            (before_ms,)
        )
        conn.commit()

        deleted = cursor.rowcount
        if deleted > 0:
            logger.info("Deleted %d messages before %d", deleted, before_ms)
        return deleted

    # --- Cleanup Operations ---

    def cleanup_old_messages(self, keep_count: int = 1000) -> int:
        """
        Remove old messages, keeping the most recent ones.

        Args:
            keep_count: Number of recent messages to keep

        Returns:
            Number of messages deleted
        """
        conn = self._connect()

        cursor = conn.execute(
            """
            DELETE FROM messages
            WHERE id NOT IN (
                SELECT id FROM messages
                ORDER BY timestamp DESC
                LIMIT ?
            )
            """,
            (keep_count,)
        )
        conn.commit()

        deleted = cursor.rowcount
        if deleted > 0:
            logger.info("Cleaned up %d old messages", deleted)
        return deleted


def create_memory_store() -> MemoryStore:
    """
    Factory function to create MemoryStore with settings from environment.

    Environment variables:
        JOI_MEMORY_DB: Path to database file (default: /var/lib/joi/memory.db)
        JOI_MEMORY_KEY: SQLCipher encryption key (optional, for future use)
    """
    db_path = os.getenv("JOI_MEMORY_DB", "/var/lib/joi/memory.db")
    encryption_key = os.getenv("JOI_MEMORY_KEY")

    return MemoryStore(db_path, encryption_key)
