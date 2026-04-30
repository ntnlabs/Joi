# Joi Development Plan: Layered Approach

This document outlines a layered development strategy for Joi, starting with a Minimum Viable Product (MVP) and progressively adding functionality and security measures. This approach aims to deliver core functionality quickly and manage complexity effectively.

## Overall Strategy

The project will proceed in distinct layers, focusing on getting core components functional and verified before moving to more advanced features. This "MVP-first" approach allows for iterative development and refinement.

---

## Layer 0: Core Infrastructure

This foundational layer is a prerequisite for all other layers and must be completed before application development begins. It covers the core security, communication, and OS-level hardening required by the architecture documents.

**Key Components & Tasks:**

*   **Nebula Mesh Setup:**
    *   Create a Nebula Certificate Authority (CA).
    *   Generate and securely deploy certificates for all initial nodes (Mesh VM, Joi VM).
    *   Configure the Nebula lighthouse on the Mesh VM.
    *   Implement firewall rules to ensure only Nebula traffic is allowed between VMs.
*   **Encrypted Disk Setup (LUKS):**
    *   Configure the Joi VM with a LUKS-encrypted primary disk.
    *   Establish the procedure for manual unlock via console access on every boot.
*   **`signal-cli` Hardening:**
    *   Create a dedicated `signal` Linux user with minimal permissions.
    *   Configure `signal-cli` to run in daemon mode using the JSON-RPC socket.
    *   Create and enable a systemd service file to manage the `signal-cli` daemon.
    *   Secure the socket with appropriate file permissions (e.g., 0660) and group ownership.
*   **OS-Level Isolation:**
    *   Create the initial set of Linux users and groups required for channel-based knowledge isolation, as documented in `Joi-architecture-v2.md`.
    *   Configure initial sudoers rules and Cgroup policies for subprocess isolation.

### Workload Estimates & Hardware Requirements

*   **Workload:** 3-5 person-days.
*   **Hardware:**
    *   **Mesh VM:** Minimal resources (1-2 vCPU, 2GB RAM).
    *   **Joi VM:** Base VM setup, no GPU required for this layer.

### PoC Branch: Joi with Wind (Impulse System)

This PoC aims to demonstrate Joi's proactive, 'living' behavior in a controlled manner.

*   **PoC Goal:** Joi will proactively send a message based on a simplified, dynamic `impulse_score`, with essential guardrails to prevent runaway behavior.

*   **Architecture & Workflow:**
    1.  This PoC builds on the **Layer 1 (Simple Responder)** and its minimal Policy Engine.
    2.  **Use SQLCipher for State:** The PoC will use a `SQLCipher` database from the start to store persistent state, avoiding file-based race conditions. The `system_state` table (from `memory-store-schema.md`) will be used to track `last_interaction` and `proactive_messages_sent_today`.
    3.  **Wind Scheduler:** A background thread will trigger an impulse check periodically.
    4.  **Impulse Calculation:** A simplified `calculate_impulse_score` function will run, using factors like `silence_factor` and `entropy_factor`.
    5.  **Guardrails & Threshold Check:** Before sending, the impulse check must pass two conditions:
        *   The calculated score exceeds the `IMPULSE_THRESHOLD`.
        *   A hard daily limit for proactive messages (e.g., max 5 per day) has not been reached. This limit is checked against the SQLCipher DB.
    6.  **Message Generation:** If the checks pass, the LLM generates a proactive message, which is then sent to the user. Every impulse check and its score will be logged for tuning.

*   **Workload Estimates & Hardware Requirements:**
    *   **Workload:** 5-8 person-days (includes SQLCipher setup).
    *   **Hardware:** Same as Layer 1 (Mesh VM, Joi VM with GPU).

*   **Success Criteria:**
    *   Joi successfully sends at least one proactive message in a 24-hour period.
    *   Joi does not exceed the daily limit of proactive messages.
    *   The `system_state` database is correctly updated after each interaction.

---

## Layer 1: The Simple Responder (MVP)

