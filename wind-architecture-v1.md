# Wind Architecture v1

> Focused design for Joi's proactive "Wind" behavior.
> Version: 1.0 (Draft)
> Last updated: 2026-03-31

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
- `proactive_sent_today` (legacy, kept for schema compatibility)
- `proactive_day_bucket` (legacy, kept for schema compatibility)
- `proactive_fire_times` (rolling 24h fire timestamps — see v12 sliding window cap)
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

#### Dynamic Topic Merge (Lexical + Embedding-Driven)

Explicit subsystem name: **Dynamic Topic Merge**

Recommended evolution path:
- **v1**: lexical similarity + evidence overlap + `novelty_key` dedupe
- **v2+**: add embedding similarity for better semantic merge quality

Embedding-driven merge notes:
- only compare topics within the **same conversation**
- use embedding similarity as an additional signal, not the only signal
- keep a merge confidence score for auditability
- never allow cross-conversation embedding merge

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

#### Rejection / Negative Feedback Memory

Explicit subsystem name: **Negative Feedback Memory**

This memory should persist rejection signals over time so Wind does not repeatedly retry rejected topics or autonomy behaviors.

It includes:
- topic-level rejection memory (topic-specific)
- conversation-level autonomy rejection memory (`wind_snooze_until`, fatigue increases)
- optional cooldown/forgiveness rules after long inactivity

This is a memory subsystem, not just a one-tick penalty.

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

### Interest Trend Scoring / Long-Term Topic Modeling

Explicit subsystem name: **Interest Trend Scoring / Long-Term Topic Modeling**

Topic affinity should support trend-aware behavior, not only static weights.

Recommended model (per conversation + topic family):
- short-term interest signal (recent engagement)
- long-term interest baseline
- trend direction (rising / stable / falling)

Practical implementation options:
- EMA-based short-term score
- EMA-based long-term score
- derived trend slope or delta (`short_term - long_term`)

Why it matters:
- helps curiosity choose timely topics
- prevents overcommitting to historically liked but currently cold topics
- helps detect renewed interest in previously rejected or dormant topics

This should remain scoped per conversation/topic family (never global).

## Curiosity / Discovery Loop (Planned, Optional in v1 Rollout)

Curiosity is a separate proactive mechanism for exploring **new** or weakly-developed topics.

It should complement Wind continuation, not replace it.

### Discovery / Curiosity Generator

Explicit subsystem name: **Discovery / Curiosity Generator**

This subsystem generates discovery candidates (or discovery prompts) from anchored signals:
- facts (as context anchors)
- weak tension topics
- recent novel events
- topic affinity + trend signals

It should not directly send messages.
It should produce candidates that enter the same proactive pipeline with stricter caps and decay.

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

## LLM Role Architecture (Execution Plan)

This chapter describes how Wind-related behavior is executed at inference time.

It is not only conceptual behavior planning; it defines **runtime LLM role separation** and call responsibilities.

### Primary Deployment Profile (Current Plan)

Use **one base LLM model** with **four role-specific Modelfiles**:

- `joi-brain`
- `joi-consolidator`
- `joi-tension`
- `joi-curiosity`

This is the primary design for now.

### Why This Split Is Good

- clear responsibility boundaries per role
- easier prompt tuning without changing base model weights
- consistent language/style characteristics across roles (same base model family)
- simpler operations than mixing multiple model families early

### Role Responsibilities (Hard Boundaries)

#### `joi-brain`

Purpose:
- user-facing replies
- proactive message drafting (Wind/reminder/critical content rendering)

Output style:
- natural language (user-visible)

Should not be responsible for:
- memory consolidation writes
- tension mining
- curiosity candidate generation

#### `joi-consolidator`

Purpose:
- fact extraction
- context summarization
- memory maintenance outputs (structured)

Output style:
- strict structured output (JSON/schema-driven)

Should not be responsible for:
- user-facing chat replies
- proactive topic discovery decisions

#### `joi-tension`

Purpose:
- tension topic extraction (unfinished ideas, unresolved threads, momentum candidates)

Output style:
- structured candidate topics + evidence + scores

Should not be responsible for:
- sending messages
- final timing decisions

