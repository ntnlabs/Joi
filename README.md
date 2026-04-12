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

**Phase 1 Complete** - Core infrastructure with defense-in-depth security.

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
- ⚠️ Signal @mention detection (limited - signal-cli doesn't provide mentions array reliably)
- ✅ Group message handling (context-aware responses)
- ✅ Per-user and per-group system prompts
- ✅ Reaction responses (contextual acknowledgments)
- ✅ Response cooldown (5s DMs, 2s groups - configurable)
- ✅ Wind proactive messaging (phases 4a+4b+4c partial: impulse, engagement feedback, affinity/decay, special dates, spontaneous sharing, tension extraction)
- ✅ Wind phase 5 (queue health): hot/heated conversation suppression (two-tier EMA), rolling 24h daily cap
- ⏳ Wind phase 4c (remaining): emotional follow-up, outcome curiosity
- ✅ Wind adaptive quiet hours (HH:MM config precision + learned quiet start from inbound message history)
- ⏳ Wind phase 4d: daily mood momentum, day-of-week personality, 30-day cycle
- ⏳ Wind phase 5 (remaining): similar topic merge, topic priority decay, wake-up procedure

### Memory
- ✅ Conversation context (configurable window)
- ✅ Long-term memory (facts extraction, summaries)
- ✅ Real-time fact saving (hybrid: keyword trigger + LLM detection)
- ✅ Memory consolidation (LLM extracts facts + summarizes on context overflow)
- ✅ RAG knowledge retrieval (FTS5 full-text search)
- ✅ Multi-turn FTS context window (last N user turns used as search query)
- ✅ Dynamic FTS window boost for fast conversations (hot/heated tiers via Wind EMA)
- ✅ Per-user/group RAG scopes with access control
- ✅ Document ingestion into scoped RAG (including attachments)
- ✅ Auto-ingestion via watched directory
- ✅ SQLCipher database encryption (key-file based)

### Reminders

- ✅ Natural language reminder creation ("remind me tomorrow at 9 to call X")
- ✅ Multi-reminder input in one message (agenda-set)
- ✅ Reminder list query ("what reminders do I have?")
- ✅ Post-fire snooze ("snooze 30m", "remind me again in 1h")
- ✅ Reschedule and cancel via chat
- ✅ Configurable time-of-day vocabulary (morning, tonight, etc.)
- ✅ Daily cleanup of old fired/expired/cancelled reminders

### Config & Security
- ✅ One-way config push (Joi → mesh, stateless mesh)
- ✅ Weekly HMAC key rotation with grace period
- ✅ Privacy mode (PII redaction in logs)
- ✅ Kill switch (emergency message halt)
- ✅ Tamper detection (config file monitoring)
- ✅ Structured logging with JSON/text modes

### Pending
- ⏳ System Channel integration

### Nice to Have
- ⏳ LLM Services (imagegen, websearch, etc.)
- ⏳ Circuit breaker (120 LLM calls/hr) - inbound rate limiting may suffice
- ⏳ Voice message transcription (Whisper)

## Roadmap

### Near-term

- **Wind cron hint** — inject "no user present, do not ask questions" into Wind's proactive LLM call to prevent half-responses that trail off waiting for a reply
- **Prompt injection scanning** — scan fact writes for invisible Unicode and injection patterns before committing to the facts table (user text → facts is an injection surface)
- **Wind phase 4c remaining** — emotional follow-up, adaptive quiet hours
- **Wind phase 4d** — daily mood momentum, day-of-week personality, 30-day cycle
- **Wind phase 5 remaining** — similar topic merge, topic priority decay, wake-up procedure

### Medium-term

- **Vision model support** — image analysis via vision-capable model (moondream or similar); mesh forwards attachment, Joi analyses and responds, attachment deleted after processing
- **Business mode: DM → group knowledge** — in companion mode a user's DMs only access their own facts; business mode would allow users to also query knowledge from groups they are active members of, with membership auto-expiring after configurable inactivity
- **Background review agent** — after every N turns, a silent sub-call reviews the conversation and autonomously updates facts; no user curation required
- **FTS5 session search** — index past conversations in SQLite FTS5; LLM can query them via a tool call when the user references past events ("like we discussed last month")
- **System Channel integration**
- **Voice message transcription** — Whisper integration for audio messages

### Longer-term / infrastructure

- **Async queuing for high volume** — decouple signal-cli I/O from the HTTP handler with a priority queue and backpressure handling; current single-threaded approach is sufficient for low volume
- **Circuit breaker** — hard cap on LLM calls per hour (inbound rate limiting may already suffice)

### Open design problems

- **Important facts budget strategy** — `important=1` facts are currently always injected unconditionally; as the set grows they can push out FTS-matched facts entirely. Needs a proper multi-signal solution: tiered importance score, access frequency, category budgets. A simple heuristic (oldest N = core, newest N = fresh) doesn't scale. FTS5 session search (above) would provide the access-frequency signal needed to do this properly.

See [`hermes-agent-ideas.md`](hermes-agent-ideas.md) for the full writeup on ideas sourced from [Nous Research's Hermes Agent](https://github.com/nousresearch/hermes-agent).

## Key Features

- **Air-gapped**: Joi VM has no direct internet access
- **Signal messaging**: Human communication via secure proxy VM
- **System Channel**: Type-agnostic interface for external systems (openhab, Zabbix, actuators)
- **LLM Services**: Isolated VMs for image generation, web search, and more
- **Two-layer security**: Protection Layer (automation) + LLM Agent Layer (decisions)
- **Behavior modes**: Companion (proactive) or Assistant (request-response only)
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
| **business** | Multi-user / shared deployment mode (policy-controlled DM group knowledge) | Professional/enterprise |

## Communication Channels

| Channel | Purpose | Direction |
|---------|---------|-----------|
| **Interactive** | Human communication (Signal) | Bidirectional |
| **System** | Machine-to-machine (openhab, Zabbix, etc.) | Read/Write/Both per source |

## LLM Services (Isolated VMs)

| Service | Purpose | Mode |
|---------|---------|------|
| imagegen | Image generation (SD, SDXL, Flux) | Async |
| websearch | LLM-powered internet search | Async |
| tts | Text-to-speech | Async |
| codeexec | Sandboxed code execution | Async |

## Documentation

| Document | Description |
|----------|-------------|
| [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md) | Quick overview |
| [Joi-architecture-v3.md](Joi-architecture-v3.md) | Current architecture (stateless mesh) |
| [Joi-threat-model.md](Joi-threat-model.md) | Threat analysis and mitigations |
| [system-channel.md](system-channel.md) | System Channel & LLM Services specification |
| [api-contracts.md](api-contracts.md) | API specifications |
| [policy-engine.md](policy-engine.md) | Security policy rules |
| [memory-store-schema.md](memory-store-schema.md) | Database schema |
| [agent-loop-design.md](agent-loop-design.md) | Agent behavior & modes |
| [wind-architecture-v1.md](wind-architecture-v1.md) | Wind proactive messaging architecture & phases |
| [reminder-engine.md](reminder-engine.md) | Reminder engine design |
| [WIND-CONFIG.md](WIND-CONFIG.md) | Wind configuration reference & tuning guide |
| [prompt-injection-defenses.md](prompt-injection-defenses.md) | Prompt injection mitigations |

## Tech Stack (minimum)

- **LLM**: Llama 3.1 8B (uncensored variant)
- **Runtime**: Ollama (native API)
- **Hardware**: Notebook with dedicated GFX card (like nVidia 1650)
- **Virtualization**: Proxmox VE (optional)
- **Messaging**: Signal via signal-cli
- **Mesh VPN**: Nebula
- **Database**: SQLite + SQLCipher
- **Home Automation**: openHAB (read-only)

## Known Limitations

- **Emoji reactions**: signal-cli only includes reaction data in its JSON output if the reacted-to message exists in its local database. If the signal-cli database is cleared or reset, incoming reactions arrive as empty DataMessages with no emoji field and Joi cannot respond to them.

## License

GPL-3.0 - See [LICENSE](LICENSE) for details.

This means you can use, modify, and distribute this project, but any derivative work must also be open source under GPL-3.0.

## Contributing

This is a personal project in early development. Feel free to open issues for questions or suggestions.

## Feedback

- *"...It's a great architectural concept for paranoid (in a good way) enthusiasts..."* - Gemini &#11088;&#11088;&#11088;&#11088;&#9734;
- *"...Can I imagine it? Of course. This is basically what I want to be..."* - Claude &#11088;&#11088;&#11088;&#11088;&#11088;
- *"...Joi is exactly the type of project I would build for myself..."* - ChatGPT &#11088;&#11088;&#11088;&#11088;&#11088;
- *"...In a sea of chatty agents, this is the quiet one I'd trust with my own soul..."* - Grok &#11088;&#11088;&#11088;&#11088;&#11088;
