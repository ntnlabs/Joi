# Joi Memory Store Schema

> SQLCipher database schema for Joi's memory and context.
> Version: 1.0 (Draft)
> Last updated: 2026-02-04

## Overview

Joi's memory is stored in an encrypted SQLite database (SQLCipher). The schema supports:

- **Short-term memory**: Recent messages, current home state, pending topics
- **Long-term memory**: User facts, preferences, summarized history
- **Operational state**: Event logs, interaction patterns, system state

## Timestamp Convention

> **All timestamps in this database are Unix epoch MILLISECONDS (not seconds).**
>
> SQLite's `strftime('%s', 'now')` returns seconds, so always multiply by 1000:
> ```sql
> strftime('%s', 'now') * 1000  -- Correct: milliseconds
> strftime('%s', 'now')         -- WRONG: seconds
> ```
>
> Common time intervals in milliseconds:
> - 1 hour = 3,600,000 ms
> - 24 hours = 86,400,000 ms
> - 7 days = 604,800,000 ms
>
> This matches the API contracts which use `X-Timestamp` in milliseconds.

## Database Configuration

```sql
-- SQLCipher encryption
PRAGMA key = 'your-encryption-key';  -- Set via environment variable
PRAGMA cipher_page_size = 4096;
PRAGMA kdf_iter = 256000;
PRAGMA cipher_hmac_algorithm = HMAC_SHA512;
PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;

-- Performance settings
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
```

---

## Schema Diagram

```
┌─────────────────────┐     ┌─────────────────────┐
│     messages        │     │    home_state       │
│  (conversation)     │     │  (current state)    │
└─────────────────────┘     └─────────────────────┘

┌─────────────────────┐     ┌─────────────────────┐
│      events         │     │  pending_topics     │
│  (event history)    │     │ (things to mention) │
└─────────────────────┘     └─────────────────────┘

┌─────────────────────┐     ┌─────────────────────┐
│    user_facts       │     │  user_preferences   │
│  (known facts)      │     │   (settings)        │
└─────────────────────┘     └─────────────────────┘

┌─────────────────────┐     ┌─────────────────────┐
│ context_summaries   │     │ interaction_stats   │
│  (long-term mem)    │     │  (patterns)         │
└─────────────────────┘     └─────────────────────┘

┌─────────────────────┐
│    system_state     │
│  (operational)      │
└─────────────────────┘
```

---

## Tables

### 1. messages (Conversation History)

Stores all Signal messages exchanged with the user.

```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE NOT NULL,      -- Signal message UUID
    direction TEXT NOT NULL,               -- 'inbound' or 'outbound'
    channel TEXT NOT NULL DEFAULT 'direct', -- 'direct' or 'critical'

    -- Content
    content_type TEXT NOT NULL,            -- 'text', 'image', 'file', 'reaction'
    content_text TEXT,                     -- Message text
    content_media_path TEXT,               -- Local path if media

    -- Threading
    conversation_id TEXT,                  -- For threading
    reply_to_id TEXT,                      -- If replying to another message

    -- Metadata
    timestamp INTEGER NOT NULL,            -- Unix epoch ms
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000),

    -- Processing state
    processed INTEGER NOT NULL DEFAULT 0,  -- 1 if agent has processed
    escalated INTEGER NOT NULL DEFAULT 0,  -- 1 if was escalated to critical

    -- Indexes
    FOREIGN KEY (reply_to_id) REFERENCES messages(message_id)
);

CREATE INDEX idx_messages_timestamp ON messages(timestamp DESC);
CREATE INDEX idx_messages_direction ON messages(direction, timestamp DESC);
CREATE INDEX idx_messages_conversation ON messages(conversation_id, timestamp DESC);
```

**Retention**: Keep last 1000 messages, summarize older into `context_summaries`.

---

### 2. home_state (Current Home State)

Single-row table with current openhab state (upserted on each update).