#### `joi-curiosity`

Purpose:
- discovery / curiosity candidate generation
- propose new topic probes anchored in relevance/novelty

Output style:
- structured discovery candidates + scores/anchors

Should not be responsible for:
- user-facing replies
- direct sends
- overriding Wind guardrails

### Shared Base Model Strategy

All four roles use the same base model (current primary plan), but differ by:
- Modelfile system prompt
- role-specific constraints
- output format expectations (natural language vs JSON)
- temperature / generation parameters (role-specific tuning)

This allows role specialization without introducing multi-model ops complexity too early.

### Invocation Policy (Who Calls What)

#### User inbound message path
- `joi-brain` handles reply generation

#### Memory maintenance path
- `joi-consolidator` handles facts + summaries

#### Tension mining path
- `joi-tension` handles tension topic extraction

#### Curiosity/discovery path
- `joi-curiosity` handles discovery candidate generation

Wind orchestration decides:
- whether a topic is worth pursuing
- when to send
- which candidate to promote

LLM roles do not replace Wind orchestration logic.

### Call Priority / Budget Policy (Recommended)

Highest priority:
1. `joi-brain` (user-facing latency-sensitive calls)

Medium priority:
2. `joi-consolidator` (maintenance, deferrable)
3. `joi-tension` (topic mining, deferrable)

Lowest priority:
4. `joi-curiosity` (discovery, most deferrable)

Recommended behavior under load:
- degrade/skip curiosity first
- then defer tension/consolidation
- preserve user-facing `joi-brain` calls whenever possible

### Failure Behavior by Role

- `joi-brain` failure:
  - user-visible fallback/error path
  - highest urgency to recover

- `joi-consolidator` failure:
  - defer maintenance
  - no immediate user-facing impact

- `joi-tension` failure:
  - no new tension topics mined
  - Wind can continue using existing topics

- `joi-curiosity` failure:
  - skip discovery cycle
  - no direct user-facing impact

### Future Upgrade Path (Non-Primary Profiles)

The current plan is **same base model, multiple Modelfiles**.

Possible future evolution:
- different base models per role
- dedicated inference node(s) for maintenance roles
- stronger model for `joi-brain`, smaller model for maintenance roles
- separate capacity pool for curiosity/tension jobs

These are future options, not required for Wind v1.

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

## Rollout Plan

### Phase 0-1: Foundation + Shadow Mode ✅
- Per-conversation Wind state (wind_state table)
- Topic queue (pending_topics table)
- Decision logging (wind_decision_log table)
- Hard gates + impulse scoring
- Shadow mode: full evaluation, log decisions, no sends

Success criteria:
- Decision logs look sensible
- No obvious spammy drafts
- Topic selection quality is acceptable

### Phase 2: Basic Live Mode ✅
- Enable actual sends via `_send_to_mesh()`
- Strict caps (low daily limit, 1-2 per day)
- Limited recipient allowlist
- Aggressive cooldowns
- Simple topic lifecycle: pending → mentioned → done
- LLM message generation (joi-brain drafts proactive message)

Success criteria:
- Messages feel natural, not robotic
- No spam complaints
- User engagement on some topics

### Phase 3: Tuning ✅
Tuning existing behavior + adding natural variance (no new LLM calls needed).

**Config tuning:**
- [x] Observe real behavior patterns
- [x] Adjust factor weights, thresholds, dampers
- [x] Tune quiet windows based on activity patterns
- [x] Improve topic ranking and dedupe
- [x] Relax caps toward intended companion defaults

**Natural variance (WindMood):**
- [x] **Bounded random walk**: threshold drifts slowly over time (±0.1 from baseline)
- [x] **Accumulated impulse**: score accumulates across ticks, triggers when crossing threshold
- [x] **Soft probability**: sigmoid-based trigger probability, not hard threshold
- [x] **Per-conversation persistence**: drift and accumulation survive restarts (stored in DB)

**Context management:**
- [x] **Compact before Wind**: trigger consolidation before sending new topic
- [x] **Topic-first prompting**: structure prompt with topic as focus, context as background

Success criteria:
- [x] Wind doesn't feel robotic or predictable
- [x] No "always at 7 AM" pattern
- [x] Natural day-to-day variance

