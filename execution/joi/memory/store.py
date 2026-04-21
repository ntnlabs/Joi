"""
Joi Memory Store - SQLite/SQLCipher database for conversation and state.

See memory-store-schema.md for full schema documentation.
"""

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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

# Common stopwords filtered out from FTS queries
_STOPWORDS = {
    # English
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "they", "it",
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "was", "are", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "that", "this", "what", "which", "who",
    # Slovak function words (no semantic value in FTS)
    "je", "sa", "to", "ta", "ten", "tá", "tú", "tej", "tých",
    "nie", "aj", "ani", "ale", "ak", "že", "čo", "kto", "kde", "keď",
    "som", "si", "sú", "sme", "ste", "bol", "bola", "bolo", "boli",
    "ho", "mu", "ju", "ich", "im", "tu", "tam", "tak", "už", "len",
    "pri", "pre", "bez", "nad", "pod", "cez", "ako", "aby",
}

DEFAULT_KEY_FILE = "/etc/joi/memory.key"


def load_encryption_key(key_file: Optional[str] = None) -> Optional[str]:
    """
    Load encryption key from file.

    Uses JOI_MEMORY_KEY_FILE if set, otherwise falls back to /etc/joi/memory.key.
    Returns None if key file not found — MemoryStore enforces via JOI_REQUIRE_ENCRYPTED_DB.

    Generate the key file with:
        sudo /opt/Joi/execution/joi/scripts/generate-memory-key.sh
    """
    key_file_path = key_file or os.getenv("JOI_MEMORY_KEY_FILE", DEFAULT_KEY_FILE)

    key_path = Path(key_file_path)

    try:
        if not key_path.exists():
            logger.warning(
                "Key file %s not found — generate with: "
                "sudo /opt/Joi/execution/joi/scripts/generate-memory-key.sh",
                key_path,
            )
            return None

        # Check permissions — refuse to use key if too permissive (like ssh/munge)
        mode = key_path.stat().st_mode & 0o777
        if mode > 0o600:
            logger.critical(
                "Key file %s has insecure permissions %o (must be 600 or stricter) — "
                "fix with: chmod 600 %s",
                key_path, mode, key_path,
                extra={"action": "startup_fatal"},
            )
            os._exit(78)

        key = key_path.read_text().strip()
        if not key:
            logger.warning("Key file %s is empty", key_path)
            return None
        if len(key) < 32:
            logger.warning("Encryption key is shorter than recommended (32+ chars)")
        if not re.fullmatch(r"[0-9a-fA-F]+", key):
            logger.critical(
                "Key file %s does not contain a valid hex string — "
                "regenerate with: sudo /opt/Joi/execution/joi/scripts/generate-memory-key.sh",
                key_path,
                extra={"action": "startup_fatal"},
            )
            os._exit(78)
        return key

    except PermissionError:
        logger.warning("Cannot access key file %s (permission denied)", key_path)
        return None
    except Exception as e:
        logger.error("Failed to read encryption key", extra={"path": str(key_path), "error": str(e)})
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
    conversation_id: str  # Which conversation this fact belongs to
    category: str  # 'personal', 'preference', 'relationship', etc.
    key: str
    value: str
    confidence: float
    source: str  # 'stated', 'inferred', 'configured'
    learned_at: int
    last_verified_at: Optional[int]
    important: bool = False  # Core facts always included in context
    expires_at: Optional[int] = None  # ms epoch; None = permanent
    detected_at: Optional[int] = None  # ms epoch of source message; None = unknown


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
    key_points_json: Optional[str] = None


@dataclass
class KnowledgeChunk:
    """A chunk of knowledge from a document."""
    id: int
    source: str  # document path or identifier
    title: str
    content: str
    chunk_index: int
    created_at: int
    scope: str = ""  # access scope (conversation_id or empty for global)


# Schema version for migrations
SCHEMA_VERSION = 14

# SQL for creating tables
SCHEMA_SQL = f"""
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
    archived INTEGER NOT NULL DEFAULT 0
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
    ('schema_version', '{SCHEMA_VERSION}'),
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
    important INTEGER NOT NULL DEFAULT 0,
    learned_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000),
    last_referenced_at INTEGER,
    last_verified_at INTEGER,
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000),
    expires_at INTEGER,
    detected_at INTEGER,
    UNIQUE(conversation_id, category, key, active)
);

CREATE INDEX IF NOT EXISTS idx_facts_category ON user_facts(category, active);
CREATE INDEX IF NOT EXISTS idx_facts_active ON user_facts(active, confidence DESC);
CREATE INDEX IF NOT EXISTS idx_facts_conversation ON user_facts(conversation_id, active);
CREATE INDEX IF NOT EXISTS idx_facts_important ON user_facts(important, active);
CREATE INDEX IF NOT EXISTS idx_facts_expires ON user_facts(expires_at) WHERE expires_at IS NOT NULL;

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
    scope TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT,
    embedding BLOB,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000),
    UNIQUE(scope, source, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_source ON knowledge_chunks(source);
CREATE INDEX IF NOT EXISTS idx_knowledge_scope ON knowledge_chunks(scope);

-- Wind state table (per-conversation proactive messaging state)
CREATE TABLE IF NOT EXISTS wind_state (
    conversation_id TEXT PRIMARY KEY,
    last_user_interaction_at TEXT,
    last_outbound_at TEXT,
    last_proactive_sent_at TEXT,
    last_impulse_check_at TEXT,
    proactive_sent_today INTEGER DEFAULT 0,
    proactive_day_bucket TEXT,
    unanswered_proactive_count INTEGER DEFAULT 0,
    wind_snooze_until TEXT,
    updated_at TEXT NOT NULL,
    -- WindMood columns
    threshold_offset REAL DEFAULT NULL,
    accumulated_impulse REAL DEFAULT 0.0,
    -- Engagement tracking columns (Phase 4a)
    engagement_score REAL DEFAULT 0.5,
    total_proactives_sent INTEGER DEFAULT 0,
    total_engaged INTEGER DEFAULT 0,
    total_ignored INTEGER DEFAULT 0,
    total_deflected INTEGER DEFAULT 0,
    last_engaged_at TEXT,
    last_deflected_at TEXT,
    -- Hot conversation suppression (Phase 5)
    convo_gap_ema_seconds REAL DEFAULT NULL,
    -- Tension mining pointer (epoch ms of newest mined message)
    last_tension_mined_message_ts INTEGER DEFAULT NULL,
    -- Rolling 24h fire timestamps for sliding window cap (v12)
    proactive_fire_times_json TEXT DEFAULT NULL,
    -- Phase 4d: Named emotional state (Plutchik)
    mood_state TEXT DEFAULT 'neutral',
    mood_intensity REAL DEFAULT 0.5,
    mood_updated_at TEXT DEFAULT NULL,
    -- User mood (per-message classification)
    user_mood_state TEXT DEFAULT 'neutral',
    user_mood_intensity REAL DEFAULT 0.5,
    user_mood_updated_at TEXT DEFAULT NULL,
    -- Adaptive quiet start learned from inbound message timestamps
    learned_quiet_start_minutes INTEGER DEFAULT NULL,
    -- End-of-day task tracking (v15)
    last_daily_tasks_at TEXT DEFAULT NULL
);

-- Pending topics table (topic queue for Wind)
CREATE TABLE IF NOT EXISTS pending_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    topic_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT,
    priority INTEGER DEFAULT 50,
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL,
    expires_at TEXT,
    due_at TEXT,
    mentioned_at TEXT,
    novelty_key TEXT,
    source_event_id TEXT,
    -- Engagement tracking columns (Phase 4a)
    outcome TEXT DEFAULT NULL,
    outcome_at TEXT DEFAULT NULL,
    retry_count INTEGER DEFAULT 0,
    last_retry_at TEXT DEFAULT NULL,
    sent_message_id TEXT DEFAULT NULL,
    -- Outcome curiosity emotional context (Phase 4c)
    emotional_context TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_topics_conv_status
    ON pending_topics(conversation_id, status);
CREATE INDEX IF NOT EXISTS idx_pending_topics_due
    ON pending_topics(due_at, status);

-- Topic feedback table (per-conversation topic family preferences)
CREATE TABLE IF NOT EXISTS topic_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    topic_family TEXT NOT NULL,
    rejection_weight REAL DEFAULT 0.0,
    interest_weight REAL DEFAULT 0.0,
    engagement_count INTEGER DEFAULT 0,
    ignore_count INTEGER DEFAULT 0,
    deflection_count INTEGER DEFAULT 0,
    last_positive_at TEXT,
    last_negative_at TEXT,
    cooldown_until TEXT,
    undertaker INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE(conversation_id, topic_family)
);
CREATE INDEX IF NOT EXISTS idx_topic_feedback_conv
    ON topic_feedback(conversation_id);

-- Wind decision log table (observability)
CREATE TABLE IF NOT EXISTS wind_decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    eligible INTEGER,
    gate_result TEXT,
    impulse_score REAL,
    threshold REAL,
    factor_breakdown TEXT,
    selected_topic_id INTEGER,
    decision TEXT NOT NULL,
    skip_reason TEXT,
    draft_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_wind_log_conv_ts
    ON wind_decision_log(conversation_id, timestamp);

-- Reminders table (standalone, not part of Wind)
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    title TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    recurrence TEXT,
    created_at TEXT NOT NULL,
    fired_at TEXT,
    expires_at TEXT,
    snooze_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminders_due
    ON reminders(due_at, status);
CREATE INDEX IF NOT EXISTS idx_reminders_conv
    ON reminders(conversation_id, status);

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

-- FTS5 for user_facts (indexes key and value)
CREATE VIRTUAL TABLE IF NOT EXISTS user_facts_fts USING fts5(
    key,
    value,
    content=user_facts,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON user_facts BEGIN
    INSERT INTO user_facts_fts(rowid, key, value) VALUES (new.id, new.key, new.value);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON user_facts BEGIN
    INSERT INTO user_facts_fts(user_facts_fts, rowid, key, value) VALUES('delete', old.id, old.key, old.value);
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON user_facts BEGIN
    INSERT INTO user_facts_fts(user_facts_fts, rowid, key, value) VALUES('delete', old.id, old.key, old.value);
    INSERT INTO user_facts_fts(rowid, key, value) VALUES (new.id, new.key, new.value);
END;

-- FTS5 for context_summaries (indexes summary_text)
CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts USING fts5(
    summary_text,
    content=context_summaries,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS summaries_ai AFTER INSERT ON context_summaries BEGIN
    INSERT INTO summaries_fts(rowid, summary_text) VALUES (new.id, new.summary_text);
END;

CREATE TRIGGER IF NOT EXISTS summaries_ad AFTER DELETE ON context_summaries BEGIN
    INSERT INTO summaries_fts(summaries_fts, rowid, summary_text) VALUES('delete', old.id, old.summary_text);
END;

CREATE TRIGGER IF NOT EXISTS summaries_au AFTER UPDATE ON context_summaries BEGIN
    INSERT INTO summaries_fts(summaries_fts, rowid, summary_text) VALUES('delete', old.id, old.summary_text);
    INSERT INTO summaries_fts(rowid, summary_text) VALUES (new.id, new.summary_text);
END;

-- Notes table (user-created named notes with optional soft reminder)
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    embedding BLOB,
    remind_at TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_notes_conv ON notes (conversation_id, archived);
CREATE INDEX IF NOT EXISTS idx_notes_remind ON notes (remind_at)
    WHERE remind_at IS NOT NULL AND archived = 0;

-- FTS5 for notes (indexes title and content)
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    title,
    content,
    content=notes,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, title, content) VALUES (new.id, new.title, new.content);
END;

CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, content) VALUES('delete', old.id, old.title, old.content);
END;

CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, content) VALUES('delete', old.id, old.title, old.content);
    INSERT INTO notes_fts(rowid, title, content) VALUES (new.id, new.title, new.content);
END;

-- Tasks table (named checkable lists, per-conversation)
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT    NOT NULL,
    list_name       TEXT    NOT NULL,
    item_text       TEXT    NOT NULL,
    done            INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    done_at         INTEGER,
    archived        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tasks_conversation_list ON tasks (conversation_id, list_name, archived);

-- Wind quiet samples table (rolling buffer of daily sign-off times for adaptive quiet hours)
CREATE TABLE IF NOT EXISTS wind_quiet_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    day_date TEXT NOT NULL,          -- 'YYYY-MM-DD' local date
    last_inbound_minutes INTEGER NOT NULL,  -- minutes since midnight (local tz)
    recorded_at TEXT NOT NULL,
    UNIQUE(conversation_id, day_date)  -- one row per conversation per day
);
"""


