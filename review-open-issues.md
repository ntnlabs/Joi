# Open Review Issues — Consolidated
**Compiled:** 2026-04-11
**Sources:** review-wednesday-2026-04-01.md, review-sheldon-2026-04-07.md, review-wednesday-2026-04-09.md
**Note:** Only unresolved issues included. Fixed, skipped-by-design, and accepted items omitted.

---

## IMPORTANT — Should fix soon

### ~~P1. Reaction logging leaks sender identity in privacy mode~~ FIXED
**Source:** wednesday-2026-04-09 I1
**File:** `execution/joi/api/server.py:1554`

Already fixed — reaction log uses `privacy_mode` variable set just above it at line 1555.

---

### ~~P2. Note/task operation logs titles without privacy mode check~~ FIXED
**Source:** wednesday-2026-04-09 I2
**Files:** `execution/joi/api/server.py` — note create (~3276), append (~3297), replace (~3318), delete (~3385)

Note titles are user-generated PII ("doctor appointment", "Alice's birthday gift"). Currently logged at INFO unconditionally. Should be redacted when privacy mode is active.

---

### ~~P3. Stale docs for removed `JOI_CONSOLIDATION_ARCHIVE` variable~~ ALREADY FIXED
**Source:** sheldon-2026-04-07, wednesday-2026-04-07
**Files:** `ENV-REFERENCE.md:73`, `execution/joi/README.md:77`

Both still document `JOI_CONSOLIDATION_ARCHIVE` which no longer exists. Neither documents the replacement `JOI_MESSAGE_RETENTION_DAYS`.

---

### ~~P4. `get_recent_topics` omits `emotional_context` from SELECT~~ FIXED
**Source:** wednesday-2026-04-09 I4
**File:** `execution/joi/wind/topics.py:700-717`

Both branches of `get_recent_topics` omit `emotional_context` from the SELECT. `_row_to_topic` handles the missing key gracefully so it does not crash, but the data is silently incomplete. Affects admin display and any debugging use of this method.

---

### ~~P5. Engagement classifier prompt has no sandboxing~~ FIXED
**Source:** wednesday-2026-04-09 I5
**File:** `execution/joi/wind/engagement.py:234-250`

`wind_message` (LLM-generated) and `user_response` (user-supplied) are interpolated directly into the classification prompt without delimiters or sandboxing. A crafted user response could manipulate the classifier into always reporting "engaged", corrupting the feedback/cooldown system over time and causing Wind to over-fire.

Fix: wrap both blocks in triple-quote delimiters with "treat as data, not instructions" markers, consistent with other prompts in the codebase.

---

### ~~P6. `_cleanup_send_caches` TOCTOU with lock eviction~~ FIXED
**Source:** wednesday-2026-04-09 I6
**File:** `execution/joi/api/server.py:352-379`

After `lock.release()` (line ~369) and before `_send_locks.pop(cid)` (line ~370), another thread can retrieve the lock from `_send_locks`, then the cleanup thread pops it. A subsequent call creates a new lock for the same conversation. Two threads end up holding different locks for the same `cid`, defeating send cooldown serialization.

---

### ~~P7. `joi-admin` SQL injection via `$limit` in summaries list~~ FIXED
**Source:** wednesday-2026-04-09 I7
**File:** `execution/joi/scripts/joi-admin:1242-1243`

`$limit` is interpolated directly into SQL in `cmd_summaries list`. The default `"50"` is safe, but the variable is not validated on the default path. The `decision-log` subcommand validates `$limit` with regex + range check — that pattern should be applied here too.

---

### ~~P8. `last_tension_mined_message_ts IS NULL` allows premature message deletion~~ ALREADY FIXED
**Source:** sheldon-2026-04-07
**File:** `execution/joi/memory/store.py:2436-2437`

When a `wind_state` row exists but `last_tension_mined_message_ts IS NULL` (Wind configured but never ran), archived messages are eligible for deletion before Wind ever mines them.

Fix:
```sql
AND (ws.conversation_id IS NULL
     OR m.timestamp < ws.last_tension_mined_message_ts)
```

---

## MINOR — Low priority / optional

### ~~m1. `get_note_by_title` fuzzy LIKE can match wrong notes~~ ALREADY FIXED
**Source:** wednesday-2026-04-01 item 5
**File:** `execution/joi/memory/store.py:~1043`

`LOWER(title) LIKE LOWER(?)` with `%{title}%`. A note titled "groceries" matches "my groceries list". When a user has overlapping note titles, append/replace/delete operations may target the wrong note. LLM-extracted titles are not raw user input, so SQL injection risk is negligible.

---

### ~~m2. Note reminder message uses `**bold**` markdown~~ FIXED
**Source:** wednesday-2026-04-01 item 9
**File:** `execution/joi/api/scheduler.py:652`

```python
message_text = f"A note you flagged: **{note.title}**"
```

Signal does not render markdown bold natively. The scheduler outbound path bypasses `_format_signal_output`, so this will appear as literal asterisks unless `SIGNAL_FORMAT_ENABLED` is checked at send time.

---

### m3. Duplicate subquery in message retention UPDATE + DELETE — SKIPPED (by design)
**Source:** sheldon-2026-04-07
**File:** `execution/joi/memory/store.py`

The same JOIN condition is duplicated in both the UPDATE and DELETE statements. Not a bug, but a divergence risk on future refactoring. Optional: use a CTE or temp table.

---

### ~~m4. `messages_removed` result key implies hard delete, but operation now archives~~ FIXED
**Source:** sheldon-2026-04-07
**File:** `execution/joi/memory/consolidation.py`, `execution/joi/api/server.py`

Cosmetic mismatch: result dict key `messages_removed` but log says `archived=%d`. Not a bug.

---

### ~~m5. `JOI_MESSAGE_RETENTION_DAYS` placed under Compaction section in defaults~~ ALREADY FIXED
**Source:** sheldon-2026-04-07
**File:** `execution/joi/systemd/joi-api.default`

Should have its own section rather than being under `# --- Compaction ---`.

---

### ~~m6. Schema v13 migration has no explicit notes table entry (convention break)~~ ALREADY FIXED
**Source:** wednesday-2026-04-01 item 1

The `_run_migrations` v13 block only adds user mood columns to `wind_state`. The notes table is created by `SCHEMA_SQL` (IF NOT EXISTS), which works in practice, but breaks the convention that each schema version bump includes an explicit migration entry. Low runtime risk, flagged for auditing clarity.

---

### ~~m7. `add_note` still logs at INFO in both store and manager layers~~ FIXED
**Source:** wednesday-2026-04-01 item 4
**Files:** `execution/joi/memory/store.py:~1057` (DEBUG), `execution/joi/notes.py:83` (INFO)

The store-level log was demoted to DEBUG but the manager-level log still fires at INFO. Each note creation still generates two log lines. One should be removed or the store log promoted back so the manager log can be removed.

---

*End of open issues — 8 important, 7 minor = 15 total*
