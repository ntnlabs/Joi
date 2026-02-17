# Joi Architecture v2 (Security-Hardened)

## Goals
- Offline LLM on Proxmox VM with GPU passthrough (Llama 3.1 8B + NVIDIA RTX 3060).
- Free-running agent that reacts to context and can message the user.
- No direct WAN from Joi VM; Signal messaging only via proxy.
- openhab is read-only to Joi (ingest all events, no control).
- Security-first transport and validation across all boundaries.

> **Implementation:** See local `dev-notes.md` for tech stack, project structure, and development notes. Code lives in `execution/joi/` and `execution/mesh/`.

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
- **Behavior Mode:** Configurable between `companion` (proactive, organic engagement) and `assistant` (request-response only). See `agent-loop-design.md`.
- Local memory store (short-term + long-term).
- Policy engine enforcing read-only rules and outbound restrictions.
- Circuit breaker for agent actions and outbound messaging.
- **Emergency Stop:** Multiple options to cut communication (see below).

**Emergency Stop Options (in order of preference):**

| Method | Access Required | Speed | Notes |
|--------|-----------------|-------|-------|
| Proxmox mobile app | Phone + Proxmox account | Seconds | Shutdown mesh VM |
| Proxmox web UI | Browser + LAN/VPN | Seconds | Shutdown mesh or joi VM |
| SSH to Proxmox host | SSH key + network | Seconds | `qm stop <vmid>` |
| Physical power button | Physical access | Seconds | NUC power button (hard stop) |
| Network switch | Physical access | Seconds | Unplug vmbr1 uplink |
| Router firewall | Router admin access | Minutes | Block mesh VM's WAN access |

> **If you don't have mobile app access:**
> Bookmark Proxmox web UI on your phone's browser. Add to home screen for quick access.
> Test emergency stop procedure quarterly to ensure you can execute it under stress.

**What each stop method achieves:**
- Shutdown mesh VM → Cuts all Signal communication (joi isolated)
- Shutdown joi VM → Stops AI completely (LUKS locks disk)
- Physical power off → Everything stops, requires LUKS unlock to restart
- Network disconnect → Isolates but doesn't stop (joi continues running locally)

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
- **Message size limit: 1500 characters** (universal, no exceptions). Longer content → use file upload.
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

### Message Size Limits

> **Limit Hierarchy (single source of truth):**
>
> | Layer | Limit | Purpose |
> |-------|-------|---------|
> | Signal (mesh) | 1500 chars | User-facing messages via Signal |
> | API inbound (joi) | 4096 chars | Internal API requests (includes metadata) |
> | API outbound (joi) | 2048 chars | Internal API responses |
> | Knowledge query | 4096 bytes | joi-retrieve query JSON |
> | Search result | 500 chars | Per-result snippet from web search |
>
> The 1500 char Signal limit is the most restrictive and user-facing. Internal API
> limits are higher to accommodate structured payloads with metadata/headers.

```yaml
# /etc/mesh-proxy/limits.yaml (AUTHORITATIVE for mesh limits)
message:
  max_length: 1500    # Signal messages, user-facing
```

```yaml
# /etc/joi/policy.yaml (AUTHORITATIVE for joi limits)
content:
  input:
    signal:
      max_length: 4096    # API payload including metadata
  output:
    max_length: 2048      # API response including metadata
```

**On rejection (mesh):**
```
"Message too long ({len} chars). Maximum is 1500.
 For longer content, please send as a file."
```

This is enforced at mesh before forwarding to joi. Pushes users toward file uploads for large content (which has proper quota management).

### Mesh Integrity Monitoring

Joi monitors mesh health via heartbeat. If mesh appears compromised, joi shuts down.

**Principle:** Never trust mesh to monitor itself. Mesh collects data, joi validates it.

**Heartbeat Protocol (Challenge-Response):**

```
joi-challenger (every 30s) ────► mesh-watchdog
   │                                │
   │ Challenge:                     │ Must respond within 10s:
   │ - nonce (random)               │ - challenge_response (HMAC)
   │                                │ - process status
                                    │ - binary hashes
                                    │ - config hashes
                                    ▼
                            joi-verifier
                              │
                              │ Validates:
                              │ - HMAC matches (proves mesh has secret)
                              │ - Hashes match known-good
                              │ - All processes running
                              │ - Response within 10s timeout
```

**Challenge from joi:**
```json
{
  "challenge": "random-nonce-from-joi",
  "timestamp": 1707350400000
}
```

**Response from mesh (within 10 seconds):**
```json
{
  "challenge_response": "hmac-sha256(challenge + shared_secret)",
  "timestamp": 1707350400500,
  "processes": {
    "nebula": {"running": true, "pid": 1234, "user": "root"},
    "signal-cli": {"running": true, "pid": 2345, "user": "signal"},
    "mesh-proxy": {"running": true, "pid": 3456, "user": "mesh-proxy"},
    "mesh-watchdog": {"running": true, "pid": 4567, "user": "root"}
  },
  "hashes": {
    "/usr/local/bin/mesh-proxy": "sha256:abc123...",
    "/usr/local/bin/signal-cli": "sha256:def456...",
    "/usr/local/bin/mesh-watchdog": "sha256:ghi789...",
    "/etc/mesh-proxy/config.yaml": "sha256:jkl012..."
  }
}
```

**joi-verifier checks:**
- Challenge response HMAC is correct (proves mesh has shared secret)
- Response received within 10 seconds
- Hashes match known-good (stored on joi, immutable)
- All expected processes running with correct user (including mesh-watchdog itself)

**Attack Window:**
- Old design: 90 seconds (3 missed heartbeats)
- New design: ~10 seconds max (challenge timeout)
- Attacker cannot predict when challenge comes (joi-initiated)

> **Limitation:** Automated attacks can complete in <5 seconds. This protects against
> manual/slower attacks. Real-time intrusion detection is a post-PoC consideration.

**Compromise Response:**

| Condition | Action |
|-----------|--------|
| Hash mismatch | **joi shuts down immediately** |
| Process not running / wrong user | **joi shuts down immediately** |
| Challenge response timeout (>10s) | **joi shuts down immediately** |
| Challenge response HMAC invalid | **joi shuts down immediately** |
| mesh-watchdog not in process list | **joi shuts down immediately** |

**Why shutdown instead of alerting?**
- openhab is read-only (cannot send alerts through it)
- If mesh is compromised, attacker controls Signal (cannot alert through it)
- Shutdown is: easy, safe, detectable
- Owner notices joi offline → investigates via Proxmox console

**LUKS makes shutdown a security lockout:**
- joi disk is LUKS-encrypted
- Shutdown = disk locked
- Attacker cannot power joi back up (no LUKS passphrase)
- Only owner can unlock via Proxmox console
- This is a one-way trip for the attacker

