# Joi - Project Summary

> Quick reference for understanding this project. Last updated: 2026-02-08

## What is Joi?

Joi is a **security-focused, offline AI personal assistant** running as a Proxmox VM with GPU acceleration. It communicates via Signal messaging, integrates with external systems through a generic System Channel, and can leverage isolated LLM services for extended capabilities.

## Project Status

**Phase: Architecture/Planning** - No implementation code yet, only documentation.

## Key Concepts

| Concept | Description |
|---------|-------------|
| **Two-Layer Security** | Protection Layer (automation, LLM cannot bypass) + LLM Agent Layer (trusted decisions) |
| **Interactive Channel** | Human communication via Signal (bidirectional) |
| **System Channel** | Machine-to-machine communication (type-agnostic, read/write/both per source) |
| **LLM Services** | Isolated VMs for image generation, web search, TTS, code execution |
| **Behavior Modes** | `companion` (proactive, organic) or `assistant` (request-response only) |

## Technology Stack

| Component | Technology | Notes |
|-----------|------------|-------|
| LLM | Llama 3.1 8B (uncensored) | Primary - must be unlocked variant |
| LLM Backup | Gemma 2 9B, Phi-3, Mistral | Fallback options |
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
