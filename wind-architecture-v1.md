# Wind Architecture v1

> Focused design for Joi's proactive "Wind" behavior.
> Version: 1.0 (Draft)
> Last updated: 2026-02-22

## Purpose

Define a practical, safe, and explainable architecture for Wind behavior:
- Joi may initiate contact proactively
- Proactive messages should feel organic, not random
- Wind must be conservative and heavily guarded
- Wind failures must never break normal request-response messaging

This document focuses only on Wind behavior (impulse + proactive outreach).

## Core Idea

Wind is a **two-stage system**:

1. **Decision stage**: Should Joi say something now?
2. **Generation stage**: If yes, what is the single best thing to say?

This separation keeps behavior predictable, tunable, and safe.

## Design Principles

1. **Topic-driven, not random**
- Most proactive messages should come from a queue of "things worth mentioning".
- Randomness may shape timing, but not content selection.

2. **Conservative by default**
- Quiet hours, cooldowns, caps, and dampers apply before any send.

3. **Per-conversation state**
- Wind state must be tracked per recipient/conversation, not globally.

4. **Explainable decisions**
- Every Wind tick should produce a readable reason for send/skip.

5. **Isolation from core messaging**
- Wind logic runs in background scheduler paths.
- Wind errors do not block inbound/outbound user message handling.

## High-Level Flow

```text
Scheduler tick
  -> Eligible conversations scan
  -> Hard gates (fast fail)
  -> Impulse score calculation
  -> Threshold check
  -> Topic selection
  -> Proactive draft generation (LLM)
  -> Final send-worthiness check
  -> _send_to_mesh()
  -> State/topic updates + structured log
```

## Unified Proactive Outbound Pipeline (Recommended)

Wind should share one outbound pipeline with other proactive-capable sources.

### Why

- Reuses the same safety checks and send path
- Keeps behavior consistent across proactive message types
- Avoids duplicate logic for message generation, validation, and logging

### Trigger Sources (same pipeline, different trigger logic)

- `wind` (impulse-driven, probabilistic timing)
- `reminder` (scheduled/deterministic, time-driven)
- `critical` (event-driven, highest priority, separate override rules)

### Shared Pipeline Stages

All proactive sources should reuse:
- target selection / routing
- guardrails and rate-awareness
- message generation constraints
- final send-worthiness check
- `_send_to_mesh()`
- structured decision/send logging

### Trigger Logic Must Stay Separate

- **Wind**: waits for impulse score + threshold
- **Reminder**: fires when `due_at` is reached (should not wait for impulse)
- **Critical**: event-driven path, may bypass quiet-hour suppression depending on policy

This keeps the architecture unified without blurring behavior semantics.

## Components

### 1. Wind Scheduler Hook

Runs inside the existing Joi scheduler loop as a periodic task.

Responsibilities:
- Trigger Wind evaluation cadence
- Spread work across ticks (avoid spikes)
- Apply backoff on repeated Wind failures

Notes:
- Current scheduler scaffold already exists in `execution/joi/api/server.py`
- Wind hook is currently placeholder (`_check_wind_impulse()`)

### 2. Eligibility Scanner

Determines which conversations are candidates for Wind evaluation.

Inputs:
- Allowed Wind recipients (policy/config)
- Conversation activity metadata
- Per-conversation Wind state

Output:
- List of candidate conversation IDs for this tick

Rules:
- Skip conversations without Wind enabled
- Skip blocked/suppressed conversations
- Skip conversations in temporary quiet/snooze period

### 3. Hard Gates (Fast Fail Layer)

Cheap checks that run before scoring. If any fail, skip immediately.

Recommended gate order:
1. Wind globally enabled
2. Conversation eligible for Wind
3. Not in quiet hours (unless critical override path)
4. Minimum cooldown since last proactive send
5. Daily proactive cap not reached
6. Max unanswered proactive streak not exceeded
7. Recent user interaction too fresh -> skip
8. Outbound limiter / system health guard (LLM degraded, queue pressure)

Why:
- Avoid expensive scoring and LLM calls when a send is impossible anyway

## State Model (Per Conversation)

Wind must track state per conversation (DM/group target), not global `system_state` only.

### Required Fields (v1)

- `conversation_id`
- `last_user_interaction_at`
- `last_outbound_at`
- `last_proactive_sent_at`
- `last_impulse_check_at`
- `proactive_sent_today`
- `proactive_day_bucket` (date key / reset marker)
- `unanswered_proactive_count`
- `last_positive_response_at` (optional but useful)
- `wind_snooze_until` (optional user-controlled suppression)

### Why Per-Conversation Matters

Without per-conversation state:
- one active chat can suppress Wind for everyone
- daily caps become inaccurate
- silence calculations become meaningless

## Topic Queue ("Things Worth Mentioning")

Wind quality depends more on topic quality than on scoring math.

### Topic Sources

Typical sources:
- Significant system events (weather shift, sensor anomalies, reminders)
- Follow-up opportunities ("you asked earlier...")
- Lightweight observations (only if useful/relevant)
- Time-based soft prompts (very limited)

Facts can influence topics, but should usually not directly trigger proactive sends.

