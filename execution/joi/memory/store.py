"""
Joi Memory Store - SQLite/SQLCipher database for conversation and state.

See memory-store-schema.md for full schema documentation.
"""

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

# SQLCipher support: use sqlcipher3 if available, otherwise sqlite3
try:
    import sqlcipher3 as sqlite3
    SQLCIPHER_AVAILABLE = True
except ImportError:
    import sqlite3
    SQLCIPHER_AVAILABLE = False

logger = logging.getLogger("joi.memory")

# Default path for encryption key file
DEFAULT_KEY_FILE = "/etc/joi/memory.key"


def load_encryption_key(key_file: Optional[str] = None) -> Optional[str]:
    """
    Load encryption key from file.

    Args:
        key_file: Path to key file (default: /etc/joi/memory.key)

    Returns:
        Encryption key string or None if not available
    """
    key_path = Path(key_file or os.getenv("JOI_MEMORY_KEY_FILE", DEFAULT_KEY_FILE))

    try:
        if not key_path.exists():
            return None

        # Check permissions (should be 600 or stricter)
        mode = key_path.stat().st_mode & 0o777
        if mode > 0o600:
            logger.warning(
                "Key file %s has insecure permissions %o (should be 600 or stricter)",
                key_path, mode
            )

        key = key_path.read_text().strip()
        if len(key) < 32:
            logger.warning("Encryption key is shorter than recommended (32+ chars)")
        return key if key else None
    except PermissionError:
        logger.warning(
            "Cannot access key file %s (permission denied) - running unencrypted",
            key_path
        )
        return None
    except Exception as e:
        logger.error("Failed to read encryption key from %s: %s", key_path, e)
        return None


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
    sender_id: Optional[str] = None  # transport_id (phone number)
    sender_name: Optional[str] = None  # display name


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


@dataclass
class KnowledgeChunk:
    """A chunk of knowledge from a document."""
    id: int
    source: str  # document path or identifier
    title: str
    content: str
    chunk_index: int
    created_at: int


# Schema version for migrations
SCHEMA_VERSION = 4

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
    sender_id TEXT,
    sender_name TEXT,
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

-- User facts table (long-term memory about user, per-conversation)
CREATE TABLE IF NOT EXISTS user_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL DEFAULT '',
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
    UNIQUE(conversation_id, category, key, active)
);

CREATE INDEX IF NOT EXISTS idx_facts_category ON user_facts(category, active);
CREATE INDEX IF NOT EXISTS idx_facts_active ON user_facts(active, confidence DESC);
CREATE INDEX IF NOT EXISTS idx_facts_conversation ON user_facts(conversation_id, active);

-- Context summaries table (compressed conversation history, per-conversation)
CREATE TABLE IF NOT EXISTS context_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL DEFAULT '',
    summary_type TEXT NOT NULL,
    period_start INTEGER NOT NULL,
    period_end INTEGER NOT NULL,
    summary_text TEXT NOT NULL,
    key_points_json TEXT,
    message_count INTEGER,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000)
);

CREATE INDEX IF NOT EXISTS idx_summaries_period ON context_summaries(summary_type, period_end DESC);
CREATE INDEX IF NOT EXISTS idx_summaries_conversation ON context_summaries(conversation_id, period_end DESC);

-- Knowledge chunks table (for RAG)
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000),
    UNIQUE(source, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_source ON knowledge_chunks(source);

-- FTS5 virtual table for full-text search
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    title,
    content,
    content=knowledge_chunks,
    content_rowid=id
);

-- Triggers to keep FTS in sync with knowledge_chunks
CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge_chunks BEGIN
    INSERT INTO knowledge_fts(rowid, title, content) VALUES (new.id, new.title, new.content);
END;

CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge_chunks BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content) VALUES('delete', old.id, old.title, old.content);
END;

CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE ON knowledge_chunks BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content) VALUES('delete', old.id, old.title, old.content);
    INSERT INTO knowledge_fts(rowid, title, content) VALUES (new.id, new.title, new.content);
