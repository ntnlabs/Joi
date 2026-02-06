# Joi

**Security-focused offline AI personal assistant**

Joi is an air-gapped AI assistant running on a local Proxmox VM with GPU acceleration. It integrates with home automation (read-only) and communicates via Signal messaging through a secure proxy.

## Status

**Phase: Architecture & Planning** - Documentation complete, implementation pending.

## Key Features

- **Air-gapped**: Joi VM has no direct internet access
- **Signal messaging**: Secure communication via proxy VM
- **Home automation**: Read-only integration with openHAB
- **GPU accelerated**: RTX 3060 via Thunderbolt eGPU
- **Policy engine**: Enforces security constraints on all actions
- **Defense in depth**: Multiple security layers (Nebula mesh, rate limits, input sanitization)

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        INTERNET                             │
└─────────────────────┬───────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────────┐
│              mesh VM (Ubuntu 24 LTS)                        │
│              Signal bot + Nebula lighthouse                 │
└─────────────────────┬───────────────────────────────────────┘
                      │ Nebula mesh VPN
┌─────────────────────▼───────────────────────────────────────┐
│          Proxmox Host ──── eGPU (RTX 3060)                  │
│  ┌────────────────────────────────────────────────────────┐ │
│  │                 Joi VM (isolated)                      │ │
│  │   LLM (Llama 3.1 8B) + Policy Engine + Memory Store    │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────┬───────────────────────────────────────┘
                      │ Nebula mesh (read-only)
┌─────────────────────▼───────────────────────────────────────┐
│                    openHAB                                  │
└─────────────────────────────────────────────────────────────┘
```

## Security Model

| Principle | Implementation |
|-----------|----------------|
| No WAN access | Joi VM on isolated network, no default route |
| Authenticated transport | Nebula mesh VPN with certificate auth |
| Read-only home automation | Policy engine blocks all writes to openHAB |
| Rate limiting | Per-user/per-conversation limits |
| Input sanitization | Unicode normalization, length limits, pattern blocking |
| Prompt injection defense | Layered: input framing, output validation, policy engine |

## Documentation

| Document | Description |
|----------|-------------|
| [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md) | Quick overview |
| [Joi-architecture-v2.md](Joi-architecture-v2.md) | Current architecture (security-hardened) |
| [Joi-threat-model.md](Joi-threat-model.md) | Threat analysis and mitigations |
| [api-contracts.md](api-contracts.md) | API specifications |
| [policy-engine.md](policy-engine.md) | Security policy rules |
| [memory-store-schema.md](memory-store-schema.md) | Database schema |
| [agent-loop-design.md](agent-loop-design.md) | Agent behavior design |
| [prompt-injection-defenses.md](prompt-injection-defenses.md) | Prompt injection mitigations |
| [Plan.md](Plan.md) | Implementation plan |
| [Alt-Plan.md](Alt-Plan.md) | Alternative implementation approach |

## Tech Stack

- **LLM**: Llama 3.1 8B (uncensored variant)
- **Runtime**: Ollama with OpenAI-compatible API
- **Hardware**: ASUS NUC 13 Pro + RTX 3060 eGPU
- **Virtualization**: Proxmox VE
- **Messaging**: Signal via signal-cli
- **Mesh VPN**: Nebula
- **Database**: SQLite + SQLCipher
- **Home Automation**: openHAB (read-only)

## License

MIT

## Contributing

This is a personal project in early development. Feel free to open issues for questions or suggestions.