Recommended use of facts:
- enrich topic context (e.g., remembered preferences, birthdays)
- boost/deprioritize topic priority
- improve phrasing relevance

Practical rule:
- **facts are context**
- **topics are triggers**

### Topic Fields (v1)

- `id`
- `conversation_id` (or scoped target)
- `topic_type`
- `title`
- `content`
- `priority` (0-100)
- `status` (`pending`, `mentioned`, `expired`, `dismissed`)
- `created_at`
- `expires_at`
- `mentioned_at`
- `source_event_id` (optional)
- `novelty_key` (recommended for dedupe)

### Topic Lifecycle

1. Topic created
2. Topic remains `pending`
3. Wind selects one topic
4. On successful proactive send -> mark `mentioned`
5. If stale -> mark `expired`
6. If redundant/no longer relevant -> `dismissed`

Rules:
- Prefer one topic per proactive message
- Expire topics aggressively (stale proactive messages feel wrong)
- Deduplicate near-identical topics

## Impulse Engine

Impulse answers: "Is now a good time to speak?"

### Score Structure (v1)

Use an additive score with explicit factors:

```text
impulse_score =
    base_impulse
  + silence_factor
  + topic_pressure
  + time_factor
  + entropy_factor
  - engagement_damper
  - fatigue_damper
```

### Recommended Factors

#### 1. Base Impulse
- Small constant baseline to avoid a system that never initiates
- Low weight

#### 2. Silence Factor
- Increases with time since `last_user_interaction_at`
- Capped to prevent runaway growth

#### 3. Topic Pressure
- Based on number and priority of pending topics
- Prefer weighted pressure (priority-aware), not just count

#### 4. Time Factor
- Shapes behavior by time/day rhythm
- Strong negative in sleep hours
- Mild positive in good engagement windows (e.g., evening)

#### 5. Entropy Factor
- Small bounded randomness
- Used as tie-breaker / natural variance
- Never strong enough to force low-quality proactive sends

#### 6. Engagement Damper
- Reduces score when recent outbound volume is already high
- Prevents chatty bursts

#### 7. Fatigue Damper
- Strongly reduces score when recent proactive messages were ignored
- Key anti-spam mechanism

### Threshold

- Single configurable threshold per conversation or default profile
- Score and threshold must both be logged for tuning

## Topic Selection (After Threshold Pass)

Once threshold passes, Wind still needs to pick the best topic.

### Selection Strategy (v1)

Pick one topic using a weighted score:
- `topic_score = priority + recency_bonus + relevance_bonus - staleness_penalty`

Selection rules:
- Skip expired topics
- Skip duplicates (same `novelty_key`)
- Prefer fresh, high-priority, actionable topics
- Do not bundle multiple unrelated topics into one message

If no valid topic remains:
- Abort proactive send
- Log `skip_reason=no_viable_topic`

## Proactive Message Generation

LLM is used only after:
- hard gates pass
- impulse threshold passes
- topic selected

### Input to LLM (v1)

Provide:
- target conversation context (limited)
- selected topic only
- concise behavioral instructions for proactive outreach
- constraints (length, tone, non-speculative, one topic)

### Generation Rules

- One main topic
- Short message
- No unnecessary preamble
- No hallucinated details
- No stack of updates
- No code/URLs unless topic explicitly requires it

### Final Send-Worthiness Check (Post-LLM)

Before sending:
- Non-empty output
- Length within limit
- Mentions selected topic (semantic/keyword sanity)
- No disallowed patterns
- Not near-duplicate of recent proactive message

If it fails:
- Keep topic pending (or downgrade/retry later depending on failure type)
- Log reason

## Context Window Handling (Multiple Joi Messages)

Wind/reminders/critical messages can create short bursts of Joi-originated messages between user turns.

If raw chronology is fed directly into the model, Joi may:
- answer the wrong thread
- over-focus on its own recent proactive/priority message
- repeat itself because it sees multiple consecutive Joi messages

This is a **context assembly problem**, not only a Wind problem.

### v1 Recommendation: Special Internal Headers for Non-Regular Joi Messages

To keep implementation simple, use **special headers** for non-regular Joi outbound messages and keep regular reply messages clean.

Examples (internal storage/context representation):
- `[JOI-WIND] ...`
- `[JOI-REMINDER] ...`
- `[JOI-CRITICAL] ...`

Regular replies remain untagged.

Notes:
- These headers are for internal transcript/context handling (not user-facing transport text).
- Header format should be stable and machine-readable.

### Why Headers Work Well (v1)

- Lower implementation complexity than introducing a large metadata model immediately
- Context builder can detect special messages with simple parsing
- Modelfile/system prompt can explicitly teach the model how to interpret them
- Keeps normal conversation turns visually clean

### Context Builder Rules (v1)

When assembling context for a new inbound user message:

1. Include recent user turns and normal Joi replies as usual
2. Detect special Joi messages via headers (`[JOI-WIND]`, `[JOI-REMINDER]`, `[JOI-CRITICAL]`)
3. Do **not** blindly include long bursts of consecutive Joi messages verbatim
4. Collapse special messages into a compact block if needed
5. Keep unresolved critical items visible until acknowledged/resolved
6. Limit reminder/Wind carry-over (summarize or cap count)

