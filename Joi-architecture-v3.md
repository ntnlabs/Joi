# Joi Architecture v3

> **Status:** Current authoritative architecture document.
> **Supersedes:** Joi-architecture-v2.md
> **Last updated:** 2026-02-18

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
| **Mesh is stateless** | No config files on disk, memory only |
| **Defense-in-depth** | Nebula + HMAC + policy validation |
| **Fail-secure** | Empty policy denies all; rotation has grace period |
| **No traces** | Mesh restart = clean slate |

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
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│                         mesh VM                                 │
│              Signal bot + Nebula lighthouse                     │
│                    (STATELESS)                                  │
└───────────────────────────┬─────────────────────────────────────┘
                            │ Nebula mesh VPN (encrypted)
┌───────────────────────────▼─────────────────────────────────────┐
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
| mesh | 10.42.0.1 | Signal proxy, config receiver | Stateless |
| joi | 10.42.0.10 | LLM agent, config authority | Stateful |

---

## Stateless Mesh Architecture

**Mesh stores nothing on disk.** All configuration comes from Joi via config push.

### On Mesh Startup
1. Mesh starts with empty policy (denies all messages)
2. Uses `MESH_HMAC_SECRET` from environment for initial auth
3. Waits for Joi to push config via `/config/sync`
4. Applies config in memory

### On Mesh Restart
1. All config is lost (by design - no traces)
2. Messages are dropped until config received
3. **Automatic recovery:** Joi polls mesh `/config/status` every tick (~60s)
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
| `/etc/default/mesh-signal-worker` | Env vars (HMAC seed, Signal account) | Yes |
| `/var/lib/signal-cli/` | Signal account data | Yes |
| (memory only) | Policy, rotated HMAC keys | No |

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
  "hmac_rotation": {
    "new_secret": "<64-char-hex>",
    "effective_at_ms": 1708300060000,
    "grace_period_ms": 60000
  }
}
```

### Config Endpoints

| Endpoint | Direction | Auth | Purpose |
|----------|-----------|------|---------|
| `POST mesh:8444/config/sync` | joi → mesh | HMAC | Push config |
| `GET mesh:8444/config/status` | joi → mesh | None | Get config hash |

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
| Joi: `/etc/default/joi-api` | `JOI_HMAC_SECRET` env var |
| Mesh: `/etc/default/mesh-signal-worker` | `MESH_HMAC_SECRET` env var (seed) |
| Mesh: memory | Rotated keys (lost on restart) |

---

## HMAC Key Rotation

Weekly automatic rotation with 60-second grace period.

### Rotation Flow

```
1. Joi generates new 32-byte secret
2. Joi pushes config with hmac_rotation field
3. Mesh stores new key, keeps old key for grace period
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

### On Mesh Restart During Rotation

Mesh uses env var seed. Joi will push current key on next sync.

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

Note: Outbound rate limiting (messages/hour) is not implemented. Only per-conversation cooldown prevents rapid-fire responses.

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

### HMAC Rotation
- [ ] Rotation triggered (manual or weekly)
- [ ] Both keys work during grace period
- [ ] Old key rejected after grace period
- [ ] Mesh restart uses env seed

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
