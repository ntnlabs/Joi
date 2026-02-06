# Joi Architecture (High-Level)

> **NOTE:** This is the original architecture document. See `Joi-architecture-v2.md` for the current security-hardened version with Nebula mesh and Proxmox VM configuration.

## Goals (SUPERSEDED - see v2)
- ~~Offline LLM on RPi 5 (Qwen 1.5B + Hailo 10H).~~ → Now: Proxmox VM + RTX 3060 + Llama 3.1 8B
- "Free-running" agent that reacts to context and can message the user.
- No direct WAN from Joi; Signal messaging only via proxy.
- openhab is read-only to Joi (ingest all events, no control).

## Components

### 1) Joi Core (RPi 5, offline)
- LLM runtime on Hailo 10H.
- Always-on agent loop (context-driven behavior).
- Local memory store (short-term + long-term).
- Policy engine enforcing read-only rules and outbound restrictions.

### 2) openhab Event Ingest (Read-only)
- Push all events to Joi (event bus / webhook / MQTT).
- Event normalizer to stable schema (presence, car status, sensors, weather).
- Rate/batch rules for noisy signals.

### 3) Signal Communications (Proxy-only)
- Signal bot runs on proxy machine.
- RPi sends HTTPS webhook to proxy for outbound messages.
- Proxy validates HMAC, allowlists recipient, logs all sends.
- Two-way: Signal -> proxy -> Joi; Joi -> proxy -> Signal.

### 4) Optional Local Terminal
- Local text UI for debugging or direct chat.

## Trust Boundaries
- RPi has no direct WAN access.
- Proxy is the only egress for Signal.
- openhab is read-only from Joi's perspective.

## openhab Event Strategy
- Push for fast/critical changes (presence, car arrival, storms, sunrise/sunset reached).
- Batch for high-frequency sensors (e.g., temp delta) every N minutes.
- Pull on schedule for daily forecast summary (e.g., morning/evening).

## Proxy Hardening (Summary)
- HTTPS webhook endpoint with HMAC + timestamp.
- IP allowlist (RPi only), rate limits, and audit logs.
- Recipient allowlist (owner phone only).
- Signal bot via signal-cli (daemon mode). Note: signald is deprecated and no longer functional.

## Security Notes (from Threat Model)
- **Proxy → Joi auth is undefined**: must implement signed messages (keypair) plus TLS and replay protection.
- **openhab authentication is undefined**: require mTLS or PSK and strict schema validation before LLM.
- **Prompt injection defenses**: never pass raw event data to the LLM; enforce strict templating and output validation.
- **Signal credential protection**: store keys encrypted (e.g., TPM/HSM or filesystem encryption).
- **Rate limiting and circuit breakers**: cap message/send rates and agent actions to prevent runaway loops.

## Open Questions / Next Decisions
- Choose exact proxy transport (mTLS vs HMAC + firewall).
- Decide push mechanism from openhab (Event Bus, MQTT, webhook).
- Define item names for weather + sunrise/sunset once available.