END;
"""


class MemoryStore:
    """
    SQLite-based memory store for Joi.

    Thread-safe via connection-per-thread pattern.
    Supports SQLCipher encryption when key is provided.
    """

    def __init__(self, db_path: str, encryption_key: Optional[str] = None):
        """
        Initialize memory store.

        Args:
            db_path: Path to SQLite database file
            encryption_key: SQLCipher encryption key (if None, tries to load from file)
        """
        self._db_path = db_path
        self._local = threading.local()

        # Load encryption key from file if not provided directly
        if encryption_key is None:
            encryption_key = load_encryption_key()

        self._encryption_key = encryption_key
        self._encrypted = False

        # Check if we can actually use encryption
        if self._encryption_key:
            if SQLCIPHER_AVAILABLE:
                self._encrypted = True
                logger.info("SQLCipher encryption enabled")
            else:
                logger.warning(
                    "Encryption key provided but sqlcipher3 not installed - "
                    "running UNENCRYPTED. Install: pip install sqlcipher3-binary"
                )

        # Ensure directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        # Initialize schema
        self._init_schema()

        encryption_status = "encrypted" if self._encrypted else "unencrypted"
        logger.info("Memory store initialized: %s (%s)", db_path, encryption_status)

    def _connect(self) -> sqlite3.Connection:
        """Get or create a connection for the current thread."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row

            # SQLCipher encryption - must be set before any other operations
            if self._encrypted and self._encryption_key:
                # PRAGMA doesn't support parameterized queries
                # Use key as passphrase (matches migration script's ATTACH ... KEY 'passphrase')
                conn.execute(f"PRAGMA key = '{self._encryption_key}';")

                # Verify encryption is working by querying the database
                try:
                    conn.execute("SELECT count(*) FROM sqlite_master")
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to open encrypted database - wrong key? Error: {e}"
                    ) from e

            # Performance settings
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")

            self._local.conn = conn
        return self._local.conn

    def _init_schema(self) -> None:
        """Initialize database schema."""
        conn = self._connect()
        # Run migrations first (for existing databases)
        self._run_migrations(conn)
        # Then create any missing tables/indexes
        conn.executescript(SCHEMA_SQL)
        conn.commit()

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        """Run database migrations for schema updates."""
        # Check if messages table exists first
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'")
        if not cursor.fetchone():
            return  # Fresh database, no migrations needed

        # Check columns in messages table
        cursor = conn.execute("PRAGMA table_info(messages)")
        columns = [row[1] for row in cursor.fetchall()]

        if "archived" not in columns:
            logger.info("Migration: Adding 'archived' column to messages table")
            conn.execute("ALTER TABLE messages ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
            conn.commit()

        if "sender_id" not in columns:
            logger.info("Migration: Adding 'sender_id' column to messages table")
            conn.execute("ALTER TABLE messages ADD COLUMN sender_id TEXT")
            conn.commit()

        if "sender_name" not in columns:
            logger.info("Migration: Adding 'sender_name' column to messages table")
            conn.execute("ALTER TABLE messages ADD COLUMN sender_name TEXT")
            conn.commit()

        # Check user_facts table for conversation_id
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_facts'")
        if cursor.fetchone():
            cursor = conn.execute("PRAGMA table_info(user_facts)")
            fact_columns = [row[1] for row in cursor.fetchall()]
            if "conversation_id" not in fact_columns:
                logger.info("Migration: Adding 'conversation_id' column to user_facts table")
                conn.execute("ALTER TABLE user_facts ADD COLUMN conversation_id TEXT NOT NULL DEFAULT ''")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_conversation ON user_facts(conversation_id, active)")
                # Drop and recreate unique constraint (SQLite doesn't support ALTER CONSTRAINT)
                # Existing facts get conversation_id='' which works for backward compatibility
                conn.commit()

        # Check context_summaries table for conversation_id
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='context_summaries'")
        if cursor.fetchone():
            cursor = conn.execute("PRAGMA table_info(context_summaries)")
            summary_columns = [row[1] for row in cursor.fetchall()]
            if "conversation_id" not in summary_columns:
                logger.info("Migration: Adding 'conversation_id' column to context_summaries table")
                conn.execute("ALTER TABLE context_summaries ADD COLUMN conversation_id TEXT NOT NULL DEFAULT ''")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_summaries_conversation ON context_summaries(conversation_id, period_end DESC)")
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
        sender_id: Optional[str] = None,
        sender_name: Optional[str] = None,
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
            sender_id: Sender's transport ID (phone number for Signal)
            sender_name: Sender's display name

        Returns:
            Database row ID of inserted message
        """
        conn = self._connect()
        now_ms = int(time.time() * 1000)

        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO messages (
                message_id, direction, channel, content_type, content_text,
                content_media_path, conversation_id, reply_to_id, sender_id, sender_name,
                timestamp, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id, direction, channel, content_type, content_text,
                content_media_path, conversation_id, reply_to_id, sender_id, sender_name,
                timestamp, now_ms
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
                       content_text, conversation_id, reply_to_id, timestamp, created_at,
                       archived, sender_id, sender_name
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
                       content_text, conversation_id, reply_to_id, timestamp, created_at,
                       archived, sender_id, sender_name
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
                sender_id=row["sender_id"],
                sender_name=row["sender_name"],
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

    def get_distinct_conversation_ids(self, min_messages: int = 1) -> List[str]:
        """Get list of distinct conversation IDs with at least min_messages."""
        conn = self._connect()
        cursor = conn.execute(
            """
            SELECT conversation_id, COUNT(*) as msg_count
            FROM messages
            WHERE conversation_id IS NOT NULL AND conversation_id != ''
                  AND archived = 0 AND content_type = 'text'
            GROUP BY conversation_id
            HAVING msg_count >= ?
            ORDER BY MAX(timestamp) DESC
            """,
            (min_messages,)
        )
        return [row["conversation_id"] for row in cursor.fetchall()]

    def get_message_count_for_conversation(self, conversation_id: str, include_archived: bool = False) -> int:
        """Get count of messages for a specific conversation."""
        conn = self._connect()
        if include_archived:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND content_type = 'text'",
                (conversation_id,)
            )
        else:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND content_type = 'text' AND archived = 0",
                (conversation_id,)
            )
        row = cursor.fetchone()
        return row[0] if row else 0

    def get_last_interaction_for_conversation(self, conversation_id: str) -> int:
        """Get timestamp of last message in a conversation."""
        conn = self._connect()
        cursor = conn.execute(
            "SELECT MAX(timestamp) FROM messages WHERE conversation_id = ? AND content_type = 'text'",
            (conversation_id,)
        )
        row = cursor.fetchone()
        return row[0] if row and row[0] else 0

    # --- User Facts Operations ---

    def store_fact(
        self,
        category: str,
        key: str,
        value: str,
        confidence: float = 0.8,
        source: str = "inferred",
        source_message_id: Optional[str] = None,
        conversation_id: str = "",
    ) -> int:
        """
        Store or update a fact about the user for a specific conversation.

        If fact with same conversation_id+category+key exists, updates it.
        """
        conn = self._connect()
        now_ms = int(time.time() * 1000)

        # Try to update existing active fact for this conversation
        cursor = conn.execute(
            """
            UPDATE user_facts
            SET value = ?, confidence = ?, source = ?, source_message_id = ?,
                last_verified_at = ?, updated_at = ?
            WHERE conversation_id = ? AND category = ? AND key = ? AND active = 1
            """,
            (value, confidence, source, source_message_id, now_ms, now_ms, conversation_id, category, key)
        )

        if cursor.rowcount == 0:
            # Insert new fact
            cursor = conn.execute(
                """
                INSERT INTO user_facts (
                    conversation_id, category, key, value, confidence, source, source_message_id,
                    learned_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (conversation_id, category, key, value, confidence, source, source_message_id, now_ms, now_ms)
            )

        conn.commit()
        logger.debug("Stored fact for %s: %s.%s = %s (confidence: %.2f)", conversation_id or "global", category, key, value, confidence)
        return cursor.lastrowid or 0

    def get_facts(
        self,
        min_confidence: float = 0.5,
        category: Optional[str] = None,
        conversation_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[UserFact]:
        """Get active user facts for a conversation, optionally filtered by category."""
        conn = self._connect()

        # Build query based on filters
        conditions = ["active = 1", "confidence >= ?"]
        params: list = [min_confidence]

        if conversation_id is not None:
            conditions.append("conversation_id = ?")
            params.append(conversation_id)

        if category:
            conditions.append("category = ?")
            params.append(category)

        params.append(limit)
        where_clause = " AND ".join(conditions)

        cursor = conn.execute(
            f"""
            SELECT id, category, key, value, confidence, source,
                   learned_at, last_verified_at
            FROM user_facts
            WHERE {where_clause}
            ORDER BY category, confidence DESC
            LIMIT ?
            """,
            params
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

    def get_facts_as_text(self, min_confidence: float = 0.5, conversation_id: Optional[str] = None) -> str:
        """Get facts formatted as text for LLM context."""
        facts = self.get_facts(min_confidence=min_confidence, conversation_id=conversation_id)
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
        conversation_id: str = "",
    ) -> int:
        """Store a conversation summary for a specific conversation."""
        conn = self._connect()
        now_ms = int(time.time() * 1000)

        cursor = conn.execute(
            """
            INSERT INTO context_summaries (
                conversation_id, summary_type, period_start, period_end, summary_text,
                key_points_json, message_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (conversation_id, summary_type, period_start, period_end, summary_text,
             key_points_json, message_count, now_ms)
        )
        conn.commit()

        logger.info("Stored %s summary for %s period %d-%d (%d messages)",
                    summary_type, conversation_id or "global", period_start, period_end, message_count)
        return cursor.lastrowid or 0

    def get_recent_summaries(
        self,
        summary_type: str = "conversation",
        days: int = 7,
        limit: int = 10,
        conversation_id: Optional[str] = None,
    ) -> List[ContextSummary]:
        """Get recent summaries within the last N days for a conversation."""
        conn = self._connect()
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - (days * 24 * 60 * 60 * 1000)

        if conversation_id is not None:
            cursor = conn.execute(
                """
                SELECT id, summary_type, period_start, period_end, summary_text,
                       message_count, created_at
                FROM context_summaries
                WHERE conversation_id = ? AND summary_type = ? AND period_end > ?
                ORDER BY period_end DESC
                LIMIT ?
                """,
                (conversation_id, summary_type, cutoff_ms, limit)
            )
        else:
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

    def get_summaries_as_text(self, days: int = 7, conversation_id: Optional[str] = None) -> str:
        """Get summaries formatted as text for LLM context."""
        summaries = self.get_recent_summaries(days=days, conversation_id=conversation_id)
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
        older_than_ms: int = None,
        limit: int = 200,
        exclude_recent: int = 0,
        conversation_id: Optional[str] = None,
    ) -> List[Message]:
        """
        Get old non-archived messages that should be summarized.

        Args:
            older_than_ms: Only get messages older than this (optional)
            limit: Maximum messages to return
            exclude_recent: Always exclude the N most recent messages (for context window)
            conversation_id: Filter by conversation ID (None = all conversations)
        """
        conn = self._connect()

        # Build conversation filter
        convo_filter = ""
        convo_params = []
        if conversation_id is not None:
            convo_filter = " AND conversation_id = ?"
            convo_params = [conversation_id]

        if exclude_recent > 0:
            # Get messages excluding the most recent N (preserve context window)
            cursor = conn.execute(
                f"""
                SELECT id, message_id, direction, channel, content_type,
                       content_text, conversation_id, reply_to_id, timestamp, created_at, archived
                FROM messages
                WHERE content_type = 'text' AND archived = 0{convo_filter}
                  AND id NOT IN (
                      SELECT id FROM messages
                      WHERE content_type = 'text' AND archived = 0{convo_filter}
                      ORDER BY timestamp DESC
                      LIMIT ?
                  )
                ORDER BY timestamp ASC
                LIMIT ?
                """,
                convo_params + convo_params + [exclude_recent, limit]
            )
        else:
            # Original behavior: filter by timestamp
            cursor = conn.execute(
                f"""
                SELECT id, message_id, direction, channel, content_type,
                       content_text, conversation_id, reply_to_id, timestamp, created_at, archived
                FROM messages
                WHERE content_type = 'text' AND timestamp < ? AND archived = 0{convo_filter}
                ORDER BY timestamp ASC
                LIMIT ?
                """,
                [older_than_ms or 0] + convo_params + [limit]
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

    def archive_messages_before(self, before_ms: int, conversation_id: Optional[str] = None) -> int:
        """Archive (soft-delete) messages older than timestamp."""
        conn = self._connect()

        if conversation_id is not None:
            cursor = conn.execute(
                "UPDATE messages SET archived = 1 WHERE timestamp < ? AND conversation_id = ? AND archived = 0",
                (before_ms, conversation_id)
            )
        else:
            cursor = conn.execute(
                "UPDATE messages SET archived = 1 WHERE timestamp < ? AND archived = 0",
                (before_ms,)
            )
        conn.commit()

        archived = cursor.rowcount
        if archived > 0:
            logger.info("Archived %d messages before %d", archived, before_ms)
        return archived

    def delete_messages_before(self, before_ms: int, conversation_id: Optional[str] = None) -> int:
        """Hard delete messages older than timestamp (use archive_messages_before for soft delete)."""
        conn = self._connect()

        # First, clear reply_to_id references to messages we're about to delete
        # to avoid FOREIGN KEY constraint failures
        if conversation_id is not None:
            conn.execute(
                """
                UPDATE messages SET reply_to_id = NULL
                WHERE reply_to_id IN (
                    SELECT message_id FROM messages WHERE timestamp < ? AND conversation_id = ?
                )
                """,
                (before_ms, conversation_id)
            )
            cursor = conn.execute(
                "DELETE FROM messages WHERE timestamp < ? AND conversation_id = ?",
                (before_ms, conversation_id)
            )
        else:
            conn.execute(
                """
                UPDATE messages SET reply_to_id = NULL
                WHERE reply_to_id IN (
                    SELECT message_id FROM messages WHERE timestamp < ?
                )
                """,
                (before_ms,)
            )
            cursor = conn.execute(
                "DELETE FROM messages WHERE timestamp < ?",
                (before_ms,)
            )
        conn.commit()

        deleted = cursor.rowcount
        if deleted > 0:
            logger.info("Deleted %d messages before %d", deleted, before_ms)
        return deleted

    # --- Knowledge Operations (RAG) ---

    def store_knowledge_chunk(
        self,
        source: str,
        title: str,
        content: str,
        chunk_index: int = 0,
        metadata_json: Optional[str] = None,
    ) -> int:
        """Store a knowledge chunk for RAG retrieval."""
        conn = self._connect()
        now_ms = int(time.time() * 1000)

        cursor = conn.execute(
            """
            INSERT OR REPLACE INTO knowledge_chunks (
                source, title, content, chunk_index, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (source, title, content, chunk_index, metadata_json, now_ms)
        )
        conn.commit()

        logger.debug("Stored knowledge chunk: %s [%d]", source, chunk_index)
        return cursor.lastrowid or 0

    def search_knowledge(
        self,
        query: str,
        limit: int = 5,
    ) -> List[KnowledgeChunk]:
        """
        Search knowledge base using FTS5 full-text search.

        Args:
            query: Search query (plain text, will be sanitized)
            limit: Maximum number of results

        Returns:
            List of matching KnowledgeChunk objects, ranked by relevance
        """
        conn = self._connect()

        # Sanitize query for FTS5: extract words and wrap in quotes
        # This prevents FTS5 special syntax from breaking the query
        import re
        words = re.findall(r'\w+', query)
        if not words:
            return []

        # Join words with OR, each word quoted to treat as literal
        fts_query = " OR ".join(f'"{word}"' for word in words[:20])  # Limit to 20 words

        try:
            # Use FTS5 MATCH for full-text search
            cursor = conn.execute(
                """
                SELECT k.id, k.source, k.title, k.content, k.chunk_index, k.created_at,
                       bm25(knowledge_fts) as rank
                FROM knowledge_chunks k
                JOIN knowledge_fts f ON k.id = f.rowid
                WHERE knowledge_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, limit)
            )
        except sqlite3.OperationalError as e:
            logger.warning("FTS5 search failed: %s (query: %s)", e, fts_query[:100])
            return []

        return [
            KnowledgeChunk(
                id=row["id"],
                source=row["source"],
                title=row["title"],
                content=row["content"],
                chunk_index=row["chunk_index"],
                created_at=row["created_at"],
            )
            for row in cursor.fetchall()
        ]

    def get_knowledge_by_source(self, source: str) -> List[KnowledgeChunk]:
        """Get all chunks from a specific source."""
        conn = self._connect()

        cursor = conn.execute(
            """
            SELECT id, source, title, content, chunk_index, created_at
            FROM knowledge_chunks
            WHERE source = ?
            ORDER BY chunk_index
            """,
            (source,)
        )

        return [
            KnowledgeChunk(
                id=row["id"],
                source=row["source"],
                title=row["title"],
                content=row["content"],
                chunk_index=row["chunk_index"],
                created_at=row["created_at"],
            )
            for row in cursor.fetchall()
        ]

    def delete_knowledge_source(self, source: str) -> int:
        """Delete all chunks from a source."""
        conn = self._connect()

        cursor = conn.execute(
            "DELETE FROM knowledge_chunks WHERE source = ?",
            (source,)
        )
        conn.commit()

        deleted = cursor.rowcount
        if deleted > 0:
            logger.info("Deleted %d chunks from source: %s", deleted, source)
        return deleted

    def get_knowledge_sources(self) -> List[Dict[str, Any]]:
        """Get list of all knowledge sources with chunk counts."""
        conn = self._connect()

        cursor = conn.execute(
            """
            SELECT source, COUNT(*) as chunk_count, MAX(created_at) as last_updated
            FROM knowledge_chunks
            GROUP BY source
            ORDER BY source
            """
        )

        return [
            {"source": row["source"], "chunk_count": row["chunk_count"], "last_updated": row["last_updated"]}
            for row in cursor.fetchall()
        ]

    def get_knowledge_as_context(self, query: str, max_tokens: int = 1000) -> str:
        """
        Search knowledge and format as context for LLM.

        Args:
            query: Search query
            max_tokens: Approximate max tokens (chars / 4)

        Returns:
            Formatted context string
        """
        chunks = self.search_knowledge(query, limit=10)
        if not chunks:
            return ""

        lines = ["Relevant knowledge:"]
        total_chars = 0
        max_chars = max_tokens * 4  # Rough estimate

        for chunk in chunks:
            chunk_text = f"\n[{chunk.title}]\n{chunk.content}"
            if total_chars + len(chunk_text) > max_chars:
                break
            lines.append(chunk_text)
            total_chars += len(chunk_text)

        return "\n".join(lines)

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
        JOI_MEMORY_KEY_FILE: Path to encryption key file (default: /etc/joi/memory.key)

    The encryption key is loaded from the key file, not from environment variables,
    to avoid key leakage in logs or process listings.
    """
    db_path = os.getenv("JOI_MEMORY_DB", "/var/lib/joi/memory.db")

    # Key is loaded from file inside MemoryStore.__init__
    return MemoryStore(db_path)
