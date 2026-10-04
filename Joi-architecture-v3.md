# Joi Architecture v3

> **Status:** Living design document. Updated as decisions are made.
> **Branch:** `v3` (stable line: tag `v2.0` on `main`)
> **Builds on:** `Joi-architecture-v2.0.md` — v3 describes *changes* to v2.0;
> anything not mentioned here stays as v2.0 describes it.
> **Started:** 2026-10-04

## Why v3

v2.0 works: Joi runs, remembers, and messages proactively. But the behaviour
engine has hit its limits, as recorded in the planning and idea documents:

- **Proactive feels like a topic lottery, not rhythm.** No real morning or
  evening, shallow time sense, no dialogue sense, no "during" for activities
  (`wind-architecture-v2.md`, "Why a v2").
- **Mood and context barely reach replies.** Joi and user mood live in Wind
  state but rarely shape a reactive reply.
- **Memory has no behaviour around it.** Joi cannot cleanly accept a
  correction, forget on request, say "I'm not sure", or follow standing rules
  like "keep it short" or "don't bring this up again"
  (`ideas/memory-improvement-ideas.md`).
- **Changes can't be measured.** There is no evaluation harness; behaviour
  changes are judged by feel.
- **Memory is one undifferentiated pile.** Facts, episodes, rules, notes and
  Wind feedback are not distinct kinds of memory, and records carry little
  metadata or provenance.

v3 makes Joi more human-like by rewriting the behaviour engine around those
limits.

## Principles

1. **Rewrite in place, never from scratch.** v3 evolves the v2.0 codebase.
   Every change starts from existing code and states what changes. No parallel
   systems, no new core written beside the old one.
2. **Contracts first.** A few stable seams are designed and built before new
   features, so later features plug in instead of forcing rewrites. Adding
   richer metadata, a new retrieval signal or a new intent must not ripple
   through the codebase.
3. **Only design seams for known needs.** Seams are shaped around what the
   planning and idea documents already ask for — not around speculation.
4. **Behaviour before capabilities.** New capabilities (search, System
   Channel, openhab, Zabbix, imagegen, TTS) wait until after v3.
5. **v2.0 invariants hold.** Everything in "Architectural Invariants" of
   `Joi-architecture-v2.0.md` carries over unchanged.
6. **Wind v2 principles become Joi-wide.** "Derive, don't tune" and "signal
   pressure routes to intensity, not rate" (`wind-architecture-v2.md`) apply to
   the whole behaviour engine, not just proactive messaging.

## Scope

### In v3

| Area | Source | Order |
|------|--------|-------|
| Stable seams (contracts) | this document | **First** |
| Behavioural memory: correction and forgetting, anti-confabulation, procedural rules, Wind governance | `ideas/memory-improvement-ideas.md` | **First feature** |
| Reply-path behaviour: sitrep, match/counter/mimic, mood drift on replies | `wind-architecture-v2.md` | Later |
| Proactive rhythm: intent dispatcher, morning/evening, dialogue follow-up, topic carry, spark | `wind-architecture-v2.md` | Later |
| Evaluation harness for memory and behaviour | `ideas/memory-improvement-ideas.md` | Later |
| Deep memory rework: episodes, richer metadata, provenance, multi-signal retrieval | `ideas/memory-improvement-ideas.md`, `ideas/memory-scaling-ideas.md` | Later |

The order of the "Later" rows is not decided yet.

Scope limits in `wind-architecture-v2.md` ("Out of scope": no prompt-engine
rewrite, no rework of Wind v1 phases 4a–5) are **lifted** where v3 needs it.
That doc stays the detailed design for proactive behaviour.

### Fixed before beta

All v2.0 known gaps (`Joi-architecture-v2.0.md`, "Known Gaps") must be fixed
before v3 reaches beta:

1. Config-sync cadence vs. mesh staleness watchdog (idle Joi lets mesh clear
   the HMAC key)
2. Unlinked Signal device not detected at runtime
3. Failed proactive sends are silent (no retry, no awareness, no visibility)
4. Failed HMAC rotations retry daily instead of hourly

### Not in v3

- New capabilities (see Principle 4)
- Multi-user coordination of group-chat rhythm (per-conversation only)

## Approach: Contracts First

Four seams are designed first. Each is one interface the rest of the system
depends on; what sits behind it can change without touching callers.

1. **Memory record.** One record shape for every kind of memory, with
   extensible metadata (type, source, scope, speaker, confidence, provenance,
   validity). Correction and forgetting are operations on validity
   (superseded, expired), not special cases.
