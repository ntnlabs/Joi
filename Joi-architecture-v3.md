# Joi Architecture v3

> **Status:** Current authoritative architecture document.
> **Supersedes:** Joi-architecture-v2.md
> **Last updated:** 2026-04-19

## Goals

- Offline LLM on Proxmox VM with GPU passthrough (Llama 3.1 8B + NVIDIA RTX 3060)
- Free-running agent that reacts to context and can message the user
- No direct WAN from Joi VM; Signal messaging only via stateless proxy
- Security-first with defense-in-depth at all boundaries
- Joi is the single source of truth for all configuration

> **Implementation:** See local `dev-notes.md` for development notes. Code lives in `execution/joi/` and `execution/mesh/`.

## Key Design Principles

| Principle | Implementation |
|-----------|----------------|
| **Joi is authoritative** | All config lives on Joi, pushed to mesh |
| **Mesh is stateless** | No config files on disk; HMAC key is memory-only, always pushed by Joi |
| **Defense-in-depth** | Nebula + HMAC + policy validation |
| **Fail-secure** | Empty policy denies all; rotation has grace period; bootstrap protected by UFW |
| **No traces** | Mesh restart = clean slate; key gone when VM stops |

---

## Architectural Invariants

> **This document is the authoritative source of truth for Joi's architecture.**
> All other documents (`system-channel.md`, `agent-loop-design.md`, `policy-engine.md`, etc.) describe subsystems in more detail but are subordinate to the boundaries defined here. If any other document contradicts an invariant below, this document wins.

These invariants are **non-negotiable**. They define what Joi is. Changing them requires an explicit, conscious decision — not a gradual feature addition.

### 1. Network Perimeter

- Joi VM has **no direct WAN access** — no outbound internet, no inbound internet connections.
- All traffic to and from Joi travels over the **Nebula mesh** (encrypted, certificate-authenticated).
- The Nebula enclave is the trust boundary. "External systems" means external to the Joi VM — all System Channel targets (openhab, Zabbix, actuators, LLM service VMs) are enclave-internal Nebula nodes. Nothing in this project is internet-facing from Joi's side.

### 2. LLM Trust and Autonomy

- The LLM operates within the **Protection Layer** — rate limits, circuit breakers, and validation rules it cannot bypass.
- Within those bounds, the LLM is **trusted to act autonomously**. This is intentional. Joi is a digital entity with its own initiative — it does not require owner confirmation for every action.
- The existing precedent is **Wind**: proactive Signal messages are sent without approval on every tick. System Channel operations within the enclave extend the same autonomy model to machine systems; they do not introduce a new trust category.
- Autonomy is bounded by the enclave. Nothing Joi initiates reaches the internet.

### 3. LLM Model Policy

- Only **trusted, vetted models** may be used. Chinese-origin models (Qwen, DeepSeek, etc.) are permanently banned — supply chain security.
- Primary models must be **uncensored** and **Slovak-capable**.

### 4. What This Project Will Build

The System Channel connector design is intentionally generic (transport-agnostic, type-agnostic). However, **this project** will only implement services that are explicitly listed below. The connector is open; the roadmap is not.

| Service | Status |
|---------|--------|
| openhab (read) | Planned |
| Zabbix (read/write) | Planned |
| websearch (DDG + fetch) | Planned |
| imagegen | Planned |
| TTS | Planned |

**`codeexec` will never be implemented.** Arbitrary code execution by the LLM on any VM is permanently out of scope for this project, regardless of sandboxing.

### 5. Human Communication Channel

- Signal is the **primary human communication channel** for the foreseeable future.
- Other messaging transports (Telegram, WhatsApp, etc.) may be added via the mesh's transport abstraction without architectural change.
- No web UI, no public API, no direct HTTP interface to end users.

### 6. Data Residency

- All user data — conversation context, facts, summaries, RAG — stays on the **Joi VM only**.
- No data leaves the enclave except as part of a Signal/transport message in response to a user interaction.

---

## Hardware Platform

- **Host:** ASUS NUC 13 Pro NUC13ANHI7 (i7-1360P, Thunderbolt 4)
- **Virtualization:** Proxmox VE
- **GPU:** NVIDIA RTX 3060 12GB in eGPU enclosure (TB4 connection)
- **Joi VM:** Dedicated VM with GPU passthrough for LLM inference
- **Mesh VM:** Lightweight proxy VM with WAN access

---

## Network Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                          INTERNET                               │
└──────────────┬───────────────────────────┬──────────────────────┘
               │                           │
┌──────────────▼──────────┐   ┌────────────▼────────────┐
│        mesh VM          │   │       search VM         │
│  Signal/comms proxy     │   │  DDG + page fetch       │
│  Nebula lighthouse      │   │  trafilatura extraction  │
│  (STATELESS)            │   │  (STATELESS)            │
└──────────────┬──────────┘   └────────────┬────────────┘
               │ Nebula VPN                │ Nebula VPN
               └──────────────┬────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────────┐
