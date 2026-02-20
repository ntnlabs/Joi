# Joi - Project Summary

> Quick reference for understanding this project. Last updated: 2026-02-20

## What is Joi?

Joi is a **security-focused, offline AI personal assistant** running as a Proxmox VM with GPU acceleration. It communicates via Signal messaging, integrates with external systems through a generic System Channel, and can leverage isolated LLM services for extended capabilities.

## Project Status

**Phase: Implementation** - Core system operational with defense-in-depth security.

### Milestones

| Date | Milestone |
|------|-----------|
| 2026-02-20 | **Document receiving via Signal** - Users can send .txt/.md files via Signal for RAG ingestion; type/size validation, auto-forwarding to Joi, scoped knowledge storage |
| 2026-02-20 | **Self-describing facts** - Facts include person names in values ("Peter is a developer"), unified conversation_id storage, fixed input label and retrieval key mismatches |
| 2026-02-19 | **Count-based memory compaction** - Fixed memory drift bug: compact oldest N messages when context exceeded (no more "forgotten then remembered" summaries) |
| 2026-02-19 | **Business mode DM group knowledge** - Configurable mode (companion/business) with optional DM access to group knowledge based on real Signal memberships |
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
| **LLM Services** | Isolated VMs for image generation, web search, TTS, code execution |
| **Behavior Modes** | `companion` (proactive, organic) or `assistant` (request-response only) |
| **Operating Modes** | `companion` (default, DM group knowledge off) or `business` (configurable DM group access) |

## Technology Stack

| Component | Technology | Notes |
|-----------|------------|-------|
| LLM | Llama 3.1 8B abliterated | Primary - mannix/llama3.1-8b-abliterated |
| LLM Config | Ollama Modelfile | Baked-in personality, per-user/group models |
| LLM Requirements | Uncensored + Slovak | No restrictive filters, good Slovak support |
| Hardware | ASUS NUC 13 Pro + RTX 3060 eGPU | Proxmox host with GPU passthrough |
| Virtualization | Proxmox VE | Joi runs as isolated VM |
| Messaging | Signal | Via secure proxy VM (Interactive Channel) |
| Mesh VPN | Nebula | All VMs on encrypted mesh |
| System Channel | Generic API | openhab, Zabbix, actuators, LLM Services |
| Database | SQLite + SQLCipher | Encrypted local storage |
| Proxy Host | mesh.homelab.example | Ubuntu 24 LTS, 2GB RAM, 16GB disk |

**LLM Policy:** Chinese models (Qwen, DeepSeek, etc.) are banned for security/trust reasons.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         INTERNET                                │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│              mesh.homelab.example (Ubuntu 24 LTS)               │
│              Signal bot + Nebula lighthouse                     │
└───────────────────────────┬─────────────────────────────────────┘
                            │ Nebula mesh VPN
┌───────────────────────────▼─────────────────────────────────────┐
│       ASUS NUC 13 Pro (Proxmox Host) ──TB4──► eGPU (RTX 3060)   │
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

## Documentation Files

| File | Purpose |
|------|---------|
| `AGENTS.md` | Development guidelines, coding standards, planned structure |
| `Joi-architecture-v2.md` | Current architecture (security-hardened) |
| `system-channel.md` | System Channel & LLM Services specification |
| `agent-loop-design.md` | Agent behavior, impulse system, behavior modes |
| `Joi-threat-model.md` | Threat analysis, attack surfaces, mitigations |

## Planned Directory Structure

```
/src/           - Runtime code, adapters, and agents
/tests/         - Unit and integration tests
/assets/        - Diagrams and visual documentation
```

## Coding Standards (from AGENTS.md)

- **Indentation**: 2 spaces (YAML/JSON), 4 spaces (Python)
- **Commits**: Concise, imperative mood
- **Secrets**: Never in repo, use `.env` files

## Hardware Platform

| Component | Product | Notes |
|-----------|---------|-------|
| Mini PC / Host | ASUS NUC 13 Pro NUC13ANHI7 | Proxmox VE, Thunderbolt 4 |
| GPU | NVIDIA RTX 3060 12GB | eGPU enclosure via TB4 |
| eGPU Enclosure | TBD | See hardware-budget-analysis.md |

See `hardware-budget-analysis.md` for detailed budget and sourcing options.

## Critical TODOs (from threat model)

1. ~~Implement Proxy → Joi authentication~~ ✓ RESOLVED (Nebula mesh VPN)
2. ~~Add prompt injection defenses~~ ✓ RESOLVED (see prompt-injection-defenses.md)
3. ~~Protect Signal credentials~~ ✓ RESOLVED (LUKS + file permissions, documented in Joi-architecture-v2.md)
4. ~~Enforce read-only constraints at all layers~~ ✓ RESOLVED (policy-engine.md)
5. ~~Add rate limiting on agent actions~~ ✓ RESOLVED (policy-engine.md, 60/hr direct, unlimited critical)

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

Select top 1-3 summaries under token budget. Summaries stay core memory, but only relevant ones enter each turn.

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

**Behavior:**
- First encounter → create Joi identity, link available transport IDs
- Subsequent encounters → lookup by transport ID, resolve to Joi identity
- Facts stored under Joi identity (cross-transport, cross-conversation)

**Scoping rules (unchanged):**
- Group facts → scoped to group_id (privacy boundary)
- DM facts → scoped to user's Joi identity
- Business mode → explicit opt-in to bridge scopes