### Suggested Context Shape (v1)

Instead of raw assistant bursts, provide:
- recent transcript turns (user + direct replies)
- compact pending special messages block (if any)

Example (internal context shape):

```text
Recent conversation:
- User: ...
- Joi: ...
- User: ...

Pending special messages:
- [JOI-CRITICAL] Smoke alert in kitchen (unacknowledged)
- [JOI-REMINDER] Dentist appointment tomorrow 09:00
```

### Modelfile / Prompt Instruction (Required)

The model should be explicitly taught:
- special headers identify Joi-originated system-class messages
- user's newest inbound message remains primary unless user references a special message
- `[JOI-CRITICAL]` items have highest priority
- reminder/Wind items may be acknowledged briefly, but should not hijack unrelated replies

### Future Upgrade Path (optional)

If needed later, this header approach can evolve into richer structured metadata.

For v1, headers are a pragmatic and readable solution.

## Safety and Guardrails

### Mandatory Guardrails (v1)

1. Daily proactive cap per conversation
2. Cooldown between proactive messages
3. Quiet hours suppression
4. Unanswered proactive streak cap
5. Outbound rate limiter awareness
6. LLM timeout/backoff
7. No retries that create spam loops
8. Critical alerts use separate path (not standard Wind topic flow)

### Failure Behavior

If Wind fails at any stage:
- Do not block normal messaging
- Log structured error
- Leave topic pending unless invalid/stale
- Apply backoff to next Wind evaluation for that conversation

## Scheduled Tasks / Reminders (Interaction with Wind)

Scheduled tasks should reuse the **same proactive outbound pipeline** but not the same trigger mechanism.

### Recommended Model

- **Wind** = impulse-driven trigger (`should I reach out now?`)
- **Reminder** = deterministic trigger (`this is due now`)

They share:
- topic/message preparation path
- generation constraints
- send-worthiness checks
- send path (`_send_to_mesh()`)
- logging and observability

They differ in:
- trigger conditions
- priority/override semantics
- retry policy

### Important Data Modeling Rule

Do not model scheduled reminders only as generic facts.

Reminders need dedicated scheduling semantics (for example):
- `due_at`
- `timezone`
- `recurrence`
- `status`
- `last_fired_at`

Facts can support reminder context, but reminders require a proper task/reminder store.

## Observability (Required for Tuning)

Wind must produce structured logs for every evaluation.

### Per-Tick Log Fields (recommended)

- `conversation_id`
- `eligible` (bool)
- `gate_result` (`pass` / specific fail reason)
- `factor_breakdown` (all impulse factors)
- `impulse_score`
- `threshold`
- `selected_topic_id` (or null)
- `decision` (`send` / `skip`)
- `skip_reason`
- `send_result` (`ok` / error)
- `llm_latency_ms` (if generation attempted)

### Metrics (recommended)

- Wind checks per hour
- Wind sends per hour/day
- Skip reasons (count by type)
- Topic queue depth
- Topic expiration rate
- Proactive reply rate (future)

## Rollout Plan (Best Practice)

### Phase 0: Data + Logging Only
- Build per-conversation Wind state
- Build topic queue ingestion
- No proactive sends

### Phase 1: Shadow Mode
- Run full Wind evaluation
- Select topic + draft message
- Log everything
- Do not send

Success criteria:
- Decision logs look sensible
- No obvious spammy drafts
- Topic selection quality is acceptable

### Phase 2: Low-Risk Live Mode
- Enable sends with strict caps (e.g., very low daily limit)
- Limited recipient allowlist
- Aggressive cooldowns

### Phase 3: Tuning
- Adjust factor weights, thresholds, dampers, quiet windows
- Improve topic ranking and dedupe

### Phase 4: Normal Companion Wind
- Relax caps to intended companion defaults
- Continue monitoring and feedback tuning

## Integration Points (Current Codebase)

### Already Available

- Scheduler thread in `execution/joi/api/server.py`
- `_send_to_mesh()` path (dev-notes confirms this is ready)
- SQLCipher-backed memory store
- `system_state` table (global state)
- Outbound rate limiter scaffold

### Needed for v1

1. Wind-specific scheduler hook implementation
2. Per-conversation Wind state storage and helpers
3. Topic queue storage + lifecycle helpers (if not already implemented in runtime)
4. Topic creation pipeline from events/context
5. Impulse score calculator with structured factor logging
6. Proactive generation + final send-worthiness validation
7. Wind policy/config (recipient allowlist, caps, quiet hours, thresholds)

## Non-Goals (v1)

- Full learning/personalization of impulse factors
- Multi-topic proactive messages
- Autonomous long-form planning
- Cross-conversation proactive coordination
- Runtime mode switching

## Summary

Wind v1 should be:
- **topic-driven**
- **pipeline-sharing (with reminders/critical paths)**
- **per-conversation**
- **guardrail-first**
- **explainable**
- **rolled out in shadow mode first**

If those constraints are respected, Wind can feel organic without becoming noisy or unsafe.
