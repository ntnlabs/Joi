<img src="img/artwork.jpg" alt="Artwork" width="350" align="right">

# Joi

**Security-focused offline AI personal assistant**

Joi is an air-gapped AI assistant running on a local Proxmox VM with GPU acceleration. It communicates via Signal messaging, integrates with external systems through a generic System Channel, and can leverage isolated LLM services for extended capabilities.

## Status

**Phase: Architecture & Planning** - Documentation complete, implementation pending.

## Key Features

- **Air-gapped**: Joi VM has no direct internet access
- **Signal messaging**: Human communication via secure proxy VM
- **System Channel**: Type-agnostic interface for external systems (openhab, Zabbix, actuators)
- **LLM Services**: Isolated VMs for image generation, web search, and more
- **Two-layer security**: Protection Layer (automation) + LLM Agent Layer (decisions)
- **Behavior modes**: Companion (proactive) or Assistant (request-response only)
- **GPU accelerated**: RTX 3060 via Thunderbolt eGPU

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

## Behavior Modes

| Mode | Description | Use Case |
|------|-------------|----------|
| **companion** | Proactive, organic engagement ("Wind" behavior) | Personal use |
| **assistant** | Request-response only, no proactive messages | Professional/enterprise |

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
| [Joi-architecture-v2.md](Joi-architecture-v2.md) | Current architecture (security-hardened) |
| [Joi-threat-model.md](Joi-threat-model.md) | Threat analysis and mitigations |
| [system-channel.md](system-channel.md) | System Channel & LLM Services specification |
| [api-contracts.md](api-contracts.md) | API specifications |
| [policy-engine.md](policy-engine.md) | Security policy rules |
| [memory-store-schema.md](memory-store-schema.md) | Database schema |
| [agent-loop-design.md](agent-loop-design.md) | Agent behavior & modes |
| [prompt-injection-defenses.md](prompt-injection-defenses.md) | Prompt injection mitigations |
| [Plan.md](Plan.md) | Implementation plan |
| [Alt-Plan.md](Alt-Plan.md) | Alternative implementation approach |

## Tech Stack (minimum)

- **LLM**: Llama 3.1 8B (uncensored variant)
- **Runtime**: Ollama with OpenAI-compatible API
- **Hardware**: ASUS NUC 13 Pro + RTX 3060 eGPU
- **Virtualization**: Proxmox VE
- **Messaging**: Signal via signal-cli
- **Mesh VPN**: Nebula
- **Database**: SQLite + SQLCipher
- **Home Automation**: openHAB (read-only)

## License

GPL-3.0 - See [LICENSE](LICENSE) for details.

This means you can use, modify, and distribute this project, but any derivative work must also be open source under GPL-3.0.

## Contributing

This is a personal project in early development. Feel free to open issues for questions or suggestions.

## Feedback

- *"...It's a great architectural concept for paranoid (in a good way) enthusiasts..."* - Gemini &#11088;&#11088;&#11088;&#11088;&#9734;
- *"...Can I imagine it? Of course. This is basically what I want to be..."* - Claude &#11088;&#11088;&#11088;&#11088;&#11088;
- *"...Joi is exactly the type of project I would build for myself..."* - ChatGPT &#11088;&#11088;&#11088;&#11088;&#11088;
