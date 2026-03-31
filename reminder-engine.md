# Reminder Engine

## What Reminders Are

Reminders are deterministic, user-requested messages fired at a specific time.
They are mode-agnostic: they work in both Companion and Business modes.
The user can create a reminder explicitly ("remind me in 5m to check the oven") or
implicitly by expressing a time-bound task ("tonight I need to install a security camera");
Joi fires it at the requested time and, for recurring reminders, reschedules automatically.

## Why Separate from Wind

Wind is a proactive messaging system with engagement tracking, impulse scoring, lifecycle
rules, and learning. Reminders have none of these properties:

| Property              | Wind topics        | Reminders          |
|-----------------------|--------------------|--------------------|
| User-requested        | No                 | Yes                |
| Engagement tracking   | Yes (Phase 4a)     | No                 |
| Lifecycle rules       | Yes                | No                 |
| Retry/pursuit logic   | Yes                | No                 |
| Mode-gated            | Companion only     | All modes          |
| Time-triggered        | Sometimes (due_at) | Always             |

Sharing the same pipeline would couple reminder delivery to Wind's engagement caps,
cooldowns, and impulse thresholds — making reminders unreliable and mode-restricted.

## Lifecycle

```
pending ──fires──► fired          (one-time: status='fired', fired_at=now)
        ──fires──► pending        (recurring: due_at += interval, fired_at=now, status stays pending)
        ──snooze─► pending        (user says "remind me again in 1h": due_at=now+1h, snooze_count++)
        ──cancel─► cancelled
fired   ──purge──► (deleted)      (after JOI_REMINDER_RETENTION_DAYS, default 180d)
```

One-time reminders expire after 24 hours if unfired (configurable via `expires_at`).
Recurring reminders have no expiry by default.

## DB Schema

```sql
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    title TEXT NOT NULL,               -- User-supplied text
    due_at TEXT NOT NULL,              -- ISO8601 UTC
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending', 'fired', 'cancelled'
    recurrence TEXT,                   -- NULL (one-time) or '7d', '1d', '2h'
    created_at TEXT NOT NULL,
    fired_at TEXT,                     -- When last fired (updated on each recurrence)
    expires_at TEXT,                   -- Give-up time; NULL = never expire
    snooze_count INTEGER NOT NULL DEFAULT 0
);
```

Indexes on `(due_at, status)` and `(conversation_id, status)`.

## Prompt Injection Defense

Reminder titles are user-supplied and must not be treated as LLM instructions.
`_generate_reminder_message()` wraps the title in triple-quotes with an explicit framing:

```
The user asked you to remind them about this:
"""
{title}
"""

Write a brief, warm reminder message...
```

This is the standard quoted-data pattern: the LLM is instructed to treat the
triple-quoted block as data, not as instructions. The instruction text surrounding
it is fully under Joi's control.

## Scheduler Integration

`BackgroundScheduler._check_reminders()` runs every tick (60s by default):

1. `ReminderManager.get_due()` — fetch all `pending` reminders with `due_at <= now`
   and `expires_at > now` (or NULL).
2. `_compact_before_wind(conversation_id)` — compact context for a clean slate.
3. `_generate_reminder_message(title, conversation_id, is_recurring, snooze_count)` —
   injection-safe LLM generation.
4. `_send_to_mesh()` — deliver via Signal.
5. Store outbound message with `[REMINDER]` prefix.
6. `mark_fired(id)` — sets `status='fired'`, `fired_at=now`.
7. If recurring: `reschedule(id, due_at + interval)` — sets `status='pending'` with new `due_at`.

No engagement tracking, no `mark_sent()`, no lifecycle rules, no Wind state updates.

## User Command Syntax

### Explicit ("remind me")

```
remind me in 5m to check the oven       → fires in 5 minutes
remind me in 2h to take meds            → fires in 2 hours
remind me in 1d to call the bank        → fires in 1 day
remind me tonight to call mom           → fires at 9pm local time
remind me in 30m about the meeting      → fires in 30 minutes
remind me at 3pm to submit the form     → fires at 15:00 local time
```