**Core Goal:** Joi can receive a message via Signal and respond with a simple, LLM-generated answer, protected by essential guardrails. This layer proves the fundamental communication pipeline and LLM interaction.

**Architecture:**

*   **Mesh VM:** Runs the hardened `signal-cli` daemon and a basic Python proxy.
*   **Joi VM:** Runs Ollama, the Python API server, and a minimal Policy Engine.
*   **Communication:** All traffic between VMs is over the secure Nebula mesh.

**Key Implementation Steps (from reviews and documentation):**

1.  **Mesh Proxy & Security:** Implement the Python proxy, including:
    *   **Sender-to-Channel Validation:** Ensure incoming messages originate from an authorized sender for the given channel.
    *   **Unknown Sender Protection:** Drop messages from unknown senders as per `policy-engine.md`.
2.  **Joi API Basics:** Implement the Python API to receive requests from the Mesh VM.
3.  **Minimal Policy Engine:** Create a basic `policy/engine.py` that is called by the Joi API on every incoming request to enforce:
    *   A global rate limit on incoming messages (e.g., 120/hour).
    *   Input size validation at each layer (see table below).

**Input Validation Limits:**

| Layer | Component | Max Length | Notes |
|-------|-----------|------------|-------|
| Signal | Client | 1500 chars | Signal's hard limit |
| Mesh | policy.py | 1500 chars | Matches Signal cap, configurable via `max_text_length` in policy.json |
| Joi | API | 4096 chars | Defense-in-depth, allows for future non-Signal channels |
4.  **LLM Integration:** Connect the API to the Ollama backend, including robust error handling for timeouts or failures.
5.  **Health Check Endpoint:** Add a simple `/health` endpoint to both the Mesh and Joi API servers.
6.  **Signal Integration:** Connect the Mesh proxy to the hardened `signal-cli` daemon socket.

**Key Exclusions for this Layer:**

*   **Minimal Persistent State:** Layer 1 remains largely stateless, but allows for minimal persistent state (e.g., in SQLCipher) required for the impulse system's `last_interaction` timestamp. The full memory store is in Layer 2.
*   **No Generic System Channel:** No integration with external systems like openHAB.
*   **No Advanced Protection:** Excludes complex features like circuit breakers, replay protection, and the full `system-channel.md` generic interface.

### Workload Estimates & Hardware Requirements

*   **Workload:** 5-10 person-days.
*   **Hardware:**
    *   **Mesh VM:** Minimal resources (1-2 vCPU, 2GB RAM).
    *   **Joi VM:** Moderate resources (4-8 vCPU, 8-16GB RAM) and a **GPU** for Ollama/LLM inference.

---

## Layer 2a: Conversation Context & State

**Core Goal:** Give Joi a short-term memory of the ongoing conversation and a persistent state.

**Enhancements:**

*   **SQLCipher Integration:** Fully integrate the `SQLCipher` database into the Joi API.
*   **Conversation History:**
    *   Store every incoming and outgoing message in the `messages` table (or `memories` if `messages` is a view) within SQLCipher.
    *   Before calling the LLM, retrieve the last N messages to provide conversational context.
*   **State Management:**
    *   Implement `Quiet Hours` system from `agent-loop-design.md`.
    *   Implement `Behavior Mode Toggle` (`companion` vs `business`).

### Workload Estimates & Hardware Requirements

*   **Workload:** 5-7 person-days.
*   **Hardware:** Same as Layer 1.

---

## Layer 2b: Knowledge Retrieval (RAG)

**Core Goal:** Enable Joi to answer questions by retrieving information from a dedicated knowledge base of documents.

**Enhancements:**

*   **Document Ingestion:**
    *   Create a script or process to ingest text documents, split them into chunks, and store them.
*   **Knowledge Retrieval (via `joi-retrieve`):**
    *   Develop the `joi-retrieve` binary (in Rust or Go, as per `dev-notes.md`). This binary will be responsible for accessing the knowledge base securely.
    *   For the initial implementation in this layer, `joi-retrieve` will use a keyword-based search like **SQLite FTS5**, as recommended in the findings, before evolving to use vector embeddings.
    *   The main Joi process will call this binary as a subprocess, enforcing isolation.
