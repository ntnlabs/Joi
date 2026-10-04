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

## Related Documents

| Document | Role |
|----------|------|
| `Joi-architecture-v2.0.md` | What v3 builds on |
| `wind-architecture-v2.md` | Detailed proactive behaviour design (v3 source) |
| `ideas/memory-improvement-ideas.md` | Behavioural and deep memory directions |
| `ideas/memory-scaling-ideas.md` | Retrieval and compaction directions |
| `ideas/hermes-agent-ideas.md` | Borrowed agent ideas |