2. **Recall.** One retrieval interface that returns typed items with
   provenance. Ranking is internal, so retrieval can evolve (FTS → hybrid →
   multi-signal) without callers changing.
3. **Turn pipeline.** One sequence for replies and Wind alike: perceive →
   assemble → decide → render → validate → send → log. Each stage has fixed
   inputs and outputs. Generalises the Wind v2 intent dispatcher to replies.
4. **Decision log.** One structured, privacy-safe record of what was used,
   what was decided and why. Feeds the evaluation harness and Wind governance
   ("why sent / why not sent").

These seams reshape `execution/joi/memory/store.py` and the reply flow in
`execution/joi/api/server.py` in place, into a few clear modules.

*Each seam's detailed design is added below as it is agreed.*

### Seam 1: Memory Record — envelope plus per-kind detail

**v2.0 today.** Each kind of memory has its own table (`user_facts`,
`context_summaries`, `knowledge_chunks`, `notes`), each with its own FTS
table, vector table and triggers. Metadata differs per table. A correction
overwrites a fact in place (`UNIQUE(conversation_id, category, key)`), so
history is lost. Summaries record only a time span, not the messages they
came from.

**v3.** One `memory_items` table holds the **envelope** every memory shares;
small **detail tables** hold what is specific to a kind.

Envelope (common to all kinds):

| Field | Purpose |
|-------|---------|
| kind | fact, summary, knowledge, note; later episode, rule |
| scope | conversation the memory belongs to (or global) |
| speaker | who it came from (first-class for group chats) |
| source | stated, inferred, admin, ingested |
| confidence | how sure Joi is |
| provenance | source message IDs or document reference |
| valid_from / valid_until | when the memory is (or was) true |
| superseded_by | the record that replaced this one |
| text | searchable content (one FTS index, one vector index) |
| metadata_json | fields that are not first-class columns yet |
| created_at / updated_at | timestamps |

Detail tables keep kind-specific columns, keyed to the envelope: a fact's
category and key, a note's reminder time, a knowledge chunk's document and
position.

**What this buys:**
- Richer metadata later is one envelope column, or a `metadata_json` key
  until it earns a column. No change ripples through every kind.
- **Correction** writes a new record and sets `superseded_by` on the old one.
  **Forgetting** sets `valid_until`. History survives; no special cases.
- New kinds (episodes, procedural rules) are a detail table plus a `kind`
  value; recall finds them without extra work.
- Group-chat hardening is a query condition: scope and speaker are on every
  record.
- One FTS index and one vector index instead of four of each.

**Stays as is:** `messages` (conversation history and the provenance target),
Wind state tables, reminders, tasks, conversation settings. They are history
or operational state, not memory.

**Cost:** the largest single change in v3 — migrating the four tables into
envelope plus detail and reworking every read/write path in
`memory/store.py`, in place.

### Seam 2: Recall — items, not text

**v2.0 today.** Each kind of memory has its own `*_as_context()` function
that both searches and formats finished prompt text, each with its own token
budget (facts 400, summaries 1500, knowledge 1500). `_build_enriched_prompt`
in `api/server.py` concatenates those strings. Pinned facts, recently expired
facts, notes and knowledge scopes are separate special cases; Wind calls the
summary functions directly. Retrieval and formatting are coupled once per
kind. The generic search core in `memory/hybrid.py` (eligible rows → vector +
FTS → RRF fusion) is reused.

**v3.** One recall call — roughly `recall(scope, query, purpose)` — returns a
list of **items**, never text. Formatting moves to the turn pipeline's
*assemble* stage (seam 3).

Each item is a memory envelope (seam 1) plus **why it matched**: which
retriever found it (words, meaning, or both) and its score.

| Contract rule | Meaning |
|---------------|---------|
| Items, not text | Recall never formats prompts; callers decide presentation |
| Purpose, not knobs | The caller states why it recalls (reply, Wind render, consolidation, debug). A purpose profile sets kinds, budgets, time windows and must-include items — "derive, don't tune" applied to memory |
| Scope enforced inside | DM vs group, speaker attribution and knowledge scopes are applied in recall only, never by callers |
| Current by default | Superseded and expired items are excluded unless the purpose asks for history; when included they are marked as past |
| Visible degradation | If a retriever fails (e.g. embeddings unavailable), the result carries an explicit degraded flag and reason, logged via seam 4 — no silent fallback |

v2.0 special cases become profile rules: pinned facts are "always include
core facts"; the "past events" block is "include items whose validity ended
in the last 7 days, marked as past".