│                      Proxmox Host                               │
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
└─────────────────────────────────────────────────────────────────┘
```

### VM Roles

| VM | IP (Nebula) | Role | State |
|----|-------------|------|-------|
| mesh | 10.42.0.1 | Comms proxy (Signal, future Telegram/WhatsApp) | Stateless |
| search | 10.42.0.2 | External search (DDG, page fetch, extraction) — two NICs: WAN + Nebula | Stateless |
| joi | 10.42.0.10 | LLM agent, config authority | Stateful |

---

## Stateless Mesh Architecture

**Mesh stores nothing on disk.** All configuration — including the HMAC key — comes from Joi via config push and lives in memory only. When mesh restarts, the key is gone. Joi re-bootstraps within ~60 s.

### On Mesh Startup
1. Mesh starts with empty policy (denies all messages) and no HMAC key (waiting state)
2. Waits for Joi to push config via `/config/sync` (bootstrap push)
3. Bootstrap `/config/sync` is allowed through unauthenticated — UFW restricts port 8444 to Joi's Nebula IP only, so this is safe
4. Mesh stores policy and HMAC key in memory; sends back a challenge response to confirm key receipt
5. From this point all pushes are HMAC-authenticated

### On Mesh Restart
1. Policy/config and HMAC key are lost (by design - no traces)
2. Mesh returns to waiting state (no key)
3. **Automatic recovery:** Joi polls mesh `/config/status` every tick (~60 s)
   - `hmac_configured: false` → Joi sends bootstrap push → recovered
   - Mesh returns empty hash → Joi detects restart → pushes config
   - Mesh returns wrong hash → Joi detects drift → pushes fresh config
   - Mesh unreachable → Joi retries next tick

### Why Stateless?
- **No traces:** Restart leaves no evidence of previous config
- **Single source of truth:** Joi owns all configuration
- **Simpler recovery:** No rollback logic, just push again
- **Security:** Compromised mesh can't persist malicious config

### Mesh Files (minimal)

| Path | Purpose | Persistent |
|------|---------|------------|
| `/etc/default/mesh-signal-worker` | Env vars (Signal account, Joi endpoint) | Yes |
| `/var/lib/signal-cli/` | Signal account data | Yes |
| (memory only) | Policy + HMAC key | No |

---

## Config Push: Joi → Mesh

Joi pushes config to mesh on:
- Startup
- Policy file change (detected via tamper check)
- Every 10 minutes (periodic sync)
- Manual trigger via admin endpoint

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
    "inbound": {
      "max_per_hour": 120,
      "max_per_minute": 20
    }
  },
  "validation": {
    "max_text_length": 1500
  },
  "security": {
    "privacy_mode": true,
    "kill_switch": false
  },
  "bootstrap_hmac_key": "<64-char-hex>",  // Always included; mesh stores only if no key yet
  "bootstrap_challenge": "<32-char-hex>", // Nonce; mesh returns HMAC(key, nonce) as confirmation
  "hmac_rotation": {                      // Optional: key rotation
    "new_secret": "<64-char-hex>",
    "effective_at_ms": 1708300060000,
    "grace_period_ms": 60000
  }
}
```

### Config Endpoints

| Endpoint | Direction | Auth | Purpose |
|----------|-----------|------|---------|
| `POST mesh:8444/config/sync` | joi → mesh | HMAC (or none on bootstrap) | Push config + bootstrap key |
| `GET mesh:8444/config/status` | joi → mesh | None | Get config hash + hmac_configured |

---

## HMAC Authentication

All requests between Joi and mesh are authenticated with HMAC-SHA256.

### Headers

```
X-Nonce: <uuid4>
X-Timestamp: <unix-epoch-ms>
X-HMAC-SHA256: HMAC-SHA256(nonce + timestamp + body, secret)
```

### Validation
1. Verify timestamp within 5 minutes
2. Verify nonce not seen before (15-minute retention)
3. Verify HMAC signature matches

### Key Storage

| Location | Purpose |
|----------|---------|
| Joi: `/etc/default/joi-api` | `JOI_HMAC_SECRET` env var (persistent, Joi is the source of truth) |
| Mesh: RAM only | Key pushed by Joi on every bootstrap; never written to disk |
| Mesh: `MESH_HMAC_SECRET` env var | Emergency fallback only (existing deployments during transition) |

### Bootstrap Protocol

On every config push, Joi includes `bootstrap_hmac_key` (current key) and `bootstrap_challenge` (random nonce) in the payload. Mesh:
- Stores the key if it has none yet (`_hmac_configured = False`)
- Computes `HMAC(key, challenge)` and returns it as `challenge_response`