### Phase 4a: Engagement Foundation ✓ *Complete (2026-03-13)*
- [x] **Response quality feedback**: EMA engagement_score per conversation (0.5 neutral)
  - Two-tier classification: direct reply (deterministic) → LLM → 12h timeout
  - Model: `mannix/llama3.1-8b-abliterated` via `joi-engagement` Ollama model
  - No keyword matching — works across languages
- [x] **Topic engagement tracking**: detect if user engaged, ignored, or deflected
  - `pending_topics.outcome`, `outcome_at`, `retry_count`, `sent_message_id`
  - Lifecycle rules per topic type (tension/affinity/discovery/reminder/followup)
  - Re-queue with retry_count tracking; lifecycle-aware expiry
- [x] **Topic types with lifecycle**: type-specific retry and expiry behavior
- [x] **Negative feedback memory**: `topic_feedback` table with rejection_weight, 5%/day decay
  - Cooldown triggered at rejection_weight ≥ 0.7 (7-day block per family)
- [x] **Impulse factor**: engagement_score feeds into impulse calculation (weight 0.2)
- [x] **Admin commands**: show-engagement, show-feedback, topic-history, clear-cooldowns

### Phase 4b: Learning & Pursuit ✓ *Complete (2026-03-31)*
*Depends on: Phase 4a (needs engagement data)*

> **Design note**: Phase 4a introduced downward pressure only (rejection_weight → cooldown/block).
> Phase 4b adds the upward direction (interest_weight → affinity bonus). Together they make the
> system symmetric: topics that bore or annoy fade away, topics that resonate come back more.

- ✅ **Pursuit state machine**: bounded stubbornness, multi-attempt topics
  - Track attempt count per topic
  - Configurable max attempts before giving up
  - Back-off timing between attempts
