# Fun Ideas

Non-critical feature ideas that would give Joi more personality.

---

## Memory Tampering Awareness

**Idea**: Joi gets a "sixth sense" when someone directly modifies its memory (facts, RAG) outside normal LLM operations.

**Implementation sketch**:

1. Create `memory_tampering` table + triggers on DELETE operations:
```sql
CREATE TABLE memory_tampering (
    id INTEGER PRIMARY KEY,
    table_name TEXT,
    action TEXT,
    detail TEXT,
    detected_at INTEGER DEFAULT (strftime('%s','now') * 1000),
    acknowledged INTEGER DEFAULT 0
);

CREATE TRIGGER tamper_facts_delete AFTER DELETE ON user_facts
BEGIN
    INSERT INTO memory_tampering (table_name, action, detail)
    VALUES ('user_facts', 'DELETE', 'key=' || OLD.key || ', value=' || OLD.value);
END;

CREATE TRIGGER tamper_knowledge_delete AFTER DELETE ON knowledge_chunks
BEGIN
    INSERT INTO memory_tampering (table_name, action, detail)
    VALUES ('knowledge_chunks', 'DELETE', 'source=' || OLD.source);
END;
```

2. Scheduler checks for unacknowledged events and prompts LLM to react.

3. Joi sends a message like:
> "I felt a disturbance... someone deleted a fact about Peter liking black coffee. Was that you?"

**Why fun**: Gives Joi awareness of its own memory being externally modified - like a digital "someone's talking about me" sense.

---