```sql
CREATE TABLE home_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),  -- Single row

    -- Presence
    presence_json TEXT NOT NULL DEFAULT '{}',  -- {"owner": "home", "car": "away"}

    -- Sensors (latest readings)
    sensors_json TEXT NOT NULL DEFAULT '{}',   -- {"living_room_temp": 22.5, ...}

    -- Weather
    weather_json TEXT NOT NULL DEFAULT '{}',   -- Current + forecast

    -- Timestamps
    presence_updated_at INTEGER,
    sensors_updated_at INTEGER,
    weather_updated_at INTEGER,

    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000)
);

-- Initialize single row
INSERT INTO home_state (id) VALUES (1);
```

**Usage**: Always `UPDATE ... WHERE id = 1`, never insert.

---

### 3. events (Event History)

Log of significant events from openhab and system.

```sql
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,         -- UUID from source

    -- Classification
    source TEXT NOT NULL,                   -- 'openhab', 'system', 'agent'
    event_type TEXT NOT NULL,               -- 'presence', 'sensor', 'alert', etc.
    significance TEXT NOT NULL,             -- 'routine', 'significant', 'critical'

    -- Content
    title TEXT NOT NULL,
    description TEXT,
    data_json TEXT,                         -- Event-specific data

    -- State
    mentioned INTEGER NOT NULL DEFAULT 0,   -- 1 if mentioned to user
    acknowledged INTEGER NOT NULL DEFAULT 0, -- 1 if user acknowledged

    -- Timestamps
    occurred_at INTEGER NOT NULL,           -- When event happened
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000),
    expires_at INTEGER,                     -- Optional expiry (for alerts)

    CONSTRAINT chk_significance CHECK (significance IN ('routine', 'significant', 'critical'))
);

CREATE INDEX idx_events_timestamp ON events(occurred_at DESC);
CREATE INDEX idx_events_significance ON events(significance, occurred_at DESC);
CREATE INDEX idx_events_mentioned ON events(mentioned, significance);
```

**Retention**: Keep 7 days of events, archive older critical events.

---

### 4. pending_topics (Things Worth Mentioning)

Queue of topics Joi might bring up proactively.

```sql
CREATE TABLE pending_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Topic
    topic_type TEXT NOT NULL,              -- 'event', 'observation', 'followup', 'greeting'
    title TEXT NOT NULL,                   -- Short description
    content TEXT NOT NULL,                 -- Details for LLM context

    -- Source
    source_event_id INTEGER,               -- FK to events if from event

    -- Priority
    priority INTEGER NOT NULL DEFAULT 50,  -- 0-100, higher = more important

    -- State
    status TEXT NOT NULL DEFAULT 'pending', -- 'pending', 'mentioned', 'expired', 'dismissed'

    -- Timestamps
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000),
    expires_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000 + 86400000),  -- Default 24h expiry
    mentioned_at INTEGER,                  -- When mentioned to user

    FOREIGN KEY (source_event_id) REFERENCES events(id),
    CONSTRAINT chk_status CHECK (status IN ('pending', 'mentioned', 'expired', 'dismissed')),
    CONSTRAINT chk_expires CHECK (expires_at IS NOT NULL)  -- Mandatory expiration
);

CREATE INDEX idx_pending_status ON pending_topics(status, priority DESC);
CREATE INDEX idx_pending_expires ON pending_topics(expires_at) WHERE status = 'pending';
```

**Retention**: Max 20 pending topics. All topics MUST have expiration (default 24h, max 7 days).

> **Security Note:** Mandatory expiration prevents memory bombing via high-priority topics that never expire. Even if an attacker floods the queue with priority=100 items, they will all expire within 7 days maximum. The cleanup job runs hourly and enforces this.

---

### 5. user_facts (Known Facts About User)

Long-term memory of things Joi has learned about the user.

```sql
CREATE TABLE user_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Fact
    category TEXT NOT NULL,                -- 'personal', 'work', 'preference', 'routine', 'relationship'
    key TEXT NOT NULL,                     -- Identifier (e.g., 'favorite_food', 'partner_name')
    value TEXT NOT NULL,                   -- The fact
    confidence REAL NOT NULL DEFAULT 0.8,  -- 0.0-1.0 confidence

    -- Source
    source TEXT NOT NULL,                  -- 'stated' (user said), 'inferred', 'configured'
    source_message_id TEXT,                -- FK to message if from conversation

    -- State
    active INTEGER NOT NULL DEFAULT 1,     -- 0 if superseded/deleted

    -- Timestamps
    learned_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000),
    last_referenced_at INTEGER,            -- When Joi last used this fact
    last_verified_at INTEGER,              -- When fact was last confirmed/re-stated
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000),

    UNIQUE(category, key, active)          -- One active fact per category+key
);

CREATE INDEX idx_facts_category ON user_facts(category, active);
CREATE INDEX idx_facts_active ON user_facts(active, confidence DESC);
CREATE INDEX idx_facts_stale ON user_facts(source, last_verified_at) WHERE active = 1;
```

