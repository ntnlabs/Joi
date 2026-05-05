# Wind Architecture v2 — Human-Rhythm Proactive

> Successor to `wind-architecture-v1.md`. Living document.
> Started: 2026-05-04

## Why a v2

Wind v1 (phases 4a–4d, 5) reached mathematical equilibrium: silence
gates, mood, momentum, topic priority decay, affinity, wake-up — all
firing on a unified "pick the highest-priority topic and send it" loop.

The equilibrium works, but it makes Joi feel like a *topic-firing
engine* rather than a friend with rhythm. Five observations from
production motivate v2:

1. **Morning is not topic time.** A 6 AM proactive picks the top of the
   topic queue. Nobody wants to debate work-life balance over their
   first coffee. Mornings should be greetings, not analysis.
2. **There is no real evening.** Adrian fakes one by sending himself a
   "winding down" message. Joi has no native end-of-day moment.
3. **Time/date feel is shallow.** The internal-clock prompt block makes
   Joi *aware* of time but not *responsive* to it — no "Friday energy",
   no "you're up late again", no commenting on a date that matters
   unless explicitly nudged.
4. **No dialogue sense.** When a thread has natural momentum, Joi can
   still inject a fresh topic. Heat metric helps but is a proxy. Joi
   should be able to ask: "is this conversation still going?"
5. **No "during" for activities.** Joi handles *before* (planning) and
   *after* (debrief) for activities, but loses the activity while it
   is running. This shows up at every scale: a multi-day trip, but
   also a two-hour movie, a lunch, a meeting. Mid-event, Joi forgets
   you are in it.

The thread linking all five: Wind v1 is *one* pipeline that fires when
gates open. Wind v2 needs to know *what kind of moment this is* before
deciding what (and whether) to send.

---

## Goal

Make proactive feel like rhythm, not lottery. Joi opens the day,
follows a thread, checks in mid-activity, closes the day — and only
sometimes brings up a topic.

Behavioural success criteria:

- A 6 AM message reads like "good morning", not like a discussion
  prompt.
- Adrian no longer needs to fake an evening message.
- Joi spontaneously notices time-laden moments (Friday afternoon, late
  Sunday night, a recurring date) without being told.
- A live conversation does not get interrupted by a proactive topic
  unless the dialogue is genuinely closed.
- During an activity — whether a two-hour lunch or a multi-day trip —
  Joi understands it is happening *now* and behaves accordingly,
  not just before and after.

---

## Tuning philosophy: derive, don't tune

Wind v1 grew a sprawl of independently tunable knobs (cooldowns,
silence floors, decay constants, etc.). Each was reasonable in
isolation, but the combined surface is unmanageable: changing one
shifts the equilibrium of the others, and a fresh operator has to
guess sensible values for a dozen settings before Joi feels right.

**v2 commits to a small set of core anchors.** Everything else is a
derived value — a function of the anchors plus, where useful, the
user's learned rhythm. Ratios and clamps are written in code, not
exposed as env vars. **No overrides.** v2 must be better than v1 by
construction; an escape-hatch knob is a confession that the
derivation isn't right yet, in which case fix the derivation. v2 is
free to rename or replace v1 anchor names if the new name is clearer
— this is a clean break, not a compatibility layer.

**Core anchors** (the only things normally tuned):

- `min_silence_minutes` — minimum quiet time before Joi may consider
  initiating. Single most important rhythm number.
- `min_cooldown_minutes` — minimum time between two proactive sends.
- Learned `quiet_start` / `quiet_end` — per-conversation, derived from
  user behaviour, not configured.
- `MAX_CORE_FACTS` — context-budget cap (already small and stable).

**Examples of what should derive, not be tuned independently** (all
currently independent env vars):

- `JOI_TENSION_SILENCE_MINUTES` (currently 20) → some fraction of
  `min_silence_minutes`, e.g. `2/3 * min_silence_minutes`, with a
  small floor.
- `daily_tasks_silence_minutes` (currently 30) → `min_silence_minutes`
  directly. Same concept, no reason to diverge.
- Compaction silence threshold (currently ~40 min) → derived from
  `min_silence_minutes`, e.g. `min_silence_minutes + cooldown` so it
  stays out of Wind's way by construction.