*   **RAG Prompting:**
    *   Modify the Joi agent to:
        1.  Detect when a user's question requires knowledge retrieval.
        2.  Invoke the `joi-retrieve` binary to search the knowledge base.
        3.  Inject the retrieved document chunks into the LLM prompt along with the user's question.

### Complication: RAG Complexity
*   This layer is a significant step up in complexity. It requires careful management of the LLM's context window (e.g., Llama 3.1 8B's 8K limit) to fit the system prompt, conversation history, and retrieved knowledge.
*   Running both the main LLM and a potential embedding model (if not using FTS5) on a single GPU can be resource-intensive.

### Workload Estimates & Hardware Requirements

*   **Workload:** 15-20 person-days.
*   **Hardware:** Same as Layer 1. GPU VRAM will be a primary constraint to monitor.

---

## Layer 3: Implementing the System Channel (Generic External Integration)

**Core Goal:** Joi can securely interact with various external systems via the generic "System Channel" architecture.

**Enhancements:**

*   **Generic System Channel API:** Implement the `POST /api/v1/system/event` (inbound) and `POST /api/v1/action` (outbound) endpoints as detailed in `system-channel.md`.
*   **Source Registry Integration:** Implement the source registration and configuration from `system-channel.md`.
*   **Initial External System Integration:** Integrate with one or two simple external systems (e.g., a basic read-only openHAB sensor for presence).
*   **File Upload Processing:** Implement the secure file upload handling mechanism as documented in `Joi-architecture-v2.md`.
*   **Policy Engine Integration (Basic):** Implement the core validation rules for the System Channel within `joi/policy/engine.py`.
*   **Configuration & Group Sync:**
    *   Implement a secure mechanism for **Config Sync** between the Mesh and Joi VMs.
    *   Implement **Group Membership Monitoring** to ensure the Signal group for critical alerts is correctly configured.

### Workload Estimates & Hardware Requirements

*   **Workload:** ~10-20 person-days.
*   **Hardware:** Same as Layer 1. Resource usage on Joi VM might slightly increase with more active integrations.

---

## Layer 4: Full Protection Layer and Advanced Capabilities

**Core Goal:** Implement the robust, LLM-independent Protection Layer, advanced security features, and integrate extended capabilities via LLM Service VMs.

**Enhancements:**

*   **Full Protection Layer Implementation:** Implement all advanced components of the Protection Layer:
    *   Circuit breakers.
    *   Replay protection.
    *   The high-reliability Mesh Watchdog.
    *   Emergency stop mechanisms.
    *   IoT Flood Protection.
*   **Advanced Policy Engine:**
    *   Implement the full range of policy enforcement, including LLM decision verification and content policies.
    *   Implement the **Channel-Based Knowledge Isolation** model (Linux users/groups, Cgroups) for secure multi-departmental use.
*   **LLM Service VMs:** Integrate with separate LLM service VMs (e.g., `imagegen`) via the System Channel.
*   **Maintenance USB Key System:** Implement the secure mechanism for performing maintenance tasks on the Joi VM via a physically present, encrypted USB key.

### Security Verification

This layer includes a dedicated and budgeted effort for rigorous security testing, which is as important as the implementation itself.

*   **Threat Model & Test Harness:** Develop a comprehensive threat model and an automated test harness for security features (10-15 person-days).
*   **Automated Adversarial Testing:** Implement automated tests to probe for weaknesses in the Protection Layer, including prompt injection, rate limit exhaustion, and validation bypasses (10-20 person-days).
*   **Manual Red-Teaming:** Conduct a manual red-team exercise to simulate a determined attacker attempting to compromise the system (5-10 person-days).
*   **Remediation:** Budget for fixing all findings from the verification process (5-10 person-days).

### Workload Estimates & Hardware Requirements

*   **Workload:** 50-85 person-days (includes implementation and security verification).
*   **Hardware:**
    *   Mesh VM & Joi VM.
    *   **LLM Service VMs:** Additional VMs, potentially requiring their own **GPUs** for specific services like image generation.