> **LUKS Configuration Requirements:**
> - **No TPM:** VMs cannot use TPM auto-unlock (Proxmox doesn't expose TPM to VMs)
> - **Manual unlock only:** LUKS passphrase entered via Proxmox console on every boot
> - **No keyfile:** Keyfiles would defeat the security purpose (attacker with disk access = game over)
> - **Consequence:** Reboots require manual intervention. This is a feature, not a bug.
>
> This is the correct configuration for security-critical VMs. Automated LUKS unlock
> (via TPM, keyfile, or network) would allow an attacker who gains VM access to
> simply reboot and regain access.

**Maintenance Mode (USB Key):**

The mesh heartbeat shutdown creates an operational problem: how to perform planned
mesh maintenance (updates, reboots) without triggering joi shutdown?

Solution: A physical USB key with cryptographic proof enables maintenance mode.

```
┌─────────────────────────────────────────────────┐
│ Maintenance USB Key                             │
│  └── /maintenance/key.pem (Ed25519 private key) │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│ Joi Config (/etc/joi/maintenance-pubkey.pem)    │
│  └── Ed25519 public key                         │
└─────────────────────────────────────────────────┘
```

**Activation Flow:**

1. Owner plugs USB key into Proxmox host (passed through to joi VM)
2. Joi detects USB mount via udev rule
3. Joi reads private key from `/maintenance/key.pem`
4. Joi generates random challenge nonce
5. Joi signs challenge with private key
6. Joi verifies signature against stored public key
7. If valid → maintenance mode activated, heartbeat suspended
8. Joi logs: `MAINTENANCE_MODE_ACTIVATED source=usb_key`
9. USB removal (or timeout) → normal operation resumes

**USB Detection (udev rule):**
```bash
# /etc/udev/rules.d/99-maintenance-key.rules
ACTION=="add", SUBSYSTEM=="block", ENV{ID_FS_LABEL}=="JOI_MAINTENANCE", \
  RUN+="/usr/local/bin/joi-maintenance-check"
```

**Verification Script:**
```bash
#!/bin/bash
# /usr/local/bin/joi-maintenance-check

MOUNT_POINT="/mnt/maintenance-key"
PRIVATE_KEY="$MOUNT_POINT/maintenance/key.pem"
PUBLIC_KEY="/etc/joi/maintenance-pubkey.pem"
CHALLENGE_FILE="/tmp/maintenance-challenge"

# Mount the USB
mkdir -p "$MOUNT_POINT"
mount -o ro LABEL=JOI_MAINTENANCE "$MOUNT_POINT" || exit 1

# Check if key exists
if [[ ! -f "$PRIVATE_KEY" ]]; then
    logger -t joi-maintenance "No private key found on USB"
    umount "$MOUNT_POINT"
    exit 1
fi

# Generate challenge
openssl rand -hex 32 > "$CHALLENGE_FILE"

# Sign challenge with USB private key
SIGNATURE=$(openssl pkeyutl -sign -inkey "$PRIVATE_KEY" \
    -in "$CHALLENGE_FILE" | base64 -w0)

# Verify signature with stored public key
echo "$SIGNATURE" | base64 -d | \
    openssl pkeyutl -verify -pubin -inkey "$PUBLIC_KEY" \
    -in "$CHALLENGE_FILE"

if [[ $? -eq 0 ]]; then
    logger -t joi-maintenance "MAINTENANCE_MODE_ACTIVATED source=usb_key"
    touch /var/run/joi-maintenance-mode
    # Notify joi-challenger to suspend heartbeat checks
    systemctl kill -s USR1 joi-challenger
else
    logger -t joi-maintenance "MAINTENANCE_KEY_INVALID"
fi

# Cleanup
shred -u "$CHALLENGE_FILE"
umount "$MOUNT_POINT"
```

**Deactivation (USB removal):**
```bash
# /etc/udev/rules.d/99-maintenance-key.rules (continued)
ACTION=="remove", ENV{ID_FS_LABEL}=="JOI_MAINTENANCE", \
  RUN+="/usr/local/bin/joi-maintenance-end"

# /usr/local/bin/joi-maintenance-end
#!/bin/bash
rm -f /var/run/joi-maintenance-mode
systemctl kill -s USR2 joi-challenger
logger -t joi-maintenance "MAINTENANCE_MODE_DEACTIVATED"
```

**Security Properties:**
| Property | Guarantee |
|----------|-----------|
| Physical presence | USB must be plugged into Proxmox host |
| Cryptographic proof | Private key required (can't clone by USB ID alone) |
| No network path | Mesh cannot trigger maintenance mode |
| Public key safe | Stored public key is not secret |
| Audit trail | All activations/deactivations logged |
| Fail-secure | Invalid key → mode not activated |

**Key Generation:**
```bash
# Generate key pair (on air-gapped machine)
openssl genpkey -algorithm ED25519 -out maintenance-key.pem
openssl pkey -in maintenance-key.pem -pubout -out maintenance-pubkey.pem

# Install public key on joi
sudo install -o root -g root -m 0644 maintenance-pubkey.pem /etc/joi/maintenance-pubkey.pem

# Prepare USB key (FAT32 for compatibility)
# Label MUST be "JOI_MAINTENANCE"
sudo mkfs.vfat -n JOI_MAINTENANCE /dev/sdX1
sudo mount /dev/sdX1 /mnt/usb
sudo mkdir /mnt/usb/maintenance
sudo cp maintenance-key.pem /mnt/usb/maintenance/key.pem
sudo umount /mnt/usb

# Securely delete original private key
shred -u maintenance-key.pem
```

**Multiple Keys (optional):**

For redundancy, multiple public keys can be authorized:
```bash
# /etc/joi/maintenance-pubkeys/
#   ├── primary.pem      (carried by owner)
#   └── backup.pem       (stored in safe)
```

Verification script checks against all keys in directory.

**Timeout Safety:**

Optional maximum maintenance duration prevents forgotten USB:
```yaml
# /etc/joi/config.yaml
maintenance:
  max_duration_minutes: 60  # Auto-exit after 1 hour
  warning_at_minutes: 50    # Log warning before timeout
```

After timeout, maintenance mode deactivates even if USB still present.
Owner must re-insert USB to extend.

**Known-Good Hashes (stored on joi):**
```yaml
# /etc/joi/mesh-baseline.yaml
# Updated only during legitimate mesh updates
# File is immutable (chattr +i)

mesh_binaries:
  /usr/local/bin/mesh-proxy: "sha256:..."
  /usr/local/bin/signal-cli: "sha256:..."

mesh_configs:
  /etc/mesh-proxy/config.yaml: "sha256:..."
  /etc/mesh-proxy/identities.yaml: "sha256:..."
  /etc/nebula/config.yaml: "sha256:..."

expected_processes:
  - name: nebula
    user: root
  - name: signal-cli
    user: signal
  - name: mesh-proxy
    user: mesh-proxy
```

**Challenge-Response Shared Secret:**

The HMAC shared secret proves mesh possesses a secret known only to joi and legitimate mesh.

```
# Storage locations (both VMs):

# On joi VM:
/etc/joi/secrets/mesh-hmac.key
  - Owner: root:joi
  - Permissions: 0640
  - Format: 32 bytes, hex-encoded (64 chars)
  - Immutable: chattr +i

# On mesh VM:
/etc/mesh-proxy/secrets/hmac.key
  - Owner: root:mesh-proxy
  - Permissions: 0640
  - Format: same as joi
  - Protected by: LUKS disk encryption
```

**Key Generation (initial setup):**
```bash
# Generate on air-gapped machine or joi VM
openssl rand -hex 32 > mesh-hmac.key

# Install on joi
sudo install -o root -g joi -m 0640 mesh-hmac.key /etc/joi/secrets/mesh-hmac.key
sudo chattr +i /etc/joi/secrets/mesh-hmac.key

# Install on mesh (transfer securely - e.g., via Proxmox console copy)
sudo install -o root -g mesh-proxy -m 0640 mesh-hmac.key /etc/mesh-proxy/secrets/hmac.key

# Delete original
shred -u mesh-hmac.key
```

**Key Rotation:**
- Rotate annually or on suspected compromise
- Rotation requires Proxmox console access to both VMs
- Steps: generate new key → install on mesh → install on joi → verify heartbeat works

**Threat Model:**
| Attack | Prevention |
|--------|-----------|
| Key extraction from mesh | LUKS encryption + file permissions (0640) |
| Key extraction from joi | LUKS encryption + immutable flag + 0640 perms |
| Brute force HMAC | 256-bit key = computationally infeasible |
| Replay attack | Fresh nonce per challenge |
| MITM | Nebula encryption (attacker cannot see challenge/response) |

**Updating Baseline After Legitimate Changes:**
```bash
# On mesh: regenerate hashes
mesh-watchdog --export-hashes > /tmp/new-hashes.yaml

# Transfer to joi securely (via Proxmox console or direct)
# On joi: update baseline
chattr -i /etc/joi/mesh-baseline.yaml
cp /tmp/new-hashes.yaml /etc/joi/mesh-baseline.yaml
chattr +i /etc/joi/mesh-baseline.yaml
systemctl restart joi
```

## LLM Safety and Validation
- Never pass raw openhab events to LLM; use structured templates.
- Output validation and allowlists for outbound messages.
- Rate limiting and circuit breaker for agent actions.
- Sliding context window with summarization and hard limits.
- Assumption: only the owner can interact with Joi; no third-party inputs are expected.

## Memory Architecture

Joi has multiple layers of memory with different persistence and management.

### Memory Layers

| Layer | Persistence | Source | Reset clears? |
|-------|-------------|--------|---------------|
| **1. Context window** | Ephemeral | Recent messages in conversation | ✅ Yes |
| **2. Conversation summary** | Session | Auto-generated when context overflows | ✅ Yes |
| **3. Session knowledge** | Session | Facts extracted during conversation | ✅ Yes |
| **4. Uploaded data** | Session | Files user sent (PDFs, images, docs) | ✅ Yes |
| **5. Explicit memory** | Until contradicted | User says "remember this" | ❌ No |
| **6. Permanent knowledge** | Read-only | Files in knowledge/ folder | ❌ No |
| **7. User profile** | Config | Set at registration | ❌ No |
| **8. Event history** | Logged | openhab events | ❌ No |
| **9. Shared knowledge** | Read-only | shared/ folder | ❌ No |

### Layer Details

**1. Context window**
- Sliding window of recent messages (configurable size)
- Directly visible to LLM in each request
- Oldest messages dropped or summarized when window full

**2. Conversation summary**
- Generated when context window overflows
- Preserves key points from earlier in conversation
- "Earlier we discussed X, decided Y..."

**3. Session knowledge**
- Facts and information extracted during conversation
- Stored in session-scoped database
- Example: "User's tax documents show income of X"

**4. Uploaded data**
- Files sent by user during conversation
- Processed and stored temporarily
- Cleared on reset or after retention period

**5. Explicit memory**
- User explicitly asks Joi to remember something
- Persists until contradicted or explicitly forgotten
- Managed conversationally, not by reset command

**6-9. Permanent layers**
- Not affected by reset
- Managed through configuration or file updates

> **Shared knowledge writability:**
> - `shared/` folder is **admin-write only** (not writable by joi service)
> - Contains: common reference data, shared procedures, household info
> - Updates: Admin places files via Proxmox console or SSH
> - Joi can READ shared/ but CANNOT WRITE to it (defense against LLM manipulation)
> - Owner: `root:joi-all-channels`, mode `750` (read-only for joi users)
>
> If Joi needs to save something for all users, admin must manually move it
> from a user's folder to shared/. This prevents LLM-driven pollution of
> shared knowledge.

### Reset Command

```
User: /reset
Joi: Session cleared. I've forgotten our recent conversation,
     working documents, and any files you shared.
     Your permanent memories and preferences are unchanged.
```

**What reset clears:**
- Context window (layer 1)
- Conversation summaries (layer 2)
- Session knowledge (layer 3)
- Uploaded data (layer 4)

**What reset preserves:**
- Explicit memories (layer 5) - managed conversationally
- Permanent knowledge (layer 6)
- User profile (layer 7)
- Event history (layer 8)
- Shared knowledge (layer 9)

### Explicit Memory Management

Explicit memories are managed conversationally, not by wildcard commands:

```
User: I have a BMW.
Joi: Got it, I'll remember you drive a BMW.

[later]

User: I sold my BMW, I have an Audi now.
Joi: Updated - you now drive an Audi.
     (internally: BMW fact marked as superseded, Audi fact added)

User: What car do I have?
Joi: You drive an Audi. (You previously had a BMW.)
```

**Explicit forget:**
```
User: Forget that I ever had a BMW.
Joi: Done. I've removed all memory of you having a BMW.
```

### Admin Memory Purge

> **Security Note:** Explicit memories persist across /reset by design. This creates a risk:
> if a malicious memory is injected (e.g., via prompt injection in uploaded content),
> it survives reset and continues influencing Joi's behavior.

**Admin purge command** (run on joi VM via Proxmox console):
```bash
# /usr/local/bin/joi-purge-memories
#!/bin/bash
# Purges ALL explicit memories - use only for security incidents

RECIPIENT=${1:-all}

if [[ "$RECIPIENT" == "all" ]]; then
    echo "DANGER: This will delete ALL explicit memories for ALL users."
    echo "Type 'PURGE ALL' to confirm:"
    read confirm
    [[ "$confirm" != "PURGE ALL" ]] && exit 1

    sqlite3 /var/lib/joi/memory.db "UPDATE user_facts SET active = 0, purged_at = datetime('now'), purge_reason = 'admin_security_purge';"
    echo "All explicit memories purged."
else
    echo "Purging memories for recipient: $RECIPIENT"
    sqlite3 /var/lib/joi/memory.db "UPDATE user_facts SET active = 0, purged_at = datetime('now'), purge_reason = 'admin_security_purge' WHERE user_id = '$RECIPIENT';"
fi

# Force joi to reload memory index
systemctl reload joi
```

**When to use:**
- Suspected prompt injection attack that planted malicious memories
- User requests complete data deletion (GDPR-style)
- Security incident response

**Audit trail:** Purged memories are soft-deleted with timestamp and reason, not hard-deleted.

### Memory Store Security

- Encrypt at rest (SQLCipher or LUKS)
- Integrity checks (checksums, append-only log)
- Retention policies and automatic pruning
- Session data cleared on reset
- Uploaded files deleted after processing or on reset

> **Context Summary Validation (Security Gap):**
>
> Context summaries are LLM-generated and fed back as context. Current validation
> uses a **blocklist** of suspicious patterns (see `memory-store-schema.md`).
>
> **Problem:** Blocklists are bypassable. An attacker can craft prompt injections
> that avoid known patterns but still influence behavior.
>
> **Recommended improvement (post-PoC):**
> - Use **allowlist** validation: summaries should only contain factual statements
> - Structure validation: enforce summary format (bullet points, no imperatives)
> - Length limits: cap summary size to prevent payload injection
> - Semantic validation: flag summaries that contain instruction-like language
>
> ```python
> # Improved validation (allowlist approach)
> def validate_summary_strict(summary: str) -> bool:
>     # Must be structured as bullet points or short paragraphs
>     if re.search(r'(you must|you should|always|never|important:)', summary, re.I):
>         return False  # Instruction-like language not allowed in summaries
>
>     # Must not contain code or special characters
>     if re.search(r'[<>{}\\`]', summary):
>         return False
>
>     # Must be reasonable length
>     if len(summary) > 2000:
>         return False
>
>     return True
> ```

### Uploaded Data Handling

Users can send files to Joi. Validation is split between mesh (security) and joi (policy).

**Validation Flow:**

```
User sends file
    ↓
MESH: Extension allowed? ──No──► Reject + notify user
    ↓ Yes
MESH: Magic bytes match? ──No──► Reject + notify user
    ↓ Yes
MESH: Under 100MB hard limit? ──No──► Reject + notify user
    ↓ Yes
Forward to Joi via Nebula
    ↓
JOI: Under user's size limit? ──No──► Reject + notify user
    ↓ Yes
JOI: Session file count OK? ──No──► Reject + notify user
    ↓ Yes
JOI: Session total size OK? ──No──► Reject + notify user
    ↓ Yes
JOI: User quota OK? ──No──► Reject + notify user
    ↓ Yes
JOI: Content valid (parse)? ──No──► Reject + notify user
    ↓ Yes
Store and process
```

**Mesh Checks (universal security - same for all users):**

```yaml
# /etc/mesh-proxy/uploads.yaml
uploads:
  # Security gating - is this safe to forward?
  allowed_extensions:
    - .txt
    # - .pdf    # DISABLED: CVE risk in pdftotext/pdfplumber (see security note below)
    - .csv
    - .md
    - .json

  magic_validation: true      # Check file magic bytes
  reject_mismatch: true       # Reject if magic doesn't match extension
  hard_max_size_mb: 50        # Reduced from 100MB to match extraction limits
```

> **PDF DISABLED (PoC Security Decision):**
> PDF parsing tools (pdftotext/poppler, pdfplumber) have had multiple CVEs including
> buffer overflows and malformed font handling exploits. Until proper sandboxing is
> implemented (seccomp subprocess, separate VM, or Rust-based parser), PDF uploads
> are rejected at the mesh layer. Users receive: "PDF uploads are temporarily disabled
> for security hardening. Please copy text content into a .txt file."
>
> **Re-enable checklist:**
> - [ ] Implement seccomp-sandboxed subprocess for PDF parsing
> - [ ] OR deploy pdf-parser in isolated VM with minimal attack surface
> - [ ] OR switch to memory-safe parser (pdf-rs, pdf.js server-side)
> - [ ] Add quarterly CVE monitoring for chosen parser

| Extension | Validation | Notes |
|-----------|------------|-------|
| .txt | UTF-8 + no binary | Reject if contains NUL bytes or invalid UTF-8 |
| .csv | UTF-8 + structure | Must have consistent column count, reject embedded NUL |
| .json | Parse + validate | Must parse as valid JSON, reject on syntax error |
| .md | UTF-8 + no binary | Same as .txt, reject if contains NUL bytes |

> **Text file validation (no magic bytes available):**
> Files without magic bytes (.txt, .csv, .md) use content validation instead:
> ```python
> def validate_text_file(content: bytes) -> bool:
>     # 1. Reject binary content (NUL bytes indicate binary)
>     if b'\x00' in content:
>         return False
>
>     # 2. Must be valid UTF-8
>     try:
>         content.decode('utf-8')
>     except UnicodeDecodeError:
>         return False
>
>     # 3. Reject suspiciously high ratio of control characters
>     text = content.decode('utf-8')
>     control_chars = sum(1 for c in text if ord(c) < 32 and c not in '\n\r\t')
>     if len(text) > 0 and control_chars / len(text) > 0.1:
>         return False
>
>     return True
> ```
> This catches attempts to disguise binary files (executables, archives) as text.

Mesh does NOT have per-user limits - it only does universal security checks.

**Joi Checks (per-user policy - requires full config):**

```yaml
# /etc/joi/recipients.yaml
recipients:
  owner:
    upload_limits:
      max_file_size_mb: 50        # User's personal limit (under mesh's 100MB)
      max_session_files: 20
      max_session_total_mb: 200
      max_storage_quota_mb: 1000

  partner:
    upload_limits:
      max_file_size_mb: 10
      max_session_files: 10
      max_session_total_mb: 50
      max_storage_quota_mb: 500
```

Joi has the full context: user identity, session state, storage usage.

**Storage Structure:**

```
# Runtime (tmpfs) - cleared on reboot or /reset
/var/lib/joi/session-uploads/     # tmpfs mount
├── owner_private/
│   └── session_abc123/
│       ├── document.txt
│       └── data.csv
└── owner_public/
    └── session_def456/

# Persistent (disk) - only for explicitly saved knowledge
/var/lib/joi/knowledge/
├── owner/
│   └── private/
│       └── saved/                # Explicitly saved from session
└── group/
    └── owner_public/
        └── saved/
```

> **Session uploads in tmpfs:** Session uploads live in tmpfs during runtime.
> This prevents sensitive uploaded data from persisting on disk after a crash.
> Only data explicitly saved to knowledge base moves to persistent storage.
>
> ```bash
> # /etc/fstab - session uploads tmpfs (separate from extraction tmpfs)
> tmpfs /var/lib/joi/session-uploads tmpfs size=1G,mode=750,uid=joi,gid=joi 0 0
> ```
>
> On `/reset`: session tmpfs is cleared. On reboot: automatically cleared.
> On crash: no session data persists (defense in depth).

Uploads inherit channel permissions - Linux access control works automatically.

**File Processing (on joi):**

| Format | Processing | Tool |
|--------|------------|------|
| .txt, .md | Direct use | None |
| .csv | Parse to structured data | Python csv |
| .json | Parse and validate | Python json |

> **PDF Support (DISABLED in PoC):** See "PDF DISABLED" note above. PDF will be
> re-enabled once sandboxing is implemented. Current workaround: users copy text
> content into .txt files.

**Safe Extraction (tmpfs isolation):**

Extraction happens in tmpfs to protect main filesystem from zip-bomb style attacks.

```
Upload arrives
    ↓
Store in tmpfs: /var/lib/joi/extract/
    ↓
Extract/parse in tmpfs
    ↓
Check: extracted size < limit?
    ↓ Yes                          ↓ No
Move to permanent storage     Delete + notify user
    ↓                              "Extraction too large"
Delete tmpfs copy
```

**Tmpfs Configuration:**
```bash
# /etc/fstab
tmpfs /var/lib/joi/extract tmpfs size=512M,mode=750,uid=joi,gid=joi 0 0
```

**Extraction Limits:**
```yaml
# /etc/joi/extraction.yaml
extraction:
  tmpfs_path: /var/lib/joi/extract
  max_extracted_size_mb: 50       # Absolute cap per file
  max_ratio: 10                   # Max 10x inflation (1MB input → 10MB max output)
  timeout_seconds: 30             # Kill extraction if takes too long
  lock_file: /run/joi/extract.lock  # In /run (tmpfs) - auto-cleared on reboot
  max_concurrent: 3               # Max 3 concurrent extractions (150MB worst case)
  queue_max: 5                    # Max pending extractions before rejecting
  max_memory_mb: 256              # Per-extraction memory limit (OOM protection)
```

> **Lock file in /run (tmpfs):** The lock file is in `/run/joi/` which is tmpfs on
> modern Linux (cleared on reboot). This prevents stale locks after crash/reboot.
> Create directory at boot: `mkdir -p /run/joi && chown joi:joi /run/joi`
> Add to systemd: `RuntimeDirectory=joi` in joi.service

> **Tmpfs Sizing Rationale:**
>
> | Limit | Value | Worst Case |
> |-------|-------|------------|
> | max_extracted_size_mb | 50MB | 50MB per file |
> | max_concurrent | 3 | 150MB concurrent |
> | Emergency threshold | 80% | 410MB triggers cleanup |
> | Safe headroom | ~100MB | For cleanup operations |
> | **Total tmpfs** | **512MB** | Fits worst case + headroom |
>
> The mesh hard limit (50MB input) × max_ratio (10x) = 500MB theoretical max per file,
> but max_extracted_size_mb (50MB) caps actual output. With max 3 concurrent extractions,
> worst case is 150MB, well within 512MB tmpfs.
>
> **If tmpfs fills despite limits:** Emergency cleanup triggers at 80% (410MB),
> deletes oldest files until under 50% (256MB). New extractions wait (LOCK_SH blocked)
> until cleanup completes.

**Garbage Collection:**

```yaml
# Cleanup policy
cleanup:
  # Regular cleanup (cron daily at 3am)
  schedule: "0 3 * * *"
  max_age_hours: 24               # Delete files older than 24h

  # Emergency cleanup (when tmpfs gets full)
  emergency_threshold_percent: 80  # Trigger at 80% full
  emergency_action: delete_oldest  # Delete oldest files until under 50%
```

**Cleanup Script:** `/usr/local/bin/joi-extract-cleanup`
```bash
#!/bin/bash
EXTRACT_DIR="/var/lib/joi/extract"
LOCK_FILE="/run/joi/extract.lock"
MAX_AGE_HOURS=24
EMERGENCY_THRESHOLD=80

# Ensure lock directory exists (should be created by systemd, but be safe)
mkdir -p /run/joi
chown joi:joi /run/joi

# Acquire EXCLUSIVE lock - blocks all extractions during cleanup
exec 200>"$LOCK_FILE"
flock -x 200
echo "Lock acquired, cleanup starting"

# Regular cleanup - delete old files
find "$EXTRACT_DIR" -type f -mmin +$((MAX_AGE_HOURS * 60)) -delete
find "$EXTRACT_DIR" -type d -empty -delete

# Emergency cleanup - check tmpfs usage
USAGE=$(df "$EXTRACT_DIR" --output=pcent | tail -1 | tr -d ' %')
if [[ $USAGE -gt $EMERGENCY_THRESHOLD ]]; then
    echo "EMERGENCY: tmpfs at ${USAGE}%, cleaning oldest files"
    # Delete oldest files until under 50% (safe now - no concurrent extractions)
    while [[ $(df "$EXTRACT_DIR" --output=pcent | tail -1 | tr -d ' %') -gt 50 ]]; do
        OLDEST=$(find "$EXTRACT_DIR" -type f -printf '%T+ %p\n' | sort | head -1 | cut -d' ' -f2-)
        if [[ -n "$OLDEST" ]]; then
            rm -f -- "$OLDEST"
        else
            break
        fi
    done
fi

echo "Cleanup complete"
# Lock auto-released on script exit
```

**Cron Entry:**
```
0 3 * * * /usr/local/bin/joi-extract-cleanup >> /var/log/joi/extract-cleanup.log 2>&1
```

**Extraction with Lock, Timeout, Memory Limit, and Concurrency Limit:**
```python
import fcntl
import subprocess
import threading
import resource
import os
import signal

LOCK_FILE = "/run/joi/extract.lock"  # In tmpfs - auto-cleared on reboot
TIMEOUT_SECONDS = 30
MAX_CONCURRENT = 3
MAX_MEMORY_BYTES = 256 * 1024 * 1024  # 256MB

# Semaphore limits concurrent extractions
_extraction_semaphore = threading.Semaphore(MAX_CONCURRENT)

def _set_resource_limits():
    """Set memory limit for subprocess (called in child after fork)."""
    # Limit virtual memory to prevent OOM
    resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
    # Limit CPU time as backup to timeout
    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT_SECONDS + 5, TIMEOUT_SECONDS + 10))

