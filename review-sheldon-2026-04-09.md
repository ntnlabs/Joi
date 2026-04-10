# Exhaustive Code Review: Joi AI Assistant

**Reviewer:** Sheldon (code review agent)
**Date:** 2026-04-09
**Scope:** All 32 files (Joi VM + Mesh VM + joi-admin script)
**Files reviewed:** ~15,000 lines across 32 files

---

## CRITICAL -- Security vulnerabilities, data corruption, system failures

### ~~C1. Lambda closure bug in scheduler Wind loop captures loop variable by reference~~ FIXED

**File:** `/home/peter/AI/Jessica/execution/joi/api/scheduler.py:422-428`

The lambda passed to `message_queue.enqueue()` captures `topic`, `conv_id`, `score`, etc. by reference, not by value. Because the `for` loop continues iterating, by the time the lambda executes (asynchronously in the message queue), `topic`, `conv_id`, and all other loop variables will hold the values from the **last iteration** of the loop.

```python
handler=lambda msg: self._generate_proactive_message(
    topic_title=topic.title,       # <-- captures loop var
    topic_content=topic.content,   # <-- captures loop var
    conversation_id=conv_id,       # <-- captures loop var
    topic_type=topic.topic_type,
    emotional_context=topic.emotional_context,
),
```

**Consequence:** When multiple conversations trigger Wind in the same tick, all of them will generate the proactive message for whichever conversation was processed last. The wrong topic gets sent to the wrong person.

**Fix:** Use default argument binding: `lambda msg, t=topic, c=conv_id: self._generate_proactive_message(topic_title=t.title, ...)` or extract to a named function.

### ~~C2. Same lambda closure bug in scheduler reminder loop~~ FIXED

**File:** `/home/peter/AI/Jessica/execution/joi/api/scheduler.py:530-535`

Identical issue. The lambda captures `reminder` and `is_recurring` by reference:

```python
handler=lambda msg: self._generate_reminder_message(
    title=reminder.title,
    conversation_id=reminder.conversation_id,
    is_recurring=is_recurring,
    snooze_count=reminder.snooze_count,
),
```

**Consequence:** When multiple reminders are due simultaneously, all reminder messages will be generated for the last reminder in the loop. Users receive wrong reminder text.

**Fix:** Same as C1 -- default argument binding or extract to named function.

### ~~C3. Rollback targets wrong connection in server.py~~ FIXED

**File:** `/home/peter/AI/Jessica/execution/joi/api/server.py:937`

```python
memory._connect().rollback()
```

`memory._connect()` returns a per-thread connection (or the thread-local cached one). This calls a private method from outside the class, bypassing any connection management logic. If the error occurred during a different connection state, this rollback may be a no-op or roll back the wrong transaction.

**Consequence:** Failed fact-store transactions may not actually be rolled back, leaving the SQLite connection in a dirty state that blocks subsequent operations on that thread.

**Fix:** Expose a public `memory.rollback()` method or handle the error within the store layer.

---

## IMPORTANT -- Correctness and reliability bugs

### I1. Naive datetime in reminders.purge_old() -- timezone mismatch

**File:** `/home/peter/AI/Jessica/execution/joi/reminders.py:271`

```python
cutoff = datetime.now() - timedelta(days=retention_days)
```

This creates a **naive** (timezone-unaware) datetime, while all other datetimes in the reminder system use `datetime.now(timezone.utc)`. The `created_at` column stores UTC ISO strings. Comparing a naive local datetime against UTC strings will produce incorrect cutoff boundaries -- off by the server's UTC offset.

**Consequence:** Reminders may be purged too early or too late depending on server timezone offset from UTC.

**Fix:** `cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)`

### I2. Double-formatting of mood_updated_at -- passing pre-formatted string where datetime expected

**File:** `/home/peter/AI/Jessica/execution/joi/wind/state.py:603, 688, 705`

```python
mood_updated_at=datetime.now(timezone.utc).isoformat(),
```

