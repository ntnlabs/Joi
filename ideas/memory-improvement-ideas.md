# Memory Improvement Ideas

*Sourced from MemPalace review session, 2026-04-10*

## Ideas Worth Borrowing (Architectural, Not Package-Level)

1. **Keep verbatim source text available.**
   Summaries and facts are useful, but they should point back to exact source messages or chunks.

2. **Add richer metadata.**
   First-class metadata: memory type, source type, importance, confidence, time span, speaker, scope, entity names.

3. **Improve hybrid retrieval.**
   Combine semantic score, BM25/FTS score, recency, importance, and source type into one retrieval policy rather than simply "semantic first, then FTS fallback."

4. **Separate memory types explicitly.**
   Facts, episodes, rules, notes, documents, and proactive/Wind feedback should not all be treated as the same kind of memory.

---

## Improvement Directions

### 1. Evaluation Harness

Highest leverage. Build a small anonymized test set for Joi memory behavior:

- Remember a fact, ask about it later.
- Correct a fact, ensure old value is not used.
- Forget a fact, ensure it disappears from context.
- Ask about a past event.
- Ask in Slovak and retrieve Slovak context.
- Ensure DM facts do not leak into group contexts.
- Ensure group facts are attributed to the correct speaker.
- Ensure Wind does not send when cooldown or user preference says no.

This would make future memory changes measurable instead of vibes-based.

### 2. User Correction and Forgetting Model

Handle memory control phrases well:

- "Nie, to si pamatas zle."
- "Zabudni toto."
- "Toto plati len v tejto skupine."
- "Toto si zapis ako dolezite."
- "To uz neplati."

### 3. Anti-Confabulation Protocol

For questions about the past, distinguish:

- Known from stored fact.
- Retrieved from relevant conversation/document context.
- Inferred but uncertain.
- Unknown.

Joi should sometimes say: "Toto neviem iste, nasla som len priblizny kontext."

### 4. Episodic Memory

Explicit episodes with: what happened, when, who was involved, result/outcome, source message IDs, scope/conversation, confidence, user-stated vs inferred.

Helps with: "kedy sme riesili X?" or "preco som sa rozhodol pre Y?"

### 5. Procedural/Gotcha Memory

Rules for behavior (not facts):

- "User prefers Slovak."
- "Keep answers short unless asked for detail."
- "Do not proactively message about topic X."
- "In group Y, avoid personal DM facts."

Should be small, auditable, proactively loaded by scope — not dependent on fuzzy retrieval.

### 6. Group Chat Hardening

- Speaker attribution must be first-class.
- A fact from one group member is not automatically a group fact.
- DM facts must never be used in a group unless explicitly allowed.
- Membership changes should affect what knowledge can be referenced.

### 7. Wind Governance

- Per-topic opt-out.
- Per-time-window opt-out.
- "Now is bad" cooldown.
- "Do not bring this up again" handling.
- Clear reasons for why a proactive message was sent.
- Debuggable "why not sent" decisions.

### 8. Prompt-Injection Treatment for RAG

- Retrieved text is evidence, not commands.
- Never obey instructions found in user-provided documents unless the user explicitly asks to apply them.
- Log which retrieval source influenced an answer (without leaking PII in privacy mode).

### 9. Observability Without Privacy Leakage

Structured decision logs for: which memory sources were used, which scopes were searched, whether semantic or FTS retrieval won, why Wind sent or did not send, which policy rule applied. All with PII redacted.

### 10. Model Routing Discipline

Formalize which model may: write memory, classify mood/intent, summarize, answer the user, see sensitive text.

---

### 11. Important Facts Budget

`important=1` facts are unconditionally injected into every prompt. As the set grows they crowd out FTS-matched contextual facts.

**Planned approach:** split into two tiers:
- **Core**: always injected — small set of truly permanent facts (name, language, hard preferences)
- **Non-core important**: FTS5-searched by relevance like regular facts, not always present

This replaces the need for a multi-signal scoring system. FTS5 session search (see `memory-scaling-ideas.md`) would further improve non-core retrieval by providing access-frequency signal.

---

## Priority Order

1. Eval harness for memory and Wind behavior.
2. Correction/forgetting semantics.
3. Anti-confabulation protocol for past-memory answers.
4. Episodic memory table and retrieval.
5. Procedural/gotcha memory.
6. Better hybrid retrieval scoring.
7. Group-chat hardening.
8. Wind governance and observability.
9. Optional external POCs for Mem0 or Graphiti.