def extract_file(input_path: str, output_dir: str) -> str:
    """Extract file with concurrency limit, lock coordination, memory limit, and timeout."""

    # 1. Check concurrency limit (non-blocking)
    if not _extraction_semaphore.acquire(blocking=False):
        raise ExtractionError("Too many concurrent extractions, try again in a moment")

    try:
        # 2. Acquire SHARED lock (blocks during cleanup)
        with open(LOCK_FILE, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ExtractionError("Cleanup in progress, try again in a moment")

            # 3. Check tmpfs space
            if get_tmpfs_usage_percent() > 95:
                raise ExtractionError("Extraction space full, try again later")

            # 4. Run extraction with timeout, memory limit, and process group
            # Note: PDF disabled in PoC - this handles .txt, .csv, .json, .md only
            process = None
            try:
                process = subprocess.Popen(
                    ["cat", input_path],  # Simple passthrough for text files
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    preexec_fn=_set_resource_limits,
                    start_new_session=True  # New process group for clean kill
                )
                stdout, stderr = process.communicate(timeout=TIMEOUT_SECONDS)
                if process.returncode != 0:
                    raise ExtractionError(f"Extraction failed: {stderr.decode()[:200]}")
                return stdout.decode()
            except subprocess.TimeoutExpired:
                # Kill entire process group (catches any child processes)
                if process:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    process.wait()  # Reap zombie
                raise ExtractionError("Extraction timed out, file too complex")

            # Lock auto-released when 'with' block exits
    finally:
        # Always release semaphore
        _extraction_semaphore.release()
```

**Lock Coordination:**
- Extractions acquire SHARED lock (multiple can run concurrently)
- Cleanup acquires EXCLUSIVE lock (blocks new extractions, waits for running ones)
- No race condition possible between extraction and cleanup

**What This Protects Against:**
- Zip bomb: small file → huge extraction → tmpfs fills → main disk safe
- Infinite loop: extraction killed after 30 seconds
- Race condition: lock prevents cleanup/extraction conflicts
- Leftover files: daily cleanup + emergency cleanup
- Tmpfs exhaustion: max 3 concurrent extractions (150MB worst case vs 512MB tmpfs)
- PDF exploits: PDF disabled until sandboxing implemented
- Stale locks after crash: lock file in /run (tmpfs), auto-cleared on reboot
- OOM from extraction: 256MB memory limit per extraction subprocess
- Zombie processes: process group kill on timeout catches all children
- Session data persistence: session uploads in tmpfs, cleared on reset/reboot

**Limit Notifications:**

All notifications go through Joi to the user. Mesh rejections are forwarded to Joi which notifies user.

| Limit Hit | Checked By | Response |
|-----------|------------|----------|
| Extension not allowed | Mesh | "I can't process .{ext} files. I accept: txt, csv, md, json." |
| PDF upload attempted | Mesh | "PDF uploads are temporarily disabled for security. Please copy text into a .txt file." |
| Magic byte mismatch | Mesh | "This file doesn't appear to be a valid {ext} file. Please check the file." |
| Over 100MB hard limit | Mesh | "This file is too large ({size}MB). Maximum is 100MB." |
| Over user's size limit | Joi | "This file is {size}MB but your limit is {max}MB. Please send a smaller file." |
| Session file count | Joi | "You've uploaded {n} files this session (limit: {max}). Use /reset to start fresh." |
| Session size limit | Joi | "This session has {used}MB of uploads (limit: {max}MB). Use /reset to free up space." |
| Storage quota | Joi | "You've reached your storage quota ({quota}MB). Please /reset old sessions." |
| Invalid content | Joi | "I couldn't read this {ext} file. It may be corrupted or password-protected." |
| Extraction too large | Joi | "This file expands to more than I can safely process. Please send a simpler file." |
| Extraction timeout | Joi | "This file is taking too long to process. It may be too complex." |
| Extraction space full | Joi | "I'm temporarily unable to process files. Please try again in a few minutes." |
| Too many concurrent | Joi | "Processing multiple files right now. Please try again in a moment." |
| Cleanup in progress | Joi | "System maintenance in progress. Please try again in a moment." |

**Quota Warnings (proactive):**

```yaml
upload_warnings:
  warn_at_percent: 80    # Warn when 80% of limit reached
```

```
Joi: Just a heads up - you've used 85% of your session upload limit.
     You can upload about 30MB more before hitting the cap.
```

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
│   └── shared/                 # 750 joi:joi-all-channels (not world-readable)
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
    └── shared.db               # 640 joi:joi-all-channels
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
groupadd joi-all-channels       # All channel users (for shared/ access)

# Add all channel users to joi-all-channels
usermod -aG joi-all-channels joi-owner-private
usermod -aG joi-all-channels joi-owner-public
usermod -aG joi-all-channels joi-partner-private
usermod -aG joi-all-channels joi-partner-public
usermod -aG joi-all-channels joi-family
usermod -aG joi-all-channels joi-critical
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
Acquire config read lock (prevents reload during lookup)
    ↓
Lookup: allowed_channel_users["owner_private_dm"]
    ↓
Not found? → DENY (security error, logged)
    ↓
Found: joi-owner-private
    ↓
Verify user exists: getpwnam("joi-owner-private")
    ↓
User missing? → DENY (config/system mismatch, alert)
    ↓
Release config read lock
    ↓
Spawn knowledge retrieval subprocess as that user
    ↓
Linux kernel enforces file access
    ↓
Only permitted files/databases readable
    ↓
Results returned to main Joi process
```

**TOCTOU Protection:**
```python
# Atomic configuration reload (prevents race conditions)
class ConfigManager:
    def __init__(self):
        self._config = {}
        self._lock = threading.RLock()

    def reload(self, new_config_path: str):
        """Atomic config reload via temp file + rename."""
        # 1. Load and validate new config
        new_config = load_and_validate(new_config_path)

        # 2. Verify all users exist
        for channel, user in new_config["allowed_channel_users"].items():
            try:
                pwd.getpwnam(user)
            except KeyError:
                raise ConfigError(f"User {user} does not exist")

        # 3. Atomic swap under lock
        with self._lock:
            self._config = new_config

    def get_channel_user(self, channel: str) -> Optional[str]:
        """Get user for channel (thread-safe)."""
        with self._lock:
            return self._config.get("allowed_channel_users", {}).get(channel)
```

**Error Handling:**
| Scenario | Action |
|----------|--------|
| User deleted after lookup | Subprocess fails, logged as security event |
| Config file corrupted | Reject reload, keep old config, alert |
| Unknown channel | DENY, log security event |

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

### joi-retrieve Binary Specification

The `joi-retrieve` binary is the ONLY way to access knowledge. It runs as the channel user via sudo.

**Binary Requirements:**
- **Owner/permissions:** `root:root 0755` (immutable via `chattr +i`)
- **Location:** `/usr/local/bin/joi-retrieve`
- **Language:** Compiled (Go/Rust preferred) - no interpreter injection

**Input Validation (STRICT):**
```
joi-retrieve <query-type> <query-json>

Query types (whitelist):
  - search      Search knowledge base
  - get         Get specific document by ID
  - list        List documents in category

Query JSON (validated):
  - Max length: 4096 bytes
  - Must be valid JSON
  - No path components allowed (no /, .., ~)
  - Allowed characters (allowlist):
    - Alphanumeric: a-z A-Z 0-9
    - Punctuation: . , : ; ? ! ' " - _ @ # ( ) [ ]
    - Whitespace: space, tab, newline
    - DENIED: / \ .. ~ $ ` { } < > | & * ^
  - Document IDs must be UUIDv4 format (prevents enumeration)
```

**Example:**
```bash
# Valid
sudo -u joi-owner-private /usr/local/bin/joi-retrieve search '{"q":"password reset","limit":5}'

# Invalid - rejected before execution
sudo -u joi-owner-private /usr/local/bin/joi-retrieve search '{"path":"/etc/shadow"}'
# Error: path components not allowed in query
```

**Output:**
- JSON only (structured, never raw file contents)
- Max output size: 1MB (truncated if exceeded)
- Includes metadata (source, timestamp, access level)

```json
{
  "status": "ok",
  "results": [
    {
      "id": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
      "title": "Password Policy",
      "snippet": "...",
      "source": "owner/private/security.md",
      "accessed_as": "joi-owner-private"
    }
  ],
  "truncated": false
}
```

**Security Properties:**
- No shell metacharacter processing
- No path traversal possible (queries by ID, not path)
- No arbitrary file reads (only indexed knowledge)
- Timeout: 30 seconds max
- Memory limit: 256MB via cgroup

### Sudoers Hardening

```sudoers
# /etc/sudoers.d/joi
# Hardened configuration for knowledge retrieval

# Reset environment - prevent LD_PRELOAD and similar attacks
Defaults:joi env_reset
Defaults:joi !env_keep
Defaults:joi secure_path="/usr/local/bin:/usr/bin:/bin"

# Audit all sudo usage
Defaults:joi log_output
Defaults:joi logfile=/var/log/joi-sudo.log

# Binary must match exact path - no arguments in sudoers (validated in binary)
joi ALL=(joi-owner-private) NOPASSWD: /usr/local/bin/joi-retrieve
joi ALL=(joi-owner-public) NOPASSWD: /usr/local/bin/joi-retrieve
joi ALL=(joi-partner-private) NOPASSWD: /usr/local/bin/joi-retrieve
joi ALL=(joi-partner-public) NOPASSWD: /usr/local/bin/joi-retrieve
joi ALL=(joi-family) NOPASSWD: /usr/local/bin/joi-retrieve
joi ALL=(joi-critical) NOPASSWD: /usr/local/bin/joi-retrieve
```

**Binary Protection:**
```bash
# Ensure binary is immutable
chown root:root /usr/local/bin/joi-retrieve
chmod 755 /usr/local/bin/joi-retrieve
chattr +i /usr/local/bin/joi-retrieve

# Verify before each update (remove immutable, update, re-add)
lsattr /usr/local/bin/joi-retrieve
# ----i---------e------- /usr/local/bin/joi-retrieve
```

### Subprocess Security Model

All knowledge retrieval runs in sandboxed subprocesses with strict limits.

**Resource Limits (via systemd/cgroup):**
```ini
# /etc/systemd/system/joi-retrieve@.service
[Service]
User=%i
MemoryMax=256M
CPUQuota=50%
TimeoutSec=30
PrivateTmp=true
PrivateNetwork=true          # No network access (only loopback)
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true   # No /proc/sys, /sys modifications
ProtectKernelModules=true    # No module loading
NoNewPrivileges=true
```

**Execution Model:**
```python
def retrieve_knowledge(channel_user: str, query: dict) -> dict:
    """Execute knowledge retrieval in sandboxed subprocess."""

    # 1. Validate and re-serialize query BEFORE subprocess
    #    This ensures joi-retrieve only receives known-safe JSON
    #    Any parser exploit would run as main joi user, not channel user
    if not validate_query(query):
        raise SecurityError("Invalid query format")

    # 2. Re-serialize to canonical JSON (eliminates malformed input)
    try:
        canonical = {
            "type": str(query.get("type", "")),
            "q": str(query.get("q", ""))[:1000],
            "limit": min(int(query.get("limit", 10)), 100),
            "id": str(query.get("id", "")) if "id" in query else None
        }
        query_json = json.dumps(canonical, ensure_ascii=True)
    except (TypeError, ValueError) as e:
        raise SecurityError(f"Query serialization failed: {e}")

    if len(query_json) > 4096:
        raise SecurityError("Query too large")

    # 3. Execute via sudo with timeout
    result = subprocess.run(
        ["sudo", "-u", channel_user, "/usr/local/bin/joi-retrieve",
         query["type"], query_json],
        capture_output=True,
        timeout=30,  # Hard timeout
        env={},      # Empty environment
    )

    # 4. Parse and validate output
    if result.returncode != 0:
        log_retrieval_failure(channel_user, query, result.stderr)
        return {"status": "error", "results": []}

    # 5. Validate output is valid JSON
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError:
        log_security_event("Invalid JSON from joi-retrieve")
        return {"status": "error", "results": []}

    return output
```

**Communication:**
- Subprocess returns JSON to stdout
- Subprocess writes errors to stderr (logged, not returned to LLM)
- No IPC, no shared memory, no sockets

### Sender-to-Channel Validation

The mesh proxy validates Signal sender identity before forwarding to Joi.

**Mesh Proxy Validation Flow:**
```
Signal message arrives (DM or Group - same flow)
    ↓
signal-cli provides cryptographic sender identity (phone number)
    ↓
Mesh looks up phone in /etc/mesh-proxy/identities.yaml
    ↓
Not found? → DROP (unknown sender, logged)
    ↓
Found: Get recipient_id (e.g., "owner", "partner")
    ↓
Determine channel from Signal conversation type:
  - DM → {recipient_id}_private_dm
  - Group → lookup group_id in groups.yaml
    ↓
Include validated channel in API call to Joi:
  X-Validated-Channel: owner_private_dm
  X-Validated-Sender: owner
  X-Signal-Sender-Hash: sha256(phone)  # For logging only
    ↓
Joi trusts X-Validated-Channel header (mesh is trusted)
```

**Critical: Whitelist applies to ALL messages**
- DM from unknown sender → DROP
- Group message from unknown sender → DROP
- Even if attacker adds themselves to Signal group, they're not in identities.yaml → DROP
- Signal group membership is irrelevant; our whitelist is authoritative

**Security Properties:**
- Signal provides cryptographic sender verification
- Mesh validates against known identities (whitelist)
- Channel is determined by mesh (joi cannot override)
- Compromised mesh could forge, but mesh is in trust boundary

**Identity Config (mesh):**
```yaml
# /etc/mesh-proxy/identities.yaml
# Authoritative mapping of phone → recipient

identities:
  "+1555XXXXXXX1":
    recipient_id: owner
    name: "Owner"

  "+1555XXXXXXX2":
    recipient_id: partner
    name: "Partner"

# Groups (Signal group IDs)
# Note: members list is for alerting only. Security comes from identities whitelist above.
# Even if someone joins the Signal group, they can't send messages unless in identities.
groups:
  "base64-group-id-1":
    channel: family_group
    expected_members: [owner, partner]  # For monitoring alerts only

  "base64-group-id-2":
    channel: critical_group
    expected_members: [owner]  # For monitoring alerts only
```

### Signal Group Membership Monitoring

Group membership monitoring is **informational only** - security comes from the sender whitelist (identities.yaml). However, unexpected group changes may indicate:
- Compromised phone adding unauthorized members
- Social engineering attempts
- Accidental group modifications

**Monitoring Strategy (Alerting Only):**
```yaml
# /etc/joi/groups.yaml
# Expected group membership (verified periodically)

groups:
  critical_group:
    signal_group_id: "base64-group-id"
    expected_members:
      - owner
    alert_on_change: true  # Alert if membership changes

  family_group:
    signal_group_id: "base64-group-id-2"
    expected_members:
      - owner
      - partner
    alert_on_change: true
```

**Verification (mesh proxy):**
```python
def check_group_membership():
    """Periodic check (every 6 hours) of Signal group membership."""

    for group_id, config in groups.items():
        # Get current members from signal-cli
        current = signal_cli.get_group_members(group_id)
        expected = set(config["expected_members"])

        added = current - expected
        removed = expected - current

        if added or removed:
            alert_critical(
                f"Group {group_id} membership changed!\n"
                f"Added: {added}\n"
                f"Removed: {removed}"
            )
            # Also log to audit trail
            log_security_event("group_membership_change", group_id, added, removed)
```

**Alerts (Informational - Security Not Dependent on This):**
- Unexpected member added → Alert owner (investigate why)
- Expected member removed → Alert owner (may indicate compromise)
- Group renamed → Warning

**Why This Is Not Security-Critical:**
Unknown senders are dropped at mesh regardless of Signal group membership. This monitoring just helps detect anomalies that may warrant investigation.

### Write Isolation Model

Knowledge retrieval is READ-ONLY. Writes follow a separate path.

**Write Flow:**
```
Joi wants to save knowledge
    ↓
Main joi process (not subprocess) handles write
    ↓
Write path determined by CURRENT channel context
    ↓
Append-only log (cannot modify existing documents)
    ↓
New document indexed for future retrieval
```

**Write Rules:**
- Private DM context → Write to owner/private/ (or partner/private/)
- Group context → Write to group folder
- Critical context → Write to critical/safety/ only
- **Cross-channel writes BLOCKED** - cannot write to folder you can't read

**Implementation:**
```python
def save_knowledge(channel: str, document: dict) -> bool:
    """Save knowledge to appropriate folder based on current channel."""

    # 1. Determine write path from channel
    channel_user = get_channel_user(channel)
    write_path = get_write_path(channel_user)  # Maps user → folder

    # 2. Validate: channel user must have write access to path
    if not can_write(channel_user, write_path):
        log_security_event("Attempted cross-channel write blocked")
        return False

    # 3. Write as main joi user (has write access to all)
    # Document tagged with source channel for audit
    document["_source_channel"] = channel
    document["_timestamp"] = time.time()
    document["_id"] = generate_id()

    # 4. Append to write-ahead log
    append_to_log(write_path, document)

    # 5. Index for retrieval
    index_document(write_path, document)

    return True
```

**Folder Write Permissions:**
```bash
# Main joi user has write access to all folders
# Channel users have READ-ONLY access

# Knowledge folders: owned by main joi, readable by channel users
chown -R joi:joi-owner-readers /var/lib/joi/knowledge/group/owner_public/
chmod -R 750 /var/lib/joi/knowledge/group/owner_public/

# Private folders: only channel user can read, only joi can write
chown joi:joi-owner-private /var/lib/joi/knowledge/owner/private/
chmod 750 /var/lib/joi/knowledge/owner/private/
```

### Directory and File Defaults

**Umask and Setgid Configuration:**
```bash
# All channel directories use setgid to preserve group ownership
chmod g+s /var/lib/joi/knowledge/owner/private/
chmod g+s /var/lib/joi/knowledge/group/owner_public/
chmod g+s /var/lib/joi/knowledge/group/family/
# ... etc for all knowledge directories

# Main joi service runs with restrictive umask
# /etc/systemd/system/joi.service
[Service]
UMask=0027  # New files: 640, new dirs: 750
```

**Expected File Permissions:**
| Location | File Mode | Dir Mode | Owner | Group |
|----------|-----------|----------|-------|-------|
| owner/private/ | 640 | 750 | joi | joi-owner-private |
| group/owner_public/ | 640 | 750 | joi | joi-owner-readers |
| group/family/ | 640 | 750 | joi | joi-family-readers |
| critical/safety/ | 640 | 750 | joi | joi-critical |
| shared/ | 640 | 750 | joi | joi-all-channels |

### Configuration Validation

Automated checks to prevent misconfiguration.

**Validation Script:** `/usr/local/bin/joi-validate-config`

**Script Security (who watches the watchmen?):**
```bash
# Script must be root-owned and immutable
chown root:root /usr/local/bin/joi-validate-config
chmod 755 /usr/local/bin/joi-validate-config
chattr +i /usr/local/bin/joi-validate-config
```

```bash
#!/bin/bash
# Run on startup and after any config change

ERRORS=0

# 0. Self-check: verify this script hasn't been tampered with
if [[ $(stat -c %U:%G "$0") != "root:root" ]]; then
    echo "CRITICAL: Validation script not owned by root:root!"
    exit 1
fi

# 1. Verify joi-retrieve binary
if [[ ! -f /usr/local/bin/joi-retrieve ]]; then
    echo "ERROR: joi-retrieve binary missing"
    ERRORS=$((ERRORS+1))
fi
if [[ $(stat -c %U:%G /usr/local/bin/joi-retrieve) != "root:root" ]]; then
    echo "ERROR: joi-retrieve not owned by root:root"
    ERRORS=$((ERRORS+1))
fi
if ! lsattr /usr/local/bin/joi-retrieve 2>/dev/null | cut -d' ' -f1 | grep -q 'i'; then
    echo "WARNING: joi-retrieve not immutable (chattr +i)"
fi

# 2. Verify critical CANNOT access private folders
for private_group in joi-owner-private joi-partner-private; do
    if id -nG joi-critical | grep -qw "$private_group"; then
        echo "CRITICAL: joi-critical is in $private_group group!"
        ERRORS=$((ERRORS+1))
    fi
done

# 3. Verify channel_users.yaml matches recipients.yaml
# ... (compare expected users exist)

# 4. Verify directory permissions match expected
for dir in /var/lib/joi/knowledge/owner/private \
           /var/lib/joi/knowledge/group/owner_public \
           /var/lib/joi/knowledge/critical/safety; do
    if [[ -d "$dir" ]]; then
        perms=$(stat -c %a "$dir")
        if [[ "$perms" != "750" && "$perms" != "700" ]]; then
            echo "WARNING: $dir has unexpected permissions: $perms"
        fi
    fi
done

# 5. Verify umask and RuntimeDirectory in service file
if ! grep -q "UMask=0027" /etc/systemd/system/joi.service; then
    echo "WARNING: joi.service missing UMask=0027"
fi
if ! grep -q "RuntimeDirectory=joi" /etc/systemd/system/joi.service; then
    echo "WARNING: joi.service missing RuntimeDirectory=joi (needed for lock files)"
fi

# 6. Check group memberships match recipients.yaml
# Compare /etc/group against expected configuration

if [[ $ERRORS -gt 0 ]]; then
    echo "VALIDATION FAILED: $ERRORS critical errors"
    exit 1
fi

echo "Configuration validation passed"
exit 0
```

**Known Limitations (Deferred):**

| Issue | Reason Deferred |
|-------|-----------------|
| **Binary hash verification** | No proper tooling available. Immutable flag (`chattr +i`) provides basic protection. Future: integrate with AIDE or Tripwire for file integrity monitoring. |
| **Sudoers content validation** | Sudoers files require root to modify. If attacker has root, they can bypass any validation. Theoretical risk only - root compromise = game over regardless. |

**Run Validation:**
- On systemd service start (ExecStartPre)
- After any config file change (inotify watch)
- Daily via cron (drift detection)

### Recipient Revocation Procedure

When removing a recipient (e.g., partner leaves household):

**Revocation Script:** `/usr/local/bin/joi-revoke-recipient`
```bash
#!/bin/bash
# Usage: joi-revoke-recipient <recipient_id>
# Example: joi-revoke-recipient partner

set -e  # Exit on error

RECIPIENT=$1
if [[ -z "$RECIPIENT" ]]; then
    echo "Usage: joi-revoke-recipient <recipient_id>"
    exit 1
fi

echo "Revoking recipient: $RECIPIENT"

# 1. Remove from all reader groups
for group in $(grep "joi-.*-readers" /etc/group | cut -d: -f1); do
    gpasswd -d "joi-${RECIPIENT}-private" "$group" 2>/dev/null || true
    gpasswd -d "joi-${RECIPIENT}-public" "$group" 2>/dev/null || true
done

# 2. Remove recipient's own groups
groupdel "joi-${RECIPIENT}-readers" 2>/dev/null || true

# 3. Kill any active processes for these users
pkill -u "joi-${RECIPIENT}-private" 2>/dev/null || true
pkill -u "joi-${RECIPIENT}-public" 2>/dev/null || true

# 4. Lock channel users (don't delete - preserve audit trail)
usermod -L "joi-${RECIPIENT}-private"
usermod -L "joi-${RECIPIENT}-public"

# 5. Update identities config (joi is authoritative source)
# Remove recipient from identities.yaml
sed -i "/^  \".*\":$/,/^  \".*\":$/{/recipient_id: ${RECIPIENT}/,/^  \"/d}" \
    /etc/joi/identities.yaml 2>/dev/null || true
# Also update local config files
sed -i "/^  ${RECIPIENT}:/,/^  [a-z]/d" /etc/joi/recipients.yaml 2>/dev/null || true
sed -i "/${RECIPIENT}_/d" /etc/joi/channel_users.yaml 2>/dev/null || true

# 6. Invalidate joi's in-memory cache (reload config)
systemctl reload joi 2>/dev/null || kill -HUP $(pidof joi-agent) 2>/dev/null
echo "Joi config updated and cache invalidated"

# 7. IMMEDIATE PUSH to mesh (no race window)
echo "Pushing config to mesh..."
CONFIG_JSON=$(cat /etc/joi/identities.yaml | python3 -c "import sys,yaml,json; print(json.dumps(yaml.safe_load(sys.stdin)))")
CONFIG_HASH=$(echo -n "$CONFIG_JSON" | sha256sum | cut -d' ' -f1)
TIMESTAMP=$(date +%s%3N)

PUSH_RESULT=$(curl -s -X POST \
    --cacert /etc/nebula/ca.crt \
    --cert /etc/nebula/joi.crt \
    --key /etc/nebula/joi.key \
    -H "Content-Type: application/json" \
    -d "{\"config\": $CONFIG_JSON, \"hash\": \"$CONFIG_HASH\", \"timestamp\": $TIMESTAMP}" \
    "https://10.42.0.1:8444/config/sync" 2>&1)

if echo "$PUSH_RESULT" | grep -q '"status":"applied"'; then
    echo "Config pushed to mesh successfully - revocation is IMMEDIATE"
else
    echo "WARNING: Config push failed (mesh may be down). Will retry on next heartbeat."
    echo "Push result: $PUSH_RESULT"
    echo "Max delay until sync: 30 seconds (next heartbeat)"
fi

echo ""
echo "OPTIONAL: Archive or delete knowledge folders:"
echo "  /var/lib/joi/knowledge/${RECIPIENT}/"
echo "  /var/lib/joi/data/${RECIPIENT}/"

# 8. Run validation
/usr/local/bin/joi-validate-config
```

**What Happens to Existing Knowledge:**
- Recipient's private knowledge becomes inaccessible (user locked)
- Recipient's public knowledge remains readable by others (if shared)
- To fully purge: delete knowledge folders (optional, manual)

### Config Sync Between Joi and Mesh

Joi is the authoritative source for identity configuration. Mesh receives config via **push from joi** (never pulls).

> **Security Principle:** Mesh NEVER initiates config fetches. A compromised mesh could
> refuse to fetch updates or fetch from an attacker-controlled endpoint. Joi pushes
> config directly; mesh only receives and applies.

**Push-Based Sync (Immediate):**

```
Revocation on joi:
    ↓
joi-revoke-recipient updates /etc/joi/identities.yaml
    ↓
Script triggers immediate config push:
    POST https://mesh:8444/config/sync
    Body: { "config": {...}, "hash": "abc123...", "timestamp": ... }
    Auth: Nebula certificate (joi only)
    ↓
Mesh receives, validates joi certificate, applies config
    ↓
Mesh responds: { "status": "applied", "hash": "abc123..." }
    ↓
Revoked user blocked immediately (no race window)
```

**Mesh Config Endpoint (receives push, never fetches):**
```
POST /config/sync
Body: { "hash": "...", "config": { ... }, "timestamp": ... }
Auth: Nebula certificate - ONLY joi (10.42.0.10) can call this

Response: { "status": "applied", "previous_hash": "...", "new_hash": "..." }
```

**Hash Verification (defense in depth):**

Joi also includes config hash in every outbound request header. If mesh's local hash
doesn't match, it logs a warning (config desync detected) but does NOT fetch - it
waits for joi to push. This catches edge cases like network failures during push.

```yaml
# Every joi → mesh request includes:
X-Config-Hash: sha256(identities.yaml)

# Mesh compares to local hash
# Mismatch → log warning, but NEVER fetch from joi
# Joi is responsible for re-pushing on next heartbeat if push failed
```

**Heartbeat Re-Push:**

During each 30-second health check (joi-challenger), joi also verifies mesh has correct config:
1. Mesh response includes its current config hash
2. If mismatch: joi immediately re-pushes config
3. This handles cases where initial push failed (network, mesh restart)

**What This Prevents:**
- Revocation race window (push is immediate)
- Compromised mesh refusing to sync (mesh cannot initiate, only receive)
- Configuration drift after network failures (heartbeat re-push)

**Revocation Flow (Immediate, No Race Window):**
```
1. Admin runs joi-revoke-recipient on joi VM
2. Script updates /etc/joi/identities.yaml
3. Script invalidates joi cache
4. Script immediately pushes config to mesh (POST /config/sync)
5. Mesh applies config, logs change
6. Revoked user blocked immediately - zero race window
7. If push fails: joi retries on next heartbeat (max 30s delay)
```

### Config Authoritative Sources

> **Single source of truth for each config file:**

| Config File | Authoritative Source | Sync Direction | Notes |
|-------------|---------------------|----------------|-------|
| `identities.yaml` | **joi** | joi → mesh | User identities, pushed on change |
| `recipients.yaml` | joi only | N/A | Per-user limits, joi-local |
| `channel_users.yaml` | joi only | N/A | Channel mapping, joi-local |
| `limits.yaml` | **mesh** | mesh-local | Message limits, mesh-local |
| `uploads.yaml` | **mesh** | mesh-local | Upload security, mesh-local |
| `mesh-baseline.yaml` | joi only | N/A | Known-good hashes, immutable |
| `policy.yaml` | joi only | N/A | Content policy, joi-local |
| `extraction.yaml` | joi only | N/A | Extraction limits, joi-local |

**Mesh-local configs (`limits.yaml`, `uploads.yaml`):**
These are security enforcement configs that live only on mesh. Joi doesn't manage them.
If these need updating, admin SSHs to mesh and edits directly. These rarely change.

**Why not push all config from joi?**
Mesh security configs (`uploads.yaml`, `limits.yaml`) are enforcement boundaries. They should
be conservative defaults that rarely change. Pushing from joi would mean a compromised joi
could weaken mesh security. Mesh-local = mesh enforces even if joi is compromised.

### Config Rollback Mechanism

Bad config can break mesh. Mesh implements automatic rollback on failure.

**Mesh Config Apply with Rollback:**
```bash
# /usr/local/bin/mesh-apply-config
#!/bin/bash
set -e

CONFIG_DIR="/etc/mesh-proxy"
BACKUP_DIR="/var/lib/mesh-proxy/config-backup"
NEW_CONFIG="$1"
CONFIG_NAME=$(basename "$NEW_CONFIG")

# 1. Backup current config
mkdir -p "$BACKUP_DIR"
BACKUP_FILE="$BACKUP_DIR/${CONFIG_NAME}.$(date +%Y%m%d_%H%M%S)"
cp "$CONFIG_DIR/$CONFIG_NAME" "$BACKUP_FILE" 2>/dev/null || true

# 2. Apply new config
cp "$NEW_CONFIG" "$CONFIG_DIR/$CONFIG_NAME"

# 3. Validate config (syntax check)
if ! mesh-proxy --validate-config "$CONFIG_DIR/$CONFIG_NAME"; then
    echo "ERROR: Config validation failed, rolling back"
    cp "$BACKUP_FILE" "$CONFIG_DIR/$CONFIG_NAME"
    exit 1
fi

# 4. Reload service
if ! systemctl reload mesh-proxy; then
    echo "ERROR: Service reload failed, rolling back"
    cp "$BACKUP_FILE" "$CONFIG_DIR/$CONFIG_NAME"
    systemctl restart mesh-proxy
    exit 1
fi

# 5. Health check (give service 5s to stabilize)
sleep 5
if ! curl -sf http://localhost:8444/health > /dev/null; then
    echo "ERROR: Health check failed, rolling back"
    cp "$BACKUP_FILE" "$CONFIG_DIR/$CONFIG_NAME"
    systemctl restart mesh-proxy
    exit 1
fi

echo "Config applied successfully"

# 6. Keep only last 10 backups
ls -t "$BACKUP_DIR/${CONFIG_NAME}."* 2>/dev/null | tail -n +11 | xargs rm -f 2>/dev/null || true
```

**Rollback Triggers:**
| Failure | Action |
|---------|--------|
| YAML syntax error | Immediate rollback, don't reload |
| Service reload fails | Rollback + restart service |
| Health check fails (5s) | Rollback + restart service |
| Joi push rejected | Mesh keeps old config, logs error |

**Manual Rollback:**
```bash
# List available backups
ls -la /var/lib/mesh-proxy/config-backup/

# Restore specific backup
cp /var/lib/mesh-proxy/config-backup/identities.yaml.20240101_120000 /etc/mesh-proxy/identities.yaml
systemctl reload mesh-proxy
```

### Audit Logging

All security-relevant actions are logged.

**auditd Rules:**
```bash
# /etc/audit/rules.d/joi.rules

# Log all access to knowledge directories
-w /var/lib/joi/knowledge/ -p rwxa -k joi-knowledge

# Log all access to data directories
-w /var/lib/joi/data/ -p rwxa -k joi-data

# Log config changes
-w /etc/joi/ -p wa -k joi-config
-w /etc/mesh-proxy/ -p wa -k mesh-config

# Log joi-retrieve execution
-w /usr/local/bin/joi-retrieve -p x -k joi-retrieve

# Log sudo usage (in addition to sudoers log)
-w /var/log/joi-sudo.log -p wa -k joi-sudo
```

**Log Locations:**
| Log | Location | Purpose |
|-----|----------|---------|
| Sudo actions | /var/log/joi-sudo.log | All privilege switches |
| Audit trail | /var/log/audit/audit.log | File access, config changes |
| Application | /var/log/joi/joi.log | Application events |
| Security events | /var/log/joi/security.log | Blocked actions, anomalies |

**Log Retention:**
- Keep 90 days online
- Archive to encrypted backup monthly
- Never delete security logs without explicit approval

### Mount Options

Filesystem hardening for `/var/lib/joi/`:

```bash
# /etc/fstab entry (if separate partition)
/dev/mapper/joi-data /var/lib/joi ext4 defaults,nosuid,nodev,noexec 0 2
```

**Mount Options:**
- `nosuid` - Prevent setuid binaries (no privilege escalation)
- `nodev` - Prevent device files (no /dev access)
- `noexec` - Prevent execution (knowledge is data only)

**If Not Separate Partition:**
```bash
# Bind mount with options
mount --bind /var/lib/joi /var/lib/joi
mount -o remount,nosuid,nodev,noexec /var/lib/joi

# Add to /etc/fstab for persistence
/var/lib/joi /var/lib/joi none bind,nosuid,nodev,noexec 0 0
```

## Secrets Management

### SQLCipher Database Encryption (joi VM)

The memory database (`/var/lib/joi/memory.db`) is encrypted at rest using SQLCipher (AES-256).

#### Prerequisites

```bash
# Install SQLCipher library and CLI
sudo apt install sqlcipher libsqlcipher-dev

# Install Python bindings
pip install sqlcipher3-binary
```

#### Key Management

**Key file location:** `/etc/joi/memory.key`

The encryption key is:
- 64 hex characters (256 bits of entropy)
- Stored in a dedicated file (NOT in environment variables)
- Owned by the service user with mode 600

```bash
# File structure
/etc/joi/
├── memory.key      # 600, joi:joi - encryption key
```

#### Initial Setup (Fresh Install)

```bash
# 1. Generate encryption key (run as root)
cd /opt/joi/execution/joi/scripts
sudo ./generate-memory-key.sh joi    # 'joi' is the service user

# 2. Verify permissions
ls -la /etc/joi/
# drwx------ joi joi /etc/joi/
# -rw------- joi joi /etc/joi/memory.key

# 3. Ensure database directory is writable
sudo chown joi:joi /var/lib/joi

# 4. Start service - database will be created encrypted
sudo systemctl start joi-api
```

#### Migration (Existing Unencrypted Database)

If you have an existing unencrypted database:

```bash
# 1. Stop the service
sudo systemctl stop joi-api

# 2. Generate key if not already done
cd /opt/joi/execution/joi/scripts
sudo ./generate-memory-key.sh joi

# 3. Run migration script
sudo ./migrate-to-encrypted.sh

# 4. Fix ownership (migration runs as root)
sudo chown joi:joi /var/lib/joi/memory.db

# 5. Restart service
sudo systemctl start joi-api
```

The migration script:
- Creates a backup at `/var/lib/joi/memory.db.unencrypted.backup`
- Exports all data to a new encrypted database
- Verifies the new database before replacing

#### File Permissions Summary

| Path | Owner | Mode | Purpose |
|------|-------|------|---------|
| `/etc/joi/` | joi:joi | 700 | Key directory |
| `/etc/joi/memory.key` | joi:joi | 600 | Encryption key |
| `/var/lib/joi/` | joi:joi | 750 | Database directory |
| `/var/lib/joi/memory.db` | joi:joi | 600 | Encrypted database |
| `/var/lib/joi/nonces.db` | joi:joi | 600 | Nonce store (unencrypted, ephemeral) |

#### Verification

Check logs for encryption status:
```bash
journalctl -u joi-api | grep -i "memory store\|sqlcipher"
```

Expected output:
```
SQLCipher encryption enabled
Memory store initialized: /var/lib/joi/memory.db (encrypted)
```

#### Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `Permission denied: /etc/joi/memory.key` | Wrong ownership | `sudo chown joi:joi /etc/joi /etc/joi/memory.key` |
| `attempt to write a readonly database` | Wrong DB ownership | `sudo chown joi:joi /var/lib/joi/memory.db` |
| `file is not a database` | Unencrypted DB with key present | Run migration script |
| `file is not a database` (after migration) | Key format mismatch | Re-run migration with fresh key |

#### Backup Warning

**⚠️ CRITICAL: Back up the encryption key securely!**

Without `/etc/joi/memory.key`, the database cannot be decrypted. Store a copy:
- In a password manager
- On encrypted removable media
- NOT in the same location as the database

#### Key Rotation

SQLCipher supports re-keying:
```bash
# Stop service first
sudo systemctl stop joi-api

# Use sqlcipher CLI
sqlcipher /var/lib/joi/memory.db
> PRAGMA key = 'old_key';
> PRAGMA rekey = 'new_key';
> .quit

# Update key file
echo "new_key" | sudo tee /etc/joi/memory.key
sudo chown joi:joi /etc/joi/memory.key
sudo chmod 600 /etc/joi/memory.key

# Restart
sudo systemctl start joi-api
```

#### Production Hardening (Future)

| Option | Pros | Cons |
|--------|------|------|
| **Current (key file)** | Simple, works | Key on disk |
| **Manual unlock on boot** | Key never on disk | Manual intervention on reboot |
| **systemd-creds + TPM** | Encrypted at rest | Requires TPM passthrough |
| **HashiCorp Vault** | Audit logging, rotation | Infrastructure complexity |

For PoC, the key file approach is acceptable since the joi VM disk is LUKS-encrypted (defense in depth).

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

**If node key is compromised:**
1. Add compromised cert fingerprint to blocklist on all nodes
2. Generate new cert for that node with `nebula-cert sign`
3. Deploy new cert to affected node
4. Restart Nebula on all nodes to reload blocklist

**If CA key (`ca.key`) is compromised - FULL RECOVERY REQUIRED:**

> **This is a critical incident.** An attacker with the CA key can sign rogue
> certificates and join the mesh as any node. Full replacement required.

```bash
# NEBULA CA COMPROMISE RECOVERY PROCEDURE
# Estimated time: 30-60 minutes (requires Proxmox console access to all VMs)

# 1. IMMEDIATE: Shut down all Nebula-connected VMs
#    This prevents attacker from using rogue certs
proxmox-console: shutdown joi mesh openhab

# 2. On air-gapped machine: Generate NEW CA
nebula-cert ca -name "homelab.example-v2"
# Output: ca.crt (new), ca.key (new)

# 3. Sign new certs for ALL nodes
nebula-cert sign -ca-crt ca.crt -ca-key ca.key \
    -name "mesh" -ip "10.42.0.1/24" -groups "gateway"
nebula-cert sign -ca-crt ca.crt -ca-key ca.key \
    -name "joi" -ip "10.42.0.10/24" -groups "ai"
nebula-cert sign -ca-crt ca.crt -ca-key ca.key \
    -name "openhab" -ip "10.42.0.20/24" -groups "openhab"

# 4. Deploy new certs to each VM (via Proxmox console, NOT network)
#    For each VM:
#    - Copy new ca.crt, <node>.crt, <node>.key
#    - Replace /etc/nebula/ca.crt, host.crt, host.key
#    - Clear any cached sessions: rm /var/lib/nebula/*

# 5. Move NEW ca.key to offline storage
shred -u ca.key  # Or move to encrypted USB

# 6. Restart VMs and verify mesh connectivity
proxmox-console: start mesh  # Lighthouse first
proxmox-console: start joi openhab
# Verify: ping 10.42.0.10 from mesh

# 7. Regenerate HMAC shared secret (may also be compromised)
#    See "Challenge-Response Shared Secret" section

# 8. Review logs for attacker activity during compromise window
```

**Post-Recovery:**
- Rotate all secrets that traversed the mesh during compromise window
- Review audit logs for unauthorized access
- Consider how CA key was compromised and prevent recurrence

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

### Host Firewall Rules (UFW)

Both VMs use UFW with deny-by-default policy. Only explicitly allowed traffic passes.

**Joi VM:**
```bash
sudo ufw reset
sudo ufw default deny incoming
sudo ufw default deny outgoing

# Docker bridge (for Ollama container)
sudo ufw allow in on docker0
sudo ufw allow out on docker0

# SSH from gateway (adjust IP as needed)
sudo ufw allow from 172.22.22.4 to any port 22

# NTP from internal NTP server
sudo ufw allow from 172.22.22.3 to any port 123/udp
sudo ufw allow out to 172.22.22.3 port 123/udp

# Nebula UDP (vmbr1)
sudo ufw allow 4242/udp
sudo ufw allow out 4242/udp

# Nebula overlay - mesh proxy communication
sudo ufw allow from 10.42.0.1 to any port 8443
sudo ufw allow out to 10.42.0.1 port 8444

# Package management (apt/pip) - can be removed after setup
sudo ufw allow out to any port 53
sudo ufw allow out to any port 80
sudo ufw allow out to any port 443

sudo ufw enable
```

**Mesh VM:**
```bash
sudo ufw reset
sudo ufw default deny incoming
sudo ufw default deny outgoing

# Loopback (internal services)
sudo ufw allow in on lo
sudo ufw allow out on lo

# SSH from gateway (adjust IP as needed)
sudo ufw allow from 172.22.22.4 to any port 22

# NTP from internal NTP server
sudo ufw allow from 172.22.22.3 to any port 123/udp
sudo ufw allow out to 172.22.22.3 port 123/udp

# Nebula UDP
sudo ufw allow 4242/udp
sudo ufw allow out 4242/udp

# Nebula overlay - joi API communication
sudo ufw allow from 10.42.0.10 to any port 8444
sudo ufw allow out to 10.42.0.10 port 8443

# WAN - Signal (HTTPS + DNS)
sudo ufw allow out to any port 443
sudo ufw allow out to any port 53

sudo ufw enable
```

**Notes:**
- Docker uses `docker0` bridge (172.17.0.0/16), not loopback. Allowing `lo` does NOT enable Docker networking.
- Gateway IP (172.22.22.4) and NTP IP (172.22.22.3) are examples - adjust for your setup.
- Nebula overlay IPs: mesh=10.42.0.1, joi=10.42.0.10
- Port 80/443 on Joi is for apt/pip - can be removed after initial setup for full air-gap.

### Time Synchronization (NTP)

All VMs on vmbr1 must maintain synchronized clocks for API timestamp validation (5-minute tolerance).

**NTP Source:** Dedicated NTP VM on vmbr1 network (e.g., `172.22.22.3`)
- NTP VM syncs to external NTP (pool.ntp.org) via WAN
- All vmbr1 clients sync to NTP VM (including gateway, mesh, joi, openhab)
- This keeps joi air-gapped (no direct internet NTP access)

**Configuration (all VMs on vmbr1):**
```bash
# /etc/chrony/chrony.conf (or /etc/ntp.conf)
server 172.22.22.3 iburst prefer  # NTP VM IP (adjust as needed)
# No pool.ntp.org - isolated network
```

**VMs requiring sync:**
| VM | NTP Client | Notes |
|----|------------|-------|
| gateway | Yes | Admin node should use same trusted internal time source |
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

> **NTP Attack Vector:**
> If an attacker compromises the NTP VM (time source), they could:
> - Set clocks far in future → all cached nonces appear valid for replay
> - Set clocks far in past → all timestamps rejected, denial of service
> - Slowly drift clocks → gradual timestamp manipulation
>
> **Mitigations:**
> 1. **Rate limit clock changes:** chrony's `makestep` limits sudden jumps
>    ```bash
>    # /etc/chrony/chrony.conf
>    makestep 1 3    # Allow 1s step only in first 3 updates after boot
>    maxchange 100 1 1  # Log and exit if offset > 100s after 1 update
>    ```
> 2. **Monotonic nonces:** Use strictly increasing nonce (not just random) so replays
>    of old challenges are rejected even if clock is manipulated
> 3. **Cross-check mesh/joi clocks:** During heartbeat, compare joi and mesh timestamps.
>    If delta > 60 seconds, alert (possible NTP compromise or network issue)
> 4. **NTP VM is trusted infrastructure:** If compromised, treat as network integrity incident.

## Open Questions / Next Decisions
- ✓ RESOLVED: mesh ↔ joi auth uses Nebula certificates (annual rotation recommended).
- ✓ RESOLVED: Nebula lighthouse runs on mesh.homelab.example.
- Finalize Nebula IP ranges (currently planned: 10.42.0.0/24).

## Post-PoC Improvements

Security improvements deferred from PoC phase:

| Improvement | Current State | Target State | Value |
|-------------|---------------|--------------|-------|
| **Physical devices** | VMs on Proxmox (Proxmox = root of trust) | Dedicated hardware for mesh and joi (removes virtualization from trust chain) | High - Proxmox compromise no longer game over |
| **Kernel-enforced write isolation** | Writes via main joi process (app-level checks) | Separate `joi-write` binary per channel via sudo, mirroring read isolation | High - eliminates write path bugs |
| **Centralized logging** | All logs local to joi VM | Dedicated log server (not joi, not mesh) receives forwarded logs | Medium - tamper evidence |
| **Binary hash verification** | Immutable flag only | AIDE/Tripwire integration for cryptographic verification | Low - detects maintenance tampering |

### Supply Chain Security

> **Threat:** Compromised dependencies (Python packages, npm modules, system packages)
> could introduce backdoors, credential theft, or remote access into joi or mesh VMs.

**Current Dependencies (audit quarterly):**

| Component | VM | Package Source | Risk Level |
|-----------|-----|----------------|------------|
| Python + pip packages | joi | PyPI | High - large attack surface |
| Ollama | joi | GitHub releases | Medium - single binary |
| signal-cli | mesh | GitHub releases | High - handles credentials |
| Nebula | all | GitHub releases | Medium - critical but small |
| System packages | all | Ubuntu/Rocky repos | Low - distro-signed |

**Mitigation Strategy:**

1. **Pin versions:** Lock all dependency versions in requirements.txt / package-lock.json
   ```bash
   # Example: requirements.txt with hashes
   requests==2.31.0 --hash=sha256:abc123...
   ```

2. **Verify signatures:** For GitHub releases, verify GPG signatures where available
   ```bash
   # signal-cli releases are signed
   gpg --verify signal-cli-X.Y.Z.tar.gz.asc
   ```

3. **Minimal dependencies:** Prefer standard library over third-party packages
   - Use `subprocess` not third-party process managers
   - Use `json` not third-party JSON libraries
   - Use `sqlite3` (built-in) for simple storage

4. **Airgap updates:** Download updates on separate machine, verify, transfer via USB
   ```bash
   # On trusted machine with internet
   pip download -d ./packages -r requirements.txt
   sha256sum ./packages/* > checksums.txt

   # Transfer to joi VM via Proxmox console upload
   # Verify checksums match
   pip install --no-index --find-links=./packages -r requirements.txt
   ```

5. **Dependency scanning:** Run `pip-audit` or `safety` before updates (on trusted machine)

**Known High-Risk Dependencies:**
- `pdftotext`/`pdfplumber`: Disabled in PoC (CVE risk)
- `signal-cli`: JVM + many transitive deps (monitor for CVEs)
- Any ML/AI libraries: Large, complex, many native extensions

**Update Schedule:**
- Security patches: Apply within 7 days of disclosure
- Minor versions: Monthly review, apply if needed
- Major versions: Quarterly review, test before deployment

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