- ✅ **Topic Affinity Model**: symmetric counterpart to rejection_weight accumulation
  - `interest_weight` (already in `topic_feedback` schema) accumulates on engagements
  - High `interest_weight` for a family → affinity bonus in topic selection or impulse scoring
  - Bonus is a lower effective barrier: high-affinity families get surfaced more readily
  - Accumulation is gradual (mirrors rejection: multiple engagements needed to earn it)
  - No hard cap on interest_weight, but bonus effect should be bounded (don't spam one topic)
  - Decay rate slower than rejection (interest should persist longer than annoyance fades)
  - Recurring topics for high-affinity subjects — system learns what the user actually likes talking about

### Phase 4c: Intelligence (Mostly Complete)
*Depends on: Phase 4b (benefits from affinity data)*

**Requires: Low-priority background queue** (see Background Processing Architecture)

- **Emotional follow-up**: LLM flags emotional weight during background processing
  - Higher impulse to check in with care next day
- **Unfinished threads**: LLM detects open questions/pending topics
  - Higher impulse to continue that thread
- **Outcome curiosity**: LLM extracts future events ("meeting tomorrow")
  - Surface as curiosity topic when date arrives
- ✅ **Tension extraction** (joi-curiosity model): mine unfinished threads from conversation
  - Detect open questions, unresolved topics, pending plans
  - Auto-generate tension topics from conversation history
- ✅ **Impulse/engagement feedback**: engagement outcomes feed into impulse and affinity scoring
  - `record_engagement` updates topic state and feedback model
  - Affinity/decay state machine: `mark_engaged`, `cooldown`, `retry` transitions
- ✅ **Affinity/decay**: interest_weight and rejection_weight accumulate and decay
  - Configurable `interest_decay_rate`; pursuit back-off via `pursuit_backoff_hours`
  - `convert_affinity` converts discovery topics to affinity topics on engagement
- **Curiosity/discovery** (joi-curiosity model): generate exploratory probes
  - Low-frequency probing for new interests
  - Convert successful discoveries to affinity topics
- ✅ **Special dates**: birthdays, anniversaries from stored facts
  - `_generate_special_date_topics` triggers warm check-ins
- ✅ **Spontaneous sharing**: Joi "discovers" something interesting to share
  - `_generate_spontaneous_topics` — from knowledge base, low frequency, high relevance
- ✅ **Adaptive quiet hours**: learn activity patterns
  - Track when user typically responds
  - Shift quiet windows based on observed patterns
  - `_compute_learned_quiet_start()` — exponentially-weighted circular mean over a rolling buffer of daily sign-off times (`wind_quiet_samples` table, one row per conversation per day). Requires ≥3 samples before overriding config default; recent days weighted higher than older ones.
  - Persisted to `learned_quiet_start_minutes` in `wind_state`; overrides config default in `_check_not_quiet_hours()`

### Phase 4d: Personality Variance
*Depends on: Phase 3 (WindMood foundation)*

Extends WindMood with higher-level personality features that shape engagement over days/weeks.

- **Daily mood (momentum-based)**: smooth transitions influenced by yesterday's engagement
  - Mood multiplier (0.7-1.3) affecting impulse scores
  - Good conversations lift mood, inactivity drifts it down
  - Persist per conversation in wind_state
- **Day-of-week personality**: different profiles by day
  - Weekdays: more task-focused, check-ins about work/plans
  - Weekends: more relaxed, casual topics, lighter mood
  - Monday: week ahead check-in
  - Friday: lighter, wind-down mood
- **Momentum (upward only)**: engaging conversations boost next-day impulse
  - Never goes below baseline (Joi shouldn't go quiet if user is quiet)
  - Measures: message count, response length, conversation duration
- **30-day cycle (optional)**: longer-term mood rhythm layered on daily variance
  - Adds ~30 day rhythm to baseline mood
  - Some weeks slightly more energetic/chatty
  - Config: `mood_cycle_enabled` (default: true)

Together, Phase 4a-4d transform Wind from "queue-based reminders" to "genuine companion initiative."

### Phase 5: Queue Health & Resilience (Partial)
*Depends on: Phase 2 (live mode, topic queue)*

Collects orphaned features — implemented or planned but not assigned to any phase. Focuses on keeping
the topic queue clean and Wind state coherent over time, especially across inactivity gaps.

- **Hot conversation suppression** ✅ *Implemented*
  - EMA of inter-message gap per conversation (`convo_gap_ema_seconds`)
  - When EMA ≤ `active_convo_gap_minutes`, conversation is "hot" — Wind requires longer silence before firing
  - Prevents Wind from interrupting active back-and-forth exchanges
  - Config: `active_convo_gap_minutes`, `active_convo_silence_minutes`

- **Rolling 24h daily cap** ✅ *Implemented*
  - Fire timestamps stored as `proactive_fire_times_json`; each slot expires 24h after it happened
  - Replaces the old `YYYY-MM-DD` day bucket which exhausted the cap by noon every day
  - Fatigue factor also uses the rolling count instead of `proactive_sent_today`
  - Old columns (`proactive_sent_today`, `proactive_day_bucket`) kept in schema for compatibility

- **Similar Topic Merge** ✅ *Implemented*
  - `normalize_topic_family()` groups topics by type + normalised title into families
  - `novelty_key` deduplication prevents duplicate probes per family per time window
  - End-of-day LLM dedup pass (`deduplicate_topics_for`) merges near-duplicate pending topics

- **Topic priority decay + affinity protection** ✅ *Implemented*
  - Pending topics lose priority each day; rate scales with queue depth via sqrt:
    `points = max(base, round(base × sqrt(pending_count / reference)))`
    Defaults: base=4, reference=8 → 8 topics=4 pts/day, 30→8, 100→14
  - Topics created today excluded (freshly mined topics not penalised on day 0)
  - Priority floors at 0 — topics remain selectable, just lower priority
  - **Affinity protection:** after decay, partially restores priority for topics from families
    the user likes (`restore = round(points × affinity_factor × preference_score)`)
  - **Organic undertaker release:** if an undertaker family's preference climbs above threshold,
    family is released automatically — user-driven engagement signal trumps the block
  - Config: `topic_priority_decay_points`, `topic_priority_decay_reference`,
    `topic_priority_affinity_factor`, `topic_priority_undertaker_release_threshold`

- **Wake-up procedure** ✅ *Implemented (2026-04-16)*
  - Threshold: `max(floor, min(cap, convo_gap_ema * multiplier))` with floor=72h, cap=96h, multiplier=3.0
  - Range is always 3–4 days regardless of conversation frequency (daily users → 72h, weekly+ → 96h)
  - Gated by `last_wakeup_at > last_user_interaction_at` — fires once per silence gap
  - Config: `wakeup_floor_hours`, `wakeup_cap_hours`, `wakeup_ema_multiplier`
  - Procedure (in order):
    1. **Compact context** — summarize pre-pause history so it's preserved but not raw noise
    2. **Purge expired facts** — hard-delete facts whose TTL has passed
    3. **Inject gap marker** — `[JOI-PAUSE duration=Xd dates=YYYY-MM-DD→YYYY-MM-DD]` stored as `pause_marker` context summary
    4. **Reset Wind impulse** — zero `accumulated_impulse`; mark `last_wakeup_at` so procedure doesn't re-fire
    5. **Schedule proactive re-engagement** — random UTC timestamp in the next full non-quiet window stored as `wakeup_send_at`; fires when due; cancelled automatically if user messages first
  - Proactive prompt uses core (important) facts + gap duration + last observed user mood; does not count toward `proactive_fire_times` or daily cap
  - If user messages before send: `record_user_interaction()` clears `wakeup_send_at`; gap marker is already in context so reactive response is naturally gap-aware
  - Preserves: engagement history, affinity weights, undertaker blocks, snooze, all permanent facts
  - Topic decay guard: end-of-day decay skips if user silent ≥ 2 days (max 2 consecutive decays per absence)

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

## Wind v2 Enhancement Ideas

> **Note:** These ideas are now integrated into the Rollout Plan above.
> - Phase 3: mood roll, day-of-week, probability triggering, momentum
> - Phase 4a: response quality feedback
> - Phase 4c: emotional follow-up, unfinished threads, outcome curiosity, special dates, spontaneous sharing

### Implementation Details

#### Daily Mood (Momentum-Based)
Mood multiplier (0.7-1.3) affecting impulse scores, with smooth transitions.

**Not pure random** - mood drifts based on inertia and engagement:
```
today_mood =
    yesterday_mood * 0.6          # inertia (smooth transition)
  + small_random * 0.2            # natural variance
  + yesterday_engagement * 0.2    # good conversations lift mood
```

- **Inertia**: mood changes gradually, no sudden jumps (happy → rage)
- **Engagement influence**: lots of talking yesterday → slightly higher mood today
- **Bounded randomness**: small nudge, not wild swing
- **Persist in wind_state**: track mood per conversation

Example flow:
```
Day 1: mood = 1.0 (baseline)
Day 2: good conversation → mood drifts to 1.1
Day 3: quiet day → mood drifts to 1.0
Day 4: no contact → mood drifts to 0.95
Day 5: engaged again → mood drifts back up
```

Feels like consistent "emotional weather" that shifts with relationship rhythm.

#### 30-Day Cycle (Optional, Default: On)
Longer-term mood rhythm layered on top of daily variance.

```
cycle_day = day_count % 30
cycle_position = cycle_day / 30.0
cycle_modifier = sin(cycle_position * 2π) * 0.1  # ±0.1 swing
```

- Adds ~30 day rhythm to baseline mood
- Some weeks slightly more energetic/chatty
- Some weeks slightly more introspective/calm
- Combined with daily mood = layered natural variance

**Config:**
```json
"wind": {
  "mood_cycle_enabled": true  // default: true
}
```

Why: Humans have rhythms beyond daily - this adds subtle long-term variance that makes behavior feel less mechanical over time.

#### Day-of-Week Personality
Different behavior profiles by day:
- Weekdays: more task-focused, check-ins about work/plans
- Weekends: more relaxed, casual topics, lighter mood
- Monday: week ahead check-in
- Friday: lighter, wind-down mood

#### Probability-Based Triggering
Instead of hard threshold:
```python
# Old: deterministic
if score >= threshold: send()

# New: probabilistic
if random() < score: send()
```
- Score becomes probability, not gate
- Even high scores don't guarantee immediate send
- Creates natural variance in timing

#### 4. Response Quality Feedback
Learn from how user responds to proactive messages:
- Short/dismissive replies → reduce future probability
- Engaged/long replies → increase probability
- Track per conversation, influences impulse calculation

#### 5. Momentum (Upward Only)
Good conversations boost next-day impulse:
- Yesterday was engaging (long, many turns) → higher impulse today
- Never goes below baseline (Joi shouldn't go quiet if user is quiet)
- Measures: message count, response length, conversation duration

### Good Additions

#### 6. Curiosity About Outcomes
When user mentions future events ("big meeting tomorrow", "doctor appointment"):
- Store as pending curiosity item with expected date
- Next day: genuine impulse to ask "How did it go?"
- Not scheduled reminder, but context-triggered curiosity

#### 7. Emotional Follow-Up
Detect emotional content in conversations:
- Flag during message processing (keywords: worried, stressed, excited, scared)
- Or LLM rates "emotional weight 0-3"
- Higher impulse to check in with care next day

#### 8. Unfinished Threads Detection
Check last Joi message for:
- Ended with question mark?
- Contains "let me know", "tell me later", "curious how"
- Topic introduced but no follow-up
- Higher impulse to continue that thread

#### 9. Special Dates
From stored facts (birthdays, anniversaries) and calendar:
- Birthdays trigger warm check-in
- Holidays affect mood/topic selection
- Anniversaries (if stored) get acknowledgment

#### 10. Spontaneous Sharing
Joi "discovers" something interesting:
- From knowledge base or random interesting fact
- "I was reading about X and thought of you"
- Feels organic, not silence-driven
- Low frequency, high relevance requirement

### Detection Methods

**Important:** Avoid keyword matching - unreliable across languages (Slovak, German, etc.) and with typos. Use LLM-based detection instead.

**Emotional content detection:**
- LLM annotation during message processing: "Rate emotional weight 0-3"
- Or add to consolidation prompt: "Flag if emotionally significant"
- Store flag in message metadata or wind_state

**Unfinished threads:**
- LLM check on last Joi message: "Does this expect a follow-up?"
- Or during tension extraction: flag open questions
- Track topics introduced but not concluded

**Outcome curiosity:**
- LLM extracts future events during message processing
- Structured output: `{"event": "doctor appointment", "expected_date": "2026-03-12"}`
- Store in pending_topics with due date
- Surface as curiosity topic when date arrives

### Compact Before Wind (Context Management)

Problem: When Wind initiates a new topic, old context can drown it out. The LLM gets pulled back into old threads instead of focusing on the new topic.

Solution: Trigger context compaction before sending a Wind message.

**Why compaction, not reduced context window:**
- Reduced window only helps for initial send
- User's first reply loads full context → old threads flood back
- Compaction creates a **permanent boundary** - old messages become summary

**Flow:**
```
Before Wind:  [100 messages of old context]
Compact:      [100 messages] → [1 summary]
Wind send:    [summary] + [new topic] → topic lands well
User replies: [summary] + [Wind msg] + [reply] → topic still has room
Continues:    [summary] + [few new messages] → stays manageable
```

**Implementation:**
- Before generating Wind message, check if context needs compaction
- If messages > threshold (e.g., 20), trigger consolidation first
- Then generate Wind message with fresh context
- Cost: one extra LLM call (consolidation), but ensures topic focus

**Combined with topic-first prompting:**
```
YOUR FOCUS: [new topic]

Background context (reference only):
[summary of previous conversation]
```

This ensures new topics actually land and get followed through.

### Background Processing Architecture

Wind metadata extraction runs asynchronously after user gets their response.

**Two-tier queue model:**
```
HIGH PRIORITY QUEUE          LOW PRIORITY QUEUE
─────────────────────        ────────────────────────
User messages                Wind metadata extraction
  ↓                          - Emotional weight
[Immediate response]         - Future events
                             - Unfinished threads
                             - Tension mining
                                  ↓
                             [Processed during idle]
```

**Same model, minimal context:**
- Use the same LLM (stays warm, no cold start)
- Background tasks are fast - just message + extraction prompt
- No RAG/knowledge/full context overhead

**Flush on full:**
- Low-priority queue has max size threshold (e.g., 20)
- When threshold hit, temporarily elevate to high priority
- Flush until queue drops below threshold
- Return to low priority

**Benefits:**
- User response latency unchanged
- Model stays warm (no cold start penalty)
- Prevents unbounded queue growth
- Metadata stays fresh (not hours stale)
- Natural backpressure under load

### Rejected Ideas

- **Reciprocity tracking**: Would hurt introverts who never reach out first
- **Anti-pattern detection**: Over-complicated, randomness solves it better
- **Energy matching**: User will just say "busy" - not useful signal
- **Separate smaller model**: Cold start issues, model juggling complexity

## Nice to Have (Future Exploration)

Ideas worth exploring from SOMA project review and other sources.

### From SOMA: Smarter Fact Decay

Prune old low-confidence facts automatically during consolidation:
- Facts older than N days (e.g., 30)
- Below confidence threshold (e.g., 0.5)
- Not marked as important
- Never/rarely accessed (if tracking access)

Low effort, helps prevent memory bloat.

### From SOMA: Quiet Hours Deep Consolidation

Run deeper cleanup during off-peak hours:
- Merge similar/duplicate facts
- Prune stale low-confidence facts
- Strengthen frequently-accessed facts
- Archive old summaries

Can piggyback on existing quiet hours infrastructure.

### From SOMA: Failed Query Logging

Track queries where:
- RAG returned no results
- LLM confidence was low
- User had to rephrase or clarify

Useful for identifying knowledge gaps and improving over time.

### Self-Improvement (Autonomous)

Not SOMA's over-engineered 178-arbiter approach, but autonomous nonetheless.

Different approach to be designed later. Core idea: Joi learns from interaction patterns and improves without manual intervention.

Possible signals to learn from:
- User corrections ("No, I meant...")
- Repeated clarifications on same topic
- Low engagement after proactive messages
- Explicit feedback ("That's not helpful")

Could feed into:
- Fact confidence adjustments
- Topic affinity updates
- Response style adaptation
- Knowledge gap filling

Details TBD.

### Nice to Have: Memory Tampering Awareness

Joi gets a "sixth sense" when someone directly modifies its memory (facts, RAG) outside normal LLM operations.

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

## TODO / Future Improvements

### Emotional Register for Proactive Messages
**Status:** Not started

Currently `_generate_proactive_message()` uses a single LLM call that is expected to
infer tone from RAG context (facts + summaries). In practice the generator tends toward
one implicit register — casual, low-key — regardless of topic weight.

**Idea:** Split into two LLM calls:
1. **Tone call** — given topic title/content + relevant summaries, return a short
   descriptor: `playful`, `melancholic`, `curious`, `dry`, `serious`, etc.
2. **Generation call** — same as now, but with the tone descriptor injected into the
   prompt as an explicit signal.

The tone call is small and cheap. The input is the same summaries context already
fetched for generation, so no extra DB queries. Output is one word or short phrase.

**Why:** Two people could discuss "your dad's health" very differently depending on
relationship history. The summaries carry that texture; a dedicated tone pass surfaces
it explicitly rather than hoping the generator picks it up.

**When to revisit:** After the current RAG-enriched prompt ships and produces enough
real examples to judge whether tone variability is still lacking.

---

## Known Bugs

### Direct Reply Timestamp Mismatch
**Status:** Open
**Phase affected:** 4a (engagement tracking)

When the user quotes a Wind message, the direct reply fast path (`method=direct_reply`) should match via Signal envelope timestamp. Instead it falls through to LLM classification (`method=llm`).

**Root cause:** Joi stores outbound Wind messages with `timestamp=int(time.time() * 1000)` (Joi's local clock). Signal assigns its own envelope timestamp when processing the outbound message. The quote ID the user sends is Signal's timestamp — which differs from Joi's stored timestamp by more than the 5000ms tolerance in `get_topic_by_signal_timestamp()`.

**Fix:** Capture the Signal-assigned envelope timestamp from signal-cli's send response and store that instead of `time.time()`. Requires changes to mesh send flow to return the Signal timestamp back to Joi.

**Impact:** Low — LLM fallback correctly classifies engagement, just with slightly lower confidence (0.9 vs 1.0) and quality (0.7 vs 0.8).

---

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