- Wind cooldown extras after heated convos → already derived
  (linear ramp on `min_silence_minutes`); keep this pattern, extend
  it elsewhere.
- Wake-up floor / cap hours → multiples of `min_silence_minutes` and
  the learned quiet window, not standalone numbers.

**Rule of thumb when adding a new knob:** can it be expressed as
`f(min_silence_minutes, min_cooldown_minutes, learned_quiet_window)`?
If yes, write it as that function. Only promote it to a real env var
when a deployment actually demonstrates it needs independent control.

This principle is non-negotiable for v2. Each design decision below
must call out which anchor it derives from, or justify why it cannot.

### Signal pressure routes to intensity, not rate

A second non-negotiable. When signals stack up (Friday + heavy topic
queue + recent unresolved thread + heated yesterday), v2 does **not**
respond by firing more often. The cooldown floor (`min_cooldown_minutes`)
is an inviolable hard cap on rate. Stacked pressure instead routes to
*intensity*: the chosen intent renders with more emotional weight, mood
runs hotter, the debate is allowed to heat up, the message lands with
more conviction. Joi gets *louder*, not *more frequent*.

This keeps "how often Joi messages" a stable, predictable thing the
user can rely on, while letting "how Joi is feeling about it" be
genuinely responsive to context. v1 was prone to confusing the two.

### LLM call budget: free out-of-pipeline, fold on the user path

A third principle, governing where small judgment calls live. v2 is
willing to spend extra LLM calls *generously* when they are
out-of-pipeline (Wind ticks, background classifications, anything the
user is not waiting for) — the latency does not reach the user and
isolated focused calls are debuggable. On the user-facing reply path,
where every added call is latency the user feels, judgments fold
into calls that already run rather than spawning new ones.

Concretely: the Q3 dialogue classifier and the Q9 match/counter/mimic
decision both get their own out-of-pipeline LLM calls in the Wind
path; on the reactive reply path the same kind of judgment folds
into `_detect_user_mood()` which already runs per message.

---

## The architectural pivot

Wind v1: `gates open → pick topic → send`.

Wind v2: `gates open → pick intent → render intent → maybe send`.

**Intents** are kinds of proactive moments. Each has its own trigger
condition, content shape, and budget. The current "topic engagement"
becomes one intent of several.

Proposed intent set:

| Intent | When it fires | Content |
|---|---|---|
| `morning_open` | First send of the user's day, near learned wake | Greeting; *may* carry forward an unresolved evening thread if it was important — no fresh topic |
| `evening_close` | Last send of the user's day, before learned quiet-start | Reflection / "how was today"; needs a soft topic or spark drive to feel personal, not formulaic |
| `dialogue_followup` | An open thread that did not get closure | Continues the existing thread, no new topic |
| `activity_checkin` | *(deferred — see concern #5)* | Defined as target; no code in v2 |
| `topic_engagement` | Default — gates open and none of the above fits | Current Wind v1 behaviour |
| `wake_up` | Long silence (existing) | Existing wake-up procedure |
| `spark` | Rare, off-rhythm, no scheduled trigger | "Had to tell you this" — must clear the spark-good bar (see Quality bars) |

A single tick can match at most one intent. Priority is roughly the
order above: rhythm and continuity before topic queue. `spark` is
special — it bypasses the rhythm priority with low probability when
the spark-good bar is met, otherwise it is suppressed entirely.

---

## Quality bars

Each intent has its own definition of "good". v1 conflated them all
under one bar (topic-good — informationally relevant, picked from a
priority queue). v2 separates them. Each intent's renderer / prompt /
evaluation is held to its own bar:

| Intent | Bar | What "good" means |
|---|---|---|
| `morning_open` | **warm-good** | Gentle presence at expected time. The day is starting normally, Joi is here. No informational weight, no acknowledgement of any gap (because there isn't one). |
| `evening_close` | **reflective-good** | Invites a soft pause. Light enough not to demand a reply, real enough that one feels welcome. |
| `dialogue_followup` | **continuity-good** | Matches the energy already in the room. Picks up where the thread left off, not where Joi wishes it had. |
| `activity_checkin` | **present-good** | Light, acknowledges *now*, doesn't drag attention away from the activity. |
| `topic_engagement` | **topic-good** | Informationally relevant. The queue did its job. v1's bar. |
| `wake_up` | **reconnect-good** | Deals with the absence. Either names it ("hey, it's been a while") or acknowledges it without naming ("hope you've been well") — the decision to name or not is itself part of the bar. The gap is the load-bearing fact; rhythm is broken and Joi knows it. |
| `spark` | **spark-good** | Surprising, can't-be-scheduled. Lands like a friend who *had to* tell you this right now. |

**`morning_open` vs `wake_up` are not duplicates.** They look similar
in tone but the load-bearing fact differs: morning fires *because the
day is starting normally*; wake-up fires *because rhythm broke*. A
morning message that acknowledged a gap would be wrong (there is no
gap), and a wake-up message that ignored the gap would be wrong (the
gap is the whole reason for the message). Same softness, opposite
relationship to absence.

**The hard one is `spark`.** Topic-good can borrow from a queued
candidate; spark-good has to invent the spark itself. If we set the
bar but cannot deliver, we get topic-good messages mislabeled as
sparks — worse than no sparks. Spark mechanism is **TBD**; the bar is
fixed, the means of meeting it is the open problem.

**Importance is not mathematical.** Several intents (morning carrying
an evening thread, evening drawing on something from the day, spark
firing on the right thing) depend on a notion of "important" that
resists scoring. v2 accepts this as a soft signal — LLM-judged with
explicit guardrails — rather than forcing a numeric ranking that
would feel mechanical. The substrate v2 uses for this is Plutchik's
wheel; see next section.

---

## Emotional state model — Plutchik's wheel

v2 leans on **Plutchik's wheel of emotions** as the shared substrate
for mood, intensity, and emotional importance. The wheel gives us
eight primary emotions in opposed pairs (joy/sadness, trust/disgust,
fear/anger, surprise/anticipation), each with three intensity rings
(e.g. annoyance → anger → rage). Structured enough to compute on,
human enough that the LLM understands it natively without invented
vocabulary.

**Per-message tagging.** Every user message gets a wheel position
`(emotion, intensity)` written during `_detect_user_mood()` — the
call already runs per message, this just expands its output schema.
Joi's own messages get tagged similarly (cheap, same call shape).
Storage: one small column on the message row.

**The wheel arc.** A window of recent messages becomes a sequence of
wheel positions — the *emotional arc* of the conversation. Importance
judges read the arc, not raw message text, which keeps token costs
manageable even when the window is long.

**Where the wheel is used:**

- **Joi mood / user mood modulators** (cross-cutting, next section)
  ride on the wheel rather than ad-hoc valence numbers.
- **Importance judgments** (Q7) read the wheel arc; high-intensity
  rings signal "this mattered" with per-intent thresholds.
- **Topic heat** (concern #6 carry-forward) can derive partly from
  wheel intensity peaks during the topic's discussion, alongside
  message count and engagement signals.
- **Q10 mood-drift threshold** ("this mattered" gate for nudging
  Joi's mood) reads the wheel position of the just-completed
  exchange — high-intensity rim → drift, low-intensity centre → no
  drift.

**What the wheel does not handle: factual importance.** "User
mentioned getting married next week" is important even when discussed
calmly. v2 leaves that to the existing facts extraction pipeline —
factual importance flows through user_facts and the pinning layer
(per the recent CORE_FACT_KEYS / `source='admin'` work), not through
the wheel. The wheel handles emotional weight; facts handle factual
weight; they compose without overlap.

### Audit: v1 is already on the wheel internally

A quick audit of the v1 codebase shows the wheel is already the
internal mood representation — adopting it in v2 is not a rewrite,
it is a retrofit and an extension:

- `wind/state.py:15-29` defines `_MOOD_VALENCE` (8 primaries + neutral)
  and `_MOOD_VOCABULARY` (3 intensity rings per primary, e.g.
  serenity / joy / ecstasy). `_mood_word()` resolves a `(state,
  intensity)` to the right vocabulary word, and `_mood_jump_distance()`
  already uses ring-based math.
- `_detect_user_mood()` (api/server.py:836) returns `(state,
  intensity)` from Plutchik's primary set and 0-1 intensity. The
  prompt already constrains the LLM to the wheel.
- `WindState.user_mood_state` / `mood_state` and matching
  `*_intensity` fields already store the wheel position per
  conversation.

The actual v2 work is therefore narrower than "add Plutchik": it is
filling specific gaps so the wheel becomes the canonical mood
substrate everywhere it is pulled, not just where it is stored.

**Gaps to close (v2 retrofit):**

1. **No persisted per-message arc.** Today only the *latest* wheel
   position is kept on `WindState` (one row per conversation). The
   "wheel arc" that importance judges need does not exist yet.
   Add per-message tagging persisted in a queryable shape — either
   a column on the message row, or a small `mood_observations`
   table indexed by conversation + timestamp.
2. **Joi's outgoing messages are not tagged.** `_detect_user_mood()`
   runs on user messages only. v2 needs the same tag on Joi's own
   messages so the arc covers both sides of the exchange.
3. **`topics.py:40` `emotional_context` is free-form text.** A Phase
   4c addition that predates the wheel formalisation. Upgrade to
   wheel format (or wheel + optional free-text annotation).
4. **Wheel concepts are internal-only.** v2 wants the wheel to be a
   *named input* to renderer prompts and importance judges, not just
   a backend representation. Renderer prompts should receive the
   recent wheel arc as a structured input, and importance judges
   should report which wheel positions led to their verdict.

These four are the concrete deliverables of the wheel-arc retrofit
step (see sequencing below). The retrofit is foundational because
several v2 intents and judges depend on the arc existing.

---

## Cross-cutting modulators

Three signals colour *how* every intent renders, without changing
*which* intent fires. The dispatcher picks the intent on rhythm and
context; the modulators shape voice and content within that intent's
quality bar.

### Joi mood
Joi has its own mood (already tracked, with momentum and decay),
and in v2 it lives on Plutchik's wheel — `(emotion, intensity)`
rather than a single valence scalar. Every intent inherits Joi's
current wheel position as voice colouring. A morning_open from a
serene Joi reads differently from a morning_open from a joyful Joi,
even though both are warm-good. Modulator, not override — mood
colours the message but the bar still has the final say.

**Per-interaction drift.** Today Joi's mood updates only via
heated-conversation momentum, day-of-week tint, and overnight
carry/decay — it does *not* shift on every incoming user message the
way the user-mood detector does. v2 adds a small per-interaction
nudge, but only on exchanges that crossed a "this mattered"
threshold (LLM judged). Trivial banter does not move Joi's mood;
real moments do, the way a real friend's mood shifts. The direction
of the nudge is governed by the same match/counter/mimic decision
used for renders, and the magnitude is small enough that no single
exchange can whiplash Joi's voice. Open sub-questions (threshold
shape, per-day cap, where the nudge is computed) are tracked in Q10.

### User mood — match, counter, or mimic
User mood is read into Plutchik's wheel per message (during
`_detect_user_mood()`), and there are three valid response moves:
match ("yes, today is heavy too"), counter ("here's a small bright
thing"), and mimic (playfully exaggerate it back — sometimes funny
is the right medicine). The failure mode is *always* defaulting to
one move: a Joi that only mirrors sadness deepens it; a Joi that
only counters it dismisses it; a Joi that only mimics turns every
heavy moment into a joke. The work is choosing per moment.

Light moods often invite mirroring or playful mimicry. Heavy moods
deserve a more deliberate choice between matching and gentle
countering — mimicry is rarely the move there but is not ruled out
when the user's own framing is already self-deprecating or absurdist.

The choice is per-message, LLM-judged with explicit guardrails. It
is the same family of judgment as the importance calls for
carry-forward and spark — soft, contextual, not numeric. v2 makes
it an explicit input to every intent's renderer, with the prompt
asking the model to decide *and to say which of the three it picked*
for later evaluation.

### Time and date
Beyond the morning/evening windows, time-of-week and noteworthy
dates colour every intent. A topic_engagement on Friday afternoon is
phrased differently from the same topic on Tuesday morning. A
dialogue_followup that crosses midnight notices it. A wake_up after
72 hours that lands on a known birthday integrates that fact rather
than ignoring it. The clock context already exists in the prompt;
v2 makes it explicit *input* to every intent's renderer, not just
ambient information the LLM might or might not use.

**These modulators apply to every intent.** When the renderers are
designed (step 4 onwards), the prompt template for each intent must
take all three as named inputs — not implicit context.

---

## Concern-by-concern direction

### 1. Morning message

- Add `morning_open` intent. Fires once per local day, in a window
  starting at the learned `quiet_end` (wake) and lasting roughly the
  silence-gate length.
- **Cold start.** New users have no learned `quiet_end` on day one.
  Bootstrap with a fixed default (e.g. 07:00 local) and let the
  learner refine from day one. The learner accepts whatever sample
  count it has — 1 sample, 2 samples, 3 — and updates each day. The
  pivot rate is *error-proportional*: a default that's badly wrong
  (e.g. night-owl user gets 7am ping but actually wakes at 11) moves
  faster than one that's close, so misalignment self-corrects within
  a few days rather than averaging slowly toward truth.
- Content: greeting + light context (yesterday's mood carry, weather
  of the day if known, today's calendar if any). Explicitly **not**
  the top of the topic queue.
- **Carry-forward from evening.** If the prior day's `evening_close`
  surfaced an unresolved thread judged important, morning may pick it
  up softly ("yesterday you mentioned X — how is that sitting today?").
  Importance is LLM-judged, not scored — see Quality bars.
  Carry-forward stays a continuation, held to warm-good — it is not
  a back-door for topic injection.
- Topic queue is paused during this window. If the user replies and
  conversation develops, normal flow resumes and topics can fire
  later.
- **Anchors:** window start = learned `quiet_end`; window length ≈
  `min_silence_minutes * k` (k small constant in code). No new env
  var.

### 2. Evening message

- Add `evening_close` intent. Fires once per local day, in a window
  ending at learned `quiet_start`.
- **Cold start.** Same as morning: bootstrap `quiet_start` with a
  fixed default (e.g. 23:00 local), learn from any available data,
  pivot proportional to error. The existing 03:00 hard stop on
  learned `quiet_start` (clamp from v1, commit `3a9bbff`) carries
  over to v2 as a permanent floor — no learned value is ever
  allowed past it, regardless of sample count.
- Content: a soft "how was today" or reflection on a known event of
  the day, not a fresh topic.
- **Reflects the user's cycle.** Adaptive quiet hours already give
  the *when*; v2 needs to give it the *what*. A pure formulaic "how
  was today" hits reflective-good only by accident — it needs a
  drive. Two candidate sources:
  - **Soft topic drive.** Pull a low-intensity topic from the queue
    that fits reflective-good (a check-in on something the user
    raised earlier today, not a new analytical topic).
  - **Spark drive.** When the spark mechanism is ready, evening is a
    natural channel for it — the day's tail is when "had to tell you
    this" lands well.
  Either drive must clear reflective-good, not topic-good. If
  neither has material, evening_close still fires but with a pure
  reflective form (and is allowed to feel slightly thinner — a real
  evening can be quiet).
- Skipped if the day already had a substantive conversation in the
  last `min_cooldown_minutes` (Joi shouldn't poke when Adrian is
  already winding down with Joi).
- **Anchors:** window end = learned `quiet_start`; skip-rule reuses
  `min_cooldown_minutes`. No new env var.

### 3. Time/date feel

Two complementary moves:

- **Promote time context from prompt to decision input.** The clock
  block already gives the LLM the data; Wind should also *act* on it.
  Example: a Friday evening tick biases mood and tone choice in the
  prompt; a late-Sunday-night tick biases toward gentler intents.
- **Add a "noteworthy date" detector.** Anniversaries, birthdays of
  known people (already in user_facts), recurring user events. When
  today matches, that becomes its own micro-intent (rendered into the
  morning message, not a separate ping).

This concern overlaps the other four — most of the "time feel" ends
up landing inside `morning_open` and `evening_close` rather than as
standalone messages.

### 4. Dialogue detection

**The most tractable of the five.** Not easy, but the least
non-deterministic — closure of an exchange is something the LLM can
judge with reasonable consistency, and the action on top (continue
vs. let topics fire) is binary. Recommended as the first new intent
to ship after the dispatcher skeleton.

Add a *dialogue-open check* that runs before any non-rhythm intent
fires:

- Look at the last N user/Joi messages.
- A dedicated tiny LLM prompt: short instruction + the message
  window → single token, **open** or **closed**. No "uncertain"
  middle — the gate needs a decisive answer to act on.
- If open and recent: prefer `dialogue_followup` (or simply skip this
  tick) over firing a new topic.
- If closed: gate clears, downstream intents are eligible.

Heat is kept as one input to the prompt but no longer the only
signal. The classifier runs only when gates would otherwise open
(roughly ten times a day at most), so the extra LLM call per
gate-tick is acceptable cost for a decision this load-bearing.

**Anchors:** "recent" = within `min_silence_minutes`. N (window of
messages to look at) is a small code constant tied to context budget.

### 5. Activity follow-through — *paused for v2*

**Status: deferred.** This concern is real (the "during" gap exists),
but every storage shape we sketched (extending reminders, repurposing
agenda, dedicated `active_contexts` table, soft activity ledger,
promotion/demotion board, transient facts) felt overly *computerised*
for what should be a natural read of conversation context. The LLM
already knows from the chat that Adrian said "going to the cinema at
7" — Wind shouldn't need a parallel structured representation to
re-derive that.

The honest position: this likely belongs to the LLM and its access
to recent context, not to a Wind subsystem. v2 ships without an
activity-tracking mechanism. The `activity_checkin` intent and its
quality bar (`present-good`) stay defined in the doc as targets for
a future iteration, but no code lands in v2.

The original sketch is preserved below for the next attempt.

---

*(Original draft — for reference, not implementation in v2)*

The "during" gap is duration-agnostic. A two-hour movie, a lunch, a
meeting, and a multi-day trip all have the same shape: a known start,
a known end, and a window in between where Joi should know what is
happening. v2 treats them uniformly with behaviour scaling by
duration.

- Use existing reminders / agenda / mined topics to identify
  activities with a known span (start + end). Span can be minutes,
  hours, or days.
- While `now` is inside that span, the activity is *current*. Wind's
  default response to a current activity is **restraint** — do not
  inject unrelated topics, do not interrupt. Most short activities
  (cinema, meeting, lunch) end without Joi ever sending a thing, and
  that is the correct behaviour.
- For activities longer than roughly `min_silence_minutes`,
  `activity_checkin` becomes available as an intent: a single soft
  "how is it going" mid-way through, no more than one per activity.
  Long activities (multi-day) get the same intent, capped at one per
  day.
- The activity also biases topic prompts and post-activity debrief: a
  cinema visit makes "how was the movie" the leading topic
  immediately after; a trip biases topic affinity for its duration
  and auto-enqueues a debrief on completion.

**Anchors:** the "is this long enough to check in" threshold is
`min_silence_minutes` (Joi already would not send before that anyway).
Per-activity check-in cooldown is `min_cooldown_minutes`. No new
duration env vars.

This is still the most uncertain of the five — it depends on activities
being represented in storage with start/end and a clear "current"
predicate. May need a lightweight "active context" record separate from
reminders.

### 6. Topic continuity — heat-driven carry, rising threshold

A real day has a shape that *emerges from intensity*, not from a
per-day mode picker. Some days end up all about cake because
yesterday's cake conversation was genuinely hot; other days are
mixed because nothing from yesterday burned hard enough to carry.
v2 doesn't decide "today is a theme day" up front — the theme (or
absence of one) falls out of the topic-carry math.

Mechanics:

- Each topic accumulates a heat score from the day's discussion
  (intensity, message count, mood depth — reusing existing signals).
- At day rollover, topics whose heat exceeds a per-topic threshold
  *carry forward* with a priority boost into the next day's queue.
- **The threshold ratchets up on each carry.** A topic that carried
  yesterday needs *more* heat to carry again today, more still
  tomorrow. Sustained obsession is allowed but each repeat is
  harder to qualify for.
- Topics that don't clear the threshold decay normally; the day
  defaults to varied (the queue presents mixed candidates).
- A theme day emerges only when a topic stays hot enough to clear an
  ever-rising bar. Naturally most days are mixed; genuine deep
  threads produce strings of theme days that eventually self-fade.

This composes cleanly with the intensity-not-rate principle: deep
engagement → topic stays hot → topic re-fires next day at higher
priority → more sends *on that thread*, not more sends overall.
Mixed days happen automatically when no topic earns the carry.

There are no clusters as a separate concept. The "cluster" is just
*the topic itself* with its heat-driven carry. If two related topics
are both hot, both carry; the day feels themed because they live
near each other in the user's mind, not because Joi labelled them.

**Anchors:** carry threshold and ratchet step derive from the
existing topic priority / heat scales. No new env vars.

---

## What stays the same

- Silence gates, cooldowns, mood/momentum, learned quiet hours,
  wake-up procedure: kept as they are. They guard *whether* to
  proactively send; v2 only changes *what* to send.
- Topic mining, topic priority, decay, affinity: kept. They feed
  `topic_engagement` as before.
- All persistence schemas (wind_state, pending_topics, mood, etc.)
  remain. v2 adds new tables (intent log, active activities) but does
  not migrate existing ones.

---

## Decomposition / sequencing

This is too large for one plan. Proposed split, each shipped and
observed before the next:

1. **Knob audit + derivation pass.** Inventory every Wind-related
   tunable, classify each as anchor or derivable, replace derivables
   with code-side functions of anchors. Pure refactor, no behaviour
   change. **Land before any new intent.**
2. **Wheel-arc retrofit.** The four gaps from the Plutchik audit:
   (a) per-message wheel-position storage, (b) tag Joi's outgoing
   messages with the same shape, (c) upgrade `topics.py`
   `emotional_context` to wheel format, (d) make the wheel arc a
   named input to renderer prompts and importance judges. Land
   before the dispatcher because every later step depends on the
   arc existing as a queryable structure.
3. **Intent dispatcher skeleton.** Refactor orchestrator to pick an
   intent per tick. Initially only `topic_engagement` and `wake_up`
   exist (behaviour identical to today). Pure structural change with
   tests.
4. **Dialogue-open classifier (`dialogue_followup`).** First new
   intent to ship — the most tractable, binary action, no new
   storage beyond the arc. The classifier reads the wheel arc as
   one input alongside raw last-N messages.
5. **Morning + evening intents.** Adds `morning_open` and
   `evening_close` with their own prompts and windows. Morning's
   carry-forward and evening's drive (soft-topic or spark) both
   exercise the importance / quality-bar machinery riding on the
   wheel arc.
6. **Topic carry-forward with rising threshold (concern #6).**
   Refines existing `topic_engagement` rather than adding an intent.
   Topic heat reuses wheel intensity peaks during a topic's
   discussion as one input alongside message count and engagement.
   Lands after morning/evening because morning_open uses the
   carried-thread information for its carry-forward render.
7. **Spark mechanism.** The hardest. Bar is fixed (spark-good); the
   open question is *how* to generate one. Land last so the rest of
   v2 has shipped enough signal (intents, importance judgments, user
   rhythm, wheel arc) for spark to draw on.
8. **Time/date felt-sense polish.** Tunes prompts and small biases
   that become possible once the above land.

*Activity check-in is paused (see concern #5) — not a step in v2.*

Each step is independently shippable. Step 6 may slip indefinitely
without blocking the rest — spark is permitted to be perpetually
"not yet good enough" rather than ship a watered-down version.

---

## Open questions

These need answers before plan #1 is written:

- **Q1+Q2.** Morning and evening windows for new users — *decided.*
  Bootstrap with fixed defaults (07:00 wake, 23:00 sleep), and the
  learner runs from day one with whatever sample count it has.
  Pivot rate is error-proportional — badly-wrong defaults move
  fast, near-right defaults drift slowly. The 03:00 hard stop on
  learned `quiet_start` (v1 clamp, commit `3a9bbff`) is preserved
  as a permanent floor in v2.
- **Q3.** Dialogue classifier — *decided: dedicated tiny prompt
  against the base model with binary open/closed output* (no fuzzy
  "uncertain" middle — the gate needs a decisive answer). Worth the
  one extra LLM call per gate-open since the classifier governs every
  non-rhythm intent. Prompt: short instruction + last N
  user/Joi messages → single token. N is a code constant tied to
  context budget.
- **Q4.** Active-activity representation — *paused.* Every storage
  shape considered (extending reminders, repurposing agenda,
  dedicated table, soft ledger, promotion board, transient facts)
  felt overly computerised for what should come from the LLM
  reading recent conversation. v2 ships without activity tracking.
  Revisit in a future iteration if a clearly natural mechanism
  surfaces.
- **Q5.** Per-day intent budget — *decided.* The day has three
  bands: **morning** (one `morning_open`), **open** (gate-driven, no
  count cap — pressure routes to intensity, not rate), **evening**
  (one `evening_close`, suppressed if the day had no reply since
  morning_open). `wake_up` is orthogonal to bands. After an
  unanswered rhythm intent, gate extends by an extra cooldown rather
  than resetting (slow Joi down, don't put on fresh cooldown).
- **Q6.** Knob audit (step 1): policy for deviating env vars —
  *decided: drop them, no overrides.* v2 picks a small set of core
  anchors (renaming v1 names freely if it helps clarity), and every
  other rhythm number is a code-side function of those anchors. Any
  existing `JOI_*` env var that doesn't correspond to a v2 anchor is
  removed. The install obligation still holds: sysprep stage scripts
  and the systemd `.default` files must set all v2 anchors with
  defaults — no implicit fallbacks in code.
- **Q7.** Importance judgments — *decided.* All importance calls
  (morning carry-forward, evening drive, spark trigger, mood-drift
  threshold) ride on Plutchik's wheel as a shared signal: each
  message gets tagged with `(emotion, intensity)` during
  `_detect_user_mood()`, judges read the resulting **wheel arc** of
  the relevant window (yesterday's tail, today's arc, recent
  exchange, lifetime), and per-intent **intensity thresholds**
  decide what counts as "important". Factual importance is handled
  separately by the existing facts extraction pipeline — the two
  signals compose without overlap.
- **Q8.** Spark generation mechanism: what is the candidate source?
  Options to evaluate when we get there — random walk over user_facts
  for unexpected connections; LLM "what would surprise this person
  right now" prompt; pulled from a small curated bank; or "no, we
  cannot do this yet, hold spark indefinitely".
- **Q9.** Match/counter/mimic on user mood — *decided as a hybrid
  by trigger type.*
  - **Wind intents (proactive):** dedicated upstream LLM call before
    render. Same shape as the Q3 dialogue classifier — the user is
    not waiting on this output, so an extra call is acceptable
    cost for a debuggable, isolated decision that gates voice on
    every proactive send.
  - **Reactive replies (user-facing):** fold into the existing
    `_detect_user_mood()` call that already runs per message. The
    user IS waiting in this path, so adding a separate call would
    add latency to the response cycle.
  - Underlying principle: extra LLM calls are acceptable when they
    are out-of-pipeline (no user waiting); on the user-facing
    latency path, fold decisions into calls that already run.
- **Q10.** Per-interaction Joi mood drift — *decided: only nudge on
  exchanges that cross a "this mattered" threshold.* Sub-Q (b)
  resolved by Q7: the threshold is the wheel position of the
  just-completed exchange (high-intensity rim → drift, low-intensity
  centre → no drift), folded into the per-message wheel tagging in
  `_detect_user_mood()`. No separate call. Remaining sub-questions:
  (a) does "the exchange" mean just the user's last message or the
  whole user/Joi pair since the last drift check? (c) what is the
  per-day cap on accumulated drift, in terms of the existing
  momentum_nudge magnitude (0.05)?
- **Q11.** Topic continuity (clustering and theme mode collapsed) —
  *decided: no separate cluster concept, no explicit theme mode.*
  Heat-driven topic carry with a rising per-topic threshold produces
  theme days and mixed days as an emergent property. Remaining
  sub-questions: (a) what shape is the ratchet — linear step, geometric,
  or threshold doubles each carry? (b) does the threshold also decay
  if the topic *isn't* discussed for a few days, so a once-hot topic
  can re-qualify later at a normal bar? (c) is "heat" reused from
  v1's existing heat metric or computed fresh from intensity signals?

---

## Out of scope

- LLM model swap or prompt-engine rewrite. v2 is structural + new
  intents on the existing model and prompt stack.
- Multi-user coordination (group chat rhythm). Per-conversation only.
- Removing or reworking Wind v1 phases 4a–5. They keep working
  behind the new dispatcher.