def _fact_temporal_suffix(fact: "UserFact", now_ms: int) -> str:
    """Return a parenthetical temporal annotation for a fact, or empty string if none."""
    parts = []
    if fact.detected_at:
        age_ms = now_ms - fact.detected_at
        age_h = age_ms / 3_600_000
        if age_h < 1:
            parts.append("mentioned just now")
        elif age_h < 24:
            parts.append(f"mentioned {int(age_h)}h ago")
        elif age_h < 48:
            parts.append("mentioned yesterday")
        else:
            parts.append(f"mentioned {int(age_h / 24)} days ago")
    if fact.expires_at:
        ttl_ms = fact.expires_at - now_ms
        if ttl_ms <= 0:
            parts.append("expired")
        elif ttl_ms < 2 * 3_600_000:
            parts.append(f"expires in {max(1, int(ttl_ms / 60_000))}m")
        elif ttl_ms < 48 * 3_600_000:
            parts.append(f"expires in {int(ttl_ms / 3_600_000)}h")
        else:
            parts.append(f"expires in {int(ttl_ms / 86_400_000)}d")
    return f" ({', '.join(parts)})" if parts else ""


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
        self._all_connections: list = []
        self._all_connections_lock = threading.Lock()

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

        # Enforce encryption requirement if configured (default: required)
        require_encrypted = os.getenv("JOI_REQUIRE_ENCRYPTED_DB", "1") == "1"
        if require_encrypted and not self._encrypted:
            if self._encryption_key and not SQLCIPHER_AVAILABLE:
                logger.critical(
                    "Encryption key found but sqlcipher3 not installed — "
                    "install: pip install sqlcipher3-binary",
                    extra={"action": "startup_fatal"},
                )
            else:
                logger.critical(
                    "Encrypted database required but no key available. "
                    "Set JOI_MEMORY_KEY_FILE or place key at %s. "
                    "Generate: sudo /opt/Joi/execution/joi/scripts/generate-memory-key.sh",
                    DEFAULT_KEY_FILE,
                    extra={"action": "startup_fatal"},
                )
            os._exit(78)

        # Ensure directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        # Check if this is a fresh database
        is_new_db = not Path(db_path).exists()

        # Initialize schema
        self._init_schema()

        if is_new_db:
            logger.info("Created new memory database", extra={
                "path": str(db_path),
                "encrypted": self._encrypted,
                "action": "db_create"
            })

        logger.info("Memory store initialized", extra={
            "path": str(db_path),
            "encrypted": self._encrypted,
            "action": "init"
        })

    def _connect(self) -> sqlite3.Connection:
        """Get or create a connection for the current thread."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row

            # SQLCipher encryption - must be set before any other operations
            if self._encrypted and self._encryption_key:
                # PRAGMA doesn't support parameterized queries
                # Use key as passphrase (matches migration script's ATTACH ... KEY 'passphrase')
                # Escape single quotes to prevent SQL injection
                escaped_key = self._encryption_key.replace("'", "''")
                conn.execute(f"PRAGMA key = '{escaped_key}';")

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
            with self._all_connections_lock:
                self._all_connections.append(conn)
        return self._local.conn

    def rollback(self) -> None:
        """Roll back the current thread's connection transaction."""
        try:
            self._connect().rollback()
        except Exception:
            pass

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

        # Check user_facts table for conversation_id and important
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_facts'")
        if cursor.fetchone():
            cursor = conn.execute("PRAGMA table_info(user_facts)")
            fact_columns = [row[1] for row in cursor.fetchall()]
            if "conversation_id" not in fact_columns:
                logger.info("Migration: Adding 'conversation_id' column to user_facts table")
                # SQLite doesn't support ALTER CONSTRAINT, so rebuild table with new constraint
                conn.execute("""
                    CREATE TABLE user_facts_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conversation_id TEXT NOT NULL DEFAULT '',
                        category TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        confidence REAL NOT NULL DEFAULT 0.8,
                        source TEXT NOT NULL,
                        source_message_id TEXT,
                        active INTEGER NOT NULL DEFAULT 1,
                        important INTEGER NOT NULL DEFAULT 0,
                        learned_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000),
                        last_referenced_at INTEGER,
                        last_verified_at INTEGER,
                        updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000),
                        UNIQUE(conversation_id, category, key, active)
                    )
                """)
                # Copy existing data (conversation_id defaults to '' for backward compatibility)
                conn.execute("""
                    INSERT INTO user_facts_new (
                        id, conversation_id, category, key, value, confidence, source,
                        source_message_id, active, learned_at, last_referenced_at,
                        last_verified_at, updated_at
                    )
                    SELECT
                        id, '' as conversation_id, category, key, value, confidence, source,
                        source_message_id, active, learned_at, last_referenced_at,
                        last_verified_at, updated_at
                    FROM user_facts
                """)
                conn.execute("DROP TABLE user_facts")
                conn.execute("ALTER TABLE user_facts_new RENAME TO user_facts")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_conversation ON user_facts(conversation_id, active)")
                conn.commit()
            if "important" not in fact_columns:
                logger.info("Migration: Adding 'important' column to user_facts table")
                conn.execute("ALTER TABLE user_facts ADD COLUMN important INTEGER NOT NULL DEFAULT 0")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_important ON user_facts(important, active)")
                conn.commit()

            # Check if unique constraint needs fixing (column exists but wrong constraint)
            # Old DBs may have UNIQUE(category, key, active) instead of UNIQUE(conversation_id, category, key, active)
            if "conversation_id" in fact_columns:
                cursor = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_facts'"
                )
                row = cursor.fetchone()
                if row and row[0] and "UNIQUE(category, key, active)" in row[0]:
                    logger.info("Migration: Fixing user_facts unique constraint to include conversation_id")
                    # Rebuild table with correct constraint
                    conn.execute("""
                        CREATE TABLE user_facts_new (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            conversation_id TEXT NOT NULL DEFAULT '',
                            category TEXT NOT NULL,
                            key TEXT NOT NULL,
                            value TEXT NOT NULL,
                            confidence REAL NOT NULL DEFAULT 0.8,
                            source TEXT NOT NULL,
                            source_message_id TEXT,
                            active INTEGER NOT NULL DEFAULT 1,
                            important INTEGER NOT NULL DEFAULT 0,
                            learned_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000),
                            last_referenced_at INTEGER,
                            last_verified_at INTEGER,
                            updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000),
                            UNIQUE(conversation_id, category, key, active)
                        )
                    """)
                    conn.execute("""
                        INSERT INTO user_facts_new (
                            id, conversation_id, category, key, value, confidence, source,
                            source_message_id, active, important, learned_at, last_referenced_at,
                            last_verified_at, updated_at
                        )
                        SELECT
                            id, conversation_id, category, key, value, confidence, source,
                            source_message_id, active, important, learned_at, last_referenced_at,
                            last_verified_at, updated_at
                        FROM user_facts
                    """)
                    conn.execute("DROP TABLE user_facts")
                    conn.execute("ALTER TABLE user_facts_new RENAME TO user_facts")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_conversation ON user_facts(conversation_id, active)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_important ON user_facts(important, active)")
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

        # Check knowledge_chunks table for scope column
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_chunks'")
        if cursor.fetchone():
            cursor = conn.execute("PRAGMA table_info(knowledge_chunks)")
            knowledge_columns = [row[1] for row in cursor.fetchall()]
            if "scope" not in knowledge_columns:
                logger.info("Migration: Adding 'scope' column to knowledge_chunks table")
                conn.execute("ALTER TABLE knowledge_chunks ADD COLUMN scope TEXT NOT NULL DEFAULT ''")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_scope ON knowledge_chunks(scope)")
                conn.commit()
            if "embedding" not in knowledge_columns:
                logger.info("Migration: Adding 'embedding' column to knowledge_chunks table")
                conn.execute("ALTER TABLE knowledge_chunks ADD COLUMN embedding BLOB")
                conn.commit()

        # Check pending_topics table for due_at column (v6)
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pending_topics'")
        if cursor.fetchone():
            cursor = conn.execute("PRAGMA table_info(pending_topics)")
            topic_columns = {row[1] for row in cursor.fetchall()}
            if "due_at" not in topic_columns:
                logger.info("Migration: Adding 'due_at' column to pending_topics table")
                conn.execute("ALTER TABLE pending_topics ADD COLUMN due_at TEXT")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_topics_due ON pending_topics(due_at, status)")
                conn.commit()

        # Check wind_state table for WindMood columns (v7)
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='wind_state'")
        if cursor.fetchone():
            cursor = conn.execute("PRAGMA table_info(wind_state)")
            wind_columns = {row[1] for row in cursor.fetchall()}
            if "threshold_offset" not in wind_columns:
                logger.info("Migration: Adding 'threshold_offset' column to wind_state table")
                conn.execute("ALTER TABLE wind_state ADD COLUMN threshold_offset REAL DEFAULT NULL")
                conn.commit()
            if "accumulated_impulse" not in wind_columns:
                logger.info("Migration: Adding 'accumulated_impulse' column to wind_state table")
                conn.execute("ALTER TABLE wind_state ADD COLUMN accumulated_impulse REAL DEFAULT 0.0")
                conn.commit()
            # Phase 4a engagement tracking columns (v8)
            if "engagement_score" not in wind_columns:
                logger.info("Migration: Adding engagement tracking columns to wind_state table")
                conn.execute("ALTER TABLE wind_state ADD COLUMN engagement_score REAL DEFAULT 0.5")
                conn.execute("ALTER TABLE wind_state ADD COLUMN total_proactives_sent INTEGER DEFAULT 0")
                conn.execute("ALTER TABLE wind_state ADD COLUMN total_engaged INTEGER DEFAULT 0")
                conn.execute("ALTER TABLE wind_state ADD COLUMN total_ignored INTEGER DEFAULT 0")
                conn.execute("ALTER TABLE wind_state ADD COLUMN total_deflected INTEGER DEFAULT 0")
                conn.execute("ALTER TABLE wind_state ADD COLUMN last_engaged_at TEXT")
                conn.execute("ALTER TABLE wind_state ADD COLUMN last_deflected_at TEXT")
                conn.commit()
            # Phase 5: Hot conversation gap EMA
            if "convo_gap_ema_seconds" not in wind_columns:
                logger.info("Migration: Adding 'convo_gap_ema_seconds' column to wind_state table")
                conn.execute("ALTER TABLE wind_state ADD COLUMN convo_gap_ema_seconds REAL DEFAULT NULL")
                conn.commit()
            # Tension mining pointer
            if "last_tension_mined_message_ts" not in wind_columns:
                logger.info("Migration: Adding 'last_tension_mined_message_ts' column to wind_state table")
                conn.execute("ALTER TABLE wind_state ADD COLUMN last_tension_mined_message_ts INTEGER DEFAULT NULL")
                conn.commit()
            # Phase 4d: Mood system
            if "mood_state" not in wind_columns:
                logger.info("Migration: Adding mood columns to wind_state table")
                conn.execute("ALTER TABLE wind_state ADD COLUMN mood_state TEXT DEFAULT 'neutral'")
                conn.execute("ALTER TABLE wind_state ADD COLUMN mood_intensity REAL DEFAULT 0.5")
                conn.execute("ALTER TABLE wind_state ADD COLUMN mood_updated_at TEXT DEFAULT NULL")
                conn.commit()

        # Check pending_topics table for engagement columns (v8)
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pending_topics'")
        if cursor.fetchone():
            cursor = conn.execute("PRAGMA table_info(pending_topics)")
            topic_columns = {row[1] for row in cursor.fetchall()}
            if "outcome" not in topic_columns:
                logger.info("Migration: Adding engagement columns to pending_topics table")
                conn.execute("ALTER TABLE pending_topics ADD COLUMN outcome TEXT DEFAULT NULL")
                conn.execute("ALTER TABLE pending_topics ADD COLUMN outcome_at TEXT DEFAULT NULL")
                conn.execute("ALTER TABLE pending_topics ADD COLUMN retry_count INTEGER DEFAULT 0")
                conn.execute("ALTER TABLE pending_topics ADD COLUMN last_retry_at TEXT DEFAULT NULL")
                conn.execute("ALTER TABLE pending_topics ADD COLUMN sent_message_id TEXT DEFAULT NULL")
                conn.commit()
            if "emotional_context" not in topic_columns:
                logger.info("Migration: Adding emotional_context column to pending_topics table")
                conn.execute("ALTER TABLE pending_topics ADD COLUMN emotional_context TEXT DEFAULT NULL")
                conn.commit()

        # Phase 4b: Check topic_feedback for undertaker column
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='topic_feedback'")
        if cursor.fetchone():
            cursor = conn.execute("PRAGMA table_info(topic_feedback)")
            feedback_columns = {row[1] for row in cursor.fetchall()}
            if "undertaker" not in feedback_columns:
                logger.info("Migration: Adding 'undertaker' column to topic_feedback table")
                conn.execute("ALTER TABLE topic_feedback ADD COLUMN undertaker INTEGER DEFAULT 0")
                conn.commit()

        # Migration v9: Remove FK constraint on messages.reply_to_id
        # Signal quote IDs are timestamps; they never match stored UUIDs, causing
        # INSERT OR IGNORE to silently drop rows. reply_to_id is advisory only.
        cursor = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'"
        )
        row = cursor.fetchone()
        if row and row[0] and "FOREIGN KEY" in row[0]:
            logger.info("Migration v9: Removing FK constraint from messages.reply_to_id")
            conn.execute("""
                CREATE TABLE messages_new (
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
                    archived INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                INSERT INTO messages_new SELECT
                    id, message_id, direction, channel, content_type, content_text,
                    content_media_path, conversation_id, reply_to_id, sender_id, sender_name,
                    timestamp, created_at, processed, escalated, archived
                FROM messages
            """)
            conn.execute("DROP TABLE messages")
            conn.execute("ALTER TABLE messages_new RENAME TO messages")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_direction ON messages(direction, timestamp DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, timestamp DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_archived ON messages(archived, timestamp DESC)")
            conn.commit()

        # Migration v11: Add expires_at and detected_at to user_facts
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_facts'")
        if cursor.fetchone():
            cursor = conn.execute("PRAGMA table_info(user_facts)")
            fact_cols_v11 = {row[1] for row in cursor.fetchall()}
            if "expires_at" not in fact_cols_v11:
                logger.info("Migration v11: Adding 'expires_at' and 'detected_at' columns to user_facts")
                conn.execute("ALTER TABLE user_facts ADD COLUMN expires_at INTEGER")
                conn.execute("ALTER TABLE user_facts ADD COLUMN detected_at INTEGER")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_facts_expires ON user_facts(expires_at)"
                    " WHERE expires_at IS NOT NULL"
                )
                conn.commit()

        # Migration v12: rolling 24h fire timestamps for sliding window cap
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='wind_state'")
        if cursor.fetchone():
            cols = {row[1] for row in conn.execute("PRAGMA table_info(wind_state)")}
            if "proactive_fire_times_json" not in cols:
                logger.info("Migration v12: Adding 'proactive_fire_times_json' column to wind_state table")
                conn.execute(
                    "ALTER TABLE wind_state ADD COLUMN proactive_fire_times_json TEXT DEFAULT NULL"
                )
                conn.commit()

        # Migration v13: user mood tracking columns
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='wind_state'")
        if cursor.fetchone():
            cols = {row[1] for row in conn.execute("PRAGMA table_info(wind_state)")}
            if "user_mood_state" not in cols:
                logger.info("Migration v13: Adding user mood columns to wind_state table")
                conn.execute("ALTER TABLE wind_state ADD COLUMN user_mood_state TEXT DEFAULT 'neutral'")
                conn.execute("ALTER TABLE wind_state ADD COLUMN user_mood_intensity REAL DEFAULT 0.5")
                conn.execute("ALTER TABLE wind_state ADD COLUMN user_mood_updated_at TEXT DEFAULT NULL")
                conn.commit()

        # Migration v13 (cont.): notes table is created via SCHEMA_SQL (CREATE TABLE IF NOT EXISTS)
        # No ALTER TABLE needed — new table always created on startup if missing.

        # Migration v14: adaptive quiet start
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='wind_state'")
        if cursor.fetchone():
            cols = {row[1] for row in conn.execute("PRAGMA table_info(wind_state)")}
            if "learned_quiet_start_minutes" not in cols:
                logger.info("Migration v14: Adding 'learned_quiet_start_minutes' column to wind_state table")
                conn.execute(
                    "ALTER TABLE wind_state ADD COLUMN learned_quiet_start_minutes INTEGER DEFAULT NULL"
                )
                conn.commit()

        # Migration v14: tasks table is created via SCHEMA_SQL (CREATE TABLE IF NOT EXISTS)
        # No ALTER TABLE needed — new table always created on startup if missing.

        # Migration v15: per-conversation end-of-day task tracking
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='wind_state'")
        if cursor.fetchone():
            cols = {row[1] for row in conn.execute("PRAGMA table_info(wind_state)")}
            if "last_daily_tasks_at" not in cols:
                logger.info("Migration v15: Adding 'last_daily_tasks_at' column to wind_state table")
                conn.execute(
                    "ALTER TABLE wind_state ADD COLUMN last_daily_tasks_at TEXT DEFAULT NULL"
                )
                conn.commit()

        # Migration v16: wake-up procedure tracking
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='wind_state'")
        if cursor.fetchone():
            cols = {row[1] for row in conn.execute("PRAGMA table_info(wind_state)")}
            if "last_wakeup_at" not in cols:
                logger.info("Migration v16: Adding 'last_wakeup_at' column to wind_state table")
                conn.execute(
                    "ALTER TABLE wind_state ADD COLUMN last_wakeup_at TEXT DEFAULT NULL"
                )
                conn.commit()

        # Migration v17: wake-up proactive scheduled send time
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='wind_state'")
        if cursor.fetchone():
            cols = {row[1] for row in conn.execute("PRAGMA table_info(wind_state)")}
            if "wakeup_send_at" not in cols:
                logger.info("Migration v17: Adding 'wakeup_send_at' column to wind_state table")
                conn.execute(
                    "ALTER TABLE wind_state ADD COLUMN wakeup_send_at TEXT DEFAULT NULL"
                )
                conn.commit()

        # Migration v18: wind quiet samples rolling buffer
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='wind_quiet_samples'"
        )
        if not cursor.fetchone():
            logger.info("Migration v18: Creating 'wind_quiet_samples' table")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wind_quiet_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    day_date TEXT NOT NULL,
                    last_inbound_minutes INTEGER NOT NULL,
                    recorded_at TEXT NOT NULL,
                    UNIQUE(conversation_id, day_date)
                )
                """
            )
            conn.commit()

        # Check FTS integrity and rebuild if needed
        self._check_and_repair_fts_indexes(conn)

    def _check_and_repair_fts_indexes(self, conn: sqlite3.Connection) -> None:
        """Check FTS index integrity and repair if out of sync."""
        integrity = self._check_fts_integrity_internal(conn)
        for index_name, status in integrity.items():
            if not status["ok"]:
                if status["fts_count"] == 0 and status["main_count"] > 0:
                    # Empty FTS with data - rebuild
                    logger.info(
                        "FTS index %s is empty but main table has %d rows - rebuilding",
                        index_name, status["main_count"]
                    )
                    self._rebuild_fts_index_internal(conn, index_name)
                else:
                    # Count mismatch - log warning, don't auto-rebuild (might lose data)
                    logger.warning(
                        "FTS index %s out of sync: FTS=%d, main=%d (diff=%d). "
                        "Run rebuild_fts_index('%s') to repair.",
                        index_name, status["fts_count"], status["main_count"],
                        status["difference"], index_name
                    )

    def _check_fts_integrity_internal(self, conn: sqlite3.Connection) -> dict:
        """Internal: Check FTS integrity without acquiring new connection."""
        results = {}

        # Define FTS tables and their corresponding main tables
        fts_configs = [
            ("user_facts_fts", "user_facts", None),
            ("summaries_fts", "context_summaries", None),
            ("knowledge_fts", "knowledge_chunks", None),
            ("notes_fts", "notes", None),
        ]

        for fts_table, main_table, where_clause in fts_configs:
            try:
                # Count FTS rows
                cursor = conn.execute(f"SELECT COUNT(*) FROM {fts_table}")
                fts_count = cursor.fetchone()[0]

                # Count main table rows
                if where_clause:
                    cursor = conn.execute(f"SELECT COUNT(*) FROM {main_table} WHERE {where_clause}")
                else:
                    cursor = conn.execute(f"SELECT COUNT(*) FROM {main_table}")
                main_count = cursor.fetchone()[0]

                difference = abs(fts_count - main_count)
                results[fts_table] = {
                    "ok": difference == 0,
                    "fts_count": fts_count,
                    "main_count": main_count,
                    "difference": difference,
                }
            except sqlite3.OperationalError as e:
                results[fts_table] = {
                    "ok": False,
                    "error": str(e),
                    "fts_count": 0,
                    "main_count": 0,
                    "difference": 0,
                }

        return results

    def _rebuild_fts_index_internal(self, conn: sqlite3.Connection, index_name: str) -> bool:
        """Internal: Rebuild a specific FTS index."""
        try:
            conn.execute(f"INSERT INTO {index_name}({index_name}) VALUES('rebuild')")
            conn.commit()
            logger.info("Rebuilt FTS index", extra={"index": index_name, "action": "fts_rebuild"})
            return True
        except sqlite3.OperationalError as e:
            logger.error("Failed to rebuild FTS index", extra={"index": index_name, "error": str(e)})
            return False

    # --- Note Operations ---

    def add_note(
        self,
        conversation_id: str,
        title: str,
        content: str,
        remind_at: Optional[str] = None,
    ) -> int:
        """Insert a new note. Returns the new note id."""
        now_ms = int(time.time() * 1000)
        conn = self._connect()
        embedding = self._get_embedding(title + "\n\n" + content)
        cursor = conn.execute(
            """
            INSERT INTO notes (conversation_id, title, content, embedding, remind_at, created_at, updated_at, archived)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (conversation_id, title, content, embedding, remind_at, now_ms, now_ms),
        )
        conn.commit()
        note_id = cursor.lastrowid or 0
        return note_id

    def get_note_by_title(self, conversation_id: str, title: str) -> Optional[dict]:
        """
        Find active note by title (case-insensitive partial/substring match).
        Returns a row dict or None.
        """
        conn = self._connect()
        escaped = title.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        cursor = conn.execute(
            """
            SELECT id, conversation_id, title, content, embedding, remind_at, created_at, updated_at, archived
            FROM notes
            WHERE conversation_id = ? AND LOWER(title) LIKE LOWER(?) ESCAPE '\\' AND archived = 0
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (conversation_id, f"%{escaped}%"),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def append_note_content(self, note_id: int, text: str) -> None:
        """Append text to a note's content and recompute embedding."""
        now_ms = int(time.time() * 1000)
        conn = self._connect()
        row = conn.execute(
            "SELECT title, content FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        if not row:
            return
        new_content = row["content"] + ("\n" if row["content"] else "") + text
        embedding = self._get_embedding(row["title"] + "\n\n" + new_content)
        conn.execute(
            "UPDATE notes SET content = ?, embedding = ?, updated_at = ? WHERE id = ?",
            (new_content, embedding, now_ms, note_id),
        )
        conn.commit()
        logger.info("Note appended", extra={"note_id": note_id, "action": "note_append"})

    def replace_note_content(self, note_id: int, new_content: str) -> None:
        """Replace note content entirely and recompute embedding."""
        now_ms = int(time.time() * 1000)
        conn = self._connect()
        row = conn.execute(
            "SELECT title FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        if not row:
            return
        embedding = self._get_embedding(row["title"] + "\n\n" + new_content)
        conn.execute(
            "UPDATE notes SET content = ?, embedding = ?, updated_at = ? WHERE id = ?",
            (new_content, embedding, now_ms, note_id),
        )
        conn.commit()
        logger.info("Note replaced", extra={"note_id": note_id, "action": "note_replace"})

    def archive_note(self, note_id: int) -> None:
        """Soft-delete a note by setting archived=1."""
        now_ms = int(time.time() * 1000)
        conn = self._connect()
        conn.execute(
            "UPDATE notes SET archived = 1, updated_at = ? WHERE id = ?",
            (now_ms, note_id),
        )
        conn.commit()
        logger.debug("Note archived", extra={"note_id": note_id, "action": "note_archive"})

    def list_notes(self, conversation_id: str) -> list:
        """Return all active notes for a conversation, newest first."""
        conn = self._connect()
        cursor = conn.execute(
            """
            SELECT id, conversation_id, title, content, embedding, remind_at, created_at, updated_at, archived
            FROM notes
            WHERE conversation_id = ? AND archived = 0
            ORDER BY updated_at DESC
            """,
            (conversation_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def search_notes(self, conversation_id: str, query: str, limit: int = 5) -> list:
        """
        Search notes using semantic similarity (if embedding model configured) with
        FTS5 fallback. Returns a list of row dicts, deduplicated, best matches first.
        """
        import struct
        import math

        seen_ids: set = set()
        results = []

        # --- Semantic search ---
        query_embedding = self._get_embedding(query)
        if query_embedding is not None:
            n_dims = len(query_embedding) // 4
            query_vec = struct.unpack(f"{n_dims}f", query_embedding)
            mag_q = math.sqrt(sum(x * x for x in query_vec))
            conn = self._connect()
            rows = conn.execute(
                """
                SELECT id, conversation_id, title, content, embedding, remind_at, created_at, updated_at, archived
                FROM notes
                WHERE conversation_id = ? AND archived = 0 AND embedding IS NOT NULL
                """,
                (conversation_id,),
            ).fetchall()
            scored = []
            for row in rows:
                blob = row["embedding"]
                chunk_vec = struct.unpack(f"{len(blob) // 4}f", blob)
                dot = sum(a * b for a, b in zip(query_vec, chunk_vec))
                mag_c = math.sqrt(sum(x * x for x in chunk_vec))
                score = dot / (mag_q * mag_c) if mag_q and mag_c else 0.0
                scored.append((score, row))
            scored.sort(key=lambda x: x[0], reverse=True)
            for _, row in scored[:limit]:
                seen_ids.add(row["id"])
                results.append(dict(row))

        # --- FTS5 fallback ---
        if len(results) < limit:
            all_words = re.findall(r'\w+', query)
            words = [w for w in all_words if w.lower() not in _STOPWORDS and len(w) > 1]
            if not words:
                words = all_words  # Fall back to full list so FTS still runs
            if words:
                fts_query = " OR ".join(f'"{w}"' for w in words[:20])
                try:
                    conn = self._connect()
                    fts_rows = conn.execute(
                        """
                        SELECT n.id, n.conversation_id, n.title, n.content, n.embedding,
                               n.remind_at, n.created_at, n.updated_at, n.archived
                        FROM notes_fts
                        JOIN notes n ON notes_fts.rowid = n.id
                        WHERE notes_fts MATCH ? AND n.conversation_id = ? AND n.archived = 0
                        ORDER BY rank
                        LIMIT ?
                        """,
                        (fts_query, conversation_id, limit),
                    ).fetchall()
                    for row in fts_rows:
                        if row["id"] not in seen_ids:
                            seen_ids.add(row["id"])
                            results.append(dict(row))
                            if len(results) >= limit:
                                break
                except Exception as e:
                    logger.warning("Notes FTS search failed", extra={"error": str(e)})

        return results[:limit]

    def set_note_remind_at(self, note_id: int, remind_at: Optional[str]) -> None:
        """Set or clear the remind_at timestamp for a note (ISO8601 UTC string or None)."""
        now_ms = int(time.time() * 1000)
        conn = self._connect()
        conn.execute(
            "UPDATE notes SET remind_at = ?, updated_at = ? WHERE id = ?",
            (remind_at, now_ms, note_id),
        )
        conn.commit()
        logger.info("Note remind_at set", extra={"note_id": note_id, "remind_at": remind_at, "action": "note_remind_set"})

    def get_due_note_reminders(self) -> list:
        """Return all non-archived notes whose remind_at <= now (ISO8601 UTC comparison)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        cursor = conn.execute(
            """
            SELECT id, conversation_id, title, content, embedding, remind_at, created_at, updated_at, archived
            FROM notes
            WHERE remind_at IS NOT NULL AND remind_at <= ? AND archived = 0
            """,
            (now_iso,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def clear_note_remind_at(self, note_id: int) -> None:
        """Clear remind_at after it fires (one-time only)."""
        self.set_note_remind_at(note_id, None)

    # --- Task Operations ---

    @staticmethod
    def _normalize_list_name(name: str) -> str:
        return name.strip().lower()

    def add_task(self, conversation_id: str, list_name: str, item_text: str) -> int:
        """Insert a new task item. Returns the new task id."""
        now_ms = int(time.time() * 1000)
        list_name = self._normalize_list_name(list_name)
        item_text = item_text.strip()
        conn = self._connect()

        # Deduplicate: return existing active item rather than inserting duplicate
        existing = conn.execute(
            """
            SELECT id FROM tasks
            WHERE conversation_id = ? AND list_name = ? AND item_text = ?
              AND archived = 0 AND done = 0
            LIMIT 1
            """,
            (conversation_id, list_name, item_text),
        ).fetchone()
        if existing:
            logger.debug("Task already exists, skipping insert", extra={
                "task_id": existing[0],
                "conversation_id": conversation_id,
                "list_name": list_name,
                "action": "task_add_skipped",
            })
            return existing[0]

        cursor = conn.execute(
            """
            INSERT INTO tasks (conversation_id, list_name, item_text, done, created_at, archived)
            VALUES (?, ?, ?, 0, ?, 0)
            """,
            (conversation_id, list_name, item_text, now_ms),
        )
        conn.commit()
        task_id = cursor.lastrowid or 0
        logger.debug("Task added", extra={
            "task_id": task_id,
            "conversation_id": conversation_id,
            "list_name": list_name,
            "action": "task_add",
        })
        return task_id

    def get_tasks(self, conversation_id: str, list_name: str, include_archived: bool = False) -> list:
        """Return tasks for a list, ordered by created_at."""
        list_name = self._normalize_list_name(list_name)
        conn = self._connect()
        where = "conversation_id = ? AND list_name = ?"
        params: list = [conversation_id, list_name]
        if not include_archived:
            where += " AND archived = 0"
        cursor = conn.execute(
            f"SELECT id, conversation_id, list_name, item_text, done, created_at, done_at, archived"
            f" FROM tasks WHERE {where} ORDER BY created_at ASC",
            params,
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_task_lists(self, conversation_id: str) -> list:
        """Return distinct active list names for a conversation, alphabetically."""
        conn = self._connect()
        cursor = conn.execute(
            "SELECT DISTINCT list_name FROM tasks"
            " WHERE conversation_id = ? AND archived = 0"
            " ORDER BY list_name ASC",
            (conversation_id,),
        )
        return [row[0] for row in cursor.fetchall()]

    def mark_task_done(self, task_id: int) -> None:
        """Mark a task item as done."""
        now_ms = int(time.time() * 1000)
        conn = self._connect()
        conn.execute(
            "UPDATE tasks SET done = 1, done_at = ? WHERE id = ?",
            (now_ms, task_id),
        )
        conn.commit()
        logger.debug("Task marked done", extra={"task_id": task_id, "action": "task_done"})

    def reopen_task(self, task_id: int) -> None:
        """Reopen a done task item."""
        conn = self._connect()
        conn.execute(
            "UPDATE tasks SET done = 0, done_at = NULL WHERE id = ?",
            (task_id,),
        )
        conn.commit()
        logger.debug("Task reopened", extra={"task_id": task_id, "action": "task_reopen"})

    def archive_task(self, task_id: int) -> None:
        """Soft-delete a single task item."""
        conn = self._connect()
        conn.execute("UPDATE tasks SET archived = 1 WHERE id = ?", (task_id,))
        conn.commit()
        logger.debug("Task archived", extra={"task_id": task_id, "action": "task_archive"})

    def archive_task_list(self, conversation_id: str, list_name: str) -> int:
        """Soft-delete all items in a list. Returns count archived."""
        list_name = self._normalize_list_name(list_name)
        conn = self._connect()
        cursor = conn.execute(
            "UPDATE tasks SET archived = 1"
            " WHERE conversation_id = ? AND list_name = ? AND archived = 0",
            (conversation_id, list_name),
        )
        conn.commit()
        count = cursor.rowcount
        logger.info("Task list archived", extra={
            "conversation_id": conversation_id,
            "list_name": list_name,
            "count": count,
            "action": "task_list_archive",
        })
        return count

    def hard_delete_tasks(self, conversation_id: Optional[str] = None) -> int:
        """Hard-delete task rows. If conversation_id given, scoped to that conversation."""
        conn = self._connect()
        if conversation_id:
            cursor = conn.execute(
                "DELETE FROM tasks WHERE conversation_id = ?", (conversation_id,)
            )
        else:
            cursor = conn.execute("DELETE FROM tasks")
        conn.commit()
        return cursor.rowcount

    def check_fts_integrity(self) -> dict:
        """
        Check integrity of all FTS indexes.

        Returns dict with status for each FTS table:
        {
            "user_facts_fts": {"ok": True, "fts_count": 100, "main_count": 100, "difference": 0},
            "summaries_fts": {"ok": False, "fts_count": 50, "main_count": 55, "difference": 5},
            ...
        }
        """
        conn = self._connect()
        return self._check_fts_integrity_internal(conn)

    def rebuild_fts_index(self, index_name: str) -> tuple[bool, str]:
        """
        Rebuild a specific FTS index.

        Args:
            index_name: One of 'user_facts_fts', 'summaries_fts', 'knowledge_fts', 'notes_fts'

        Returns:
            (success, message)
        """
        valid_indexes = ["user_facts_fts", "summaries_fts", "knowledge_fts", "notes_fts"]
        if index_name not in valid_indexes:
            return False, f"Invalid index name. Must be one of: {valid_indexes}"

        conn = self._connect()

        # Get counts before rebuild
        integrity_before = self._check_fts_integrity_internal(conn)
        before = integrity_before.get(index_name, {})

        # Rebuild
        success = self._rebuild_fts_index_internal(conn, index_name)
        if not success:
            return False, "rebuild_failed"

        # Get counts after rebuild
        integrity_after = self._check_fts_integrity_internal(conn)
        after = integrity_after.get(index_name, {})

        return True, f"Rebuilt {index_name}: {before.get('fts_count', 0)} -> {after.get('fts_count', 0)} rows"

    def rebuild_all_fts_indexes(self) -> dict:
        """
        Rebuild all FTS indexes.

        Returns dict with results for each index.
        """
        results = {}
        for index_name in ["user_facts_fts", "summaries_fts", "knowledge_fts", "notes_fts"]:
            success, message = self.rebuild_fts_index(index_name)
            results[index_name] = {"success": success, "message": message}
        return results

    def close(self) -> None:
        """Close all database connections for all threads."""
        with self._all_connections_lock:
            for conn in self._all_connections:
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_connections.clear()
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

        logger.debug("Stored message", extra={"direction": direction, "message_id": message_id})
        return cursor.lastrowid if cursor.rowcount > 0 else 0

    def get_recent_messages(
        self,
        limit: int = 20,
        conversation_id: Optional[str] = None,
        content_type: str = "text",
        since_ts: Optional[int] = None,
    ) -> List[Message]:
        """
        Get recent messages for LLM context.

        Args:
            limit: Maximum number of messages to return
            conversation_id: Filter by conversation (optional)
            content_type: Filter by content type (default: text)
            since_ts: Only return messages with timestamp >= this value (ms, optional)

        Returns:
            List of Message objects, oldest first (for context building)
        """
        conn = self._connect()

        if conversation_id:
            if since_ts is not None:
                cursor = conn.execute(
                    """
                    SELECT id, message_id, direction, channel, content_type,
                           content_text, conversation_id, reply_to_id, timestamp, created_at,
                           archived, sender_id, sender_name
                    FROM messages
                    WHERE content_type = ? AND conversation_id = ? AND archived = 0
                      AND timestamp >= ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (content_type, conversation_id, since_ts, limit)
                )
            else:
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
            if since_ts is not None:
                cursor = conn.execute(
                    """
                    SELECT id, message_id, direction, channel, content_type,
                           content_text, conversation_id, reply_to_id, timestamp, created_at,
                           archived, sender_id, sender_name
                    FROM messages
                    WHERE content_type = ? AND archived = 0
                      AND timestamp >= ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (content_type, since_ts, limit)
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

    def get_oldest_messages(
        self,
        limit: int = 20,
        conversation_id: Optional[str] = None,
        content_type: str = "text",
        after_ts: Optional[int] = None,
    ) -> List[Message]:
        """
        Get oldest messages for compaction or tension mining.

        Args:
            limit: Maximum number of messages to return
            conversation_id: Filter by conversation (required for compaction)
            content_type: Filter by content type (default: text)
            after_ts: If set, only return messages with timestamp > after_ts (epoch ms)

        Returns:
            List of Message objects, oldest first
        """
        conn = self._connect()

        if conversation_id:
            if after_ts is not None:
                cursor = conn.execute(
                    """
                    SELECT id, message_id, direction, channel, content_type,
                           content_text, conversation_id, reply_to_id, timestamp, created_at,
                           archived, sender_id, sender_name
                    FROM messages
                    WHERE content_type = ? AND conversation_id = ? AND archived = 0
                      AND timestamp > ?
                    ORDER BY timestamp ASC
                    LIMIT ?
                    """,
                    (content_type, conversation_id, after_ts, limit)
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT id, message_id, direction, channel, content_type,
                           content_text, conversation_id, reply_to_id, timestamp, created_at,
                           archived, sender_id, sender_name
                    FROM messages
                    WHERE content_type = ? AND conversation_id = ? AND archived = 0
                    ORDER BY timestamp ASC
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
                ORDER BY timestamp ASC
                LIMIT ?
                """,
                (content_type, limit)
            )

        rows = cursor.fetchall()

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
                sender_id=row["sender_id"],
                sender_name=row["sender_name"],
            )
            for row in rows
        ]

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
        important: bool = False,
        ttl_hours: Optional[float] = None,
        detected_at: Optional[int] = None,
    ) -> int:
        """
        Store or update a fact about the user for a specific conversation.

        If fact with same conversation_id+category+key exists, updates it.

        Args:
            important: If True, fact is always included in context regardless of query.
            ttl_hours: Hours until fact expires. None = permanent.
            detected_at: ms epoch of the source message. None = unknown.
        """
        conn = self._connect()
        now_ms = int(time.time() * 1000)
        important_int = 1 if important else 0
        expires_at = int(now_ms + ttl_hours * 3_600_000) if ttl_hours else None

        # Try to update existing active fact for this conversation
        # Note: important flag can only be promoted (0->1), never demoted (1->0)
        # This preserves explicitly marked important facts even when updated via other paths
        cursor = conn.execute(
            """
            UPDATE user_facts
            SET value = ?, confidence = ?, source = ?, source_message_id = ?,
                important = CASE WHEN important = 1 THEN 1 ELSE ? END,
                last_verified_at = ?, updated_at = ?, expires_at = ?, detected_at = ?
            WHERE conversation_id = ? AND category = ? AND key = ? AND active = 1
            """,
            (value, confidence, source, source_message_id, important_int,
             now_ms, now_ms, expires_at, detected_at,
             conversation_id, category, key)
        )

        if cursor.rowcount == 0:
            # Insert new fact
            cursor = conn.execute(
                """
                INSERT INTO user_facts (
                    conversation_id, category, key, value, confidence, source, source_message_id,
                    important, learned_at, updated_at, expires_at, detected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (conversation_id, category, key, value, confidence, source, source_message_id,
                 important_int, now_ms, now_ms, expires_at, detected_at)
            )

        conn.commit()
        logger.debug("Stored fact", extra={
            "conversation_id": conversation_id or "global",
            "category": category,
            "key": key,
            "value": value,
            "confidence": confidence,
            "important": important,
            "ttl_hours": ttl_hours,
        })
        return cursor.lastrowid or 0

    def get_facts(
        self,
        min_confidence: float = 0.5,
        category: Optional[str] = None,
        conversation_id: Optional[str] = None,
        limit: int = 50,
        as_of: Optional[int] = None,
        include_expired: bool = False,
    ) -> List[UserFact]:
        """Get active user facts for a conversation, optionally filtered by category.

        Args:
            as_of: ms epoch to query facts as of (default: now). Used for temporal
                   queries like "what was my schedule yesterday?". Ignored when
                   include_expired=True.
            include_expired: If True, include expired facts (skips expiry filter).
        """
        conn = self._connect()
        now_ms = int(time.time() * 1000)
        as_of_ms = as_of if as_of is not None else now_ms

        # Build query based on filters
        conditions = ["active = 1", "confidence >= ?"]
        params: list = [min_confidence]

        if not include_expired:
            conditions.append("learned_at <= ?")
            params.append(as_of_ms)
            conditions.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(as_of_ms)

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
            SELECT id, conversation_id, category, key, value, confidence, source,
                   learned_at, last_verified_at, important, expires_at, detected_at
            FROM user_facts
            WHERE {where_clause}
            ORDER BY important DESC, category, confidence DESC
            LIMIT ?
            """,
            params
        )

        return [
            UserFact(
                id=row["id"],
                conversation_id=row["conversation_id"] or "",
                category=row["category"],
                key=row["key"],
                value=row["value"],
                confidence=row["confidence"],
                source=row["source"],
                learned_at=row["learned_at"],
                last_verified_at=row["last_verified_at"],
                important=bool(row["important"]),
                expires_at=row["expires_at"],
                detected_at=row["detected_at"],
            )
            for row in cursor.fetchall()
        ]

    def get_facts_as_text(self, min_confidence: float = 0.5, conversation_id: Optional[str] = None) -> str:
        """Get facts formatted as text for LLM context."""
        facts = self.get_facts(min_confidence=min_confidence, conversation_id=conversation_id)
        if not facts:
            return ""

        now_ms = int(time.time() * 1000)
        lines = ["Known facts about the user:"]
        by_category: Dict[str, List[UserFact]] = {}
        for fact in facts:
            by_category.setdefault(fact.category, []).append(fact)

        for category, cat_facts in sorted(by_category.items()):
            lines.append(f"\n{category.title()}:")
            for fact in cat_facts:
                suffix = _fact_temporal_suffix(fact, now_ms)
                lines.append(f"  - {fact.key}: {fact.value}{suffix}")

        return "\n".join(lines)

    def search_facts(
        self,
        query: str,
        limit: int = 10,
        min_confidence: float = 0.5,
        conversation_id: Optional[str] = None,
    ) -> List[UserFact]:
        """
        Search user facts using FTS5 with BM25 ranking.

        Args:
            query: Search query (plain text, will be sanitized)
            limit: Maximum number of results
            min_confidence: Minimum confidence threshold
            conversation_id: Filter by conversation ID (None = all)

        Returns:
            List of matching UserFact objects, ranked by relevance
        """
        conn = self._connect()

        # Sanitize query for FTS5: extract only word characters (alphanumeric + underscore)
        # This prevents FTS5 syntax injection - no quotes, operators, or special chars can pass through
        all_words = re.findall(r'\w+', query)
        words = [w for w in all_words if w.lower() not in _STOPWORDS and len(w) > 1]
        if not words:
            words = all_words  # Fall back to full list so FTS still runs
        if not words:
            return []

        # Join words with OR, each word quoted to treat as literal
        # Safe because \w+ guarantees no double quotes in words
        fts_query = " OR ".join(f'"{word}"' for word in words[:20])

        now_ms = int(time.time() * 1000)

        try:
            # Build conversation filter
            if conversation_id is not None:
                convo_filter = "AND f.conversation_id = ?"
                params = [fts_query, min_confidence, now_ms, conversation_id, limit]
            else:
                convo_filter = ""
                params = [fts_query, min_confidence, now_ms, limit]

            cursor = conn.execute(
                f"""
                SELECT f.id, f.conversation_id, f.category, f.key, f.value,
                       f.confidence, f.source, f.learned_at, f.last_verified_at,
                       f.important, f.expires_at, f.detected_at,
                       bm25(user_facts_fts) as rank
                FROM user_facts f
                JOIN user_facts_fts fts ON f.id = fts.rowid
                WHERE user_facts_fts MATCH ?
                  AND f.active = 1
                  AND f.confidence >= ?
                  AND (f.expires_at IS NULL OR f.expires_at > ?)
                  {convo_filter}
                ORDER BY rank
                LIMIT ?
                """,
                params
            )
            rows = cursor.fetchall()
            logger.debug("Facts FTS: matches found", extra={"count": len(rows), "query": fts_query[:50]})
        except sqlite3.OperationalError as e:
            logger.warning("Facts FTS5 search failed", extra={"error": str(e)})
            return []

        return [
            UserFact(
                id=row["id"],
                conversation_id=row["conversation_id"] or "",
                category=row["category"],
                key=row["key"],
                value=row["value"],
                confidence=row["confidence"],
                source=row["source"],
                learned_at=row["learned_at"],
                last_verified_at=row["last_verified_at"],
                important=bool(row["important"]),
                expires_at=row["expires_at"],
                detected_at=row["detected_at"],
            )
            for row in rows
        ]

    def get_important_facts(
        self,
        min_confidence: float = 0.5,
        conversation_id: Optional[str] = None,
        limit: int = 50,
        as_of: Optional[int] = None,
        include_expired: bool = False,
    ) -> List[UserFact]:
        """Get all important (core) facts for a conversation.

        Args:
            as_of: ms epoch to query facts as of (default: now). Ignored when
                   include_expired=True.
            include_expired: If True, include expired facts (skips expiry filter).
        """
        conn = self._connect()
        now_ms = int(time.time() * 1000)
        as_of_ms = as_of if as_of is not None else now_ms

        conditions = ["active = 1", "important = 1", "confidence >= ?"]
        params: list = [min_confidence]

        if not include_expired:
            conditions.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(as_of_ms)

        if conversation_id is not None:
            conditions.append("conversation_id = ?")
            params.append(conversation_id)

        params.append(limit)
        where_clause = " AND ".join(conditions)

        cursor = conn.execute(
            f"""
            SELECT id, conversation_id, category, key, value, confidence, source,
                   learned_at, last_verified_at, important, expires_at, detected_at
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
                conversation_id=row["conversation_id"] or "",
                category=row["category"],
                key=row["key"],
                value=row["value"],
                confidence=row["confidence"],
                source=row["source"],
                learned_at=row["learned_at"],
                last_verified_at=row["last_verified_at"],
                important=bool(row["important"]),
                expires_at=row["expires_at"],
                detected_at=row["detected_at"],
            )
            for row in cursor.fetchall()
        ]

    def get_facts_as_context(
        self,
        query: str,
        max_tokens: int = 300,
        min_confidence: float = 0.6,
        conversation_id: Optional[str] = None,
    ) -> str:
        """
        Get facts for LLM context using hybrid approach:
        1. Always include important (core) facts
        2. Add FTS search results for query relevance
        3. Respect token limit

        Args:
            query: Search query
            max_tokens: Approximate max tokens (chars / 4)
            min_confidence: Minimum confidence threshold
            conversation_id: Filter by conversation ID

        Returns:
            Formatted context string, empty if no facts
        """
        # First, get all important facts (always included)
        important_facts = self.get_important_facts(
            min_confidence=min_confidence,
            conversation_id=conversation_id,
            limit=20,
        )
        important_ids = {f.id for f in important_facts}

        # Then, search for relevant facts via FTS
        fts_facts = self.search_facts(
            query,
            limit=20,
            min_confidence=min_confidence,
            conversation_id=conversation_id,
        )
        # Filter out already-included important facts
        fts_facts = [f for f in fts_facts if f.id not in important_ids]

        # Combine: important first, then FTS matches
        all_facts = important_facts + fts_facts

        if not all_facts:
            return ""

        now_ms = int(time.time() * 1000)
        lines = ["Relevant facts about the user:"]
        total_chars = 0
        max_chars = max_tokens * 4  # Rough estimate

        by_category: Dict[str, List[UserFact]] = {}
        for fact in all_facts:
            by_category.setdefault(fact.category, []).append(fact)

        for category, cat_facts in sorted(by_category.items()):
            cat_line = f"\n{category.title()}:"
            if total_chars + len(cat_line) > max_chars:
                break
            lines.append(cat_line)
            total_chars += len(cat_line)

            for fact in cat_facts:
                suffix = _fact_temporal_suffix(fact, now_ms)
                fact_line = f"  - {fact.key}: {fact.value}{suffix}"
                if total_chars + len(fact_line) > max_chars:
                    break
                lines.append(fact_line)
                total_chars += len(fact_line)

        return "\n".join(lines)

    def reschedule_fact(
        self,
        fact_id: int,
        conversation_id: str,
        ttl_hours: float,
        new_value: Optional[str] = None,
    ) -> bool:
        """
        Reschedule a temporal fact (including expired ones) to a new expiry time.

        Optionally updates the value (e.g. "meeting moved to Monday 10am").
        Works on expired facts — no active/expiry pre-check.

        Returns True if fact was found and updated, False otherwise.
        """
        conn = self._connect()
        now_ms = int(time.time() * 1000)
        new_expires_at = int(now_ms + ttl_hours * 3_600_000)

        if new_value is not None:
            cursor = conn.execute(
                "UPDATE user_facts SET expires_at = ?, value = ?, updated_at = ? WHERE id = ? AND conversation_id = ?",
                (new_expires_at, new_value, now_ms, fact_id, conversation_id),
            )
        else:
            cursor = conn.execute(
                "UPDATE user_facts SET expires_at = ?, updated_at = ? WHERE id = ? AND conversation_id = ?",
                (new_expires_at, now_ms, fact_id, conversation_id),
            )
        conn.commit()
        return cursor.rowcount > 0

    def get_recently_expired_facts(
        self,
        days: int = 7,
        conversation_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[UserFact]:
        """Get facts that expired within the last N days."""
        conn = self._connect()
        now_ms = int(time.time() * 1000)
        since_ms = now_ms - days * 24 * 3_600_000

        conditions = [
            "active = 1",
            "expires_at IS NOT NULL",
            "expires_at <= ?",
            "expires_at > ?",
        ]
        params: list = [now_ms, since_ms]

        if conversation_id is not None:
            conditions.append("conversation_id = ?")
            params.append(conversation_id)

        params.append(limit)
        where_clause = " AND ".join(conditions)

        cursor = conn.execute(
            f"""
            SELECT id, conversation_id, category, key, value, confidence, source,
                   learned_at, last_verified_at, important, expires_at, detected_at
            FROM user_facts
            WHERE {where_clause}
            ORDER BY expires_at DESC
            LIMIT ?
            """,
            params,
        )

        return [
            UserFact(
                id=row["id"],
                conversation_id=row["conversation_id"] or "",
                category=row["category"],
                key=row["key"],
                value=row["value"],
                confidence=row["confidence"],
                source=row["source"],
                learned_at=row["learned_at"],
                last_verified_at=row["last_verified_at"],
                important=bool(row["important"]),
                expires_at=row["expires_at"],
                detected_at=row["detected_at"],
            )
            for row in cursor.fetchall()
        ]

    def purge_expired_facts(self, conversation_id: str) -> int:
        """Hard-delete facts whose TTL has expired for a conversation."""
        conn = self._connect()
        now_ms = int(time.time() * 1000)
        cursor = conn.execute(
            """
            DELETE FROM user_facts
            WHERE conversation_id = ?
              AND expires_at IS NOT NULL
              AND expires_at < ?
            """,
            (conversation_id, now_ms),
        )
        conn.commit()
        count = cursor.rowcount
        if count > 0:
            logger.info("Purged expired facts", extra={
                "conversation_id": conversation_id,
                "count": count,
            })
        return count

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

        logger.info("Stored summary", extra={
            "summary_type": summary_type,
            "conversation_id": conversation_id or "global",
            "period_start": period_start,
            "period_end": period_end,
            "message_count": message_count,
            "action": "summary_store"
        })
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
                       message_count, created_at, key_points_json
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
                       message_count, created_at, key_points_json
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
                key_points_json=row["key_points_json"],
            )
            for row in cursor.fetchall()
        ]

    def count_facts(self, min_confidence: float = 0.0) -> int:
        """Return count of active facts without loading them."""
        conn = self._connect()
        now_ms = int(time.time() * 1000)
        cursor = conn.execute(
            """
            SELECT COUNT(*) FROM user_facts
            WHERE active = 1 AND confidence >= ?
              AND learned_at <= ? AND (expires_at IS NULL OR expires_at > ?)
            """,
            (min_confidence, now_ms, now_ms)
        )
        return cursor.fetchone()[0] or 0

    def count_summaries(self, days: int = 30) -> int:
        """Return count of recent summaries without loading them."""
        conn = self._connect()
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - (days * 24 * 60 * 60 * 1000)
        cursor = conn.execute(
            "SELECT COUNT(*) FROM context_summaries WHERE period_end > ?",
            (cutoff_ms,)
        )
        return cursor.fetchone()[0] or 0

    def get_summaries_as_text(self, days: int = 7, conversation_id: Optional[str] = None,
                              max_chars: int = 6000) -> str:
        """Get summaries formatted as text for LLM context."""
        summaries = self.get_recent_summaries(days=days, conversation_id=conversation_id)
        if not summaries:
            return ""

        lines = ["Earlier in this conversation (already discussed):"]
        total_chars = 0
        for summary in reversed(summaries):  # Oldest first
            # Format timestamp as date
            date_str = datetime.fromtimestamp(summary.period_end / 1000).strftime("%Y-%m-%d")
            block = f"\n[{date_str}]\n{summary.summary_text}"
            if total_chars + len(block) > max_chars:
                break
            lines.append(block)
            total_chars += len(block)

        return "\n".join(lines)

    def search_summaries(
        self,
        query: str,
        limit: int = 5,
        days: int = 30,
        conversation_id: Optional[str] = None,
    ) -> List[ContextSummary]:
        """
        Search summaries using FTS5 with BM25 ranking.

        Args:
            query: Search query (plain text, will be sanitized)
            limit: Maximum number of results
            days: Only search summaries from last N days
            conversation_id: Filter by conversation ID (None = all)

        Returns:
            List of matching ContextSummary objects, ranked by relevance
        """
        conn = self._connect()

        # Sanitize query for FTS5: extract only word characters (alphanumeric + underscore)
        # This prevents FTS5 syntax injection - no quotes, operators, or special chars can pass through
        all_words = re.findall(r'\w+', query)
        words = [w for w in all_words if w.lower() not in _STOPWORDS and len(w) > 1]
        if not words:
            words = all_words  # Fall back to full list so FTS still runs
        if not words:
            return []

        # Calculate cutoff time
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - (days * 24 * 60 * 60 * 1000)

        # Join words with OR, each word quoted to treat as literal
        # Safe because \w+ guarantees no double quotes in words
        fts_query = " OR ".join(f'"{word}"' for word in words[:20])

        try:
            # Build conversation filter
            if conversation_id is not None:
                convo_filter = "AND s.conversation_id = ?"
                params = [fts_query, cutoff_ms, conversation_id, limit]
            else:
                convo_filter = ""
                params = [fts_query, cutoff_ms, limit]

            cursor = conn.execute(
                f"""
                SELECT s.id, s.summary_type, s.period_start, s.period_end,
                       s.summary_text, s.message_count, s.created_at,
                       bm25(summaries_fts) as rank
                FROM context_summaries s
                JOIN summaries_fts fts ON s.id = fts.rowid
                WHERE summaries_fts MATCH ?
                  AND s.period_end > ?
                  {convo_filter}
                ORDER BY rank
                LIMIT ?
                """,
                params
            )
            rows = cursor.fetchall()
            logger.debug("Summaries FTS: matches found", extra={"count": len(rows), "query": fts_query[:50]})
        except sqlite3.OperationalError as e:
            logger.warning("Summaries FTS5 search failed", extra={"error": str(e)})
            return []

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
            for row in rows
        ]

    def get_summaries_as_context(
        self,
        query: str,
        max_tokens: int = 400,
        days: int = 30,
        conversation_id: Optional[str] = None,
    ) -> str:
        """
        Search summaries and format for LLM context with token limit.

        Args:
            query: Search query
            max_tokens: Approximate max tokens (chars / 4)
            days: Only search summaries from last N days
            conversation_id: Filter by conversation ID

        Returns:
            Formatted context string, empty if no matches
        """
        summaries = self.search_summaries(
            query,
            limit=10,  # Fetch more than needed, will truncate by tokens
            days=days,
            conversation_id=conversation_id,
        )
        if not summaries:
            return ""

        lines = ["Relevant conversation history:"]
        total_chars = 0
        max_chars = max_tokens * 4  # Rough estimate

        # Sort by period_end ascending (oldest first) for chronological context
        for summary in sorted(summaries, key=lambda s: s.period_end):
            date_str = datetime.fromtimestamp(summary.period_end / 1000).strftime("%Y-%m-%d")
            summary_block = f"\n[{date_str}]\n{summary.summary_text}"
            if total_chars + len(summary_block) > max_chars:
                break
            lines.append(summary_block)
            total_chars += len(summary_block)

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
            logger.info("Archived messages", extra={"count": archived, "before_ms": before_ms})
        return archived

    def delete_processed_messages_before(self, before_ms: int) -> int:
        """
        Hard-delete fully-processed messages older than before_ms.

        A message qualifies when:
          - archived = 1 (compaction has run)
          - no wind_state row exists for the conversation (Wind not enabled), OR
          - timestamp < last_tension_mined_message_ts (Wind has mined past this message)

        A wind_state row with last_tension_mined_message_ts IS NULL means Wind is
        configured but has never run — messages are NOT eligible for deletion yet.
        """
        conn = self._connect()
        conn.execute(
            """
            UPDATE messages SET reply_to_id = NULL
            WHERE reply_to_id IN (
                SELECT m.message_id FROM messages m
                LEFT JOIN wind_state ws ON ws.conversation_id = m.conversation_id
                WHERE m.archived = 1
                  AND m.timestamp < ?
                  AND (ws.conversation_id IS NULL
                       OR m.timestamp < ws.last_tension_mined_message_ts)
            )
            """,
            (before_ms,)
        )
        cursor = conn.execute(
            """
            DELETE FROM messages
            WHERE rowid IN (
                SELECT m.rowid FROM messages m
                LEFT JOIN wind_state ws ON ws.conversation_id = m.conversation_id
                WHERE m.archived = 1
                  AND m.timestamp < ?
                  AND (ws.conversation_id IS NULL
                       OR m.timestamp < ws.last_tension_mined_message_ts)
            )
            """,
            (before_ms,)
        )
        conn.commit()
        deleted = cursor.rowcount
        if deleted > 0:
            logger.info("Purged processed messages", extra={"count": deleted, "before_ms": before_ms})
        return deleted

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
            logger.info("Deleted messages", extra={"count": deleted, "before_ms": before_ms})
        return deleted

    def delete_messages_by_ids(self, message_ids: List[str], conversation_id: Optional[str] = None) -> int:
        """Delete specific messages by their message_id (not row id)."""
        if not message_ids:
            return 0

        conn = self._connect()
        placeholders = ",".join("?" * len(message_ids))

        # First, clear reply_to_id references
        if conversation_id:
            conn.execute(
                f"""
                UPDATE messages SET reply_to_id = NULL
                WHERE reply_to_id IN ({placeholders}) AND conversation_id = ?
                """,
                (*message_ids, conversation_id)
            )
            cursor = conn.execute(
                f"DELETE FROM messages WHERE message_id IN ({placeholders}) AND conversation_id = ?",
                (*message_ids, conversation_id)
            )
        else:
            conn.execute(
                f"""
                UPDATE messages SET reply_to_id = NULL
                WHERE reply_to_id IN ({placeholders})
                """,
                message_ids
            )
            cursor = conn.execute(
                f"DELETE FROM messages WHERE message_id IN ({placeholders})",
                message_ids
            )
        conn.commit()

        deleted = cursor.rowcount
        if deleted > 0:
            logger.info("Deleted messages by ID", extra={"count": deleted})
        return deleted

    def archive_messages_by_ids(self, message_ids: List[str], conversation_id: Optional[str] = None) -> int:
        """Archive specific messages by their message_id (soft delete)."""
        if not message_ids:
            return 0

        conn = self._connect()
        placeholders = ",".join("?" * len(message_ids))

        if conversation_id:
            cursor = conn.execute(
                f"UPDATE messages SET archived = 1 WHERE message_id IN ({placeholders}) AND conversation_id = ?",
                (*message_ids, conversation_id)
            )
        else:
            cursor = conn.execute(
                f"UPDATE messages SET archived = 1 WHERE message_id IN ({placeholders})",
                message_ids
            )
        conn.commit()

        archived = cursor.rowcount
        if archived > 0:
            logger.info("Archived messages by ID", extra={"count": archived})
        return archived

    # --- Knowledge Operations (RAG) ---

    def _get_embedding(self, text: str) -> Optional[bytes]:
        """Get embedding vector for text via Ollama. Returns packed float32 bytes or None."""
        import struct
        import math
        model = os.getenv("JOI_EMBEDDING_MODEL", "").strip()
        if not model:
            return None
        ollama_url = os.getenv("JOI_OLLAMA_URL", "http://localhost:11434").rstrip("/")
        try:
            import httpx
            resp = httpx.post(
                f"{ollama_url}/api/embed",
                json={"model": model, "input": text},
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            # Ollama returns {"embeddings": [[float, ...]]}
            vec = data.get("embeddings", [[]])[0]
            if not vec:
                return None
            return struct.pack(f"{len(vec)}f", *vec)
        except Exception as e:
            logger.warning("Embedding request failed", extra={"error": str(e), "model": model})
            return None

    def store_knowledge_chunk(
        self,
        source: str,
        title: str,
        content: str,
        chunk_index: int = 0,
        metadata_json: Optional[str] = None,
        scope: str = "",
    ) -> int:
        """Store a knowledge chunk for RAG retrieval.

        Args:
            source: Source identifier (e.g., filename)
            title: Chunk title
            content: Chunk content
            chunk_index: Index within source (for multi-chunk docs)
            metadata_json: Optional JSON metadata
            scope: Access scope (conversation_id or empty for legacy global)
        """
        conn = self._connect()
        now_ms = int(time.time() * 1000)

        embedding = self._get_embedding(content)

        cursor = conn.execute(
            """
            INSERT OR REPLACE INTO knowledge_chunks (
                scope, source, title, content, chunk_index, metadata_json, embedding, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (scope, source, title, content, chunk_index, metadata_json, embedding, now_ms)
        )
        conn.commit()

        logger.debug("Stored knowledge chunk", extra={
            "source": source,
            "chunk_index": chunk_index,
            "scope": scope or None,
            "embedded": embedding is not None,
        })
        return cursor.lastrowid or 0

    def search_knowledge(
        self,
        query: str,
        limit: int = 5,
        scopes: Optional[List[str]] = None,
        min_rank: float = 0.0,
    ) -> List[KnowledgeChunk]:
        """
        Search knowledge base using FTS5 full-text search.

        Args:
            query: Search query (plain text, will be sanitized)
            limit: Maximum number of results
            scopes: List of allowed scopes (None = all, empty list = none)
                   Empty string scope ('') means legacy global knowledge.

        Returns:
            List of matching KnowledgeChunk objects, ranked by relevance
        """
        conn = self._connect()

        # Sanitize query for FTS5: extract only word characters (alphanumeric + underscore)
        # This prevents FTS5 syntax injection - no quotes, operators, or special chars can pass through
        all_words = re.findall(r'\w+', query)
        words = [w for w in all_words if w.lower() not in _STOPWORDS and len(w) > 1]
        if not words:
            words = all_words  # Fall back to full list so FTS still runs
        if not words:
            return []

        # If scopes is empty list, no access to anything
        if scopes is not None and len(scopes) == 0:
            return []

        # Join words with OR, each word quoted to treat as literal
        # Safe because \w+ guarantees no double quotes in words
        fts_query = " OR ".join(f'"{word}"' for word in words[:20])  # Limit to 20 words

        try:
            # Build scope filter
            if scopes is None:
                # No filter - access all (backwards compat / admin)
                scope_filter = ""
                params = [fts_query]
            else:
                # Filter by allowed scopes only - no global/legacy access
                # NOTE: All knowledge is created with proper scope, no legacy cleanup needed
                allowed = list(scopes)
                placeholders = ','.join('?' * len(allowed))
                scope_filter = f"AND k.scope IN ({placeholders})"
                params = [fts_query] + allowed

            # Use FTS5 MATCH for full-text search
            logger.debug("FTS query", extra={"query": fts_query[:100], "scopes": scopes})
            cursor = conn.execute(
                f"""
                SELECT k.id, k.scope, k.source, k.title, k.content, k.chunk_index, k.created_at,
                       bm25(knowledge_fts) as rank
                FROM knowledge_chunks k
                JOIN knowledge_fts f ON k.id = f.rowid
                WHERE knowledge_fts MATCH ?
                {scope_filter}
                ORDER BY rank
                LIMIT 100
                """,
                params
            )
            rows = cursor.fetchall()
            if min_rank < 0.0:
                rows = [r for r in rows if r["rank"] <= min_rank]
                logger.debug("FTS results", extra={"matches": len(rows), "min_rank": min_rank})
            else:
                logger.debug("FTS results", extra={"matches": len(rows)})
        except sqlite3.OperationalError as e:
            logger.warning("FTS5 search failed", extra={"error": str(e), "query": fts_query[:100]})
            return []

        return [
            KnowledgeChunk(
                id=row["id"],
                source=row["source"],
                title=row["title"],
                content=row["content"],
                chunk_index=row["chunk_index"],
                created_at=row["created_at"],
                scope=row["scope"],
            )
            for row in rows[:limit]
        ]

    def search_knowledge_semantic(
        self,
        query: str,
        limit: int = 10,
        scopes: Optional[List[str]] = None,
        min_score: float = 0.0,
    ) -> List[KnowledgeChunk]:
        """
        Search knowledge base using semantic (embedding) similarity.

        Returns empty list if embedding model is not configured or Ollama fails.
        Falls back gracefully — caller should then try search_knowledge() (FTS).
        """
        import struct
        import math

        query_embedding = self._get_embedding(query)
        if query_embedding is None:
            return []

        n_dims = len(query_embedding) // 4
        query_vec = struct.unpack(f"{n_dims}f", query_embedding)

        conn = self._connect()

        # If scopes is empty list, no access to anything
        if scopes is not None and len(scopes) == 0:
            return []

        if scopes is None:
            cursor = conn.execute(
                """
                SELECT id, scope, source, title, content, chunk_index, created_at, embedding
                FROM knowledge_chunks
                WHERE embedding IS NOT NULL
                """
            )
        else:
            placeholders = ",".join("?" * len(scopes))
            cursor = conn.execute(
                f"""
                SELECT id, scope, source, title, content, chunk_index, created_at, embedding
                FROM knowledge_chunks
                WHERE embedding IS NOT NULL
                  AND scope IN ({placeholders})
                """,
                list(scopes),
            )

        rows = cursor.fetchall()
        if not rows:
            return []

        # Compute cosine similarity for each chunk
        mag_q = math.sqrt(sum(x * x for x in query_vec))
        scored = []
        for row in rows:
            chunk_bytes = row["embedding"]
            chunk_vec = struct.unpack(f"{len(chunk_bytes) // 4}f", chunk_bytes)
            dot = sum(a * b for a, b in zip(query_vec, chunk_vec))
            mag_c = math.sqrt(sum(x * x for x in chunk_vec))
            score = dot / (mag_q * mag_c) if mag_q and mag_c else 0.0
            scored.append((score, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        if min_score > 0.0:
            scored = [(s, r) for s, r in scored if s >= min_score]
            logger.debug("Knowledge semantic search", extra={
                "candidates": len(rows), "above_threshold": len(scored), "limit": limit
            })
        else:
            logger.debug("Knowledge semantic search", extra={"candidates": len(rows), "limit": limit})

        return [
            KnowledgeChunk(
                id=row["id"],
                source=row["source"],
                title=row["title"],
                content=row["content"],
                chunk_index=row["chunk_index"],
                created_at=row["created_at"],
                scope=row["scope"],
            )
            for _, row in scored[:limit]
        ]

    def get_knowledge_by_source(self, source: str, scope: Optional[str] = None) -> List[KnowledgeChunk]:
        """Get all chunks from a specific source, optionally filtered by scope."""
        conn = self._connect()

        if scope is not None:
            cursor = conn.execute(
                """
                SELECT id, scope, source, title, content, chunk_index, created_at
                FROM knowledge_chunks
                WHERE source = ? AND scope = ?
                ORDER BY chunk_index
                """,
                (source, scope)
            )
        else:
            cursor = conn.execute(
                """
                SELECT id, scope, source, title, content, chunk_index, created_at
                FROM knowledge_chunks
                WHERE source = ?
                ORDER BY chunk_index
                """,
                (source,)
            )

        return [
            KnowledgeChunk(
                id=row["id"],
                scope=row["scope"],
                source=row["source"],
                title=row["title"],
                content=row["content"],
                chunk_index=row["chunk_index"],
                created_at=row["created_at"],
            )
            for row in cursor.fetchall()
        ]

    def delete_knowledge_source(self, source: str, scope: Optional[str] = None) -> int:
        """Delete all chunks from a source, optionally filtered by scope."""
        conn = self._connect()

        if scope is not None:
            cursor = conn.execute(
                "DELETE FROM knowledge_chunks WHERE source = ? AND scope = ?",
                (source, scope)
            )
        else:
            cursor = conn.execute(
                "DELETE FROM knowledge_chunks WHERE source = ?",
                (source,)
            )
        conn.commit()

        deleted = cursor.rowcount
        if deleted > 0:
            logger.info("Deleted chunks from source", extra={
                "count": deleted,
                "source": source,
                "scope": scope or None
            })
        return deleted

    def rescope_knowledge(self, old_scope: str, new_scope: str) -> int:
        """Move all knowledge from one scope to another."""
        conn = self._connect()

        cursor = conn.execute(
            "UPDATE knowledge_chunks SET scope = ? WHERE scope = ?",
            (new_scope, old_scope)
        )
        conn.commit()

        updated = cursor.rowcount
        if updated > 0:
            logger.info("Rescoped chunks", extra={
                "count": updated,
                "old_scope": old_scope,
                "new_scope": new_scope
            })
        return updated

    def get_knowledge_chunks_for_scope(self, scope: str, limit: int = 20) -> List[KnowledgeChunk]:
        """Get knowledge chunks for a scope (for spontaneous sharing selection)."""
        conn = self._connect()
        cursor = conn.execute(
            """
            SELECT id, scope, source, title, content, chunk_index, created_at
            FROM knowledge_chunks
            WHERE scope = ?
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (scope, limit)
        )
        return [
            KnowledgeChunk(
                id=row["id"],
                scope=row["scope"],
                source=row["source"],
                title=row["title"],
                content=row["content"],
                chunk_index=row["chunk_index"],
                created_at=row["created_at"],
            )
            for row in cursor.fetchall()
        ]

    def get_knowledge_sources(self) -> List[Dict[str, Any]]:
        """Get list of all knowledge sources with chunk counts and scopes."""
        conn = self._connect()

        cursor = conn.execute(
            """
            SELECT scope, source, COUNT(*) as chunk_count, MAX(created_at) as last_updated
            FROM knowledge_chunks
            GROUP BY scope, source
            ORDER BY scope, source
            """
        )

        return [
            {
                "scope": row["scope"],
                "source": row["source"],
                "chunk_count": row["chunk_count"],
                "last_updated": row["last_updated"],
            }
            for row in cursor.fetchall()
        ]

    def get_knowledge_as_context(
        self,
        query: str,
        max_tokens: int = 1000,
        scopes: Optional[List[str]] = None,
        min_similarity: float = 0.0,
        min_bm25: float = 0.0,
    ) -> str:
        """
        Search knowledge and format as context for LLM.

        Args:
            query: Search query
            max_tokens: Approximate max tokens (chars / 4)
            scopes: Allowed knowledge scopes (None = all)
            min_similarity: Minimum cosine similarity for semantic search (0.0 = no filter)
            min_bm25: Minimum BM25 rank for FTS fallback (0.0 = no filter, more negative = stricter)

        Returns:
            Formatted context string
        """
        semantic_chunks = self.search_knowledge_semantic(query, limit=10, scopes=scopes, min_score=min_similarity)
        fts_chunks = self.search_knowledge(query, limit=10, scopes=scopes, min_rank=min_bm25)

        # Merge: semantic first (higher quality), then FTS-only results not already included
        seen_ids = {c.id for c in semantic_chunks}
        chunks = list(semantic_chunks)
        for c in fts_chunks:
            if c.id not in seen_ids:
                chunks.append(c)
                seen_ids.add(c.id)

        if semantic_chunks:
            logger.debug("Knowledge: semantic results", extra={"count": len(semantic_chunks)})
        if fts_chunks:
            logger.debug("Knowledge: FTS results", extra={"count": len(fts_chunks)})
        if not chunks:
            return ""

        max_chars = max_tokens * 4  # Rough estimate
        lines = []
        total_chars = 0

        for chunk in chunks:
            chunk_text = f"\n[{chunk.title}]\n{chunk.content}"
            if total_chars + len(chunk_text) > max_chars:
                break
            lines.append(chunk_text)
            total_chars += len(chunk_text)

        if not lines:
            return ""
        return "Relevant knowledge:" + "\n".join(lines)

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
            logger.info("Cleaned up old messages", extra={"count": deleted})
        return deleted


    def record_quiet_sample(self, conversation_id: str, day_date: str, minutes: int) -> None:
        """Record or update today's last-inbound time (minutes since midnight)."""
        conn = self._connect()
        conn.execute(
            """
            INSERT INTO wind_quiet_samples (conversation_id, day_date, last_inbound_minutes, recorded_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(conversation_id, day_date) DO UPDATE SET
                last_inbound_minutes = excluded.last_inbound_minutes,
                recorded_at = excluded.recorded_at
            """,
            (conversation_id, day_date, minutes, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    def get_quiet_samples(self, conversation_id: str, limit: int = 14) -> list:
        """Return last N daily sign-off times (minutes since midnight), newest first."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT last_inbound_minutes FROM wind_quiet_samples
            WHERE conversation_id = ?
            ORDER BY day_date DESC
            LIMIT ?
            """,
            (conversation_id, limit),
        ).fetchall()
        return [row["last_inbound_minutes"] for row in rows]

    def purge_old_quiet_samples(self, keep_days: int = 60) -> None:
        """Delete quiet samples older than keep_days."""
        conn = self._connect()
        conn.execute(
            "DELETE FROM wind_quiet_samples WHERE day_date < date('now', ?)",
            (f"-{keep_days} days",),
        )
        conn.commit()


def create_memory_store() -> MemoryStore:
    """
    Factory function to create MemoryStore with settings from environment.

    Environment variables:
        JOI_MEMORY_DB: Path to database file (default: /var/lib/joi/memory.db)
        JOI_MEMORY_KEY_FILE: Path to encryption key file (required when DB is encrypted)

    The encryption key is loaded from the key file, not from environment variables,
    to avoid key leakage in logs or process listings.
    """
    db_path = os.getenv("JOI_MEMORY_DB", "/var/lib/joi/memory.db")

    # Key is loaded from file inside MemoryStore.__init__
    return MemoryStore(db_path)