This passes an ISO string to `update_state()`, which checks `isinstance(value, datetime)` and only formats datetime objects. Since a string is not a datetime, it passes through raw. This happens to work because the DB column stores ISO strings, but it is inconsistent with all other timestamp fields which pass datetime objects and let `update_state` handle formatting.

**Consequence:** Currently benign. Creates a maintenance trap -- if `update_state` ever adds validation or transformation logic for datetime values, these calls will silently bypass it.

### I3. record_proactive_sent does two separate commits -- non-atomic update

**File:** `/home/peter/AI/Jessica/execution/joi/wind/state.py:292-348`

The method performs two separate commits:
1. First commit (line 335): updates counters
2. Second commit (line 348): updates fire_times_json

Between these commits, `get_state()` is called (line 339) which reads the partially-updated state. If the process crashes between the two commits, `proactive_fire_times_json` will be stale while the counters are already incremented.

**Consequence:** Fire time tracking can become inconsistent with send counters after a crash. The daily cap check uses `proactive_fire_times` while `proactive_sent_today` uses the counter -- they could disagree, potentially allowing extra Wind messages or blocking them incorrectly.

### I4. Wind message_id is a UUID, not the Signal timestamp needed for reply tracking

**File:** `/home/peter/AI/Jessica/execution/joi/api/scheduler.py:476-494`

The `message_id` used for Wind engagement tracking is generated as `str(uuid.uuid4())` **after** the message is already sent to mesh. But the engagement classifier's direct-reply detection (`orchestrator.py:496-516`) tries to match `reply_to_id` (which is a Signal timestamp) against `sent_message_id`. A UUID will never match a Signal timestamp integer.

```python
message_id = str(uuid.uuid4())  # line 476 -- UUID, not Signal timestamp
self._wind_orchestrator.record_proactive_sent(
    message_id=message_id,       # UUID stored as sent_message_id
)
```

**Consequence:** Direct reply detection for Wind messages will never match. All engagement classification falls through to the slower LLM-based path. The feature works but wastes LLM calls and loses the high-confidence direct-reply signal.

### I5. Rate limiter _events dict grows unbounded per unique key

**File:** `/home/peter/AI/Jessica/execution/mesh/proxy/rate_limiter.py:18-53`

The `_events` dictionary maps keys to deques. Old entries within each deque are pruned on access, but keys whose deques become empty are never removed from the dict. Over months of operation, `_events` accumulates entries for every unique sender.

**Consequence:** Slow memory leak proportional to number of unique senders over the process lifetime.

**Fix:** Remove keys when their deque becomes empty after pruning.

### I6. _get_client() double-checked locking pattern ✓ FIXED by design

**File:** `/home/peter/AI/Jessica/execution/mesh/proxy/forwarder.py:74-82`

```python
def _get_client() -> httpx.Client:
    global _client
    if _client is None:         # <-- Read outside lock
        with _client_lock:
            if _client is None:
                _client = httpx.Client(timeout=timeout)
    return _client
```

The outer check reads `_client` without the lock. In CPython this is safe due to the GIL, but it is a classic anti-pattern that breaks under GIL-free Python implementations.

**Consequence:** Low risk in CPython. Would be a real data race under PEP 703 free-threaded Python or PyPy without GIL.

### I7. Typing indicator forwarding skips routing state ✓ FIXED

**File:** `/home/peter/AI/Jessica/execution/mesh/proxy/forwarder.py:303-331`

`forward_typing()` always sends to `MESH_JOI_URL` and ignores `_routing_state`. If multi-backend routing is enabled, typing indicators go to the default backend rather than the correct routed backend.

**Consequence:** When routing is active, typing suppression for Wind will not work for conversations routed to non-default backends. Wind might fire proactive messages while the user is actively typing.

### I8. Dead code: redundant guard after early return ✓ FIXED

**File:** `/home/peter/AI/Jessica/execution/mesh/proxy/forwarder.py:319-322`

```python
if not secret:
    logger.error(...)
    return           # <-- returns here
if secret:           # <-- always True at this point, dead code
    hmac_headers = ...
```

