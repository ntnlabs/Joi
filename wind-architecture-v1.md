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
- `curiosity_discovery` (novelty/relevance-driven, low-frequency probing)
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
- **Curiosity / Discovery**: novelty/relevance-driven probe with stricter caps and faster decay
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

## Topic State Evolution & Lifecycle Control (Required)

Basic topic fields are not enough for stable Wind behavior.

Without topic aging, merging, and feedback-aware reprioritization, Wind will either:
- revive old topics too late
- drop useful topics too early
- spam near-duplicate topics
- keep pushing topics the user is avoiding

### Strict Scoping Rule (Must-Have)

All Wind topic state is **per conversation (user/group)**.

This includes:
- topic queues
- topic merging
- decay/priority updates
- feedback signals
- suppressions and dampers

Never merge, score, or promote topics across different conversations.

### Topic State Fields (Recommended v1+)

Add mutable lifecycle/state fields (in `pending_topics` or companion state table):

- `base_priority` (stable seed priority)
- `dynamic_priority` (current effective priority; optional stored cache)
- `decay_rate`
- `last_evaluated_at`
- `last_user_mention_at`
- `last_joi_mention_at`
- `last_promoted_at`
- `negative_feedback_score` (topic-specific)
- `ignore_count`
- `merge_count`
- `merged_into_topic_id` (nullable, lineage)
- `archive_reason` (nullable)

These fields are distinct from extractor scores (`novelty_score`, `emotional_charge`, etc.).

### Dynamic Priority (Required)

Wind should evaluate an effective topic priority, not only a static priority.

Recommended pattern:

```text
dynamic_priority =
    base_priority
  + recency_boost
  + user_interest_boost
  + novelty_boost
  - decay_penalty
  - fatigue_penalty
  - negative_feedback_penalty
  - topic_override_penalty
```

Notes:
- `dynamic_priority` may be computed on read (preferred for transparency), or cached after evaluation.
- Clamp to a known range to simplify tuning.

### Decay / Aging Policy

Topics need both soft decay and hard expiry.

Recommended model:
- `decay_rate` controls how fast topic priority fades
- `last_evaluated_at` prevents repeated full penalties each tick
- `expires_at` is the hard stop

Refreshing signals (can slow/reset decay):
- user mentions topic again
- new evidence arrives for same topic
- topic gets selected/promoted but not yet resolved

Aging signals (increase decay pressure):
- repeated evaluations with no selection
- topic ignored after proactive mention
- explicit user topic shift

### Similar Topic Merge (Required)

Near-duplicate topics should be merged to avoid queue pollution.

Merge policy:
- only compare unresolved topics in the **same conversation**
- prefer a canonical topic (older or higher-confidence)
- merge evidence and counts into canonical topic
- increment `merge_count`
- mark merged topic as terminal (`merged`) or archive it
- preserve lineage via `merged_into_topic_id`

Suggested merge inputs:
- normalized title/content similarity
- overlapping evidence message IDs
- same `novelty_key` or related event source

### User Topic Override / Topic Shift

If the user stops engaging on topic A and clearly moves to topic B, topic A should cool down.

This is not the same as explicit rejection.

Recommended effect:
- apply `topic_override_penalty` to previous active topic(s)
- reduce `dynamic_priority`
- keep topic pending (unless it also decays/expires)

This prevents Wind from dragging the conversation back to stale threads too aggressively.

### Feedback Model (Split Required)

Separate **topic-specific** feedback from **autonomy-wide** feedback.

#### A. Topic-Specific Negative Feedback

Examples:
- "I don't want to talk about that"
- "Stop bringing that up"

Effects:
- increase `negative_feedback_score` for that topic
- reduce `dynamic_priority` sharply
- optionally mark topic `suppressed` or `dismissed`

#### B. Autonomy-Wide Negative Feedback (Conversation-Level)

Examples:
- "Don't message me now"
- "I don't want to talk at all"
- "Leave me alone today"

Effects:
- set/extend `wind_snooze_until`
- increase conversation-level fatigue damping
- lower autonomous bias for all topics in that conversation

Topic rejection and Wind rejection must not be treated as the same signal.

### Archive / Terminal States / Long Pause Reset

Wind needs long-term cleanup to avoid stale emotional residue.

Recommended terminal/archive outcomes:
- `resolved`
- `expired`
- `dismissed`
- `merged`
- `suppressed`
- `stale_after_pause`

Long-pause policy (per conversation; e.g., after prolonged inactivity):
- archive stale low-priority pending topics
- reduce/reset short-term Wind counters and dampers
- preserve explicit user preferences and hard suppressions
- keep archived topics available for audit, not active selection

### Normalization (Required for Tuning)

Keep scores in stable ranges:
- extractor scores normalized (e.g., fixed numeric bounds)
- `dynamic_priority` clamped to a known range
- penalties/boosts normalized so one factor does not dominate unexpectedly

Without normalization, Wind tuning becomes unstable and hard to reason about.

## Tension Topic Extractor (Planned, Recommended)

