<img src="img/artwork.jpg" alt="Artwork" width="350" align="right">

# Joi

**Security-focused offline AI personal assistant**

Joi is an air-gapped AI assistant running on a local Proxmox VM with GPU acceleration. It communicates via Signal messaging, integrates with external systems through a generic System Channel, and can leverage isolated LLM services for extended capabilities.

## Aim

| Joi is not | Joi is |
|------------|--------|
| A realtime API | Event-driven |
| A website chatbot | Asynchronous |
| A request-response service | Stateful |

**Joi is a digital entity, not a tool.**

## Status

### Infrastructure
- ✅ Joi VM with LUKS full-disk encryption
- ✅ Nebula mesh VPN (mesh ↔ joi encrypted tunnel)
- ✅ HMAC + nonce authentication (defense-in-depth)
- ✅ UFW firewalls (deny-by-default on both VMs)
- ✅ Joi VM has no WAN egress (isolated network)
- ✅ GPU passthrough (AI accelerator card via Thunderbolt)

### Messaging
- ✅ Signal integration (send/receive via signal-cli JSON-RPC)
- ✅ Rate limiting & message deduplication
- ✅ Policy-based sender filtering (with user feedback on rate limit)
- ✅ Message queue with owner priority
- ✅ Async mesh → joi forwarding (fire-and-forget)
- ✅ Group message handling (context-aware responses)
- ✅ Per-user and per-group system prompts
- ✅ Reaction responses (contextual acknowledgments)
- ✅ Response cooldown (configurable per DM/group)
- ⚠️ Signal @mention detection (limited — signal-cli doesn't provide mentions array reliably)

### Wind (proactive messaging)
- ✅ Impulse-based scheduler with accumulator, threshold drift, soft trigger
- ✅ Engagement feedback (ignored/deflected proactives suppress future sends)
- ✅ Topic affinity & decay (liked families surface more, rejected families suppressed)
- ✅ Special dates, spontaneous sharing, tension extraction, outcome curiosity, emotional follow-up
- ✅ Hot/heated conversation suppression (two-tier EMA)
- ✅ Adaptive quiet hours (learned from inbound message history)
- ✅ Topic priority decay with affinity protection
- ✅ Wake-up procedure (gap marker + proactive re-engagement after long silence)
- ⏳ Daily mood momentum, day-of-week personality, 30-day cycle

### Memory
- ✅ Conversation context (configurable window)
- ✅ Long-term memory (facts extraction, summaries)
- ✅ Real-time fact saving (hybrid: keyword trigger + LLM detection)
- ✅ Memory consolidation (LLM extracts facts + summarizes on context overflow)
- ✅ RAG knowledge retrieval (FTS5 full-text search)
- ✅ Multi-turn FTS context window (last N user turns used as search query)
- ✅ Dynamic FTS window boost for fast conversations
- ✅ Per-user/group RAG scopes with access control
- ✅ Document ingestion into scoped RAG (including attachments)
- ✅ Auto-ingestion via watched directory
- ✅ SQLCipher database encryption (key-file based)

### User Features
- ✅ [Reminders](reminder-engine.md) — natural language, multi-reminder input, snooze, reschedule, cancel
- ✅ [Notes](COMMANDS.md#notes-dm-only) — named notes, append, update, search, delete (DM)
- ✅ [Task lists](COMMANDS.md#task-lists-dm-only) — named lists, add/done/reopen/delete per item or list (DM)
- ✅ [Wind snooze](COMMANDS.md#wind-snooze-dm-only) — silence proactive messages for a duration or until morning

### Config & Security
- ✅ One-way config push (Joi → mesh, stateless mesh)
- ✅ Weekly HMAC key rotation with grace period
- ✅ Privacy mode (PII redaction in logs)
- ✅ Kill switch (emergency message halt)
- ✅ Tamper detection (config file monitoring)
- ✅ Structured logging with JSON/text modes

### Pending
- ⏳ System Channel integration
- ⏳ LLM Services (imagegen, websearch, etc.)
- ⏳ Voice message transcription (Whisper)

## Roadmap

### Near-term

- **Prompt injection scanning** — scan fact writes for invisible Unicode and injection patterns before committing to the facts table
- **Wind phase 4d** — daily mood momentum, day-of-week personality, 30-day cycle

### Medium-term

- **Vector search** — replace Python-side cosine similarity loop with `sqlite-vss` for in-SQLite vector search; add semantic layer to fact retrieval so keyword mismatches stop losing relevant memories
- **Vision model support** — image analysis via vision-capable model; mesh forwards attachment, Joi analyses and responds, attachment deleted after processing
- **Background review agent** — after every N turns, a silent sub-call reviews the conversation and autonomously updates facts
- **FTS5 session search** — index past conversations; LLM queries them when user references past events
- **System Channel integration**
- **Voice message transcription** — Whisper integration

### Longer-term

- **Async queuing** — decouple signal-cli I/O from HTTP handler with priority queue and backpressure
- **Circuit breaker** — hard cap on LLM calls per hour

## Key Features

- **Air-gapped**: Joi VM has no direct internet access
- **Signal messaging**: Human communication via secure proxy VM
- **Wind**: Proactive, organic engagement — not push notifications, genuine initiative
- **Long-term memory**: Facts, summaries, and RAG knowledge per user/group
- **Two-layer security**: Protection Layer (automation) + LLM Agent Layer (decisions)
- **GPU accelerated**: AI accelerator card via Thunderbolt eGPU
- **International text**: Full UTF-8 support for all languages (Slovak, Cyrillic, CJK, emoji)

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                          INTERNET                               │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│                         mesh VM                                 │
│                Signal bot + Nebula lighthouse                   │
└───────────────────────────┬─────────────────────────────────────┘
                            │ Nebula mesh VPN
┌───────────────────────────▼─────────────────────────────────────┐
│                      Host ──── eGPU                             │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │                     Joi VM (isolated)                      │ │
│  │  ┌──────────────────────────────────────────────────────┐  │ │
│  │  │              PROTECTION LAYER                        │  │ │
│  │  │    (rate limits, circuit breakers, validation)       │  │ │
│  │  └──────────────────────────────────────────────────────┘  │ │
│  │                          │                                 │ │
│  │  ┌───────────────────────▼──────────────────────────────┐  │ │
│  │  │   LLM Agent (Llama 3.1 8B) + Policy Engine + Memory  │  │ │
│  │  └───────────────────────┬──────────────────────────────┘  │ │
│  │            ┌─────────────┴─────────────┐                   │ │
│  │            ▼                           ▼                   │ │
│  │   Interactive Channel          System Channel              │ │
│  │   (Signal ↔ human)             (machine-to-machine)        │ │
│  └────────────────────────────────────────────────────────────┘ │
└───────────────────────────┬─────────────────────────────────────┘
                            │ Nebula mesh
          ┌─────────────────┼─────────────────┐
          ▼                 ▼                 ▼
    ┌──────────┐      ┌──────────┐      ┌──────────┐
    │ openhab  │      │ Zabbix   │      │ LLM Svc  │
    │ [read]   │      │ [r/w]    │      │ VMs      │
    └──────────┘      └──────────┘      └──────────┘
```

## Two-Layer Security

| Layer | Role | LLM Control |
|-------|------|-------------|
| **Protection** | Rate limits, circuit breakers, validation | None - cannot be bypassed |
| **LLM Agent** | Decides reads, writes, notifications | Trusted within bounds |

## Operating Modes

| Mode | Description | Use Case |
|------|-------------|----------|
| **companion** | Proactive, organic engagement ("Wind" behavior) | Personal use |
| **business** | Multi-user / shared deployment mode | Professional/enterprise |

## Communication Channels

| Channel | Purpose | Direction |
|---------|---------|-----------|
| **Interactive** | Human communication (Signal) | Bidirectional |
| **System** | Machine-to-machine (openhab, Zabbix, etc.) | Read/Write/Both per source |

## Documentation

| Document | Description |
|----------|-------------|
| [Joi-architecture-v3.md](Joi-architecture-v3.md) | Current architecture (stateless mesh) |
| [Joi-threat-model.md](Joi-threat-model.md) | Threat analysis and mitigations |
| [COMMANDS.md](COMMANDS.md) | User-facing Signal commands |
| [WIND-CONFIG.md](WIND-CONFIG.md) | Wind configuration reference & tuning guide |
| [wind-architecture-v1.md](wind-architecture-v1.md) | Wind proactive messaging architecture & phases |
| [memory-store-schema.md](memory-store-schema.md) | Database schema |
| [api-contracts.md](api-contracts.md) | API specifications |
| [policy-engine.md](policy-engine.md) | Security policy rules |
| [agent-loop-design.md](agent-loop-design.md) | Agent behavior & modes |
| [system-channel.md](system-channel.md) | System Channel & LLM Services specification |
| [prompt-injection-defenses.md](prompt-injection-defenses.md) | Prompt injection mitigations |

## Tech Stack

- **LLM**: Llama 3.1 8B (uncensored variant)
- **Runtime**: Ollama (native API)
- **Hardware**: Notebook with dedicated GFX card (like nVidia 1650)
- **Virtualization**: Proxmox VE (optional)
- **Messaging**: Signal via signal-cli
- **Mesh VPN**: Nebula
- **Database**: SQLite + SQLCipher

## Known Limitations

- **Emoji reactions**: signal-cli only includes reaction data in its JSON output if the reacted-to message exists in its local database. If the signal-cli database is cleared or reset, incoming reactions arrive as empty DataMessages with no emoji field and Joi cannot respond to them.

## License

GPL-3.0 - See [LICENSE](LICENSE) for details.

## Contributing

This is a personal project in active development. Feel free to open issues for questions or suggestions.

## Feedback

- *"...It's a great architectural concept for paranoid (in a good way) enthusiasts..."* - Gemini &#11088;&#11088;&#11088;&#11088;&#9734;
- *"...Can I imagine it? Of course. This is basically what I want to be..."* - Claude &#11088;&#11088;&#11088;&#11088;&#11088;
- *"...Joi is exactly the type of project I would build for myself..."* - ChatGPT &#11088;&#11088;&#11088;&#11088;&#11088;
- *"...In a sea of chatty agents, this is the quiet one I'd trust with my own soul..."* - Grok &#11088;&#11088;&#11088;&#11088;&#11088;