Joi verifies the response to confirm mesh received and applied the correct key. This happens on every push — bootstrap and normal authenticated pushes alike.

---

## HMAC Key Rotation

Weekly automatic rotation with 60-second grace period.

### Rotation Flow

```
1. Joi generates new 32-byte secret
2. Joi pushes config with hmac_rotation field (HMAC-authenticated with current key)
3. Mesh stores new key in memory, keeps old key for grace period
4. Both keys valid during grace period
5. Joi updates /etc/default/joi-api
6. Old key expires after 60 seconds
```

### Grace Period Behavior

| Time | Old Key | New Key |
|------|---------|---------|
| Before rotation | Valid | N/A |
| During grace (60s) | Valid | Valid |
| After grace | Rejected | Valid |

### On Mesh Restart During or After Rotation

Mesh returns to waiting state (no key). Joi detects `hmac_configured: false` on the next `/config/status` poll and sends a bootstrap push with the current key. Recovered within ~60 s.

### Key Staleness Watchdog

If Joi goes silent for 2 consecutive watchdog cycles (~120 s), mesh clears the HMAC key and returns to waiting state. A single missed cycle is tolerated (network blip). When Joi returns, a bootstrap push restores the key automatically.

The number of missed cycles required to clear is configurable: `MESH_CONFIG_STALENESS_CHECKS` (default: 2).

---

## Security Controls

### Privacy Mode

When enabled (default: true):
- Phone numbers redacted in logs: `+1234567890` → `+***7890`
- Group IDs redacted: `abc123...` → `[GRP:abc1...]`

Toggle: `POST joi:8443/admin/security/privacy-mode`

### Kill Switch