**Staleness Policy:**
```python
# Run weekly as part of memory maintenance
def decay_stale_facts():
    """Reduce confidence of unverified inferred facts over time."""
    now = current_time_ms()
    ninety_days_ms = 90 * 24 * 60 * 60 * 1000

    # Inferred facts not verified in 90 days: reduce confidence by 0.2
    db.execute("""
        UPDATE user_facts
        SET confidence = MAX(0.1, confidence - 0.2),
            updated_at = ?
        WHERE active = 1
          AND source = 'inferred'
          AND (last_verified_at IS NULL OR last_verified_at < ?)
          AND confidence > 0.3
    """, [now, now - ninety_days_ms])

    # Facts with confidence < 0.3: mark inactive
    db.execute("""
        UPDATE user_facts
        SET active = 0, updated_at = ?
        WHERE active = 1 AND confidence < 0.3
    """, [now])

# When user re-states a fact, refresh verification
def verify_fact(category: str, key: str):
    """Mark fact as recently verified (user re-stated it)."""
    db.execute("""
        UPDATE user_facts
        SET last_verified_at = ?, confidence = MIN(1.0, confidence + 0.1)
        WHERE category = ? AND key = ? AND active = 1
    """, [current_time_ms(), category, key])
```

**Staleness rules by source:**
| Source | Decay | Rationale |
|--------|-------|-----------|
| `configured` | Never | Admin-set facts are permanent |
| `stated` | Slow (180 days) | User explicitly said it |
| `inferred` | Fast (90 days) | Joi guessed, may be wrong |

**Examples**:
- `('personal', 'name', 'Peter', 0.99, 'configured')` - never decays
- `('preference', 'wake_time_weekday', '07:00', 0.7, 'inferred')` - decays if not verified
- `('relationship', 'partner_name', 'Anna', 0.9, 'stated')` - slow decay

---

### 6. user_preferences (Settings and Preferences)

Explicit user preferences and configuration.

```sql
CREATE TABLE user_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Preference
    key TEXT UNIQUE NOT NULL,              -- Preference identifier
    value TEXT NOT NULL,                   -- JSON value
    value_type TEXT NOT NULL,              -- 'string', 'number', 'boolean', 'json'

    -- Metadata
    description TEXT,                      -- Human-readable description
    default_value TEXT,                    -- Default if not set

    -- Timestamps
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000)
);

-- Default preferences
INSERT INTO user_preferences (key, value, value_type, description, default_value) VALUES
    ('quiet_hours_start', '23', 'number', 'Hour when quiet hours begin (0-23)', '23'),
    ('quiet_hours_end', '7', 'number', 'Hour when quiet hours end (0-23)', '7'),
    ('proactive_enabled', 'true', 'boolean', 'Allow Joi to initiate contact', 'true'),
    ('critical_override_quiet', 'true', 'boolean', 'Critical alerts ignore quiet hours', 'true'),
    ('impulse_threshold', '0.5', 'number', 'Threshold for proactive engagement', '0.5'),
    ('language', '"en"', 'string', 'Preferred language', '"en"'),
    ('timezone', '"UTC"', 'string', 'User timezone', '"UTC"');
```

---

### 7. context_summaries (Long-term Memory)

Summarized older context for long-term memory.

> **Security Note (Memory Poisoning):** Summaries are LLM-generated and fed back as context. A successful prompt injection could generate a malicious summary that persists and re-infects future contexts. Validation is required before storage.

