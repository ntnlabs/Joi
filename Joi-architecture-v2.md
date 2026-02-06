# Joi Architecture v2 (Security-Hardened)

## Goals
- Offline LLM on Proxmox VM with GPU passthrough (Llama 3.1 8B + NVIDIA RTX 3060).
- Free-running agent that reacts to context and can message the user.
- No direct WAN from Joi VM; Signal messaging only via proxy.
- openhab is read-only to Joi (ingest all events, no control).
- Security-first transport and validation across all boundaries.

## Hardware Platform
- **Host:** ASUS NUC 13 Pro NUC13ANHI7 (i7-1360P, Thunderbolt 4)
- **Virtualization:** Proxmox VE
- **GPU:** NVIDIA RTX 3060 12GB in eGPU enclosure (TB4 connection)
- **Joi VM:** Dedicated VM with GPU passthrough for LLM inference

## LLM Policy
- **Primary:** Llama 3.1 8B (Meta, US) - uncensored/unlocked variant preferred
- **Backup:** Gemma 2 9B (Google), Phi-3 (Microsoft)
- **BANNED:** Chinese models (Qwen, DeepSeek, etc.) - security/trust policy

### LLM Requirements
1. **Uncensored/Unlocked** - No overly restrictive content filters
   - Critical alerts must never be blocked (e.g., "smoke" ≠ drugs)
   - Home security context requires freedom to discuss alarms, fires, emergencies
   - Look for community fine-tunes: `llama-3.1-8b-uncensored` or similar
2. **Slovak language support** - Good comprehension and generation in Slovak
   - May need Slovak fine-tuned variant or multilingual model
   - Test Slovak capabilities before deployment