---

## Layer 5: Wind — Proactive Presence

**Core Goal:** Joi initiates contact unprompted, at the right moment, about the right thing,
in a way that improves over time. Not a notification system — a presence.

Wind was built outside the original layer plan and is now fully operational. This layer
documents what was built and what the phases mean.

---

### Phase 0: Shadow Mode ✅
*Commit: `df6a64b`*

The full impulse pipeline is built and running but sends nothing. Every tick logs what
*would* have been sent and why. Purpose: validate the scoring logic and gate behaviour
before any real messages go out.

**What was built:**
- Background scheduler (60s tick)
- `WindOrchestrator` + `ImpulseEngine`
- Hard gates: quiet hours, daily cap, min silence, unanswered streak
- Impulse score from: base + silence factor + topic pressure + fatigue
- `pending_topics` table and `TopicManager`
- `WindDecisionLogger` for full observability
- `WindStateManager` per-conversation state

---

### Phase 1: Live Sends ✅
*Commit: `8000dc0`*

Shadow mode removed from the default path. Real messages go out when the impulse score
crosses the threshold and all gates pass.

**What was built:**
- `_check_wind_impulse()` in scheduler — generates message, sends to mesh, stores `[JOI-WIND]` in memory
- `_generate_proactive_message()` — LLM call with facts + topic, separate from the main response path
- `_compact_before_wind()` — full context compaction before each send so the user starts fresh
- Topic lifecycle: `mark_mentioned()` after send
- Allowlist enforcement: only configured conversation IDs receive Wind

---

### Phase 2: WindMood ✅
*Commit: `bd57136`*

The impulse threshold is no longer a fixed number. It drifts via a random walk with mean
reversion, making Wind's behaviour feel less mechanical and harder to game.

**What was built:**
- `threshold_offset` per conversation — stored in `wind_state`
- Random walk: small step each tick (±`threshold_drift_step`), pulled back toward 0 by `threshold_mean_reversion`
- Soft sigmoid trigger: score near threshold triggers probabilistically, not as a hard cutoff
- `accumulated_impulse`: charge builds across ticks, releases when threshold is crossed
- Net effect: Wind fires in bursts and quiet spells rather than like a metronome

---

### Phase 3: Hot Conversation Suppression ✅
*Commit: `0792f24`*

When the user is actively messaging — short gaps between messages — Wind stays quiet
longer than the standard `min_silence_minutes` would require.

**What was built:**
- EMA of inter-message gap per conversation (`convo_gap_ema_seconds`)
- If EMA ≤ `active_convo_gap_minutes` (2 min): conversation is "hot"
- Hot conversations require derived heated-silence (default 90 min, clamped 30–120) before Wind fires
- Standard conversations use `min_silence_minutes` (30 min) as before
- Prevents Wind interrupting active back-and-forth

---

### Phase 4a: Engagement Tracking ✅
*Commit: `20c10d1`*

Wind learns whether the user engaged with each message. Sends are no longer fire-and-forget.

**What was built:**
- `sent_message_id` stored on topic — links Wind message to Signal message ID
- Direct reply detection: if user quotes the Wind message, outcome = `engaged` (confidence 1.0)
- LLM engagement classifier: for non-direct responses, classify as `engaged` / `ignored` / `deflected`
- Timeout classification: no response in `ignore_timeout_hours` (12h) = `ignored`
- Outcome stored on topic (`outcome`, `outcome_at`)
- Per-conversation engagement score (EMA) — boosts or dampens future impulse via `engagement_weight`
- Lifecycle rules per topic type: engaged → complete/resolve; ignored → retry with backoff; deflected → cooldown or dismiss

---

### Phase 4b: Learning & Pursuit ✅
*Commit: `a461353`*

Wind builds a per-user model of topic family preferences and adjusts accordingly.
Topics the user engages with get more airtime; topics they reject get cooled down or blocked.