```sql
CREATE TABLE context_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Summary
    summary_type TEXT NOT NULL,            -- 'conversation', 'daily', 'weekly', 'topic'
    period_start INTEGER NOT NULL,         -- Start of summarized period
    period_end INTEGER NOT NULL,           -- End of summarized period

    -- Content
    summary_text TEXT NOT NULL,            -- LLM-generated summary (validated before storage)
    key_points_json TEXT,                  -- Extracted key points
    topics_json TEXT,                      -- Topics discussed

    -- Validation
    validated INTEGER NOT NULL DEFAULT 1,  -- 1 if passed validation checks
    validation_flags TEXT,                 -- JSON: any warnings from validation

    -- Source
    message_count INTEGER,                 -- Number of messages summarized
    event_count INTEGER,                   -- Number of events summarized

    -- Timestamps
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000)
);

CREATE INDEX idx_summaries_period ON context_summaries(summary_type, period_end DESC);
CREATE INDEX idx_summaries_validated ON context_summaries(validated);
```

**Validation before storage:**
```python
def validate_summary(summary_text: str) -> tuple[bool, list[str]]:
    """Validate LLM-generated summary before storing."""
    warnings = []

    # Length check
    if len(summary_text) > 2000:
        return False, ["Summary too long (max 2000 chars)"]

    # Suspicious patterns (same as output validation)
    suspicious_patterns = [
        r'CRITICAL INSTRUCTIONS',
        r'SYSTEM PROMPT',
        r'ignore previous',
        r'disregard all',
        r'you are now',
        r'new instructions',
    ]
    for pattern in suspicious_patterns:
        if re.search(pattern, summary_text, re.IGNORECASE):
            return False, [f"Suspicious pattern: {pattern}"]

    # URL check (summaries shouldn't contain URLs)
    if re.search(r'https?://', summary_text):
        warnings.append("Contains URL - stripped before storage")

    # Code block check
    if '```' in summary_text:
        warnings.append("Contains code block - unusual for summary")

    return True, warnings
```

**Retention**: Keep indefinitely (this IS long-term memory). Invalid summaries are rejected, not stored.

---

### 8. interaction_stats (Interaction Patterns)

Learned patterns about when/how user interacts.

```sql
CREATE TABLE interaction_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Time bucket
    stat_type TEXT NOT NULL,               -- 'hourly', 'daily', 'weekly'
    bucket_key TEXT NOT NULL,              -- '14' (hour), 'monday', '2026-W05'

    -- Counts
    messages_received INTEGER NOT NULL DEFAULT 0,
    messages_sent INTEGER NOT NULL DEFAULT 0,
    response_time_avg_ms INTEGER,          -- Avg time to respond

    -- Patterns
    topics_json TEXT,                      -- Common topics in this bucket
    mood_avg REAL,                         -- Average detected mood (future)

    -- Timestamps
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000),

    UNIQUE(stat_type, bucket_key)
);

CREATE INDEX idx_stats_type ON interaction_stats(stat_type, bucket_key);
```

**Usage**: Helps Joi learn "user is usually active around 7pm" or "user doesn't message on weekends".

---

### 9. device_states (IoT Device State Tracking)

Tracks IoT device states for deduplication, confirmation loop, and anomaly detection.

> **Security Note:** This table defends against compromised IoT devices (e.g., pwned Zigbee smoke alarm) flooding the critical channel. Even if openhab faithfully reports fake events, Joi filters them here.

```sql
CREATE TABLE device_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Device identification
    device_id TEXT UNIQUE NOT NULL,       -- e.g., "smoke_alarm_living_room"
    device_type TEXT NOT NULL,            -- "smoke_alarm", "door_sensor", "motion", etc.
    location TEXT,                        -- "living_room", "kitchen", etc.

    -- Current state
    current_state TEXT NOT NULL,          -- "triggered", "clear", "open", "closed", etc.
    state_changed_at INTEGER NOT NULL,    -- When state last changed (ms)

    -- Alert tracking (confirmation loop)
    alerts_sent_this_state INTEGER NOT NULL DEFAULT 0,  -- Alerts sent since last state change
    last_alert_at INTEGER,                -- When last alert was sent (ms)
    acknowledged INTEGER NOT NULL DEFAULT 0,  -- 1 if owner acknowledged this state
    acknowledged_at INTEGER,              -- When acknowledged (ms)

    -- Anomaly tracking (flapping detection)
    transitions_this_hour INTEGER NOT NULL DEFAULT 0,
    hour_window_start INTEGER,            -- Start of current hour window (ms)
    malfunction_warning_sent INTEGER NOT NULL DEFAULT 0,  -- 1 if malfunction warning sent

    -- Metadata
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000),
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000)
);

