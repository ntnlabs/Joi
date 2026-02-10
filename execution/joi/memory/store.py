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


# Schema version for migrations
SCHEMA_VERSION = 1

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
    FOREIGN KEY (reply_to_id) REFERENCES messages(message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_messages_direction ON messages(direction, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, timestamp DESC);

-- System state table (operational state)
CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000)
);

-- Initialize default system state if not exists
INSERT OR IGNORE INTO system_state (key, value) VALUES
    ('schema_version', '1'),
    ('last_interaction_at', '0'),
    ('last_impulse_check_at', '0'),
    ('messages_sent_this_hour', '0'),
    ('messages_sent_hour_start', '0'),
    ('current_conversation_topic', ''),
    ('agent_state', '"idle"'),
    ('last_context_cleanup_at', '0'),
    ('last_memory_consolidation_at', '0');
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
                       content_text, conversation_id, reply_to_id, timestamp, created_at
                FROM messages
                WHERE content_type = ? AND conversation_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (content_type, conversation_id, limit)
            )
        else:
            cursor = conn.execute(
                """
                SELECT id, message_id, direction, channel, content_type,
                       content_text, conversation_id, reply_to_id, timestamp, created_at
                FROM messages
                WHERE content_type = ?
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
            )
            for row in rows
        ]

        return list(reversed(messages))

    def get_message_count(self, direction: Optional[str] = None, since_ms: Optional[int] = None) -> int:
        """Count messages, optionally filtered by direction and time."""
        conn = self._connect()

        query = "SELECT COUNT(*) FROM messages WHERE 1=1"
        params: List[Any] = []

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
