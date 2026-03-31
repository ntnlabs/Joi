# Reminder Engine Review — Sheldon — 2026-03-31

## [SERIOUS-1] Privacy mode violation in `_handle_reminder_command` — title logged unconditionally

**Location**: Lines 2778-2783 (LLM path)

`_handle_temporal_task` correctly checks `policy_manager.is_privacy_mode()` and redacts the title.
`_handle_reminder_command` LLM path does not:

```python
logger.info("Reminder set via LLM", extra={
    "conversation_id": conversation_id,
    "due_at": due_at.isoformat(),
    "title": title,   # always logged, even in privacy mode
    "action": "reminder_add",
})
```

**Fix**: Add the same privacy guard used in `_handle_temporal_task`.

---

## [SERIOUS-2] `_handle_reminder_snooze_command` has no logging — zero observability on snooze events

**Location**: Line 2544

The function returns user-visible text but emits no log. All other reminder operations log. If logging is added later, title would need privacy guarding.

**Fix**: Add `logger.info("Reminder snoozed", ...)` with privacy-mode-aware title redaction after `reminder_manager.snooze(...)`.

---

## [SERIOUS-3] `_TEMPORAL_TASK_TRIGGER` matches conversational messages — unnecessary LLM calls and phantom reminder risk

**Location**: Lines 436-441

Very common phrases that fire an LLM call:
- "I have a question" — `i\s+have\s+a\b`
- "I need to understand something" — matches
- "I have to say, that movie was great" — matches
- "I'm supposed to like this?" — matches

Every match calls `_parse_reminder_with_llm` (an LLM inference call). If LLM hallucinates a time (from time vocabulary in prompt), a phantom reminder is silently created — no confirmation step.

**Fix**: Remove `i\s+have\s+a\b` (matches any "I have a [noun]"). Remove standalone `i\s+must\b` (redundant, matches "I must say").

---

## [MODERATE-1] `_DURATION_MINS` regex matches unintended sequences

**Location**: Line 420

`r"(\d+)\s*m(?:in(?:utes?)?)?"` matches "5mm screw" → "5m", "16MB RAM" → "16M".

**Fix**: Add word boundary: `r"\b(\d+)\s*m(?:in(?:utes?)?)?\b"`

*(Note: Out of scope for current fixes — theoretical risk only for this use case)*

---

## [MODERATE-2] Duplicated time-label formatting — 5 locations with inconsistencies

**Location**: Lines 2493, 2535, 2769, 2834, 2868

- `_handle_reminder_command` LLM path: uses `max(1, ...)` and `"2h 15m"` format
- `_handle_temporal_task`: no `max(1, ...)` guard — can display "0m"
- Others: truncate to just `"2h"` with no minutes remainder

*(Note: Out of scope for current fixes — minor UX inconsistency)*

---

## [MODERATE-3] `expires_at` data-loss bug — reminders set >24h out expire before firing

**Location**: Lines 2762, 2826, ~2862

All creation sites set `expires_at = now + timedelta(hours=24)`. `get_due()` checks
`expires_at > now` as an absolute timestamp. A reminder due in 3 days will have
`expires_at` in the past by then and never fire.

**Fix**: `expires_at = due_at + timedelta(hours=24)`

---

## [MODERATE-4] `_handle_wind_snooze_command` mixes naive and aware datetimes

**Location**: Lines 2471-2481

Uses `datetime.now()` (naive) and `datetime.now(tz)` (aware), strips timezone for delta.
Works in single-timezone deployment but fragile.

*(Note: Out of scope — pre-existing, isolated to Wind subsystem)*

---

## [MODERATE-5] `_parse_reminder_with_llm` prompt injection surface

**Location**: Lines 2556-2568

User text embedded directly in prompt without sanitization. Limited impact (single user,
creates reminders only for themselves).

*(Note: Out of scope — single-user system)*

---

## [MODERATE-6] No cancellation check between multiple LLM calls in queued handler

**Location**: Lines 1722-1790

Potentially 4-5 sequential LLM calls (mood, reschedule, reminder, temporal task, facts)
with only one cancellation check at the top.

*(Note: Out of scope — optimization only)*

---

## [MINOR-1] Label format inconsistency between reminder paths

`_handle_temporal_task` uses `< 60` / `< 1440` while `_handle_reminder_command` LLM path
uses `>= 1440` / `>= 60`. Logically equivalent but inconsistent. LLM path shows "2h 15m",
temporal task shows "2h" for same duration.

---

## [MINOR-2] `_handle_agenda_set` logs title without privacy check

**Location**: ~line 2740-2743 — `"Agenda item added"` log.

Same privacy issue as SERIOUS-1.

---

## [MINOR-3] `_TEMPORAL_TASK_TRIGGER` has redundant patterns

`i\s+must\b` is redundant — already covered by `i\s+must\s+to\b` when followed by "to".
Standalone form only additionally matches "I must say" etc., widening false positive surface.

---

## [MINOR-4] `_REMINDER_LIST_TRIGGER` regex is very broad

`plan` and `plans` match "what are your plans for AI safety?" — triggers LLM checks for
reminder list/agenda. LLM acts as second-stage filter so no correctness issue, just burns inference.

*(Note: Out of scope)*

---

## Summary

| Severity | Count | Key Issues |
|---|---|---|
| SERIOUS | 3 | Privacy logging violation, phantom reminder risk, missing snooze audit log |
| MODERATE | 6 | expires_at data-loss, duration regex false matches, duplicated formatting, mixed datetimes, prompt injection, no mid-pipeline cancellation |
| MINOR | 4 | Label format inconsistency, agenda privacy logging, redundant regex, broad list trigger |
