# Joi Architecture v2.0

> **Status:** Authoritative description of Joi **as shipped** at git tag `v2.0`.
> **Supersedes:** `Joi-architecture-v2.md` (pre-stateless design, historical).
> **Next:** `Joi-architecture-v3.md` covers what v3 changes on top of this.
> **Last updated:** 2026-10-04

This document describes what actually runs. Anything not built at `v2.0` is
listed in [Not in v2.0](#not-in-v20) and belongs to the v3 documents.

## Goals

- Offline LLM companion; no direct WAN from the Joi host
- Free-running agent that reacts to context and can message the user (Wind)
- Signal messaging only via a stateless proxy (mesh)
- Security-first with defense-in-depth at all boundaries
- Joi is the single source of truth for all configuration

> **Implementation:** Code lives in `execution/joi/`, `execution/mesh/` and
> `execution/shared/`. Deployment steps live in `sysprep/`.

## Key Design Principles

| Principle | Implementation |
|-----------|----------------|
| **Joi is authoritative** | All config lives on Joi, pushed to mesh |
| **Mesh is stateless** | No config files on disk; HMAC key is memory-only, always pushed by Joi |
| **Defense-in-depth** | Nebula + HMAC + policy validation |
| **Fail-secure** | Empty policy denies all; rotation has grace period; bootstrap protected by UFW |
| **Integrity over uptime** | Tamper or inconsistency → stop (`os._exit(78)`), never silently degrade |
| **No traces** | Mesh restart = clean slate; key gone when the process stops |

---

## Architectural Invariants

> These invariants are **non-negotiable** and carry forward into v3. Changing
> them requires an explicit, conscious decision — not a gradual feature
> addition. Subsystem documents (`policy-engine.md`, `agent-loop-design.md`,
> etc.) are subordinate to them.

### 1. Network Perimeter

- The Joi host has **no direct WAN access** — no outbound internet, no inbound
  internet connections. Package installs use a temporary, explicitly opened
  update window (`sysprep/joi/update.sh --enable` / `--disable`).
- All traffic to and from Joi travels over the **Nebula mesh** (encrypted,
  certificate-authenticated).
- The Nebula enclave is the trust boundary. Anything Joi talks to (mesh today,
  any future service) is an enclave-internal Nebula node. Nothing in this
  project is internet-facing from Joi's side.

### 2. LLM Trust and Autonomy

- The LLM operates within the **Protection Layer** — rate limits, cooldowns,
  output validation and policy checks it cannot bypass.
- Within those bounds, the LLM is **trusted to act autonomously**. Joi is a
  digital entity with its own initiative; it does not require owner
  confirmation for every action.
- The precedent is **Wind**: proactive Signal messages are sent without
  approval. Any future machine-facing capability extends the same autonomy
  model; it does not introduce a new trust category.
- Autonomy is bounded by the enclave. Nothing Joi initiates reaches the
  internet.

### 3. LLM Model Policy

- Only **trusted, vetted models** may be used. Chinese-origin models (Qwen,
  DeepSeek, etc.) are permanently banned — supply chain security.
- Primary models should be **uncensored** and **Slovak-capable** (strongly
  recommended).

### 4. Permanently Out of Scope

**`codeexec` will never be implemented.** Arbitrary code execution by the LLM
on any host is permanently out of scope, regardless of sandboxing.

### 5. Human Communication Channel

- Signal is the **primary human communication channel**.
- Other transports (Telegram, WhatsApp, etc.) may be added via the mesh's
  transport abstraction without architectural change.
- No web UI, no public API, no direct HTTP interface to end users.

### 6. Data Residency

- All user data — conversation context, facts, summaries, RAG, Wind state —
  stays on the **Joi host only**.
- No data leaves the enclave except as part of a Signal message.

### 7. Multi-User by Default

- Joi serves multiple users and groups. Every meaningful piece of state
  (context, facts, Wind, settings) is scoped per `conversation_id`.
- Prompt injection, privacy and access control are treated as real
  multi-user threats, not hypotheticals.

---

## Deployment (lab, v2.0)

| Node | Nebula IP | Role | State |
|------|-----------|------|-------|
| mesh | 10.42.0.1 | Signal proxy, Nebula lighthouse, WAN-facing | Stateless |
| joi | 10.42.0.10 | LLM agent, config authority | Stateful |

| Node | Platform |
|------|----------|
| joi | Physical host (for now), full-disk encryption (LUKS). Intel i7-9750H (6C/12T), 16 GB RAM, NVIDIA GTX 1650 4 GB (~3.6 GB usable), driver 590-open, CUDA 13.1 |
| mesh | Virtual machine. vCPU model must be **x86-64-v3** or better (signal-cli native build requirement) |

Ports, flows and UFW rules: see `comms-matrix.md`.

```
┌──────────────────────────────────────────┐
│                INTERNET                  │
└────────────────────┬─────────────────────┘
                     │ Signal (TLS)
┌────────────────────▼─────────────────────┐
│               mesh (VM)                  │
│  signal-cli (linked device) + worker     │
│  Nebula lighthouse        (STATELESS)    │
└────────────────────┬─────────────────────┘
                     │ Nebula VPN + HMAC
┌────────────────────▼─────────────────────┐
│             joi (host, no WAN)           │
│  ┌────────────────────────────────────┐  │
│  │ PROTECTION LAYER                   │  │
│  │ rate limits, cooldowns, validation │  │
│  └─────────────────┬──────────────────┘  │
│  ┌─────────────────▼──────────────────┐  │
│  │ joi-api (FastAPI) + scheduler      │  │
│  │ Wind, memory (SQLCipher), policy   │  │
│  └─────────────────┬──────────────────┘  │
│  ┌─────────────────▼──────────────────┐  │
│  │ Ollama (docker, localhost:11434)   │  │
│  └────────────────────────────────────┘  │
└──────────────────────────────────────────┘
```

---

## Stateless Mesh

**Mesh stores nothing on disk** except signal-cli account data. All
configuration — including the HMAC key — comes from Joi via config push and
lives in memory only. When mesh restarts, the key is gone and mesh waits for
Joi.

### On Mesh Startup
1. Mesh starts with empty policy (denies all messages) and no HMAC key
2. Waits for Joi to push config via `/config/sync`
3. Bootstrap `/config/sync` is allowed unauthenticated — UFW restricts port
   8444 to Joi's Nebula IP only
4. Mesh stores policy and HMAC key in memory and returns a challenge response
   confirming key receipt
5. From then on, all requests are HMAC-authenticated

### Recovery After Mesh Restart
Joi notices on its next config-sync check (every ~10 minutes, see
[Config Push](#config-push-joi--mesh)):
- Mesh reports no config hash → Joi pushes config with bootstrap key
- Mesh reports a different hash → drift → Joi pushes fresh config
- Mesh unreachable → retried on the next sync check

Recovery is therefore **up to ~10 minutes**, not immediate. Joi's startup
always pushes immediately.

### Mesh Files

| Path | Purpose | Persistent |
|------|---------|------------|
| `/etc/default/mesh-signal-worker` | Env vars (Signal account, Joi endpoint) | Yes |
| `/var/lib/signal-cli/` | Signal account data (linked device) | Yes |
| (memory only) | Policy + HMAC key | No |

---

## Config Push: Joi → Mesh

Joi pushes config to mesh:
- **On startup** — always, forced
- **Every 10 scheduler ticks (~10 min)** — pushes if local policy hash changed,
  or if mesh reports empty/drifted config; otherwise only polls
  `/config/status`
- **Manually** — `POST /admin/config/push`

A changed policy file does *not* trigger a live push: tamper detection stops
the service, and the restart pushes the new config.

### Config Payload

```json
{
  "version": 1,
  "timestamp_ms": 1708300000000,
  "identity": {
    "bot_name": "Joi",
    "allowed_senders": ["+1234567890"],
    "groups": {
      "<group_id>": {
        "participants": ["+1234567890"],
        "names": ["Joi", "Jessica"]
      }
    }
  },
  "rate_limits": {
    "inbound": { "max_per_hour": 120, "max_per_minute": 20 }
  },
  "validation": { "max_text_length": 1500 },
  "security": { "privacy_mode": true, "kill_switch": false },
  "bootstrap_hmac_key": "<64-char-hex>",
  "bootstrap_challenge": "<32-char-hex>",
  "hmac_rotation": {
    "new_secret": "<64-char-hex>",
    "effective_at_ms": 1708300060000,
    "grace_period_ms": 60000
  }
}
```

`bootstrap_hmac_key` is always included; mesh stores it only if it has no key
yet. `hmac_rotation` is optional.

### Mesh HTTP Endpoints

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `POST /config/sync` | HMAC (none on bootstrap) | Push config + bootstrap key |
| `GET /config/status` | None | Config hash + `hmac_configured` |
| `GET /health` | None | Mesh + signal-cli status |
| `POST /api/v1/message/outbound` | HMAC | Send a Signal message |
| `POST /api/v1/typing` | HMAC | Typing indicator |
| `GET /groups/members` | HMAC | Group membership |
| `GET /api/v1/delivery/status` | HMAC | Delivery receipts |

Every inbound request to mesh refreshes its "last Joi contact" timestamp,
including unauthenticated `/health` and `/config/status`.

---

## HMAC Authentication

All Joi ↔ mesh requests are authenticated with HMAC-SHA256 (shared
implementation in `execution/shared/hmac_core.py`).

### Headers

```
X-Nonce: <uuid4>
X-Timestamp: <unix-epoch-ms>
X-HMAC-SHA256: HMAC-SHA256(nonce + timestamp + body, secret)
```

### Validation
1. Timestamp within 5 minutes
2. Nonce not seen before (15-minute retention)
3. Signature matches

### Key Storage

| Location | Purpose |
|----------|---------|
| Joi: `/etc/default/joi-api` | `JOI_HMAC_SECRET` (persistent; Joi is the source of truth) |
| Joi: `/var/lib/joi/hmac.secret` | Rotated secret, managed by the rotator |
| Mesh: RAM only | Pushed by Joi on every bootstrap; never written to disk |
| Mesh: `MESH_HMAC_SECRET` | Emergency fallback only |

### Bootstrap Challenge

Every config push carries `bootstrap_hmac_key` and a random
`bootstrap_challenge`. Mesh stores the key if it has none, computes
`HMAC(key, challenge)` and returns it. Joi verifies the response to confirm
mesh holds the correct key.

### Rotation

Weekly automatic rotation (checked once per day by the global daily tasks),
60-second grace period:

1. Joi generates a new 32-byte secret
2. Joi pushes config with `hmac_rotation` (authenticated with the current key)
3. Mesh keeps both keys valid during the grace period
4. Joi persists the new secret
5. Old key rejected after 60 seconds

On startup, Joi runs a sync check that recovers from a rotation interrupted
by a crash. Manual rotation: `POST /admin/hmac/rotate`.

### Key Staleness Watchdog (mesh)

Mesh's watchdog runs every 60 s. If no request from Joi arrived during a
cycle, it counts a miss; after `MESH_CONFIG_STALENESS_CHECKS` consecutive
misses (default **2**, ~120 s) it clears the HMAC key and returns to the
waiting state. Joi's next config push restores it.

> **Known gap:** an idle Joi contacts mesh only every ~10 minutes, so the
> watchdog can clear the key during quiet periods. See
> [Known Gaps](#known-gaps-in-v20).

---

## Security Controls

### Privacy Mode

When enabled (default: on), logs redact personal data:
- Phone numbers: `+1234567890` → `+***7890`
- Group IDs: `abc123...` → `[GRP:abc1...]`
- Message text, LLM responses and facts are omitted or redacted

Toggle: `POST /admin/security/privacy-mode`

### Kill Switch

When enabled, mesh drops all inbound messages and blocks outbound sends.
Messages are dropped silently. Toggle: `POST /admin/security/kill-switch`

### Tamper Detection

Every scheduler tick (~60 s), Joi compares SHA256 fingerprints of:
- `/etc/default/joi-api`, `/etc/joi/memory.key`, `/etc/joi/hmac.key`
- the mesh policy file (`JOI_MESH_POLICY_PATH`)
- prompt files in `JOI_PROMPTS_DIR` (`*.txt`, `*.model`, `*.context`,
  `users/*`, `groups/*`)

On mismatch the service **exits immediately** (`os._exit(78)`, EX_CONFIG).
systemd restarts it, which re-initializes the fingerprints from the current
state.

### Input / Output Validation

- Inbound text over `JOI_MAX_INPUT_LENGTH` (1500) is rejected; mesh enforces
  the same limit at transport.
- Replies are capped at `JOI_MAX_OUTPUT_LENGTH` (2500 chars); Wind messages at
  `JOI_WIND_MAX_LENGTH` (1200 chars, cut at a sentence boundary).
- Replies containing leaked system-prompt markers are blocked.

---

## Joi HTTP Endpoints

All `/admin/*` endpoints are **local-only** (request from `127.0.0.1` or from
`JOI_BIND_HOST` itself). Mutating (`POST`) admin endpoints additionally
require HMAC.

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Health and status (no auth) |
| `/api/v1/message/inbound` | POST | Message from mesh (HMAC) |
| `/api/v1/typing/inbound` | POST | Typing indicator from mesh (HMAC) |
| `/api/v1/document/ingest` | POST | Document from mesh for RAG (HMAC) |
| `/admin/config/status` | GET | Config sync status |
| `/admin/config/push` | POST | Force config push |
| `/admin/hmac/status` | GET | Rotation status |
| `/admin/hmac/rotate` | POST | Manual rotation |
| `/admin/security/status` | GET | Privacy mode / kill switch state |
| `/admin/security/privacy-mode` | POST | Toggle privacy mode |
| `/admin/security/kill-switch` | POST | Toggle kill switch |
| `/admin/fts/status` | GET | Full-text index status |
| `/admin/fts/rebuild` | POST | Rebuild full-text index |
| `/admin/routing/status` | GET | Mesh routing config |
| `/admin/routing/toggle` | POST | Enable/disable routing |
| `/admin/rag/scopes` | GET | RAG scopes and chunk counts |
| `/admin/rag/search` | GET | Test RAG search |

`joi-api` binds to `JOI_BIND_HOST` (the Nebula IP), not `127.0.0.1`.

---

## Message Flow

### Inbound (Signal → Joi)

```
1. signal-cli on mesh receives the message
2. Mesh checks policy (in memory):
   - Sender in allowed_senders (DM)?            → forward
   - Sender is a participant of a known group?  → forward
   - Known group, sender not a participant?     → forward as store_only
   - Unknown sender?                            → drop silently
3. Mesh checks inbound rate limits (in memory)
4. Mesh forwards to Joi with HMAC (worker pool, MESH_FORWARD_TIMEOUT 280 s)
5. Joi processes through its message queue (owner messages prioritized):
   - normal messages may produce a reply
   - store_only messages are stored for context only
```

Documents follow the same path to `/api/v1/document/ingest` (size capped by
`MESH_MAX_DOCUMENT_SIZE`). Mesh optionally routes to multiple backends
(routing rules pushed in policy; default backend is Joi).

### Outbound (Joi → Signal)

```
1. Joi sends a typing indicator while generating
2. Joi validates the reply (length, leak markers)
3. Joi sends to mesh with HMAC
4. Mesh sends via signal-cli and tracks delivery receipts
```

### Rate Limits

| Scope | Default | Where |
|-------|---------|-------|
| Inbound per sender | 120/hr, 20/min | Mesh (memory, from policy) |
| Outbound cooldown | 5 s DM, 2 s group | Joi, per conversation |
| Outbound total | 120/hr | Joi (`JOI_OUTBOUND_MAX_PER_HOUR`; `is_critical` bypasses) |

---

## Trust Boundaries

```
┌─────────────────────────────────────────────────────────────┐
│                       UNTRUSTED                             │
│                   (Internet, Signal)                        │
└──────────────────────────┬──────────────────────────────────┘
                           │ Boundary 1: WAN → mesh
                           │ (Signal protocol, TLS)
┌──────────────────────────▼──────────────────────────────────┐
│                     SEMI-TRUSTED                            │
│                  (mesh — stateless)                         │
│  Enforces: rate limits, sender validation, HMAC auth        │
│  Cannot: persist config, reach Joi outside Nebula           │
└──────────────────────────┬──────────────────────────────────┘
                           │ Boundary 2: mesh → Joi
                           │ (Nebula + HMAC)
┌──────────────────────────▼──────────────────────────────────┐
│                       TRUSTED                               │
│                     (Joi host)                              │
│  LLM agent, memory (SQLCipher), policy authority            │
│  Full-disk encryption (LUKS)                                │
└─────────────────────────────────────────────────────────────┘
```

Threat analysis: `Joi-threat-model.md`.

---

## LLM Runtime

### Ollama

Runs in docker on the Joi host, managed by docker compose
(`sysprep/joi/ollama-compose.yml`, deployed to `/opt/joi/ollama-runtime/`):

- Image pinned to `ollama/ollama:0.30.7`
- `OLLAMA_NO_CLOUD=1`, plus `ollama.com` blackholed in `/etc/hosts`
- `LLAMA_ARG_FIT=off` (llama.cpp auto-fit crashes on this GPU/model combo)
- `network_mode: bridge` so `localhost:11434` works from the host
- Log retention capped at 3 × 20 MB
- Existing `ollama` volume reused (`external: true`)

### Models

Models are Ollama Modelfile builds with personality and parameters baked in
(`execution/joi/ollama/`).

| Role | Selected by |
|------|-------------|
| Main conversational model | `JOI_OLLAMA_MODEL`, overridden per user/group by a `.model` file in the prompts directory |
| Intent / fact detector | `JOI_DETECTOR_MODEL` (`joi-detector`) |
| Wind tension mining | `JOI_CURIOSITY_MODEL` (`joi-curiosity`) |
| Wind engagement classifier | `JOI_ENGAGEMENT_MODEL` (`joi-engagement`) |
| Compaction (optional) | `JOI_CONSOLIDATION_MODEL` (`joi-consolidator`) |
| Translation (optional) | `JOI_TRANSLATE_MODEL_PREFIX`, enabled per conversation by a `.translate` file |
| Embeddings (optional) | `JOI_EMBEDDING_MODEL` (`bge-m3`, CPU-only by default) |

At v2.0 the main conversational models are gemma4 (E4B) based; the global
fallback is a Llama 3.1 8B abliterated build.

---

## Memory

### Storage

- **Database:** SQLite + SQLCipher (encrypted; unencrypted refused unless
  `JOI_REQUIRE_ENCRYPTED_DB=0`)
- **Location:** `/var/lib/joi/memory.db`
- **Key:** `/etc/joi/memory.key`

Schema: `memory-store-schema.md`.

### What Is Stored

| Type | Retention | Purpose |
|------|-----------|---------|
| Context | Last 50 messages (`JOI_CONTEXT_MESSAGES`) | Recent conversation |
| Facts | Permanent (pinned facts always injected) | Extracted knowledge |
| Summaries | Permanent | Compacted conversation history |
| RAG knowledge | Permanent, scoped | Ingested documents |
| Wind state | Permanent | Topics, mood, feedback, quiet samples (60 days) |
| Reminders | Fired/expired kept 180 days | `reminder-engine.md` |
| Processed messages | Kept forever by default (`JOI_MESSAGE_RETENTION_DAYS`, max 90) | Archive |

Retrieval injects facts, summaries and RAG chunks per message. Search is
hybrid: full-text (FTS5/BM25) plus, when an embedding model is set, vector
similarity, fused with reciprocal rank fusion.

### Compaction

Compaction extracts facts and writes a summary for a batch of old messages,
then archives them. It runs:
- **After a reply**, when the conversation exceeds the context size — the
  oldest `JOI_COMPACT_BATCH_SIZE` (20) messages
- **Before a Wind send** — all messages, so the user's reply lands on a clean
  context
- **On owner request** — a confirmed compact command

Per-conversation `.context` and `.compact_window` files override the defaults.

---

## Scheduler

A background thread ticks every 60 s (`JOI_SCHEDULER_INTERVAL`) after a 10 s
startup delay. Each tick runs:

| Cadence | Task |
|---------|------|
| Every tick | Knowledge ingestion check, tamper detection, Wind, reminders, note reminders |
| Every 10 ticks | Config sync with mesh |
| Every 15 ticks | Group membership refresh (business mode) |
| Every 60 ticks | Nonce cleanup, send-cache cleanup, FTS integrity check |
| Per conversation, when quiet at end of day | Wind daily tasks (dedup, mood rollup, decay, quiet sampling) |
| Once per calendar day | HMAC rotation check, reminder and message purges |

Tick errors are logged and counted; they never stop the scheduler.

---

## Wind (Proactive Messaging) — v1

Wind v1 is shipped (phases 4a–4d and 5): impulse scoring with silence and
cooldown gates, learned quiet hours, Joi mood and momentum, a per-conversation
topic queue with priority decay and affinity protection, tension mining from
recent conversation, engagement classification of replies, a morning message
with evening context, and a wake-up procedure after long silence.

Full design: `wind-architecture-v1.md`. Configuration: `wind-config.md`.

Modes: `companion` (Wind on) and `business` (request/response; DM access to
group knowledge is a separate switch, default off).

---

## File Locations

### Joi

| Path | Purpose |
|------|---------|
| `/opt/joi` | Repository checkout (`execution/joi` is the service root) |
| `/etc/default/joi-api` | Environment (template: `execution/joi/systemd/joi-api.default`) |
| `/etc/joi/memory.key` | SQLCipher key |
| `/var/lib/joi/memory.db` | Database |
| `/var/lib/joi/policy/mesh-policy.json` | Policy pushed to mesh |
| `/var/lib/joi/prompts/` | System prompts and per-conversation overrides |
| `/var/lib/joi/ingestion/` | Knowledge ingestion drop folder |
| `/opt/joi/ollama-runtime/` | Ollama docker compose |

**Path convention:** all paths are lowercase (`/opt/joi`). Older installations
keep a `/opt/Joi` → `/opt/joi` symlink because reinstalling is not an option;
new installations must use lowercase only. All repo files (systemd units,
sysprep scripts and stages) use `/opt/joi`.

### Mesh

| Path | Purpose |
|------|---------|
| `/etc/default/mesh-signal-worker` | Environment |
| `/var/lib/signal-cli/` | Signal account data |

---

## Services

| Host | Unit | Runs as | Entry point |
|------|------|---------|-------------|
| joi | `joi-api.service` | `joi` | `python3 -m api.server` in `/opt/joi/execution/joi` |
| joi | docker compose `ollama` | root | `ollama/ollama:0.30.7` |
| mesh | `mesh-signal-worker.service` | `signal` | `run-worker.sh` (spawns signal-cli over stdio JSON-RPC) |

Unit files: `execution/joi/systemd/joi-api.service`,
`execution/mesh/proxy/systemd/mesh-signal-worker.service`.

---

## Operational Requirements

These are not obvious from the code, and each has caused an outage:

- **Joi runs in `multi-user.target` (no GUI).** A display manager holds the
  CUDA context and ~9 GB RAM and breaks Ollama. Package upgrades can
  re-enable it; check after every reboot.
- **The Joi kernel is pinned** to a version with matching NVIDIA modules —
  `apt-mark hold` on the kernel and HWE metapackages, plus a GRUB default by
  name. A new kernel without modules means no GPU.
- **signal-cli native builds need an x86-64-v3 CPU.** Older vCPU models fail
  with "CPU ISA level is lower than required".
- **signal-cli is a linked device.** Signal unlinks devices that stay offline
  too long; when that happens mesh still starts, but no messages flow until
  the device is re-linked from the phone.
- **signal-cli must track Signal protocol changes.** Old versions fail to
  parse new envelopes and drop messages; update when that happens.

---

## Emergency Stop

| Method | Speed | Effect |
|--------|-------|--------|
| Kill switch (admin API) | Instant | Mesh drops messages |
| Stop `mesh-signal-worker` or the mesh VM | Seconds | All Signal traffic stops |
| Stop `joi-api` | Seconds | AI stops |
| Power off the Joi host | Seconds | Everything stops; disk locks (LUKS) |

---

## Known Gaps in v2.0

Recorded so v3 can address them deliberately:

1. **Config-sync cadence vs. mesh staleness watchdog.** Joi contacts mesh every
   ~10 minutes when idle; mesh clears the HMAC key after ~2 minutes without
   contact. During quiet periods mesh can lose the key until the next sync.
2. **Unlinked Signal device is not detected at runtime.** Mesh's health check
   only verifies that signal-cli answers RPC calls. An unlinked device still
   answers, so `/health` reports healthy while nothing is delivered. At
   startup the condition is logged only as a warning.
3. **Failed proactive sends are silent.** See `wind-architecture-v2.md`,
   "Known v1 gaps".
4. **Failed HMAC rotations retry daily, not hourly.** The rotator supports an
   hourly retry interval, but it is only consulted from the once-per-day
   global tasks.
5. **The scheduler freezes while a Wind or reminder message generates.** The
   scheduler hands generation to the message queue and waits for the result
   (600 s timeout, extended by heartbeats). Meanwhile no other scheduler work
   runs: tamper detection, other reminders, config sync with mesh. Long
   generations therefore also delay mesh contact (gap 1), and under load the
   scheduler can be frozen most of the time.

---

## Not in v2.0

Designed or planned, but not built at `v2.0`:

| Item | Spec |
|------|------|
| System Channel (machine-to-machine connector) | `system-channel.md` |
| Search VM / web search (DDG + page fetch) | `tool-external-search.md` |
| openhab (read) | `system-channel.md` |
| Zabbix (read/write) | `system-channel.md` |
| Image generation | — |
| TTS / voice | — |
| Wind v2 (human-rhythm proactive) | `wind-architecture-v2.md` |

What v3 takes on is defined in `Joi-architecture-v3.md`.

---

## References

| Document | Purpose |
|----------|---------|
| `Joi-threat-model.md` | Threat model |
| `comms-matrix.md` | Network flows, ports, IPs |
| `api-contracts.md` | API specifications |
| `policy-engine.md` | Security policy rules |
| `memory-store-schema.md` | Database schema |
| `wind-architecture-v1.md` | Wind v1 design |
| `wind-config.md` | Wind configuration |
| `reminder-engine.md` | Reminders |
| `commands.md` | User-facing Signal commands |
| `env-reference.md` | Environment variables |
| `sensitive-config.md` | Secrets reference (not in git) |
| `sysprep/` | Deployment stages for Joi and mesh |