**What this buys:**
- New metadata or new kinds reach every caller without changing any caller.
- "Why it matched" separates direct answers from loosely related background.
- Source on every item (stated vs inferred) is the basis for
  anti-confabulation: "you told me" vs "I think".
- Ranking can improve later (multi-signal, cross-kind) behind the same call.

**First version reproduces v2.0 behaviour:** same per-kind budgets, same RRF
fusion. Behaviour changes come later, as deliberate decisions.

### Seam 3: Turn Pipeline — one pipeline for every message

**v2.0 today.** Four separate paths produce messages, each with its own copy
of "gather context → build prompt → generate → check → send":
`receive_message` (≈720 lines, an ordered `if` chain whose order is only
documented in comments), `_generate_proactive_message` (Wind, its own prompt
assembly and a duplicated time injection), `_generate_reaction_response` and
`_generate_reminder_message`. The reply prompt is extended in five separate
ad-hoc places (reminder ack, shh awareness, morning-already-greeted, reminder
list, agenda).

**v3.** Every message Joi might send is a **Turn**, whatever triggered it:
an inbound message, a reaction, a Wind tick or a reminder firing. A Turn
passes through fixed stages; each stage adds its part:

| Stage | Adds to the Turn | Replaces in v2.0 |
|-------|------------------|------------------|
| perceive | mood, detected commands, corrections, facts | the ordered `if` chain of handlers |
| assemble | recalled items (seam 2), sitrep, notices | `_build_enriched_prompt` and the five ad-hoc prompt appends |
| decide | reply, fixed command answer, stay silent, or Wind intent | scattered early returns |
| render | the message text (LLM) | four separate generators |
| validate | length, leak check, formatting, translation | per-path copies |
| send + commit | send, then store and apply state changes | spread through each path |
| log | decision record (seam 4) | ad-hoc log lines |

Only **perceive** and **decide** differ by trigger; the rest is shared, so
replies and Wind use the same context, sitrep and checks. Handlers (snooze,
reminders, notes, tasks) become an explicit ordered list. The fast, no-LLM
ingress before the queue (dedupe, store, addressing) stays fast.

| Contract rule | Meaning |
|---------------|---------|
| Per conversation | A Turn carries its `conversation_id` (and, for a reply, the person it answers) end to end; every stage sees only that conversation's state |
| Queue decides when, pipeline decides how | Turns run through the message queue: one worker, three lines — time-critical (reminders), owner, normal — FIFO within a line. v2.0 has only the owner and normal lines. Priority is a Turn property, so lines can change later without ripple |
| Behaviour preserved first | First version keeps today's per-message LLM calls (mood, fact detection, reply). Folding them into fewer calls is a later, deliberate change |

#### Multiple Turns, restarts and reminders

Designed for load — many clients, long document searches, replies that take
minutes — not for one user in a simple dialogue. No rule here depends on
predicting how long another Turn's generation will take.

**One Turn produces at most one message.** Several Turns can exist for a
conversation at once. Each has an id and an optional `related_to` link. The
assemble stage gets a "conversation right now" view: open dialogue and topic,
Joi's last messages, and pending Turns for the conversation.

**Perception per message, at arrival.** Each inbound message is perceived
once, when it arrives — mood, facts, commands — and the results are stored
immediately with that message. Replies only read them, so restarting a reply
never repeats perception or duplicates side effects. Something the user asked
for ("remind me at 5") takes effect even if Joi's reply later fails. This
changes v2.0, which holds some writes until the reply is sent.

**One open reply per person.** A reply Turn belongs to a (conversation,
person) pair and answers everything that person said to Joi since Joi last
answered them. In a DM the person is the conversation. In a group, each person
who addressed Joi gets their own reply Turn.

**Late binding.** A reply builds its context when the worker starts it, from
the conversation as it is at that moment — including anything Joi sent in the
meantime (e.g. a reminder).

**Restart on every new message from that person — no cap:**

| Reply state when the person's new message arrives | What happens |
|---------------------------------------------------|--------------|
| Waiting in the queue | The message joins it |
| Generating | Generation is aborted (streamed request, connection closed) and the reply is rebuilt with the updated context |
| Generated, not yet handed to mesh | Dropped and rebuilt |
| Already handed to mesh | It goes out; the new message starts a new reply that sees it |

The drop check and the hand-off to mesh share one lock: always one or the
other, never half. A restarted reply goes **behind** other people's waiting
replies in its line — nobody jumps the queue by typing a lot. Floods are
bounded by mesh's inbound limits (20/min, 120/h per sender); context growth is
handled by compaction. v2.0's LLM client does not stream, so aborting requires
streaming reply generation (Ollama stopping on client disconnect to be
verified on the pinned version).