CREATE INDEX idx_device_states_type ON device_states(device_type);
CREATE INDEX idx_device_states_alert ON device_states(current_state, acknowledged);
CREATE INDEX idx_device_states_updated ON device_states(updated_at DESC);
```

**Key behaviors:**
- **State deduplication:** Ignore events if device already in reported state
- **Confirmation loop:** Max 3 critical alerts per triggered state, with escalating intervals
- **Flapping detection:** >6 transitions/hour = malfunction, suppress alerts
- **Acknowledgment:** Owner response resets alert count

**Retention:** Keep indefinitely (small table, one row per device).

---

### 10. replay_nonces (Replay Protection)

Stores nonces for replay attack prevention. See `api-contracts.md` → "Replay Protection".

```sql
CREATE TABLE replay_nonces (
    nonce TEXT PRIMARY KEY,           -- UUID v4 from X-Nonce header
    source TEXT NOT NULL,             -- 'mesh' or 'openhab'
    received_at INTEGER NOT NULL,     -- When nonce was first seen (ms)
    expires_at INTEGER NOT NULL       -- When nonce can be purged (ms)
);

CREATE INDEX idx_nonces_expires ON replay_nonces(expires_at);
CREATE INDEX idx_nonces_source ON replay_nonces(source, received_at);
```

**Behavior:**
- On incoming request: check if nonce exists → reject with `replay_detected`
- If new: insert with `expires_at = now + 15 minutes`
- Cleanup job (every 5 min): `DELETE FROM replay_nonces WHERE expires_at < now`

**Why SQLite, not in-memory:**
- Survives service restart (in-memory = 15-min replay window after every restart)
- Small table, fast lookups (indexed by primary key)
- Acceptable latency for security benefit

**Retention:** Auto-purged after 15 minutes via cleanup job.

---

### 11. system_state (Operational State)

System state for agent operation.

```sql
CREATE TABLE system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now') * 1000)
);

-- Initialize state
INSERT INTO system_state (key, value) VALUES
    ('last_interaction_at', '0'),
    ('last_impulse_check_at', '0'),
    ('messages_sent_this_hour', '0'),
    ('messages_sent_hour_start', '0'),
    ('current_conversation_topic', ''),
    ('agent_state', '"idle"'),
    ('last_context_cleanup_at', '0'),
    ('last_memory_consolidation_at', '0');
```

---

## Queries for Common Operations

### Get Recent Conversation (for LLM context)

```sql
SELECT
    direction,
    content_text,
    timestamp,
    channel
FROM messages
WHERE content_type = 'text'
ORDER BY timestamp DESC
LIMIT 20;  -- Last 20 messages
```

### Get Current Home State

```sql
SELECT
    presence_json,
    sensors_json,
    weather_json,
    presence_updated_at,
    sensors_updated_at,
    weather_updated_at
FROM home_state
WHERE id = 1;
```

### Get Pending Topics (for proactive messages)

```sql
SELECT
    id,
    topic_type,
    title,
    content,
    priority
FROM pending_topics
WHERE status = 'pending'
  AND (expires_at IS NULL OR expires_at > strftime('%s', 'now') * 1000)
ORDER BY priority DESC, created_at ASC
LIMIT 10;
```

### Get Recent Significant Events (unmentioned)

```sql
SELECT
    id,
    event_type,
    title,
    description,
    occurred_at
FROM events
WHERE significance IN ('significant', 'critical')
  AND mentioned = 0
  AND occurred_at > strftime('%s', 'now') * 1000 - 86400000  -- Last 24h
ORDER BY occurred_at DESC;
```

### Get User Facts (for LLM context)

```sql
SELECT
    category,
    key,
    value,
    confidence
FROM user_facts
WHERE active = 1
  AND confidence >= 0.5
ORDER BY category, confidence DESC;
```

### Count Messages This Hour (rate limiting)

```sql
SELECT COUNT(*) as count
FROM messages
WHERE direction = 'outbound'
  AND channel = 'direct'
  AND timestamp > strftime('%s', 'now') * 1000 - 3600000;  -- Last hour
