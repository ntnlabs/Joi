# Reminder Engine Review — Wednesday — 2026-03-31

## 1. Execution Order: Reminders Before Fact Extraction

The ordering is sound. Chain in `process_with_llm()`:
1. `_handle_reschedule_intent()`
2. `_handle_reminder_command()` — explicit
3. `_handle_temporal_task()` — implicit, only if explicit didn't fire
4. `_detect_and_extract_fact()` — skipped if `reminder_result` is set

The `not reminder_result` guard is correct. No bugs here.

**Observation**: `_handle_reschedule_intent` runs unconditionally before reminder creation. For compound intents ("reschedule X and remind me at 3pm") both fire correctly, fact extraction skipped. Fine for now.

---

## 2. `_handle_temporal_task()` — Implicit Reminder Detection

Structure is clean: regex gate → LLM parse → store → log. Fallthrough design correct.

**[BUG] Privacy mode inconsistency**: `_handle_temporal_task` correctly checks `policy_manager.is_privacy_mode()`. `_handle_reminder_command` LLM path (~line 2778) does NOT — logs title unconditionally. Privacy violation.

**[GAP]** Regex fallback path in `_handle_reminder_command` has NO logging at all for successful reminder creation.

---

## 3. `_TEMPORAL_TASK_TRIGGER` Regex

```python
_TEMPORAL_TASK_TRIGGER = re.compile(
    r"\b(i\s+(need|have|should|must|got)\s+to\b|i\s+have\s+a\b|"
    r"don'?t\s+forget\s+(i\b|to\b)|i\s+must\b|gotta\b|"
    r"supposed\s+to\b|i'?m\s+supposed)\b",
    re.I,
)
```

**[BUG] `i\s+have\s+a\b` too broad** — matches "I have a dog", "I have a question". Every match costs a CURIOSITY_MODEL LLM call. If LLM hallucinates a time, phantom reminder created silently.

**[BUG] `i\s+must\b` redundant** — already covered by `i\s+must\s+to\b`. Standalone form matches "I must say" which is not a task.

---

## 4. Pre-Queue Snooze Ordering

Ordering correct — reminder snooze first, Wind snooze second. `get_last_fired` guard works.

**Edge case**: `>` boundary at exactly 45 minutes still matches. Fine.

**[BUG] "later" doc/code mismatch**: `reminder-engine.md` line 147 lists "later" as valid snooze keyword but it is NOT in `_REMINDER_SNOOZE_TRIGGER`. User says "later" → normal LLM response, not snooze.

---

## 5. `_handle_reminder_snooze_command()` — 8-word guard, window, defaults

8-word guard is good. Window (45m) and default (30m) are reasonable.

**[BUG] `remind\s+me\s+in` steals new reminder requests**: "remind me in 1h to call mom" within 45m of a fired reminder gets intercepted as a snooze (pre-queue, returns early) instead of creating a new reminder. Fix: remove `remind\s+me\s+in` from trigger, keep only `remind\s+me\s+again` and `snooze`.

---

## 6. Documentation — `reminder-engine.md`

Generally accurate. Specific discrepancies:

- **"later" keyword** (line 147): listed but not in code.
- **Processing order** (lines 174-183): matches code exactly. ✓
- **Word limit for explicit reminders**: accurate — LLM path has no limit, regex fallback has 25-word limit. ✓

---

## 7. Diagram — `diagrams/query-pipeline.html`

Accurate:
- Pre-queue snooze order correct (reminder first, Wind second). ✓
- In-queue ordering matches code. ✓
- "skipped if reminder fired" note on fact detection correct. ✓

---

## 8. Duplicated Duration Formatting

Copy-pasted in 4-5 locations with subtle inconsistencies (minor UX). Not blocking.

---

## 9. Datetime Convention Note

Wind snooze uses naive datetimes; reminder snooze uses UTC-aware. Both internally consistent with their subsystems. Not a bug, but a readability trap for contributors.

---

## Summary

| Severity | Finding |
|---|---|
| Bug | `_handle_reminder_command` LLM path logs title without privacy check |
| Bug | `_REMINDER_SNOOZE_TRIGGER` `remind\s+me\s+in` steals new reminder requests within window |
| Bug | `i\s+have\s+a\b` in `_TEMPORAL_TASK_TRIGGER` — unnecessary LLM calls, phantom reminder risk |
| Bug | `i\s+must\b` redundant in `_TEMPORAL_TASK_TRIGGER` |
| Gap | Regex fallback path has no logging |
| Docs | `reminder-engine.md` lists "later" as snooze keyword but code doesn't implement it |
| Minor | Naive vs UTC-aware datetimes in adjacent snooze functions, no comment |