When enabled:
- Mesh drops all messages (doesn't forward to Joi)
- Emergency brake for incident response
- Messages silently dropped (sender not notified)

Toggle: `POST joi:8443/admin/security/kill-switch`

### Tamper Detection

Every 60 seconds, Joi checks SHA256 fingerprints of:
- `/var/lib/joi/policy/mesh-policy.json`
- `/etc/default/joi-api`
- System prompt files

On mismatch: **service exits immediately** (`os._exit(78)` - EX_CONFIG).
Systemd restarts the service, which re-initializes fingerprints from current state.

---

## Admin Endpoints (Joi)

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/admin/config/push` | POST | HMAC | Force push config to mesh |
| `/admin/config/status` | GET | IP | Show sync status |
| `/admin/hmac/rotate` | POST | HMAC | Manual HMAC rotation |
| `/admin/security/kill-switch` | POST | HMAC | Toggle kill switch |
| `/admin/security/privacy-mode` | POST | HMAC | Toggle privacy mode |
| `/health` | GET | None | Health check |

### Auth Levels

- **HMAC:** Requires valid HMAC headers (sensitive operations)
- **IP:** Requires local or Nebula IP (read-only operations)
- **None:** Public (health checks only)

---

## Message Flow

### Inbound (Signal → Joi)

```
1. Signal message received by mesh
2. Mesh checks policy (in memory):
   - Sender in allowed_senders (DM)? → forward normally
   - Sender in group participants? → forward normally
   - Sender in configured group but not participant? → forward as store_only
   - Unknown sender (not in any allowlist)? → drop silently
3. Mesh checks rate limits (in memory)
4. Mesh forwards to Joi with HMAC auth
5. Joi processes:
   - Normal messages: may trigger response
   - store_only messages: stored for context, no response
```

### Outbound (Joi → Signal)

```
1. Joi sends to mesh with HMAC auth
2. Mesh verifies HMAC
3. Mesh sends via signal-cli
4. Mesh tracks delivery status
```

### Rate Limits

| Scope | Default | Location |
|-------|---------|----------|
| Inbound per user | 120/hr, 20/min | Mesh (memory) |
| Outbound cooldown | 5s DM, 2s group | Joi (per-conversation) |

Outbound rate limiting is implemented via `OutboundRateLimiter` (sliding window, default 120 msg/hour, configurable via `JOI_OUTBOUND_MAX_PER_HOUR`). Critical messages can bypass the limit via the `is_critical` flag.

---

## Trust Boundaries

```
┌─────────────────────────────────────────────────────────────────┐
│                      UNTRUSTED                                  │
│                    (Internet, Signal)                           │
└───────────────────────────┬─────────────────────────────────────┘
                            │ Boundary 1: WAN → Mesh
                            │ (Signal protocol, TLS)
┌───────────────────────────▼─────────────────────────────────────┐
│                    SEMI-TRUSTED                                 │
│                 (Mesh VM - Stateless)                           │
│                                                                 │
│  Enforces: Rate limits, sender validation, HMAC auth            │
│  Cannot: Persist config, access Joi directly, bypass Nebula     │
└───────────────────────────┬─────────────────────────────────────┘
                            │ Boundary 2: Mesh → Joi
                            │ (Nebula + HMAC)
┌───────────────────────────▼─────────────────────────────────────┐
│                      TRUSTED                                    │
│                    (Joi VM)                                     │
│                                                                 │
│  LLM Agent, Memory, Policy Authority                            │
│  Full disk encryption (LUKS)                                    │
└─────────────────────────────────────────────────────────────────┘
```

---

## LLM Configuration

### Model Selection

| Priority | Model | Notes |
|----------|-------|-------|
| Primary | Llama 3.1 8B abliterated | Uncensored, good Slovak |
| Backup | Mistral 7B | Good multilingual |
| Banned | Qwen, DeepSeek | Security policy |

### Requirements

1. **Uncensored** - No content filters that block safety alerts
2. **Slovak support** - Comprehension and generation
3. **Instruction-following** - Respects system prompts

### Ollama Modelfile

Custom models with baked-in personalities:
- System prompt
- Temperature
- Context window
- Per-user/group model assignment

---

## Memory Architecture

### Storage

- **Database:** SQLite + SQLCipher (encrypted)
- **Location:** `/var/lib/joi/memory.db`
- **Key:** `/etc/joi/memory.key`

### Memory Types

| Type | Retention | Purpose |
|------|-----------|---------|
| Context | Configurable (default 40 msgs) | Recent conversation |
| Facts | Permanent | Extracted knowledge |
| Summaries | Permanent | Conversation summaries |
| RAG | Permanent | Ingested documents |

### Consolidation

Triggered by:
- 1 hour silence
- 200+ messages since last consolidation

Actions:
- Extract facts from conversation
- Generate summary
- Optionally archive old context

---

## File Locations

### Joi VM

| Path | Purpose |
|------|---------|
| `/etc/default/joi-api` | Environment variables |
| `/var/lib/joi/memory.db` | SQLCipher database |
| `/var/lib/joi/policy/mesh-policy.json` | Policy (pushed to mesh) |
| `/var/lib/joi/prompts/` | System prompts |
| `/var/lib/joi/rag/` | RAG document ingestion |

### Mesh VM

| Path | Purpose |
|------|---------|
| `/etc/default/mesh-signal-worker` | Environment variables |
| `/var/lib/signal-cli/` | Signal account data |

---

## Systemd Services

### Joi

```ini
# /etc/systemd/system/joi-api.service
[Service]
User=joi
WorkingDirectory=/opt/Joi/execution/joi
ExecStart=/usr/bin/python3 -m api.server
EnvironmentFile=/etc/default/joi-api
```

### Mesh

```ini
# /etc/systemd/system/mesh-signal-worker.service
[Service]
User=signal
WorkingDirectory=/opt/Joi/execution/mesh/proxy
ExecStart=/opt/Joi/execution/mesh/proxy/run-worker.sh
EnvironmentFile=/etc/default/mesh-signal-worker
ProtectSystem=strict
ReadWritePaths=/var/lib/signal-cli
```

---

## Emergency Stop

| Method | Speed | Effect |
|--------|-------|--------|
| Kill switch (API) | Instant | Mesh drops messages |
| Proxmox app | Seconds | Shutdown mesh/joi VM |
| SSH `qm stop` | Seconds | Shutdown VM |
| Physical power | Seconds | Hard stop (LUKS locks) |

---

## Verification Checklist

### Config Push
- [ ] Mesh starts empty (denies all)
- [ ] Joi pushes config on startup
- [ ] Mesh receives and applies config
- [ ] Messages flow correctly

### HMAC Bootstrap & Rotation
- [ ] Mesh starts with no key (waiting state — `hmac_configured: false` in /config/status)
- [ ] Joi sends bootstrap push → mesh accepts unauthenticated /config/sync
- [ ] Challenge response verified in Joi logs
- [ ] Rotation triggered (manual or weekly) — both keys work during grace period
- [ ] Old key rejected after grace period
- [ ] Mesh restart → key gone → Joi re-bootstraps within ~60 s
- [ ] No `/var/lib/signal-cli/hmac.secret` file exists on mesh

### Security Controls
- [ ] Privacy mode redacts phone numbers
- [ ] Kill switch drops messages
- [ ] Tamper detection logs changes

---

## References

| Document | Purpose |
|----------|---------|
| `api-contracts.md` | API specifications |
| `policy-engine.md` | Security policy rules |
| `memory-store-schema.md` | Database schema |
| `agent-loop-design.md` | Agent behavior |
| `system-channel.md` | System Channel spec |
| `SENSITIVE-CONFIG.md` | Secrets reference |
| `ENV-REFERENCE.md` | Environment variables |