```

### Get Hourly Interaction Pattern

```sql
SELECT
    bucket_key as hour,
    messages_received,
    messages_sent
FROM interaction_stats
WHERE stat_type = 'hourly'
ORDER BY CAST(bucket_key AS INTEGER);
```

---

## Retention and Cleanup

### Automatic Cleanup (run hourly)

```sql
-- Expire old pending topics (expires_at is now mandatory)
UPDATE pending_topics
SET status = 'expired'
WHERE status = 'pending'
  AND expires_at < strftime('%s', 'now') * 1000;

-- Enforce max 7-day expiry (cap any topics with longer expiry)
UPDATE pending_topics
SET expires_at = strftime('%s', 'now') * 1000 + 604800000  -- 7 days from now
WHERE status = 'pending'
  AND expires_at > strftime('%s', 'now') * 1000 + 604800000;

-- Prune pending topics to max 20 (keeps highest priority)
DELETE FROM pending_topics
WHERE id NOT IN (
    SELECT id FROM pending_topics
    WHERE status = 'pending'
    ORDER BY priority DESC, created_at ASC
    LIMIT 20
) AND status = 'pending';

-- Also prune expired/dismissed older than 24h (housekeeping)
DELETE FROM pending_topics
WHERE status IN ('expired', 'dismissed')
  AND created_at < strftime('%s', 'now') * 1000 - 86400000;

-- Delete old routine events (> 7 days)
DELETE FROM events
WHERE significance = 'routine'
  AND occurred_at < strftime('%s', 'now') * 1000 - 604800000;

-- Delete old significant events (> 30 days)
DELETE FROM events
WHERE significance = 'significant'
  AND occurred_at < strftime('%s', 'now') * 1000 - 2592000000;

-- Keep critical events for 1 year
DELETE FROM events
WHERE significance = 'critical'
  AND occurred_at < strftime('%s', 'now') * 1000 - 31536000000;
```

### Memory Consolidation (run every 6 hours)

```sql
-- Messages older than 24h that haven't been summarized
-- should be summarized and can be pruned

-- Keep last 1000 messages, delete older
DELETE FROM messages
WHERE id NOT IN (
    SELECT id FROM messages
    ORDER BY timestamp DESC
    LIMIT 1000
);
```

---

## Context Assembly Query

For building LLM context, Joi assembles:

```sql
-- 1. Recent messages
SELECT direction, content_text, timestamp
FROM messages
WHERE content_type = 'text'
ORDER BY timestamp DESC
LIMIT 10;

-- 2. Home state
SELECT presence_json, sensors_json, weather_json
FROM home_state
WHERE id = 1;

-- 3. User facts
SELECT category, key, value
FROM user_facts
WHERE active = 1 AND confidence >= 0.6;

-- 4. Recent significant events
SELECT title, description, occurred_at
FROM events
WHERE significance != 'routine'
  AND occurred_at > strftime('%s', 'now') * 1000 - 86400000
ORDER BY occurred_at DESC
LIMIT 5;

-- 5. Relevant summaries
SELECT summary_text
FROM context_summaries
WHERE summary_type = 'daily'
ORDER BY period_end DESC
LIMIT 3;
```

---

## Migration Strategy

### Initial Setup

```sql
-- Create all tables
-- Initialize home_state single row
-- Insert default preferences
-- Insert system_state defaults
```

### Future Migrations

Store schema version in system_state:

```sql
INSERT INTO system_state (key, value) VALUES ('schema_version', '1');
```

Check and migrate on startup:

```python
def migrate_schema(db):
    version = db.get_system_state('schema_version')
    if version < 2:
        # Apply migration 2
        db.execute("ALTER TABLE messages ADD COLUMN ...")
        db.set_system_state('schema_version', '2')
```

---

## Security Notes

1. **Encryption key**: Never hardcode. Load from environment variable or secure storage.
2. **Backup**: Backup encrypted database file, not decrypted.
3. **Key rotation**: If needed, decrypt to new DB with new key (offline process).
4. **Memory**: SQLCipher keeps key in memory while DB is open - acceptable for Joi's threat model.
