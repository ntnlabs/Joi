# Joi - Project Summary

> Quick reference for understanding this project. Last updated: 2026-03-19

## What is Joi?

Joi is a **security-focused, offline AI personal assistant** running as a Proxmox VM with GPU acceleration. It communicates via Signal messaging, integrates with external systems through a generic System Channel, and can leverage isolated LLM services for extended capabilities.

## Project Status

**Phase: Implementation** - Core system operational with defense-in-depth security. Wind proactive messaging live through Phase 4c (adaptive quiet hours complete; emotional follow-up remaining).

### Milestones

| Date | Milestone |
|------|-----------|
| 2026-04-13 | **Clock-time end-of-day scheduler** - Replaced tick-count daily tasks with clock-time gate (03:00 local); per-conversation `last_daily_tasks_at` in DB; global tasks (HMAC rotation, purge) tracked in `system_state` and persist across restarts |
| 2026-04-13 | **Wind Phase 4c: Adaptive quiet hours** - `_compute_learned_quiet_start()` uses circular mean of inbound message timestamps; overrides configured quiet start in `_check_not_quiet_hours()`; updated nightly during end-of-day tasks |
| 2026-04-11 | **Task list** - Natural language task management via Signal: add, done, reopen, delete, list tasks; LLM parser with list-name inference from known lists |
| 2026-03-19 | **Ollama model presence check at startup** - `_validate_models()` checks all configured model env vars against Ollama on startup; fails fast with clear error if any are missing |
| 2026-03-19 | **Wind Phase 4c: Tension extraction** - Curiosity LLM mines conversation history for unfinished threads; fires on silence (configurable) and pre-compaction; creates tension topics for Wind to follow up on |
| 2026-03-15 | **Wind Phase 4b: Learning & Pursuit** - Symmetric affinity/decay model, pursuit back-off, undertaker (permanent block), ghost probes, cooldown anti-periodicity, novelty bonus |
| 2026-03-13 | **Wind Phase 4a: Engagement Foundation** - Feedback loop for proactive messages: direct reply detection, LLM classification (joi-engagement model), 12h timeout, per-topic-family rejection/interest weights, lifecycle rules per topic type |
| 2026-03-13 | **Wind Phase 3: Natural Variance** - Organic threshold drift via bounded random walk, accumulated impulse, soft probability — Wind no longer fires at predictable intervals |
| 2026-03-10 | **Code review & refactoring** - server.py reduced ~36% by extracting MessageQueue, Scheduler, AdminRoutes, GroupCache modules; security fixes from code review |
| 2026-03-09 | **Wind Phase 2: Live proactive sends** - Full topic queue, impulse engine, shadow mode → live sends, reminder system |
| 2026-03-09 | **Wind Phase 1: Foundation** - Shadow mode, state manager, topic manager, impulse scoring, decision logger |
| 2026-03-03 | **Signal Unicode formatting** - Optional post-processor converts **bold** markdown to Unicode bold (Signal doesn't render markdown) |
| 2026-03-01 | **Per-conversation memory settings** - Context size and compaction batch configurable per user/group via files |
| 2026-02-28 | **FTS5 query filtering** - Facts and summaries injected into prompts via full-text search relevance rather than bulk loading; per-conversation consolidation model override |
| 2026-02-26 | **GPU deployment & RAG/privacy fixes** - GPU deployment; configurable output length; RAG context visibility in debug |
| 2026-02-20 | **Document receiving via Signal** - Users can send .txt/.md files via Signal for RAG ingestion; type/size validation, auto-forwarding to Joi, scoped knowledge storage |
| 2026-02-20 | **Self-describing facts** - Facts include person names in values ("Peter is a developer"), unified conversation_id storage |
| 2026-02-19 | **Count-based memory compaction** - Fixed memory drift bug: compact oldest N messages when context exceeded |
| 2026-02-19 | **Business mode DM group knowledge** - Configurable mode (companion/business) with optional DM access to group knowledge |
| 2026-02-19 | **Security gaps closed** - Joi HMAC fail-closed, bounded thread pool, outbound rate limiting, mesh status polling |
| 2026-02-18 | **Stateless mesh architecture** - Mesh stores nothing on disk; all config pushed from Joi |
| 2026-02-17 | **Config push system** - One-way Joi → mesh config sync with hash verification |
| 2026-02-17 | **HMAC key rotation** - Weekly automatic rotation with 60s grace period |
| 2026-02-17 | **Privacy mode** - PII redaction in logs (phone numbers, group IDs) |
| 2026-02-17 | **Kill switch** - Emergency message halt for incident response |
| 2026-02-17 | **Tamper detection** - SHA256 fingerprinting of config files every 60s |
| 2026-02-14 | **Per-group model & prompt configuration** - Groups can have custom Ollama models with baked-in personalities plus additional prompt overlays |
| 2026-02-14 | **joi-admin purge tool** - Safe-by-default CLI for memory management (contexts, facts, keys) |
| 2026-02-14 | **Per-user/group context sizes** - Different conversations can have different message history limits |
| 2026-02-13 | **Ollama Modelfile system** - Custom models with baked-in SYSTEM prompts, temperature, context window |
| 2026-02-13 | **Time awareness** - Optional datetime injection into system prompt |
| 2026-02-13 | **Background scheduler** - Internal daemon for periodic tasks (replaces fragile cron) |
| 2026-02-12 | **Memory consolidation** - Automatic fact extraction and context compression |
| 2026-02-11 | **RAG knowledge retrieval** - Full-text search over ingested documents |
| 2026-02-10 | **Core API operational** - Signal ↔ Mesh ↔ Joi ↔ Ollama pipeline working |

## Key Concepts

| Concept | Description |
|---------|-------------|
| **Two-Layer Security** | Protection Layer (automation, LLM cannot bypass) + LLM Agent Layer (trusted decisions) |
| **Interactive Channel** | Human communication via Signal (bidirectional) |
| **System Channel** | Machine-to-machine communication (type-agnostic, read/write/both per source) |
| **LLM Services** | Isolated VMs for image generation, web search, TTS |
| **Modes** | `companion` (personal, proactive/Wind) or `business` (professional/shared, request-response; optional DM group knowledge via policy) |
| **Wind** | Proactive messaging subsystem — topic queue, impulse engine, engagement tracking |

## Technology Stack

| Component | Technology | Notes |
|-----------|------------|-------|
| LLM (main) | joi-personal | Custom Modelfile on mannix/llama3.1-8b-abliterated |
| LLM (groups) | joi-group | Custom Modelfile, group-tuned personality |
| LLM (consolidation) | joi-consolidator | Custom Modelfile, low temp, fact extraction |
| LLM (engagement) | joi-engagement | Custom Modelfile, classifies Wind engagement outcomes |
| LLM Runtime | Ollama | All models local, no cloud calls |
| LLM Requirements | Uncensored + Slovak | No restrictive filters, good Slovak support |
| Hardware | ASUS NUC 13 Pro + NVIDIA GPU | Proxmox host with GPU passthrough |
| Virtualization | Proxmox VE | Joi runs as isolated VM |
| Messaging | Signal | Via secure proxy VM (Interactive Channel) |
| Mesh VPN | Nebula | All VMs on encrypted mesh |
| System Channel | Generic API | openhab, Zabbix, actuators, LLM Services |
| Database | SQLite + SQLCipher | Encrypted local storage |

**LLM Policy:** Chinese models (Qwen, DeepSeek, etc.) are banned for security/trust reasons.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         INTERNET                                │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│              mesh VM (Ubuntu 24 LTS)                            │
│              Signal bot + Nebula lighthouse                     │
└───────────────────────────┬─────────────────────────────────────┘
                            │ Nebula mesh VPN
┌───────────────────────────▼─────────────────────────────────────┐
│       ASUS NUC 13 Pro (Proxmox Host) ──TB4──► eGPU (NVIDIA)      │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │                      joi VM (GPU Passthrough)              │ │
│  │  ┌──────────────────────────────────────────────────────┐  │ │
│  │  │   PROTECTION LAYER (rate limits, circuit breakers)  │  │ │
│  │  └──────────────────────────────────────────────────────┘  │ │
│  │  ┌──────────────────────────────────────────────────────┐  │ │
│  │  │   LLM Agent + Policy Engine + Memory Store           │  │ │
│  │  └──────────────────────┬───────────────────────────────┘  │ │
│  │           ┌─────────────┴─────────────┐                    │ │
│  │           ▼                           ▼                    │ │
│  │  Interactive Channel          System Channel               │ │
│  │  (Signal ↔ human)             (machine-to-machine)         │ │
│  └────────────────────────────────────────────────────────────┘ │
└───────────────────────────┬─────────────────────────────────────┘
                            │ Nebula mesh
          ┌─────────────────┼─────────────────┐
          ▼                 ▼                 ▼
┌──────────────┐     ┌──────────────┐   ┌──────────────┐
│   openhab    │     │   Zabbix     │   │  LLM Service │
│   [read]     │     │   [r/w]      │   │  VMs (async) │
└──────────────┘     └──────────────┘   └──────────────┘
```

## Key Security Principles

1. **Air-gapped**: Joi VM has NO direct internet access (isolated VM network)
2. **Two-layer security**: Protection Layer (automation, LLM cannot bypass) + LLM Agent Layer
3. **Isolated VM network**: Dedicated vmbr1 for joi ↔ mesh ↔ system channel traffic
4. **LLM-gated writes**: All intentional writes go through LLM decision; Protection Layer is separate
5. **Encrypted storage**: SQLCipher for DB, LUKS for Proxmox host
6. **Nebula mesh**: Encrypted, certificate-authenticated tunnel for all VMs
7. **Policy engine**: Enforces constraints on agent actions
8. **Trusted LLMs only**: No Chinese models (supply chain security)
9. **Mesh integrity**: Joi shuts down if mesh fails heartbeat (potential compromise)
10. **Maintenance USB key**: Physical USB with Ed25519 key enables planned mesh maintenance

## Directory Structure

```
execution/
├── joi/                    # Joi VM — API, Wind, memory, config
│   ├── api/                # FastAPI server, routes, scheduler, queue
│   ├── wind/               # Proactive messaging subsystem
│   ├── memory/             # SQLite/SQLCipher store
│   ├── config/             # Settings, logging, prompts
│   └── systemd/            # Service files and defaults
└── mesh/
    └── proxy/              # Mesh proxy service (Signal ↔ Joi)

sysprep/                    # VM provisioning scripts (stage1–4)
├── joi/
└── mesh/
```

## Documentation Files

| File | Purpose |
|------|---------|
| `CLAUDE.md` | Claude Code instructions for this project |
| `AGENTS.md` | Development guidelines, coding standards |
| `ENV-REFERENCE.md` | All environment variables with defaults |
| `COMMS-MATRIX.md` | Network flows, ports, VM IPs |
| `SENSITIVE-CONFIG.md` | Secrets and deployment checklist (not in git) |
| `Joi-architecture-v3.md` | Current architecture (security-hardened) |
| `wind-architecture-v1.md` | Wind proactive messaging full design |
| `WIND-CONFIG.md` | Wind config reference — all variables, formulas, tuning guide |
| `system-channel.md` | System Channel & LLM Services specification |
| `agent-loop-design.md` | Agent behavior, impulse system, behavior modes |
| `Joi-threat-model.md` | Threat analysis, attack surfaces, mitigations |

## Future Improvements

### Selective Summary Injection
Currently all summaries (last 7 days) are injected into every prompt. This can cause style/belief drift from persistent priming. Instead, score and select only relevant summaries per turn:

**Scoring formula:** `importance = 0.55*relevance + 0.25*recency + 0.20*intent_boost - novelty_penalty`

| Factor | Description |
|--------|-------------|
| **Relevance** | Keyword/entity overlap with current message (optional: embedding similarity) |
| **Recency** | Newer summaries score higher, older decay |
| **Intent boost** | If user asks memory-style questions ("remember", "last time", "what did we decide") |
| **Novelty penalty** | Down-rank if content already in context window or structured facts |

Select top 1-3 summaries under token budget.

### Joi Identity Registry
Transport-agnostic user identity system for pinpoint-accurate fact attribution.

**Problem:** Signal only provides phone numbers for contacts; non-contacts in groups only have UUIDs. Other transports have different ID schemes.

**Solution:** Joi-internal identity registry:
```
joi_users table:
  joi_id: UUID (Joi-generated, canonical)
  display_name: str
  identities: [
    {transport: "signal", type: "phone", value: "+123456"},
    {transport: "signal", type: "uuid", value: "abc-def-123"},
  ]
```

### Wind Phases 4b–4d
See `wind-architecture-v1.md` for full design:
- **4b**: Topic Affinity Model — symmetric upward learning (interest_weight accumulation)
- **4c**: WindMood — daily mood persistence, emotional variance
- **4d**: Day-of-week personality variance