3. **Instruction-following** - Must respect system prompt and safety boundaries we define (not vendor's)

### Slovak Language Acceptance Criteria

> **Note:** First run is English-only. Slovak support is Phase 2 (post-PoC).

Before enabling Slovak mode, the model must pass these tests:

| Test | Criteria | Pass/Fail |
|------|----------|-----------|
| Basic comprehension | Correctly answer 10 simple Slovak questions | 8/10 correct |
| Generation fluency | Generate 5 paragraphs, native speaker rates as "natural" | 4/5 acceptable |
| Mixed language | Handle Slovak message, respond in Slovak (not English) | 5/5 correct |
| Diacritics | Correctly use ľ, š, č, ť, ž, ý, á, í, é, ô, ä, ú, ň, ď | No systematic errors |
| Home context vocab | Understand: teplota, vlhkosť, dym, alarm, dvere, svetlo | 6/6 correct |

**Voice (TTS/STT) Slovak requirements:**
- STT: Whisper medium or larger (has Slovak training data)
- TTS: Verify Piper Slovak voice exists (`sk_SK-*`) before committing to voice feature
- If no suitable Slovak TTS voice: voice responses in English only, or defer voice feature

**Rollout plan:**
1. First run: English only (text and responses)
2. Post-PoC: Evaluate Slovak model candidates
3. If Slovak acceptable: Enable Slovak as primary
4. If Slovak inadequate: Continue English, revisit with better models

> **Phase 1 language handling:** If owner sends voice message in Slovak, Whisper will transcribe it (Whisper supports Slovak). Joi will understand but respond in English. This is acceptable for PoC - full Slovak support comes in Phase 2.

### Model Selection Notes
```
# Candidates to evaluate:
# 1. Llama 3.1 8B Instruct (baseline)
# 2. Llama 3.1 8B uncensored fine-tunes (e.g., from HuggingFace)
# 3. Mistral 7B variants (good multilingual)
# 4. Check Slovak benchmarks before final selection
```

## Components

### 1) Joi Core (Proxmox VM, offline)
- **LLM Runtime:** Ollama with OpenAI-compatible API (GPU accelerated via RTX 3060 passthrough).
- Always-on agent loop (context-driven behavior).
- Local memory store (short-term + long-term).
- Policy engine enforcing read-only rules and outbound restrictions.
- Circuit breaker for agent actions and outbound messaging.
- **Emergency Stop:** Shutdown mesh VM via Proxmox mobile app (cuts communication).

**GPU Failure Handling:**

| Failure | Detection | Response |
|---------|-----------|----------|
| eGPU disconnect (TB4 cable) | Ollama returns GPU error | Fall back to CPU inference (slower) |
| Enclosure power loss | Same as above | Fall back to CPU inference |
| GPU driver crash | Ollama timeout or error | Restart Ollama, fall back to CPU if persists |
| VRAM exhaustion | OOM error from Ollama | Reduce context size, retry |

**CPU fallback behavior:**
- Llama 3.1 8B can run on CPU (i7-1360P) but ~10x slower
- Response time: ~5s (GPU) → ~30-60s (CPU)
- Acceptable for degraded operation, not for normal use
- Alert owner: "Running in degraded mode (CPU). Check eGPU connection."

**Monitoring:**
```yaml
gpu_health:
  check_interval_seconds: 60
  alert_on_fallback: true
  metrics:
    - gpu_available: boolean
    - inference_time_ms: gauge
    - vram_used_mb: gauge
```

### 2) openhab Event Ingest (Read-only, Authenticated)
- **openhab:** `openhab.homelab.example`
- Push all events to joi VM via HTTPS webhook.
- **Authentication:** Nebula mesh transport (openhab joins Nebula network).
  - openhab runs Nebula daemon (lightweight, ~10MB RAM)
  - All openhab → joi traffic flows over Nebula tunnel
  - Certificate-based mutual authentication via Nebula
  - No separate mTLS needed - Nebula handles encryption and auth
- Event normalizer to stable schema (presence, car status, sensors, weather).
- Strict schema validation and content-length limits before LLM.
- Rate/batch rules for noisy signals.
- Expect multiple joi endpoints (by event type), not a single shared endpoint.
- **IoT device flood protection:** Upstream IoT devices (Zigbee, WiFi sensors) have weak security and can be compromised. Joi implements state-based deduplication, confirmation loops (max 3 alerts per triggered state), and flapping detection to defend against pwned sensors flooding critical channel. See `policy-engine.md` → "IoT Event Handling".

### 3) Signal Communications (Proxy via Nebula Mesh)
- **Proxy VM:** `mesh.homelab.example` (Ubuntu 24 LTS, 2GB RAM, 16GB disk)
- Signal bot runs on mesh VM; may be rebuilt as Rocky Linux post-PoC.
- Mesh VM and Joi VM communicate via Nebula mesh VPN.
- Nebula provides: mutual certificate auth, encrypted transport, no external dependencies.
- Joi sends messages to mesh over Nebula tunnel (no direct LAN exposure).
- Mesh validates Nebula identity, allowlists recipient, logs all sends.
- Two-way: Signal -> mesh -> Nebula -> Joi; Joi -> Nebula -> mesh -> Signal.
- All external communication is via the Signal bot only.
- Replay protection via nonce/timestamp in payload.

#### Dual Channel Setup
- **Direct channel**: Normal DM with owner for conversation, proactive chat
- **Critical channel**: Signal group (owner only) for urgent alerts
- Different notification sounds allow owner to prioritize by ear
- Channel selection: rules-based for known critical events, Joi can escalate if she judges urgency

### 4) Optional Local Terminal
- Local text UI for debugging or direct chat.
- Key-based auth only; password login disabled.
- Emergency stop via Proxmox console or VM shutdown.
- Backup control path: SSH into Joi VM or Proxmox host for shutdown/maintenance.

> **Note on Proxmox Console Access:** Physical/VM console access (via Proxmox web UI) bypasses all Joi authentication. This is acceptable for a home environment where Proxmox access = full trust. The Proxmox host itself should be secured (strong password, 2FA if possible, limited network exposure).

## Trust Boundaries
- joi VM has no direct WAN access (isolated Proxmox network).
- mesh VM is the only egress for Signal.
- mesh has no public IP and no inbound port forwarding.
- openhab is read-only from joi's perspective.
- mesh ↔ joi traffic flows over Nebula mesh (certificate-authenticated, encrypted).
- openhab → joi traffic flows over Nebula mesh (openhab is a Nebula node).

## openhab Event Strategy
- Push for fast/critical changes (presence, car arrival, storms, sunrise/sunset reached).
- Batch for high-frequency sensors (e.g., temp delta) every N minutes.
- Pull on schedule for daily forecast summary (e.g., morning/evening).

## Mesh VM Hardening (mesh.homelab.example)
- **OS:** Ubuntu 24 LTS (PoC phase); may rebuild as Rocky Linux for production.
- **Resources:** 2GB RAM, 16GB disk (minimal footprint).
- Nebula mesh for mesh ↔ joi transport.
- Nebula lighthouse runs on mesh VM.
- Message payloads include HMAC + timestamp + nonce (defense-in-depth over Nebula).
- Rate limits and audit logs for all sends.
- Recipient allowlist (owner phone only).
- Signal bot via **signal-cli** (signald is deprecated and no longer functional).
- Signal credentials stored encrypted (filesystem encryption or secrets manager).

### Signal Bot: signal-cli Configuration

> **signald is DEAD** - The project is no longer maintained and does not work with current Signal servers. signal-cli is the only viable option.

**Mandatory Configuration:**

1. Create dedicated `signal` user: `useradd -r -s /sbin/nologin signal`
2. Credentials stored in `/var/lib/signal-cli/data/` with 0700 permissions
3. Run signal-cli in **daemon mode** (JSON-RPC socket) - NOT per-command invocation:
   ```bash
   # systemd service runs as 'signal' user
   sudo -u signal signal-cli -a +1555XXXXXXXXX daemon --socket /var/run/signal-cli/socket
   ```
4. mesh proxy service connects via Unix socket (no shell command invocation)
5. Never pass credentials or message content via command line arguments

**Security Hardening:**
- JVM heap dumps disabled (`-XX:+DisableExplicitGC -XX:-HeapDumpOnOutOfMemoryError`)
- signal-cli service isolated via systemd (no network access except socket)
- Socket permissions: 0660, owned by `signal:signal`, proxy has supplementary group
- Credentials path: `/var/lib/signal-cli/data/<phone-number>/`

**Operational Requirement:**
- **Signal-cli must be updated at least every 3 months** - Signal servers enforce client version expiry
- Failure to update will result in complete loss of Signal communication
- Add to operational runbook: quarterly update schedule

**Linked Device Note:**
- signal-cli registers as a "linked device" to your primary Signal account
- Appears in Signal app → Settings → Linked Devices
- On compromise: unlink device from primary phone immediately
- Audit logs are anonymized and stored locally only.
- Nebula certificates stored securely; rotate annually or on compromise.

## LLM Safety and Validation
- Never pass raw openhab events to LLM; use structured templates.
- Output validation and allowlists for outbound messages.
- Rate limiting and circuit breaker for agent actions.
- Sliding context window with summarization and hard limits.
- Assumption: only the owner can interact with Joi; no third-party inputs are expected.

## Memory Store Security
- Encrypt at rest (SQLCipher or LUKS).
- Integrity checks (checksums, append-only log).
- Retention policies and automatic pruning.

## Channel-Based Knowledge Isolation

Different channels have different knowledge access. Enforced by **Linux file permissions**, not application logic.

### Channel Types

| Channel | Purpose | Knowledge Access |
|---------|---------|------------------|
| **Private DM** | Sensitive, never shared | Everything including private/ |
| **Regular DM** | Normal assistant | Public + family + shared |
| **Family Group** | Shared with family | Family + shared only |
| **Critical Group** | Safety - full context | All public/ folders (for safety decisions) |

### Channel Model

**Simple rule:** DM = Private, Group = Shared

| Signal Conversation | Channel Type | Linux User |
|---------------------|--------------|------------|
| DM with Joi | Private | joi-{recipient}-private |
| Group: Owner + Joi | Owner public | joi-owner-public |
| Group: Family | Family | joi-family |
| Group: Critical | Critical | joi-critical |

No mode switching. The conversation type IS the mode.

### Directory Structure

```
/var/lib/joi/
├── knowledge/
│   ├── owner/
│   │   └── private/            # 700 joi-owner-private (DM only)
│   ├── partner/
│   │   └── private/            # 700 joi-partner-private (DM only)
│   ├── group/
│   │   ├── owner_public/       # 750 joi-owner-public:joi-owner-readers
│   │   ├── partner_public/     # 750 joi-partner-public:joi-partner-readers
│   │   └── family/             # 750 joi-family:joi-family-readers
│   ├── critical/               # Top level - special status
│   │   └── safety/             # 750 joi-critical:joi-critical
│   └── shared/                 # 755 world-readable
│       └── common/
└── data/
    ├── owner/
    │   ├── private.db          # 600 joi-owner-private
    │   └── public.db           # 640 joi-owner-public:joi-owner-readers
    ├── partner/
    │   ├── private.db          # 600 joi-partner-private
    │   └── public.db           # 640 joi-partner-public:joi-partner-readers
    ├── group/
    │   └── family.db           # 640 joi-family:joi-family-readers
    ├── critical.db             # 600 joi-critical
    └── shared.db               # 644 world-readable
```

**Why critical/ at top level?** Critical is special - it reads from all public folders for safety decisions. Top-level placement makes this explicit.

### Linux Users (One Per Channel)

```bash
# Channel-specific users
useradd -r -s /sbin/nologin joi-owner-private
useradd -r -s /sbin/nologin joi-owner-public
useradd -r -s /sbin/nologin joi-partner-private
useradd -r -s /sbin/nologin joi-partner-public
useradd -r -s /sbin/nologin joi-family
useradd -r -s /sbin/nologin joi-critical

# Reader groups (for folder sharing)
groupadd joi-owner-readers      # Who can read owner's public knowledge
groupadd joi-partner-readers    # Who can read partner's public knowledge
groupadd joi-family-readers     # Who can read family knowledge
```

### Whole-Folder Sharing (Set Up at Registration)

Sharing is configured once when a recipient is registered. Entire folders are shared, not individual files.

```bash
# Example: Set up owner's public folder
chown -R joi-owner-public:joi-owner-readers /var/lib/joi/knowledge/group/owner_public/
chmod -R 750 /var/lib/joi/knowledge/group/owner_public/

# Critical always gets access to all public folders
usermod -aG joi-owner-readers joi-critical
usermod -aG joi-partner-readers joi-critical
usermod -aG joi-family-readers joi-critical

# Optional: Family can read owner's public knowledge
usermod -aG joi-owner-readers joi-family
```

### Sharing Configuration

```yaml
# /etc/joi/recipients.yaml
# Configured once at registration, no runtime changes

recipients:
  owner:
    private_user: joi-owner-private
    public_user: joi-owner-public
    public_readers:
      - joi-critical      # Always (safety needs full context)
      - joi-family        # Owner shares with family

  partner:
    private_user: joi-partner-private
    public_user: joi-partner-public
    public_readers:
      - joi-critical      # Always
      # Partner not sharing with family

groups:
  family:
    user: joi-family
    readers:
      - joi-critical      # Always

  critical:
    user: joi-critical
    # Critical reads from everywhere, has its own folder for safety procedures
```

### Whitelist-Only Access Control

```yaml
# /etc/joi/channel_users.yaml
# If channel not in this list → DENIED (default deny)
allowed_channel_users:
  owner_private_dm: joi-owner-private
  owner_regular_dm: joi-owner-public
  partner_private_dm: joi-partner-private
  partner_regular_dm: joi-partner-public
  family_group: joi-family
  critical_group: joi-critical
```

**No blacklist.** If it's not in the whitelist, it's denied. No gray zones.

### Process Execution Model

```
Message arrives (sender=owner, channel=private_dm)
    ↓
Lookup: allowed_channel_users["owner_private_dm"]
    ↓
Not found? → DENY (security error, logged)
    ↓
Found: joi-owner-private
    ↓
Spawn knowledge retrieval subprocess as that user
    ↓
Linux kernel enforces file access
    ↓
Only permitted files/databases readable
    ↓
Results returned to main Joi process
```

### Sudoers Configuration

```sudoers
# /etc/sudoers.d/joi
# Main joi process can switch to channel users for knowledge retrieval
joi ALL=(joi-owner-private) NOPASSWD: /usr/local/bin/joi-retrieve
joi ALL=(joi-owner-public) NOPASSWD: /usr/local/bin/joi-retrieve
joi ALL=(joi-partner-private) NOPASSWD: /usr/local/bin/joi-retrieve
joi ALL=(joi-partner-public) NOPASSWD: /usr/local/bin/joi-retrieve
joi ALL=(joi-family) NOPASSWD: /usr/local/bin/joi-retrieve
joi ALL=(joi-critical) NOPASSWD: /usr/local/bin/joi-retrieve
```

### Why Linux Permissions?

| Benefit | Explanation |
|---------|-------------|
| Defense in depth | Even if app logic has bug, kernel blocks access |
| No SQL auth needed | File permissions = database auth |
| Auditable | `ls -la` shows exactly who can access what |
| Battle-tested | Linux permission model is decades old |
| Simple | No tokens, no passwords, just users and groups |

### Access Matrix

| Resource | owner-private | owner-public | partner-private | family | critical |
|----------|---------------|--------------|-----------------|--------|----------|
| owner/private/ | ✅ | ❌ | ❌ | ❌ | ❌ |
| group/owner_public/ | ❌ | ✅ | ❌ | ✅* | ✅ |
| partner/private/ | ❌ | ❌ | ✅ | ❌ | ❌ |
| group/partner_public/ | ❌ | ❌ | ❌ | ❌ | ✅ |
| group/family/ | ❌ | ❌ | ❌ | ✅ | ✅ |
| critical/ | ❌ | ❌ | ❌ | ❌ | ✅ |
| shared/ | ✅ | ✅ | ✅ | ✅ | ✅ |

*If owner configured sharing with family at registration

### Example: Attempted Unauthorized Access

```python
# Process running as joi-owner-public tries to read partner's private data
open("/var/lib/joi/knowledge/partner/private/secrets.md")
# → PermissionError: [Errno 13] Permission denied

# Even if application has a bug, Linux blocks it
```

## Secrets Management

### SQLCipher Key (joi VM)

The SQLCipher database key is managed as follows:

**PoC Approach (CURRENT):**

> **⚠️ WARNING: PoC-only - NOT production-ready**
>
> The PoC approach stores the key as plaintext on disk. This is acceptable for initial development but has known weaknesses:
> - Key is in memory as plaintext environment variable
> - Any process running as `joi` user can read it
> - Key may persist in shell history if ever typed manually
> - Cold boot attacks on LUKS could expose it
> - SQLCipher encryption becomes "security theater" if key protection is weak
>
> **Do not use this approach for production or with real personal data.**

1. Key stored in `/etc/joi/secrets/db.key` (file permissions: 0600, owner: joi)
2. joi VM disk is LUKS-encrypted (defense in depth, but same boot unlocks both)
3. Key loaded at service startup via environment variable

```bash
# Example: joi service reads key at startup
# /etc/systemd/system/joi.service
[Service]
EnvironmentFile=/etc/joi/secrets/env
ExecStart=/opt/joi/bin/joi-agent
User=joi
```

```bash
# /etc/joi/secrets/env (0600, owner: joi)
SQLCIPHER_KEY=<random-32-byte-hex>
```

**Production Hardening (REQUIRED before real use):**

| Option | Pros | Cons |
|--------|------|------|
| **systemd-creds** | Built-in, encrypted at rest with TPM | Requires TPM passthrough to VM |
| **Manual unlock on boot** | Key never on disk | Downtime on reboot, manual intervention |
| **HashiCorp Vault** | Industry standard, audit logging | Additional infrastructure complexity |
| **LUKS keyfile on separate USB** | Physical security, removable | Requires USB passthrough, physical access |

Recommended for production: **Manual unlock on boot** or **systemd-creds with TPM**

**Key Rotation:**
- SQLCipher supports re-keying: `PRAGMA rekey = 'newkey';`
- Rotate annually or on suspected compromise
- Backup database before re-keying

### Signal Credentials (mesh VM)

Signal credentials require special protection - compromise = impersonation.

**Storage:**
- signal-cli stores credentials in `/var/lib/signal-cli/data/<phone-number>/`
- Contains: identity keys, session keys, account info
- Protected by: file permissions (0700) + mesh VM disk encryption (LUKS)

**Backup:**
- Signal credentials MUST be backed up securely
- Loss = need to re-register number (lose message history)
- Encrypted backup to offline storage (USB, encrypted archive)

**Access Control:**
- Only `signal` user/process can read credential directory
- No SSH access except for maintenance (key-based only)
- mesh VM has no other services running

```bash
# mesh VM: signal-cli credential directory
drwx------ signal signal /var/lib/signal-cli/
drwx------ signal signal /var/lib/signal-cli/data/
```

### Nebula CA Key

The Nebula CA private key (`ca.key`) is the root of trust.

**Handling:**
1. Generate CA on air-gapped machine
2. Sign all node certs
3. Move `ca.key` to OFFLINE encrypted storage (USB drive, safe)
4. Only bring online for cert rotation (annual) or new node provisioning
5. Nodes only need `ca.crt` (public) + their own cert/key

**Compromise Response:**
- If `ca.key` is compromised: generate new CA, re-sign all nodes
- If node key is compromised: revoke cert (add to blocklist), generate new

## Logging Strategy
- Joi VM logs are stored locally only; no off-site log strategy.
- Mesh VM audit logs are anonymized and stored locally only.

## Network and Device Hardening

### Isolated VM Network (vmbr1)
- Dedicated Proxmox virtual bridge (`vmbr1`) for AI traffic only.
- **Subnet: /29 (or /30)** - Minimal address space, no room for rogue devices.
- **Connected VMs:** joi, mesh, openhab (second NIC)
- **NOT connected:** Main LAN, other VMs, IoT devices
- All inter-VM traffic stays on this isolated network.
- mesh VM has two NICs: vmbr0 (WAN for Signal + proxy) + vmbr1 (AI network)
- openhab VM has two NICs: vmbr0 (main LAN) + vmbr1 (AI network)

**IP Allocation (example: 10.99.0.0/29)**
| IP | Host | Purpose |
|----|------|---------|
| 10.99.0.0 | - | Network |
| 10.99.0.1 | mesh | Nebula lighthouse + HTTP proxy |
| 10.99.0.2 | joi | AI VM |
| 10.99.0.3 | openhab | Home automation (2nd NIC) |
| 10.99.0.4 | (reserved) | Future use |
| 10.99.0.5 | (reserved) | Future use |
| 10.99.0.6 | - | (unusable in /29) |
| 10.99.0.7 | - | Broadcast |

```
┌─────────────────────────────────────────────────────────────┐
│                    Proxmox Host                             │
│                                                             │
│  vmbr0 (Main LAN)              vmbr1 (AI Network /29)      │
│       │                              │                      │
│       ├── mesh (eth0) ◄──────► mesh (eth1) 10.99.0.1       │
│       │        │                     │                      │
│       │   [Signal +                [Nebula +               │
│       │    HTTP Proxy]              Proxy listener]        │
│       │                              │                      │
│       ├── openhab (eth0) ◄───► openhab (eth1) 10.99.0.3   │
│       │        │                     │                      │
│       │   [Main LAN                [mTLS to                │
│       │    access]                  joi]                   │
│       │                              │                      │
│       │                         joi (eth0) 10.99.0.2       │
│       │                              │                      │
│       │                        [AI Network                 │
│       │                         ONLY - no vmbr0]           │
└───────┴──────────────────────────────┴──────────────────────┘
```

**/29 rationale:** Only 6 usable IPs. An attacker would need to unplug an existing VM to get an IP. No DHCP on this network - all static.

### Additional Hardening
- Nebula mesh overlay for mesh ↔ joi (even on isolated network - defense in depth).
- TLS on all connections (openhab → joi via mTLS).
- Static IPs on vmbr1; firewall allowlists.
- Disable unused services and ports.
- Proxmox host hardening (LUKS encryption, secure boot, limited SSH access).
- joi VM: encrypted disk image, GPU passthrough isolated via IOMMU.
- joi VM firewall: allow Nebula UDP + openhab mTLS only (no vmbr0 interface).
- mesh VM firewall: vmbr0 allows Signal outbound only; vmbr1 allows Nebula only.

### Time Synchronization (NTP)

All VMs on vmbr1 must maintain synchronized clocks for API timestamp validation (5-minute tolerance).

**NTP Source:** Firewall/gateway on vmbr1 network
- Gateway syncs to external NTP (pool.ntp.org) via WAN
- All vmbr1 VMs sync to gateway as their NTP server
- This keeps joi air-gapped (no direct internet NTP access)

**Configuration (all VMs on vmbr1):**
```bash
# /etc/chrony/chrony.conf (or /etc/ntp.conf)
server 10.99.0.254 iburst prefer  # Gateway IP (adjust as needed)
# No pool.ntp.org - isolated network
```

**VMs requiring sync:**
| VM | NTP Client | Notes |
|----|------------|-------|
| joi | Yes | Critical - validates all inbound timestamps |
| mesh | Yes | Sends timestamps to joi |
| openhab | Yes | Sends event timestamps to joi |

**Why this matters:**
- API contracts require `X-Timestamp` within 5 minutes of server time
- Replay protection depends on accurate clock comparison
- Clock drift on air-gapped joi VM would break timestamp validation

**Monitoring:**
- Alert if clock offset exceeds 30 seconds
- Log NTP sync failures

## Open Questions / Next Decisions
- ✓ RESOLVED: mesh ↔ joi auth uses Nebula certificates (annual rotation recommended).
- ✓ RESOLVED: Nebula lighthouse runs on mesh.homelab.example.
- Finalize Nebula IP ranges (currently planned: 10.42.0.0/24).

## Nebula Mesh Configuration
- **Nodes:** mesh.homelab.example (lighthouse + node), Joi VM (node)
- **Lighthouse:** mesh VM acts as lighthouse (always-on, known IP)
- **IP Range:** 10.42.0.0/24 (Nebula internal network)
  - mesh: 10.42.0.1
  - joi: 10.42.0.10
- **Port:** UDP 4242 (Nebula default, configurable)
- **Certificates:** Generated via `nebula-cert` CA, stored encrypted on each node
- **Firewall Groups:** Define `mesh` and `joi` groups; restrict traffic between them

### Nebula Failure Handling

The entire system depends on Nebula mesh. If Nebula fails, joi is isolated.

**Health Monitoring:**
```yaml
# joi monitors Nebula health
nebula_health:
  check_interval_seconds: 30
  lighthouse_ping_timeout_ms: 5000
  alert_after_failures: 3          # Alert after 3 consecutive failures
  metrics:
    - nebula_handshake_success
    - nebula_lighthouse_reachable
    - nebula_tunnel_established
```

**Failure Scenarios:**

| Failure | Impact | Detection | Recovery |
|---------|--------|-----------|----------|
| Lighthouse down (mesh VM) | joi cannot reach mesh | Lighthouse ping fails | Restart mesh VM, check Nebula service |
| joi Nebula daemon crash | All communication lost | Health check fails | systemd auto-restart, alert if repeated |
| Certificate expired | Handshake fails | Auth errors in logs | Rotate certs (requires CA key) |
| Network partition (vmbr1) | Tunnel cannot establish | Ping timeout | Check Proxmox virtual bridge |

**Graceful Degradation:**
- joi continues running locally even if isolated (no panic shutdown)
- Agent loop pauses outbound messages (queues locally)
- Local terminal remains accessible via Proxmox console
- On reconnection: flush queued messages (with staleness check)

**Manual Recovery:**
```bash
# On joi VM (via Proxmox console)
systemctl status nebula
systemctl restart nebula
journalctl -u nebula -f

# On mesh VM
systemctl status nebula
ping 10.42.0.10  # ping joi's Nebula IP
```

**Future Consideration:** SMS gateway as fallback for critical alerts if Nebula is down for extended period (requires additional hardware/service).

## Certificate Infrastructure (Nebula CA)

All three VMs are full Nebula mesh nodes:

| Component | Certificate | Nebula IP | Purpose |
|-----------|-------------|-----------|---------|
| mesh | `mesh.homelab.example.crt` | 10.42.0.1 | Lighthouse + Signal gateway |
| joi | `joi.homelab.example.crt` | 10.42.0.10 | AI core |
| openhab | `openhab.homelab.example.crt` | 10.42.0.20 | Home automation events |

**Certificate Generation:**
```bash
# Generate CA (once, keep ca.key OFFLINE after initial setup)
nebula-cert ca -name "homelab.example"

# Generate node certs
nebula-cert sign -name "mesh" -ip "10.42.0.1/24" -groups "gateway"
nebula-cert sign -name "joi" -ip "10.42.0.10/24" -groups "ai"
nebula-cert sign -name "openhab" -ip "10.42.0.20/24" -groups "openhab"
```

**Port Allocation (single source of truth):**

| Port | Direction | From | To | Purpose |
|------|-----------|------|-----|---------|
| 8443 | mesh → joi | mesh | joi | Signal inbound + health check (`GET /health`) |
| 8444 | joi → mesh | joi | mesh | Signal outbound + health check (`GET /health`) |
| 8445 | openhab → joi | openhab | joi | Event webhooks + health check (`GET /health`) |
| 3128 | joi → mesh | joi | mesh | HTTP proxy (future) |
| 4242/udp | all | all | all | Nebula mesh tunnel |

> **Health checks:** Each service exposes `GET /health` on its API port. No separate health check ports needed.

**Nebula Firewall Rules (in each node's config.yaml):**
```yaml
# === joi VM firewall ===
# joi allows inbound from mesh and openhab only
firewall:
  inbound:
    - port: 8443
      proto: tcp
      groups:
        - gateway
    - port: 8445
      proto: tcp
      groups:
        - openhab
  outbound:
    - port: 8444
      proto: tcp
      groups:
        - gateway
    - port: 3128
      proto: tcp
      groups:
        - gateway
```

```yaml
# === mesh VM firewall ===
# mesh allows inbound from joi only
firewall:
  inbound:
    - port: 8444
      proto: tcp
      groups:
        - ai
    - port: 3128
      proto: tcp
      groups:
        - ai
  outbound:
    - port: 8443
      proto: tcp
      groups:
        - ai
```

## Communication Matrix

All AI traffic flows on isolated VM network (vmbr1 /29), except mesh ↔ Internet (vmbr0).

| Source | Destination | Network | Protocol | Port | Purpose | Auth |
|--------|-------------|---------|----------|------|---------|------|
| mesh | joi | vmbr1 | Nebula (UDP) | 4242 | Mesh tunnel | Nebula cert |
| joi | mesh | vmbr1 | Nebula (UDP) | 4242 | Mesh tunnel | Nebula cert |
| openhab | joi | vmbr1 | Nebula (UDP) | 4242 | Mesh tunnel | Nebula cert |
| mesh | joi | Nebula overlay | HTTPS | 8443 | Inbound Signal messages | Nebula cert |
| joi | mesh | Nebula overlay | HTTPS | 8444 | Outbound Signal messages | Nebula cert |
| openhab | joi | Nebula overlay | HTTPS | 8445 | Event webhooks | Nebula cert |
| Signal Servers | mesh | vmbr0 | Signal Protocol | 443 | Signal messaging | Signal creds |
| joi | mesh | vmbr1 | HTTP Proxy | 3128 | Web access (future) | ACL (IP allowlist) |
| mesh | Internet | vmbr0 | HTTPS | 443 | Proxied requests (future) | - |

## External Tools Framework

Joi's capabilities can be extended via **External Tools** - services that joi can call but that run outside the core AI VM. This is designed for extensibility.

### Design Principles

1. **joi never runs tools directly** - All tools are external services
2. **Gateway pattern** - All tool requests go through mesh (or dedicated gateway)
3. **Policy Engine controls all** - Rate limits, allowlists, content filtering per tool
4. **Separate compute** - Heavy tools (image gen) run on separate hardware
5. **Standardized API** - All tools follow same request/response pattern

### Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                           joi VM                                    │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    Tool Router                               │   │
│  │  • Selects tool based on request                            │   │
│  │  • Enforces Policy Engine rules                             │   │
│  │  • Formats requests, sanitizes responses                    │   │
│  └──────────────────────────┬──────────────────────────────────┘   │
│                             │                                       │
└─────────────────────────────┼───────────────────────────────────────┘
                              │ vmbr1 (/29)
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
        ▼                     ▼                     ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────────────┐
│   mesh VM     │   │  (future)     │   │  (future)             │
│               │   │  image-gen VM │   │  other-tool VM        │
│ • Web Search  │   │               │   │                       │
│   (Squid)     │   │ • Stable Diff │   │ • TTS/STT             │
│               │   │ • Own GPU     │   │ • Code execution      │
│ vmbr0: WAN    │   │               │   │ • etc.                │
└───────────────┘   └───────────────┘   └───────────────────────┘
```

### Standardized Tool API

All external tools (except simple HTTP proxy) follow this pattern:

**Request (joi → tool):**
```json
{
  "tool": "web_search",
  "request_id": "uuid",
  "timestamp": 1706918400000,
  "params": {
    "query": "weather New York",
    "max_results": 5
  }
}
```

**Response (tool → joi):**
```json
{
  "tool": "web_search",
  "request_id": "uuid",
  "status": "ok",
  "timestamp": 1706918400100,
  "result": {
    "type": "search_results",
    "data": [ ... ]
  },
  "usage": {
    "tokens": 0,
    "compute_ms": 150
  }
}
```

### Tool Registry

```yaml
# tools.yaml - External tool configuration

tools:
  web_search:
    enabled: true
    gateway: mesh
    transport: http_proxy    # Uses Squid, not tool API
    endpoint: "10.99.0.1:3128"
    policy:
      max_per_hour: 20
      max_result_size: 2048

  image_generation:
    enabled: false           # Future
    gateway: image-gen       # Separate VM
    transport: tool_api      # Standardized API
    endpoint: "10.99.0.4:8080"
    policy:
      max_per_hour: 10
      max_resolution: "1024x1024"
      blocked_prompts:
        - "nsfw"
        - "violence"

  text_to_speech:
    enabled: false           # Future
    gateway: tts-vm
    transport: tool_api
    endpoint: "10.99.0.5:8080"
    policy:
      max_per_hour: 30
      max_text_length: 1000

  code_execution:
    enabled: false           # Future - sandboxed
    gateway: sandbox-vm
    transport: tool_api
    endpoint: "10.99.0.6:8080"
    policy:
      max_per_hour: 20
      timeout_seconds: 30
      allowed_languages:
        - python
        - javascript
```

### Policy Engine Integration

```python
def request_tool(tool_name: str, params: dict) -> ToolResult:
    """Request an external tool through Policy Engine."""

    # 1. Check if tool is enabled
    tool_config = get_tool_config(tool_name)
    if not tool_config.get('enabled', False):
        return ToolResult.error(f"Tool {tool_name} is disabled")

    # 2. Policy Engine check
    policy_result = policy_engine.check_tool_request(tool_name, params)
    if not policy_result.allowed:
        return ToolResult.error(policy_result.reason)

    # 3. Route to appropriate gateway
    gateway = tool_config['gateway']
    if tool_config['transport'] == 'http_proxy':
        response = execute_via_proxy(gateway, params)
    else:
        response = execute_via_tool_api(gateway, params)

    # 4. Sanitize response
    sanitized = sanitize_tool_response(tool_name, response)

    # 5. Log usage
    log_tool_usage(tool_name, params, sanitized)

    return sanitized
```

### Adding a New Tool (Future)

1. Create VM (if needed) or add to existing gateway
2. Add to `tools.yaml` with policy config
3. Add tool-specific sanitization rules
4. Add to Policy Engine rules
5. Update prompt templates to include tool in context

### Current Tools

| Tool | Status | Gateway | Hardware |
|------|--------|---------|----------|
| Web Search | Planned | mesh (Squid) | Shared (mesh VM) |
| STT (Speech-to-Text) | Planned | mesh or dedicated | CPU (Whisper) |
| TTS (Text-to-Speech) | Future | mesh or dedicated | CPU (Piper/Coqui) |
| Image Gen | Future | image-gen VM | Separate GPU required |
| Code Exec | Future | sandbox-vm | Isolated sandbox |

### Voice Messages (Signal)

Signal supports voice messages in both directions. This enables:

**Inbound (you → Joi):** Voice message received
```
Signal voice msg → mesh → save .ogg file → STT (Whisper) → text → joi
```

**Outbound (Joi → you):** Joi responds with voice
```
joi text → TTS (Piper) → .ogg file → mesh → Signal voice msg
```

**STT Options (Speech-to-Text):**
- **Whisper** (OpenAI, open source) - Excellent accuracy, runs on CPU
- `whisper.cpp` - Optimized C++ port, ~1GB RAM for small model
- Good Slovak support in `whisper-medium` or larger

**TTS Options (Text-to-Speech):**
- **Piper** - Fast, lightweight, offline TTS
- **Coqui TTS** - More natural but heavier
- Slovak voices available (check Piper voice repository)

**Architecture Decision:**
- STT/TTS can run on mesh VM (CPU-based, lightweight)
- Or dedicated `voice-vm` if mesh is resource-constrained
- Voice files stored temporarily, deleted after processing

#### Voice Processing Implementation Details

**Location:** mesh VM (2GB RAM is sufficient for CPU-based processing)

**STT (Whisper) Configuration:**
```yaml
stt:
  engine: whisper.cpp
  model: whisper-medium  # ~1.5GB, good Slovak support
  # Alternative: whisper-small (~500MB) if RAM constrained
  language: auto  # Auto-detect (Slovak/English)
  fallback_language: sk
  max_duration_seconds: 120  # Reject voice msgs > 2 min
  temp_dir: /tmp/joi-voice/
```

**TTS (Piper) Configuration:**
```yaml
tts:
  engine: piper
  voice: sk_SK-lili-medium  # Slovak voice (check availability)
  fallback_voice: en_US-lessac-medium
  sample_rate: 22050
  output_format: ogg  # Signal compatible
  max_text_length: 1000  # Prevent abuse
```

**Processing Flow (Inbound):**
1. signal-cli receives voice message, saves to `/tmp/joi-voice/<msg_id>.ogg`
2. mesh calls whisper.cpp: `whisper -m medium -f <file> -l auto`
3. On success: transcription included in API call to joi
4. On failure: forward structured failure message to joi (see below)
5. Delete temp file after processing (max retention: 5 minutes)

**Failure Handling:**
| Failure | Action |
|---------|--------|
| Whisper fails to load | Log error, send structured failure to joi (NO file path) |
| Transcription timeout (>30s) | Abort, send structured failure to joi |
| Unrecognized language | Use best-effort transcription, add confidence score |
| File too large (>10MB) | Reject at signal-cli level, send error to user |

> **Security Note:** On STT failure, mesh sends a structured message to joi - NOT the audio file path. Joi cannot access mesh filesystem, so exposing paths would be useless and leaks internal details. The failure message should be:
> ```json
> {
>   "content": {
>     "type": "voice",
>     "voice_transcription": null,
>     "voice_transcription_failed": true,
>     "voice_duration_ms": 15000,
>     "failure_reason": "transcription_timeout"  // or "whisper_error", "unrecognized"
>   }
> }
> ```
> Joi can then respond: "I received your voice message but couldn't transcribe it. Could you send that as text?"

**Resource Limits (mesh VM):**
- Whisper process: max 1GB RAM, timeout 60s
- Piper process: max 512MB RAM, timeout 30s
- Max concurrent voice processing: 1 (queue additional requests)
- **Max queue depth: 3** (drop older messages if exceeded)

> **DoS Prevention:** Without queue depth limit, attacker could send 120 voice messages/hour × 60s timeout = 2 hours of processing backlog. With max queue of 3, backlog is capped at ~3 minutes. Dropped messages logged as potential abuse.

---

## Web Search (First External Tool)

First implementation of the External Tools Framework. Uses HTTP Proxy approach (simpler than full Tool API).

**Architecture:**
- joi NEVER directly accesses internet (maintains air-gap for LLM)
- mesh VM runs an HTTP proxy (e.g., Squid) listening on vmbr1
- joi connects to proxy for web requests; proxy fetches from internet via vmbr0
- Proxy handles filtering, logging, and result sanitization

```
joi ──[HTTP Proxy]──► mesh (Squid on vmbr1)
                          │
                          │ vmbr0
                          ▼
                    Internet (Search API)
                          │
                          ▼
                    Sanitize results
                    (strip scripts, limit size)
                          │
joi ◄──[HTTP Proxy]────── mesh
```

**Proxy Configuration (mesh VM):**
- Squid or similar proxy on mesh VM
- Listens on vmbr1 (10.99.0.1:3128) - AI network only
- ACL: Only joi IP (10.99.0.2) can connect
- Allowlist: Only approved domains (duckduckgo.com, api.weather.com, etc.)
- All other domains blocked

**joi HTTP client:**
```python
# joi uses mesh as HTTP proxy for all web requests
proxies = {
    "http": "http://10.99.0.1:3128",
    "https": "http://10.99.0.1:3128"
}
response = requests.get("https://api.duckduckgo.com/...", proxies=proxies)
```

**Security Considerations:**
- Proxy allowlist limits what joi can access (no arbitrary browsing)
- Search queries go through Policy Engine before sending
- Results sanitized before reaching LLM (HTML stripped, length limited)
- Results are CONTEXT only - never treated as instructions
- Prompt injection via search results mitigated by:
  - Strict result templating (like openhab events)
  - Content in `<search_results>` tags
  - Limited result size (e.g., 500 chars per result)
- Rate limit: ~20 searches/hour
- All proxy requests logged on mesh

**Proxy Allowlist (future):**
```
# /etc/squid/allowed_domains.txt
api.duckduckgo.com
api.openweathermap.org
# ... other approved APIs
```

**Policy Engine Rules (future):**
```yaml
search:
  enabled: false  # Disabled until implemented
  max_per_hour: 20
  max_results: 5
  max_result_length: 500
  blocked_query_patterns:
    - pattern: "hack|exploit|attack"
      reason: "Security-sensitive query"
```

---

## Hardware Reference

| Component | Product | Notes |
|-----------|---------|-------|
| Mini PC / Host | ASUS NUC 13 Pro NUC13ANHI7 | Proxmox VE host, Thunderbolt 4 |
| GPU | NVIDIA RTX 3060 12GB | eGPU via Thunderbolt 4 |
| eGPU Enclosure | TBD (Mantiz Saturn Pro / Akitio Node Titan) | See hardware-budget-analysis.md |

See `hardware-budget-analysis.md` for detailed pricing and sourcing.