**Typing, per person.** Typing is tracked per (conversation, person). Mesh
forwards both STARTED and STOPPED; v2.0 forwards only STARTED, and Joi
receives the sender but keys typing by conversation only.
- While the person is typing, their reply does not start; the worker serves
  other Turns.
- The person starts typing while their reply generates: generation is aborted.
- Typing stopped without a message: the reply restarts with the same context.
- Typing indicators can be disabled in Signal, so a new message always aborts
  too; typing only makes it earlier.

**Groups.** Only the person a reply belongs to can hold, abort or restart it.
Other members' typing and messages never do; they are context for the next
build. Like a group of people talking: when two write at once, whoever's reply
goes out first is "now" and the other answers from slightly behind. Group
typing behaviour is a separate policy point, deliberately simple in this first
version and tuned separately later.

**Reminders** run on the **time-critical line**, above the owner line:
- A firing reminder goes out ahead of waiting replies; those replies include
  it when they start (late binding).
- It renders with a small, bounded context (open topic, last few messages,
  whether a reply is being prepared), so it bridges into the conversation
  rather than interrupting it.
- The worker cannot interrupt a generation already running. If the reminder
  cannot be rendered by its deadline, it is sent as **plain text without the
  LLM, on time**. Deadline tolerance: to be decided.
- A reminder is never merged into another message, never late because of
  another Turn's cost, never lost.

*Rejected:* merging a reminder into a queued reply. Under load it ties the
reminder's delivery to that reply's generation time and queue position, which
cannot be predicted.

*Open:* note reminders. In v2.0 they are fixed text sent directly, bypassing
the LLM and the queue; whether they become Turns is not decided.

The contract — Turn ids, `related_to`, the person key, late binding, abort and
restart hooks, the time-critical line, per-message perception — is part of
the seam from day one. Behaviour built on it ships with the reply-path work.

## Phases

| Phase | Meaning |
|-------|---------|
| alpha | v3 branch under active rework |
| beta | All v2.0 known gaps fixed (see above) |

*Further phase criteria to be defined.*

## Decisions

| Date | Decision |
|------|----------|
| 2026-10-01 | Stable line tagged `v2.0`; rework lives on branch `v3`. Called v3, not "iteration II". |
| 2026-10-01 | v3 builds on v2 by rewriting in place, never from scratch. |
| 2026-10-04 | Scope is the whole behaviour engine (proactive and reply), not proactive only. Capabilities wait. |
| 2026-10-04 | All v2.0 known gaps must be fixed before beta. |
| 2026-10-04 | Behavioural memory is the first feature; all other areas stay in scope. |
| 2026-10-04 | Contracts first: memory record, recall, turn pipeline, decision log are designed before features. |
| 2026-10-04 | Memory record = shared envelope table plus per-kind detail tables (not one flat table, not per-table column contracts). Corrections supersede, forgetting expires; history is kept. |
| 2026-10-04 | Turn pipeline: every outgoing message (reply, reaction, Wind, reminder) is a Turn through shared stages perceive → assemble → decide → render → validate → send/commit → log. Per conversation; existing priority queue (owner line, normal line) kept. |
| 2026-10-04 | One Turn = at most one message; several Turns per conversation, linked by `related_to`. Each inbound message is perceived once at arrival and its results stored immediately. |
| 2026-10-04 | One open reply per (conversation, person), built late. Restarts on every new message from that person (abort while generating, drop before mesh), no cap; a restarted reply goes behind other people's waiting replies. |
| 2026-10-04 | Typing tracked per (conversation, person); mesh forwards STARTED and STOPPED. In groups only the reply's own person can hold or restart it; group behaviour tuned separately later. |
| 2026-10-04 | Reminders run on a time-critical line above the owner line, never merged into another message, plain-text fallback if not rendered by the deadline. Merging into queued replies rejected: unpredictable under load. |
| 2026-10-04 | Recall returns typed items with "why it matched", never text. Purpose profiles replace per-caller knobs; scope enforced inside recall; current-only by default; degraded retrieval is flagged, never silent. First version reproduces v2.0 ranking. |

## Related Documents

| Document | Role |
|----------|------|
| `Joi-architecture-v2.0.md` | What v3 builds on |
| `wind-architecture-v2.md` | Detailed proactive behaviour design (v3 source) |
| `ideas/memory-improvement-ideas.md` | Behavioural and deep memory directions |
| `ideas/memory-scaling-ideas.md` | Retrieval and compaction directions |
| `ideas/hermes-agent-ideas.md` | Borrowed agent ideas |
