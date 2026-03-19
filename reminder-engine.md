# Reminder Engine

## What Reminders Are

Reminders are deterministic, user-requested messages fired at a specific time.
They are mode-agnostic: they work in both Companion and Business modes.
The user creates a reminder explicitly ("remind me in 5m to check the oven");
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

```
remind me in 5m to check the oven       → fires in 5 minutes
remind me in 2h to take meds            → fires in 2 hours
remind me in 1d to call the bank        → fires in 1 day
remind me tonight to call mom           → fires at 9pm local time
remind me in 30m about the meeting      → fires in 30 minutes
```

Handled by `_handle_reminder_command()` in `server.py`.
Guard: owner + direct message only (same as Wind snooze).
Max word limit: 25 words (protects against accidental trigger on long messages).

Time expressions recognised (reused from Wind snooze):
- `_DURATION_HOURS`: `5h`, `2 hours`
- `_DURATION_MINS`: `30m`, `5 min`
- `_DURATION_DAYS`: `1d`, `3 days`
- `_DURATION_TONIGHT`: `tonight` → 9pm local time

## Key Files

| File | Role |
|------|------|
| `execution/joi/reminders.py` | `ReminderManager` class + `Reminder` dataclass |
| `execution/joi/memory/store.py` | `reminders` table in `SCHEMA_SQL` (v10) |
| `execution/joi/api/scheduler.py` | `_check_reminders()` — fires due reminders; `_purge_old_reminders()` — daily cleanup |
| `execution/joi/api/server.py` | `_handle_reminder_command()`, `_handle_reminder_snooze_command()`, `_generate_reminder_message()` |

## Post-Fire Snooze

After a reminder fires, the user can snooze it by replying with natural language:

```
remind me again in 30 minutes
remind me again in 2h
snooze
later
```

Handled by `_handle_reminder_snooze_command()` in `server.py`. Guard: only triggers if a
reminder fired within the last 2 hours — prevents stealing new reminder creation requests
like "remind me in 1h". Default snooze (no duration specified): 1 hour.

Confirmation: `Reminder snoozed for 30m. I'll remind you about "check the oven" then.`

## Cleanup

Terminal reminders (`fired`, `expired`, `cancelled`) are pruned daily by the scheduler.

Retention controlled by `JOI_REMINDER_RETENTION_DAYS` in `/etc/default/joi-api`:
- Default: `180` days
- Set to `0` to keep forever (audit mode)
- Pending reminders are never deleted

## Future / Out of Scope

- **Listing reminders**: `/reminders` command to show pending list
- **Cancellation command**: "cancel my oven reminder"
- **Group support**: currently DM only
- **Advanced recurrence**: "every Monday", "first of the month"
- **`at` time expressions**: "remind me at 3pm" (not yet parsed)