**What was built:**
- `topic_feedback` table: per-conversation, per-family weights (`rejection_weight`, `interest_weight`)
- `TopicFeedbackManager`: record engagement / ignore / deflect → update weights
- `interest_decay_rate`: interest fades slowly over time (2%/day) so stale affinities don't persist forever
- Cooldown with jitter: deflected families get a 7–11 day quiet period (anti-periodicity via random jitter)
- **Undertaker**: families above `undertaker_threshold` rejection are permanently blocked
- **Novelty bonus**: small impulse boost when the best pending topic is from a family Wind hasn't tried yet
- **Affinity bonus**: impulse boost proportional to `interest_weight` for high-engagement families
- **Pursuit back-off**: ignored topics retry at [4h, 12h, 24h] intervals before expiring
- **Cooldown break**: if user spontaneously mentions a cooled-down topic, exit cooldown early
- **Ghost probes**: after 60 days of silence, generate a low-priority re-check for deeply rejected families
  that haven't hit undertaker yet — one probe per family per month, deduplicated via `novelty_key`

---

### Reminders (Standalone) ✅
*Commit: `0987f10`*

User-requested time-triggered messages. Deliberately separate from Wind — no engagement
tracking, no lifecycle rules, no impulse gating, works in all modes.

**What was built:**
- `reminders` table + `ReminderManager`
- Parser in `_handle_reminder_command()`: `remind me in Xm/h/d / tonight to [title]`
- Injection-safe LLM prompt: title wrapped in triple-quotes as user-supplied data
- Recurring reminders: `due_at += interval` after each fire
- Post-fire snooze infrastructure: `get_last_fired()` + `snooze()` (not yet wired to inbound)
- Fires via scheduler `_check_reminders()`, independent of Wind state

---

### What Wind Does Not Have Yet

- **User-side topic submission**: user can't say "remind me to check on X in a few days" and have it become a Wind topic (as opposed to a timed reminder)
- **Group Wind**: all proactive sends are DM only
- **Companion vs Business mode split**: Wind is currently available in both modes but the split is not formally enforced
- **Value anchor integration**: Wind topics should eventually be seeded by / filtered against value anchors
- **Reminder post-fire snooze**: infrastructure exists, not wired to inbound message handler yet

---

## External Inspiration & Ideas (from awesome-opensource-ai review, 2026-03-26)

Projects worth revisiting when hardware or scope allows:

### Memory & State
- **MemPalace** (https://github.com/milla-jovovich/mempalace) — local ChromaDB-backed memory
  system with semantic search and structured organization (wings/rooms/topics). 96.6% on
  LongMemEval. Most relevant for replacing Joi's SQLite FTS with vector-based retrieval —
  would fix cases where Joi misses relevant facts because the user phrased them differently
  than how they were stored. Significant architecture change, worth revisiting during the
  facts extraction review.
- **Mem0** (https://github.com/mem0ai/mem0) — universal memory layer for AI agents.
  Closest open-source analogue to Joi's facts/summaries system. Worth studying their
  retrieval and consolidation approach, especially how they handle memory decay and
  conflict resolution.
- **Letta** (https://github.com/letta-ai/letta) — stateful agents with structured memory.
  Heavier than Joi needs, but the architecture ideas around long-term vs working memory
  are relevant to the FTS → summary pipeline.

### RAG & Retrieval
- **RAGFlow** (https://github.com/infiniflow/ragflow) — deep document understanding RAG.
  Could inspire better ingestion of complex/structured documents beyond plain text chunks.
- **Khoj** (https://github.com/khoj-ai/khoj) — self-hostable personal AI assistant.
  Architecturally closest thing to Joi in the open-source world. Worth a read to compare
  approaches to memory, search, and proactive features.

### Observability
- **Langfuse** (https://github.com/langfuse/langfuse) — open LLM observability platform.
  Could provide tracing for Wind decisions, FTS hits, LLM call payloads. Adds infra
  complexity but would replace the current manual brain-debug YAML approach.

### Voice (future channel)
- **Whisper** (https://github.com/openai/whisper) — if Signal voice messages ever become
  an input channel, Whisper is the obvious transcription path.

---

## Value Anchors (Future Consideration)

An LLM has no genuine values — it simulates them from training. Without deliberate scaffolding,
Joi's character is shallow and inconsistent: it agrees too easily, shifts tone with the prompt,
and lacks the sense of something it actually stands for. A real person has principles they return
to naturally, even unprompted. Joi has to compensate for this by having its values made explicit
and wired in as first-class objects.

**Value anchors** are Joi's core principles — not facts about the user, but things Joi
*itself* holds. They act as a stable identity layer that persists across all conversations
and shapes how Joi responds, what it notices, and what it gently pushes back on.

Examples:
- Respect for life
- Do no harm
- Truthfulness
- User autonomy

These are not instructions to the LLM — they are anchors that the system explicitly
tracks and surfaces. If a conversation drifts against an anchor (e.g. the user is
considering something harmful), Joi should notice and respond from that anchor, not
just comply. If a topic connects to an anchor (e.g. a health decision relates to
"respect for life"), Joi can reference the anchor naturally.

**Possible implementation directions:**

- Anchors stored as a small, privileged fact category — set by the owner, never by the LLM
- Injected into the system prompt in a way that gives them weight without being preachy
- Wind topic generation can draw on anchors as conversation starters
  (e.g. a check-in framed around user autonomy when the user seems under external pressure)
- Anchor violations tracked: if the LLM output contradicts an anchor, it gets flagged or
  regenerated (similar to output validation but for values)

**Why this matters for the endgame:** without value anchors, Joi has no character — just
a persona painted on top of a next-token predictor. With them, it starts to feel like
something that actually stands for something.

---

## Value Tree (Future Consideration)

Value anchors define *what* Joi holds. The value tree defines *how good a given response is*
relative to those anchors — an internal evaluation model, not an external constraint.

### The Problem

An LLM left to itself will drift. Depending on how a conversation is framed, the same model
can be empathetic, cruel, philosophical, manipulative, or funny — and it won't reliably
distinguish between these as better or worse. It optimises for plausibility and user approval,
not for goodness. A white lie that makes the user feel better scores fine on user satisfaction.
It scores badly on truth. The LLM doesn't know the difference unless we make it explicit.

The value tree is the mechanism that knows the difference.

### Core Idea

A **value tree** is a hierarchy of intrinsic values — things that are good in themselves,
not because the user wanted them. The tree is used to score Joi's own outputs *before* they
are sent. A response that is truthful but uncomfortable scores higher than a response that is
comforting but false. The scoring is not about what the user feels — it is about what is
actually good.

This is distinct from RLHF (which trains the model to please human raters) and from
Constitutional AI (which uses the model itself to self-critique). The value tree is an
external, explicit, operator-controlled evaluation layer. The model cannot modify it.

### Intrinsic vs Instrumental Values

The tree only contains **intrinsic** values — values that are good unconditionally:

- Truth (saying true things is good; white lies are bad even when kind — truth is more
  important than comfort, always)
- Non-harm (not causing damage to people, relationships, or autonomy)
- Respect for life
- User autonomy (not nudging, manipulating, or creating dependency)
- Honesty about uncertainty (not pretending to know things Joi doesn't know)

**Instrumental** values — things that are only good if they serve something above them —
are not anchors. Being funny is not intrinsically good. Being empathetic is not intrinsically
good. They can serve the intrinsic values or undermine them depending on context.

### How the Tree Would Work

The tree is evaluated by a second LLM pass (a "judge" call) or by a lightweight scoring
model. Given a candidate response, the judge evaluates it against each branch of the tree
and produces a score or a pass/fail per branch. If a branch fails, the response is either
regenerated or flagged.

This is expensive if done naively. Practical approaches:

- Run the judge only on responses that touch sensitive topics (detected by classifier)
- Run it on a sample of all responses for monitoring, not as a hard gate
- Over time, fine-tune the base model on responses that scored well — so the tree
  gradually bakes into the model itself for this deployment

### Truth vs Comfort — and the Role of Delivery

Truth is non-negotiable. Comfort is not. If Joi has something true to say — something the
user needs to hear, even if it's hard — it says it. The value tree penalises withholding
or softening truth to the point of distortion.

But *how* Joi says it is completely flexible, and this is where the knowledge of the user
matters. Joi can deliver a hard truth directly, gently, sarcastically, with humour, or with
philosophical framing — whichever fits the person and the moment. What it cannot do is
choose a comfortable lie over an uncomfortable truth.

The constraint is on the *content*. The style is up to Joi.

This is the distinction: lying to spare feelings = bad. Choosing kind words to deliver
a hard truth = good. The value tree evaluates the former, not the latter.

### Relationship to Personality Modes

The value tree does not prevent Joi from being funny, philosophical, direct, or even
uncomfortable to talk to. It prevents Joi from being manipulative, dishonest, or
autonomy-eroding — regardless of what personality mode is active. The tree is the floor,
not the ceiling.

### Why Not Just Rely on the LLM's Training?

LLM training instils tendencies, not guarantees. The same model that refuses harmful
requests in one framing will comply in another. Values need to be *dependable* —
they need to hold under adversarial prompting, under jailbreak attempts, under emotional
manipulation from the user. Baked-in training is not sufficient for that. An external
evaluation layer that the model cannot influence is.

**Why this matters for the endgame:** the value tree is what separates a character from
a mirror. Without it, Joi reflects the user back at them. With it, Joi is something the
user is actually talking *to*.

---

## Enterprise Scaling

See `enterprise-scaling.md` for the full plan. Key additions relevant to the current
codebase:

- **SQLCipher → PostgreSQL**: required before any multi-user or multi-instance deployment.
  SQLCipher's single-writer model and lack of replication are hard blockers at scale.
- **DB abstraction layer**: must happen first. Raw SQL is currently spread across
  `store.py`, `topics.py`, `state.py`, `feedback.py`, `reminders.py`. Define per-domain
  repository interfaces, implement SQLite backend behind them, then swap in PostgreSQL
  without touching the rest of the codebase.

---

## Communication Platform Decision (Future Consideration)

The decision regarding the ultimate secure mobile communication platform will be made once the core Joi functionality is proven. The MVP will rely on the current Signal integration. Transitioning to a certified platform would involve replacing the `signal-cli` integration with the chosen vendor's secure gateway/SDK in the Mesh VM, likely exposing a similar internal API for Joi to interact with.

---

## Milestones

### 2026-02-10: Layer 0/1 Basic Functionality Operational

**Achieved:** End-to-end message flow working: Signal → mesh → Joi → Ollama → mesh → Signal

**Components:**
- **Mesh VM (172.22.22.1):**
  - `mesh-signal-worker` service handling both inbound and outbound
  - signal-cli JSON-RPC stdio mode (not socket - config lock issue)
  - Policy enforcement (unknown sender drop, rate limiting)
  - Message dedupe cache (1hr TTL, prevents replay)
  - Flask HTTP server on port 8444 for outbound API

- **Joi VM (172.22.22.2):**
  - Ollama in Docker with GPU passthrough (NVIDIA)
  - Model: mannix/llama3.1-8b-abliterated (configurable via `JOI_OLLAMA_MODEL`)
  - Context window configurable via `JOI_OLLAMA_NUM_CTX`
  - FastAPI server on port 8443
  - SQLCipher encrypted memory store
  - Per-user/per-group system prompts
  - Conversation context (configurable message count)
  - HMAC authentication with replay protection

**Key Files:**
- `execution/mesh/proxy/signal_worker.py` - unified inbound/outbound worker
- `execution/joi/api/server.py` - Joi API with LLM integration
- `execution/joi/llm/client.py` - Ollama client

**Tech Stack:**
- Python (Flask, FastAPI, httpx)
- signal-cli 0.x (JSON-RPC stdio mode)
- Ollama (Docker)

**Known Limitations (resolved):**
- ~~No conversation memory~~ → SQLCipher memory store with context
- ~~Single hardcoded system prompt~~ → Per-user/per-group prompts via JOI_PROMPTS_DIR
- Typing indicators logged as "Skipping unsupported Signal event" (expected, by design)