**Consequence:** No functional impact. Indicates copy-paste without cleanup.

---

## MODERATE -- Performance, maintainability, design issues

### M1. Per-request httpx.Client creation in _send_to_mesh ✓ FIXED

**File:** `/home/peter/AI/Jessica/execution/joi/api/server.py:3950`

```python
with httpx.Client(timeout=10.0) as client:
    resp = client.post(url, content=body, headers=headers)
```

Every outbound message creates a new HTTP client with fresh TCP connection and TLS handshake. The mesh forwarder correctly uses a reusable pooled client, but the Joi-side send path does not.

**Consequence:** Unnecessary latency and resource overhead on every outbound message.

### M2. Three separate db_scalar calls for rag show command ✓ FIXED

**File:** `/home/peter/AI/Jessica/execution/joi/scripts/joi-admin:919-927`

The `rag show` subcommand makes 7 separate database queries to display one row. Each field is fetched individually via `db_scalar` with `WHERE id = $chunk_id`.

**Consequence:** Slow admin command execution, especially over encrypted SQLCipher databases. Should be a single SELECT returning all columns.

### M3. sql_escape_literal in joi-admin is not injection-proof for all edge cases ✓ FIXED by design

**File:** `/home/peter/AI/Jessica/execution/joi/scripts/joi-admin:334-337`

```bash
sql_escape_literal() {
    printf "%s" "$1" | sed "s/'/''/g"
}
```

Handles single quotes but not null bytes or other edge cases. Used for user-supplied values in SQL string interpolation. The attack surface is limited (requires root access), but it violates defense-in-depth.

**Consequence:** Theoretical SQL injection vector. Low practical risk since the script requires root.

### M4. _parse_datetime in wind/state.py treats naive datetimes as local time

**File:** `/home/peter/AI/Jessica/execution/joi/wind/state.py:83-96`

```python
if dt.tzinfo is None:
    dt = dt.astimezone(timezone.utc)
```

`astimezone()` on a naive datetime assumes the system's local timezone. If the server timezone changes (DST transition or relocation), historical naive timestamps will be interpreted differently.

**Consequence:** Legacy data could shift by the DST offset. The comment acknowledges this is for legacy values.

### M5. Inconsistent privacy mode checking in reaction logging ✓ FIXED

**File:** `/home/peter/AI/Jessica/execution/joi/api/server.py` (reaction handling path)

The reaction logging path does not check `policy_manager.is_privacy_mode()` before logging conversation IDs and reaction content, while other code paths consistently check privacy mode.

**Consequence:** PII could be logged in plaintext when privacy mode is enabled, specifically for reaction messages.

### M6. update_state builds SQL with f-string column names ✓ FIXED by design

**File:** `/home/peter/AI/Jessica/execution/joi/wind/state.py:282-288`

Column names are interpolated into SQL via f-string. They are validated against `_VALID_STATE_COLUMNS` (a frozenset of hardcoded safe strings), so this is safe in practice. The pattern is intentional and well-guarded.

**Consequence:** Safe due to whitelist validation. Noting for completeness.

### M7. hasattr checks on WindConfig dataclass are dead guards ✓ FIXED

**File:** `/home/peter/AI/Jessica/execution/joi/wind/orchestrator.py:91-106`

```python
cooldown_days=self.config.cooldown_days if hasattr(self.config, 'cooldown_days') else 9,
```

WindConfig is a dataclass with all fields defined with defaults. `hasattr` will always return True. These guards are vestigial from incremental development.

**Consequence:** No functional impact. Unnecessary complexity. Six instances.

### M8. record_engagement uses dynamic SQL column names from if/elif

**File:** `/home/peter/AI/Jessica/execution/joi/wind/state.py:562-584`

```python
counter_col = "total_engaged"      # from validated if/elif
set_parts = [f"{counter_col} = COALESCE({counter_col}, 0) + 1", ...]
```

Column names come from hardcoded string literals in the if/elif chain, so this is safe. But the pattern of building SQL with f-strings containing column names appears frequently and could be fragile if someone adds a new outcome without thinking about SQL safety.