Wind can be improved by a dedicated LLM pass that mines "forward momentum" topics from recent conversation context.

This is **not** fact extraction and **not** summarization.

Purpose:
- identify unfinished ideas
- detect unresolved questions or latent directions
- capture emotionally charged but still-open threads
- produce candidate topics for future proactive continuation

### Role in the Architecture

The tension extractor is a **topic miner**, not a sender.

It should:
- produce candidate `tension` topics
- score and annotate them
- provide evidence for why they exist

It should **not**:
- send messages
- bypass Wind guardrails
- decide final timing

Wind still controls if/when a mined tension topic becomes a proactive message.

### Prompt Intent (Concept)

The extractor prompt should ask for high-quality continuation candidates only.

Desired behavior:
- prioritize genuine forward momentum
- reject trivial continuation
- reject already-resolved topics
- avoid generic "check in" style suggestions without evidence

Example intent (paraphrased):
- identify unfinished ideas, unresolved questions, emotional spikes, latent directions, or novel conceptual threads worth autonomous continuation
- only mark a topic if it has real continuation potential

### Input Window Strategy (v1)

Use a recent context window for mining (for example, last N messages/turns), but avoid blindly scanning the full conversation every time.

Recommendations:
- start with a smaller recent window (e.g., recent slice, not the full context history)
- debounce extractor runs (not every scheduler tick)
- run primarily after new user inbound messages
- dedupe against already-active tension topics before promoting new ones

### Critical Guardrail: Prevent Self-Amplification

The extractor must not over-learn from Joi's own proactive/reminder output.

Because Joi may emit multiple non-user messages, the extractor can accidentally amplify its own prior suggestions.

Use the special-header convention in context handling:
- downweight or exclude `[JOI-WIND]` and `[JOI-REMINDER]` messages in tension mining
- treat `[JOI-CRITICAL]` separately (high-value but different semantics)
- focus primarily on user turns + direct reply context

### Output Schema (Recommended v1)

Each extracted candidate should include:

- `title`
- `topic_summary`
- `resolved_status` (`open`, `partially_resolved`, `resolved`)
- `evidence_message_ids` (required)
- `intrinsic_interest_score`
- `continuation_depth`
- `emotional_charge`
- `novelty_score`
- `decay_rate`
- `confidence`

Optional:
- `user_pull_score` (how strongly user indicated interest)
- `resolution_confidence`
- `suggested_followup_style`

### Why Evidence Is Required

Without evidence references (`evidence_message_ids`), the extractor will tend to hallucinate "latent threads."

Evidence requirements make outputs:
- auditable
- easier to tune
- safer to auto-promote into `pending_topics`

### Integration Patterns

#### Option A (simpler v1): Direct to `pending_topics`

Extractor writes directly to `pending_topics` with:
- `topic_type = tension`

Pros:
- simpler implementation

Cons:
- less separation between raw mined candidates and approved queue entries

#### Option B (recommended evolution): Staging + Promotion

1. Extractor writes to `tension_topics` (raw mined candidates)
2. Topic builder/dedupe step promotes good candidates to `pending_topics`

Pros:
- better auditing and tuning
- easier to compare extractor output vs actual promoted topics

### How Wind Uses Tension Topics

Tension topics should influence:
- topic pressure (impulse contribution)
- topic ranking during selection
- follow-up phrasing relevance

But Wind still decides timing via:
- hard gates
- impulse threshold
- send-worthiness checks

### Tuning Caution (High Importance)

Do not overweight `emotional_charge` early.

If over-weighted, Wind may become intrusive or overly intense.

Safer rollout:
- shadow mode first
- inspect mined tension topics manually
- keep emotional contribution low until behavior feels right

## Topic Affinity Model (Interest / Rejection Weights)

Wind benefits from a lightweight topic preference memory per conversation.

Goal:
- learn which topic families the user/group tends to engage with
- learn which topic families are often ignored/rejected
- feed those signals into topic ranking and discovery filtering

### Scope (Must-Have)

Affinity is tracked **per conversation** (user/group), never globally.

This prevents:
- cross-user preference bleed
- topic leaks
- incorrect assumptions across groups/DMs

### Affinity Keying (Recommended)

Track affinity by normalized topic family / cluster (not only by individual topic instance).

Examples:
- `weather`
- `sleep`
- `fitness`
- `work_project_x`
- `home_maintenance`

This allows Joi to learn preferences across similar topics without overfitting to one message.

### Suggested Affinity Fields (per conversation + topic family)

- `topic_family`
- `interest_weight` (likeness / positive pull)
- `rejection_weight` (topic-specific negative pull)
- `engagement_count`
- `ignore_count`
- `positive_response_count`
- `negative_response_count`
- `last_positive_at`
- `last_negative_at`
- `last_seen_at`
- `cooldown_until` (optional; blocks discovery/promotion temporarily)

### Signal Updates

#### Positive signals (increase `interest_weight`)
- user voluntarily returns to the topic
- user asks follow-up questions
- meaningful response after proactive mention
- explicit positive reaction to topic