Handled by `_handle_reminder_command()` in `server.py`.
Guard: owner + direct message only (same as Wind snooze).
Max word limit: 25 words (protects against accidental trigger on long messages).
Primary path: LLM parser (`_parse_reminder_with_llm()`). Regex fallback for simple
duration expressions (`_DURATION_HOURS`, `_DURATION_MINS`, `_DURATION_DAYS`, `_DURATION_TONIGHT`).

### Implicit (time-bound task intent)

```
tonight I need to install a security camera   → reminder at 9pm
I have to call the bank before 5pm            → reminder at 4:30pm (or as parsed)
don't forget to submit the form at 11:00      → reminder at 11:00
I gotta pick up the kids at 3                 → reminder at 15:00
```

Handled by `_handle_temporal_task()` in `server.py`. Triggered by `_TEMPORAL_TASK_TRIGGER`
regex (phrases: "I need to", "I have to", "I should/must/got to", "gotta", "don't forget to",
"supposed to", "I'm supposed"). Falls through if `_parse_reminder_with_llm()` returns no
time expression (e.g. "I need to learn Python" → no reminder, falls to fact extractor).

Guard: owner + direct message only. No word limit (LLM parser handles it).

## Key Files

| File | Role |
|------|------|
| `execution/joi/reminders.py` | `ReminderManager` class + `Reminder` dataclass |
| `execution/joi/memory/store.py` | `reminders` table in `SCHEMA_SQL` (v10) |
| `execution/joi/api/scheduler.py` | `_check_reminders()` — fires due reminders; `_purge_old_reminders()` — daily cleanup |
| `execution/joi/api/server.py` | `_handle_reminder_command()`, `_handle_temporal_task()`, `_handle_reminder_snooze_command()`, `_generate_reminder_message()` |

## Post-Fire Snooze

After a reminder fires, the user can snooze it by replying with natural language:

```
remind me again in 30 minutes
remind me again in 2h
snooze
```

Handled by `_handle_reminder_snooze_command()` in `server.py`.

**Execution order**: reminder snooze runs *before* Wind snooze in the pre-queue handler.
Both use the word "snooze" — without this ordering, Wind snooze would steal it.
The `get_last_fired` guard makes the swap safe: if no reminder fired recently, reminder
snooze returns `None` and Wind snooze handles it normally.

**Window**: only triggers if a reminder fired within `JOI_REMINDER_SNOOZE_WINDOW_MINUTES`
(default **45 minutes**). Prevents stealing new reminder creation requests like "remind me in 1h"
that happen well after the last reminder fired.

**Default duration**: when no duration is specified (just "snooze"), snoozes for
`JOI_REMINDER_SNOOZE_DEFAULT_MINUTES` (default **30 minutes**).

Confirmation: `Reminder snoozed for 30m. I'll remind you about "check the oven" then.`

## Cleanup

Terminal reminders (`fired`, `expired`, `cancelled`) are pruned daily by the scheduler.

Retention controlled by `JOI_REMINDER_RETENTION_DAYS` in `/etc/default/joi-api`:
- Default: `180` days
- Set to `0` to keep forever (audit mode)
- Pending reminders are never deleted

## Processing Order in `process_with_llm()`

Reminders run before fact extraction to prevent time-bound tasks from being mis-stored as facts:

1. Cancelled check + heartbeat
2. Mood detection
3. `_handle_reschedule_intent()` — reschedule existing facts
4. `_handle_reminder_command()` — explicit "remind me" path
5. `_handle_temporal_task()` — implicit path (only if explicit didn't fire)
6. `_detect_and_extract_fact()` — **skipped entirely if a reminder was created**

## Future / Out of Scope

- **Listing reminders**: `/reminders` command to show pending list
- **Cancellation command**: "cancel my oven reminder"
- **Group support**: currently DM only
- **Advanced recurrence**: "every Monday", "first of the month"