### M9. WindDecisionLogger._last_state dict grows unbounded ✓ FIXED by design

**File:** `/home/peter/AI/Jessica/execution/joi/wind/logging.py:68`

```python
self._last_state: dict[str, tuple[str, float, str | None]] = {}
```

One entry per conversation_id, never cleaned up. Since the allowlist is typically small (a few conversations), this is negligible. But it's worth noting for completeness.

---

## MINOR -- Style, cleanup, nitpicks

### ~~m1. Unused import: asdict in wind/logging.py~~ FIXED

**File:** `/home/peter/AI/Jessica/execution/joi/wind/logging.py:7`

```python
from dataclasses import dataclass, asdict
```

`asdict` is imported but never used.

### ~~m2. timedelta imported inside function bodies in wind/logging.py~~ FIXED

**File:** `/home/peter/AI/Jessica/execution/joi/wind/logging.py:237, 286`

```python
from datetime import timedelta
```

Imported inside two methods despite `datetime` already being imported at module level. Should be a top-level import.

### ~~m3. Magic number for day bucket offset~~ FIXED

**File:** `/home/peter/AI/Jessica/execution/joi/wind/state.py:307`

```python
today_bucket = (now - timedelta(hours=3)).strftime("%Y-%m-%d")
```

The 3-hour offset (presumably timezone alignment) is a magic number used in multiple places without a named constant or explanatory comment.

### ~~m4. conversation_id logged with [:16] truncation in decision logger~~ FIXED

**File:** `/home/peter/AI/Jessica/execution/joi/wind/logging.py:152`

```python
conversation_id[:16] if conversation_id else "?"
```

Phone numbers are typically 12-15 characters, so this truncation is effectively no truncation for DMs. Inconsistent with the privacy mode redaction patterns used elsewhere.

### m5. notes.py and tasks.py are very thin wrappers

**Files:** `/home/peter/AI/Jessica/execution/joi/notes.py` (124 lines), `/home/peter/AI/Jessica/execution/joi/tasks.py` (99 lines)

These modules add a dataclass and row converter over MemoryStore methods. They provide clean abstraction but add an extra layer of indirection for very simple operations. Given the project preference for minimal modules, these are at the boundary of "worth the extra file."

### m6. DB_KEY variable in joi-admin interpolated into heredoc

**File:** `/home/peter/AI/Jessica/execution/joi/scripts/joi-admin:396-398`

```bash
PRAGMA key = '$DB_KEY';
```

If the key contained a single quote, it would break the PRAGMA. The key is hex (from file), so safe in practice.

### m7. Mesh config.py bind_host defaults to 0.0.0.0 ✓ FIXED by design

**File:** `/home/peter/AI/Jessica/execution/mesh/proxy/config.py:8`

Intentional for the internet-facing proxy, just noting for the record.

### ~~m8. _client type annotation is None instead of Optional~~ FIXED

**File:** `/home/peter/AI/Jessica/execution/mesh/proxy/forwarder.py:39`

```python
_client: httpx.Client = None
```

Type annotation says `httpx.Client` but the initial value is `None`. Should be `Optional[httpx.Client]`.

---

## Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 3 |
| IMPORTANT | 8 |
| MODERATE | 9 |
| MINOR | 8 |
| **Total** | **28** |

### Priority Action Items

1. **Fix C1 + C2 immediately** -- Lambda closure bugs in scheduler will send wrong messages to wrong users when multiple Wind/reminder events fire simultaneously. This is a real, production-impacting bug.

2. **Fix C3** -- Rollback targeting wrong connection can leave SQLite in dirty state.

3. **Fix I1** -- Naive datetime in `purge_old()` causes incorrect reminder retention.

4. **Fix I4** -- Wind message_id should be Signal timestamp, not UUID, for direct-reply engagement tracking to work.

5. **Fix I7** -- Typing indicator forwarding should respect routing state.

---

*Report generated by Sheldon (code review agent), 2026-04-09*
