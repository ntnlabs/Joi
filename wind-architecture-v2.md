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
exposed as env vars, unless an operator has a real reason to override.

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
| `activity_checkin` | User is currently inside a known activity (any duration) | Restraint mode for short events; "how is it going" for long ones |
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
would feel mechanical.

---

## Cross-cutting modulators

Three signals colour *how* every intent renders, without changing
*which* intent fires. The dispatcher picks the intent on rhythm and
context; the modulators shape voice and content within that intent's
quality bar.

### Joi mood
Joi has its own mood (already tracked, with momentum and decay).
Every intent inherits Joi's current mood as voice colouring. A
morning_open from a mellow Joi reads differently from a morning_open
from a buoyant Joi, even though both are warm-good. Modulator, not
override — mood colours the message but the bar still has the
final say.

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
User mood matters, and there are three valid moves: match ("yes,
today is heavy too"), counter ("here's a small bright thing"), and
mimic (playfully exaggerate it back — sometimes funny is the right
medicine). The failure mode is *always* defaulting to one move: a
Joi that only mirrors sadness deepens it; a Joi that only counters
it dismisses it; a Joi that only mimics turns every heavy moment
into a joke. The work is choosing per moment.

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
- A small LLM classification: "is this exchange closed, or does it
  invite continuation?"
- If open and recent: prefer `dialogue_followup` (or simply skip this
  tick) over firing a new topic.

Heat is kept as one input but no longer the only signal. The
classifier is cheap and runs only when gates would otherwise open.

**Anchors:** "recent" = within `min_silence_minutes`. N (window of
messages to look at) is a small code constant tied to context budget.

### 5. Activity follow-through

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
2. **Intent dispatcher skeleton.** Refactor orchestrator to pick an
   intent per tick. Initially only `topic_engagement` and `wake_up`
   exist (behaviour identical to today). Pure structural change with
   tests.
3. **Dialogue-open classifier (`dialogue_followup`).** First new
   intent to ship — the most tractable, binary action, no new
   storage. Validates the dispatcher under real traffic before
   harder intents land.
4. **Morning + evening intents.** Adds `morning_open` and
   `evening_close` with their own prompts and windows. Morning's
   carry-forward and evening's drive (soft-topic or spark) both
   exercise the importance / quality-bar machinery.
5. **Activity check-in.** Adds activity tracking (any duration) and
   the `activity_checkin` intent.
6. **Spark mechanism.** The hardest. Bar is fixed (spark-good); the
   open question is *how* to generate one. Land last so the rest of
   v2 has shipped enough signal (intents, importance judgments, user
   rhythm) for spark to draw on.
7. **Time/date felt-sense polish.** Tunes prompts and small biases
   that become possible once the above land.

Each step is independently shippable. Step 6 may slip indefinitely
without blocking the rest — spark is permitted to be perpetually
"not yet good enough" rather than ship a watered-down version.

---

## Open questions

These need answers before plan #1 is written:

- **Q1.** Morning window: anchored on learned `quiet_end` only, or
  also a default for new users with no learned rhythm yet?
- **Q2.** Evening window: same question.
- **Q3.** Dialogue classifier: dedicated tiny model, or repurpose the
  curiosity/engagement model?
- **Q4.** Active-activity representation: extend reminders with a
  `span` flag, repurpose agenda items, or new `active_contexts`
  table?
- **Q5.** Per-day intent budget: at most one rhythm intent per day,
  or allow morning + evening + one topic? What does the silence-gate
  math do when an intent fires that the user does not reply to?
- **Q6.** Knob audit (step 1): what is the policy for env vars that
  exist today and *do* deviate from the derivation rule? Drop them
  silently, log a deprecation warning, or keep as overrides until v3?
- **Q7.** Importance judgments (carry-forward, evening drive, spark):
  one shared "is this important" prompt, or per-intent prompts? How
  much of the conversation history does the judge see, and how is it
  budgeted?
- **Q8.** Spark generation mechanism: what is the candidate source?
  Options to evaluate when we get there — random walk over user_facts
  for unexpected connections; LLM "what would surprise this person
  right now" prompt; pulled from a small curated bank; or "no, we
  cannot do this yet, hold spark indefinitely".
- **Q9.** Match/counter/mimic on user mood: who decides? Options —
  let the renderer prompt ask the model to decide per-message; or do
  a small upstream classification first and pass the decision in.
  The upstream version is more debuggable; the inline version costs
  less and may decide better with full context. Default until
  evidence: inline.
- **Q10.** Per-interaction Joi mood drift — *decided: only nudge on
  exchanges that cross a "this mattered" threshold.* Remaining
  sub-questions: (a) what does the "mattered" judgment look at — the
  user's last message alone, or the whole exchange since the last
  Joi reply? (b) is the threshold a separate LLM call or folded into
  the existing user-mood detector / match-counter-mimic prompt to
  save tokens? (c) what is the per-day cap on accumulated drift, in
  terms of the existing momentum_nudge magnitude (0.05)?

---

## Out of scope

- LLM model swap or prompt-engine rewrite. v2 is structural + new
  intents on the existing model and prompt stack.
- Multi-user coordination (group chat rhythm). Per-conversation only.
- Removing or reworking Wind v1 phases 4a–5. They keep working
  behind the new dispatcher.