#### Negative signals (increase `rejection_weight`)
- explicit topic refusal ("don't talk about that")
- repeated ignores after proactive topic mention
- abrupt subject shift after repeated prompting

### Important Distinction

Topic affinity is not the same as autonomy acceptance.

- `rejection_weight` = "I dislike this topic"
- conversation-level Wind fatigue/snooze = "I don't want proactive engagement right now"

Both signals are required.

### How Affinity Influences Wind

Use affinity in:
- topic ranking (`user_interest_boost`, `rejection_penalty`)
- curiosity/discovery candidate filtering
- dynamic priority and pursuit continuation decisions

## Curiosity / Discovery Loop (Planned, Optional in v1 Rollout)

Curiosity is a separate proactive mechanism for exploring **new** or weakly-developed topics.

It should complement Wind continuation, not replace it.

### Core Concept

- **Continuation**: follow an existing open thread (tension/ongoing topic)
- **Discovery**: gently probe for a new thread (novelty + relevance anchored)

Both use the same outbound pipeline, but discovery must be more constrained.

### Curiosity Constraints (Stricter Than Continuation)

Recommended v1 constraints:
- very low hard cap (per conversation/day)
- minimum spacing between discovery attempts
- disabled when strong continuation topics already exist
- disabled when fatigue damper is elevated
- disabled during/after recent autonomy-wide negative feedback
- stricter quiet-hour behavior
- shorter expiry / faster decay than continuation topics

Practical rule:
- **continuation has priority over discovery**

### Discovery Candidate Requirements

Discovery candidates should be anchored, not random.

Good anchors:
- fact-adjacent context (preferences, birthdays, routines) when timely
- low-confidence but relevant latent threads
- weak tension topics not yet strong enough for continuation
- recent novelty in events/environment with clear relevance

Bad anchors:
- generic "check in" loops
- repeated unanchored chit-chat prompts
- topics with high `rejection_weight`

### Discovery Scoring (Separate from Wind Impulse)

Discovery should have its own candidate score before it even reaches Wind selection.

Example factors:
- `novelty_score`
- `relevance_anchor_score`
- `safety_score`
- `timing_fit`
- `interest_weight` boost
- `rejection_weight` penalty
- `fatigue_penalty`
- small entropy

Discovery topics should:
- decay faster
- have shorter TTL
- tolerate fewer retries

### Safety Rule: Discovery Cooldown on Rejection

If a topic family crosses a rejection threshold:
- mark it discovery-ineligible for a cooldown period (`cooldown_until`)

This prevents "curious pest" behavior.

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

## Topic Pursuit (Bounded Stubbornness)

Wind should not re-decide every topic from scratch on every tick.

Sometimes Joi should be allowed to persist on a relevant topic while rules still allow it.

This is **bounded stubbornness** (topic pursuit), not spam.

### Core Idea

Not just:
- tick -> maybe topic

But:
- topic selected -> topic enters pursuit state -> scheduler checks when retry is allowed

The scheduler becomes a dispatcher for due pursuits, not only a fresh scorer.

### Topic Pursuit States (Recommended)

- `pending`
- `armed` (selected as worthy of pursuit)
- `active_pursuit`
- `mentioned`
- `snoozed`
- `dismissed`
- `expired`

### Pursuit Fields (per topic)

- `pursuit_strength`
- `attempt_count`
- `attempt_budget`
- `next_attempt_at`
- `last_attempt_at`
- `pursuit_expires_at`
- `failure_count`
- `last_skip_reason`

### When Pursuit Is Allowed

Pursuit may continue if:
- topic remains relevant
- conversation is not in quiet/snooze state
- proactive budget remains
- minimum spacing is satisfied
- no strong rejection signal exists

This matches the intended behavior:
- Joi can "pressure on" a bit when a topic matters and rules allow it

### Bounded Stubbornness Rules (Required)

- max attempts per topic
- minimum spacing between attempts
- shorter budgets for discovery topics
- longer budgets for high-value continuation topics
- hard stop on topic-specific rejection
- hard stop / global pause on autonomy-wide rejection

### Silence vs Rejection (Critical Distinction)

Persistence is allowed only while **silence does not look like rejection**.

If signals indicate rejection:
- topic-specific rejection -> suppress/dismiss that topic
- autonomy-wide rejection -> pause all pursuits (`wind_snooze_until`)

### Pursuit and Dynamic Priority

Pursued topics may receive a persistence boost, but must still degrade over time.

Additions to dynamic priority:
- `+ persistence_boost`
- `- repeated_attempt_penalty`
- `- rejection_penalty`

This creates "try again a little" behavior without runaway prompting.

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
- **Curiosity / Discovery** = novelty/relevance-driven trigger (stricter caps, faster decay)
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
- **curiosity-capable (but tightly constrained)**
- **boundedly persistent (topic pursuit)**
- **per-conversation**
- **guardrail-first**
- **explainable**
- **rolled out in shadow mode first**

If those constraints are respected, Wind can feel organic without becoming noisy or unsafe.
