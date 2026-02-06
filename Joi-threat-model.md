# Joi Threat Model

This document identifies threats to the Joi system and proposes mitigations. It should be reviewed and updated as the architecture evolves.

## 1. Assets

| Asset | Value | Confidentiality | Integrity | Availability |
|-------|-------|-----------------|-----------|--------------|
| Signal credentials (proxy) | Critical | High - impersonation | High - message tampering | Medium |
| Owner's phone number | High | High - privacy | N/A | N/A |
| Conversation history | High | High - personal data | Medium | Low |
| Long-term memory store | High | High - behavioral patterns | High - affects agent behavior | Medium |
| LLM model weights | Medium | Low - public model | High - backdoor risk | High |
| openhab event stream | Medium | Medium - presence patterns | High - false events | Medium |
| HMAC keys | Critical | High - auth bypass | High | High |
| Proxmox host hardware | Medium | Physical access = full compromise | High | High |
| GPU (eGPU enclosure) | Low | Physical access to GPU | Medium | Medium |

## 2. Threat Actors

| Actor | Motivation | Capability | Access |
|-------|------------|------------|--------|
| **Remote attacker** | Data theft, botnet, curiosity | High technical skill | Internet only (blocked by design) |
| **LAN attacker** | Lateral movement, data theft | Medium-high skill | Same network segment |
| **Compromised IoT device** | Pivot point | Low (automated) | LAN, possibly openhab |
| **Malicious household member** | Surveillance, mischief | Low-medium skill | Physical + LAN |
| **Physical intruder** | Hardware theft | Low skill | Physical access |
| **Compromised openhab** | Inject false events | Medium (automated) | Direct connection to Joi |
| **Supply chain** | Backdoor, cryptomining | High skill | Model weights, dependencies |

## 3. Attack Surfaces

```
┌─────────────────────────────────────────────────────────────────┐
│                         INTERNET                                │
│                            │                                    │
│                      [BLOCKED]                                  │
│                            │                                    │
├────────────────────────────┼────────────────────────────────────┤
│            mesh.homelab.example (Ubuntu 24 LTS)                 │
│  ┌──────────────┐    ┌─────────────┐    ┌─────────────┐        │
│  │ Signal Bot   │◄──►│ Webhook API │◄──►│ Nebula      │        │
│  │ (signal-cli) │    │ (HTTPS)     │    │ Lighthouse  │        │
│  └──────────────┘    └─────────────┘    └─────────────┘        │
│         │                   │                   │               │
├─────────┼───────────────────┼───────────────────┼───────────────┤
│         │           Nebula  │                   │               │
│         ▼            Mesh   ▼                   ▼               │
│  ┌─────────────────────────────────────────────────────┐       │
│  │        ASUS NUC 13 Pro (Proxmox Host)               │       │
│  │  ┌───────────────────────────────────────────────┐  │       │
│  │  │              Joi VM (GPU Passthrough)         │  │       │
│  │  │  ┌──────────┐ ┌────────────┐ ┌─────────────┐  │  │       │
│  │  │  │ LLM Core │ │ Policy     │ │ Memory Store│  │  │       │
│  │  │  │ (RTX3060)│ │ Engine     │ │ (SQLCipher) │  │  │       │
│  │  │  └──────────┘ └────────────┘ └─────────────┘  │  │       │
│  │  │       ▲                                       │  │       │
│  │  │       │        ┌─────────────┐                │  │       │
│  │  │       └────────│ Event       │◄── openhab    │  │       │
│  │  │                │ Normalizer  │    (push/pull) │  │       │
│  │  │                └─────────────┘                │  │       │
│  │  └───────────────────────────────────────────────┘  │       │
│  │                         │ TB4                       │       │
│  │                    [eGPU: RTX 3060]                 │       │
│  └─────────────────────────────────────────────────────┘       │
│                            ▲                                    │
│                    [Proxmox Console / SSH]                      │
└─────────────────────────────────────────────────────────────────┘
```

### Attack Surface Summary

| Surface | Exposure | Protocol | Authentication |
|---------|----------|----------|----------------|
| mesh ↔ joi tunnel | Nebula overlay | Nebula (UDP 4242) | Nebula certificates |
| mesh webhook (inbound to joi) | Nebula | HTTPS over Nebula | Nebula cert |
| joi webhook (outbound to mesh) | Nebula | HTTPS over Nebula | Nebula cert |
| openhab event stream | Nebula | HTTPS over Nebula | Nebula cert (openhab is mesh node) |
| Local terminal | Physical/LAN | TTY/SSH | Key-based SSH |
| Proxmox host disk | Physical | Filesystem | LUKS encryption |
| Signal servers | Internet | Signal Protocol | Signal credentials |

## 4. Threats by Component

### 4.1 Signal Proxy (mesh.homelab.example)

| ID | Threat | Impact | Likelihood | Risk |
|----|--------|--------|------------|------|
| P1 | Proxy host compromised via unpatched vulnerability | Full control of Signal identity, message interception | Medium | **Critical** |
| P2 | Signal credentials stolen from disk | Attacker impersonates owner | Medium | **Critical** |
| P3 | Webhook endpoint exploited (injection, overflow) | Code execution on proxy | Low | **High** |
| P4 | Replay attack on Joi→Proxy messages | Duplicate messages sent | Low | Medium |
| P5 | HMAC key extracted from RPi or proxy | Auth bypass | Low | **High** |
| P6 | Rate limit bypass floods Signal | Account banned, DoS | Low | Medium |
| P7 | Attacker on LAN spoofs RPi IP | Unauthorized message sends | Low | **High** |
| P8 | signal-cli process args expose credentials | Credential theft via /proc | Medium | **High** |
| P9 | signal-cli stdout/stderr leaks data | Information disclosure | Low | Medium |
| P10 | Java runtime vulnerabilities | Code execution | Low | **High** |
| P11 | signal-cli version expires (3-month limit) | Complete loss of Signal | Medium | **High** |

**Mitigations:**
- P1: Dedicated minimal OS, automatic security updates, no other services
- P2: Credentials in `/var/lib/signal-cli/data/` with 0700 permissions, owned by dedicated `signal` user. Never pass credentials via command arguments.
- P3: Input validation, memory-safe language (Go, Rust), fuzz testing
- P4: Include nonce/sequence number in HMAC, reject duplicates
- P5: Rotate keys periodically, store in secure enclave if available
- P6: Strict rate limits with exponential backoff
- P7: mTLS instead of IP allowlist, or firewall + MAC binding
- P8: Use signal-cli **daemon mode** (socket IPC), never per-command invocation
- P9: Redirect stdout/stderr to log file with restricted permissions
- P10: Keep JRE updated, run with minimal JVM permissions, disable heap dumps
- P11: Quarterly signal-cli update schedule in operational runbook

#### 4.1.1 signal-cli Specific Considerations

> **Note:** signald is deprecated (no longer works with Signal servers). signal-cli is the only option.

signal-cli operates differently from the now-defunct signald:

| Aspect | Security Implication | Mitigation |
|--------|---------------------|------------|
| Per-invocation model | More credential access events | **Use daemon mode (socket)** |
| Java runtime | JVM attack surface | Keep JRE updated, minimal permissions |
| Linked device | Appears in Signal device list | Monitor linked devices, unlink on compromise |
| Native library | libsignal-client vulnerabilities | Pin versions, monitor advisories |
| 3-month expiry | Signal enforces client version | Quarterly update schedule |

**Daemon mode is critical** - running signal-cli per-command exposes credentials via `/proc/[pid]/cmdline` (world-readable on Linux). Daemon mode with Unix socket achieves similar isolation to signald.

### 4.2 mesh ↔ joi Communication

| ID | Threat | Impact | Likelihood | Risk | Status |
|----|--------|--------|------------|------|--------|
| J1 | No authentication on inbound messages | Attacker injects commands as "owner" | ~~High~~ Low | ~~**Critical**~~ Low | **MITIGATED** |
| J2 | Message tampering in transit | Modified instructions to Joi | ~~Medium~~ Low | ~~**High**~~ Low | **MITIGATED** |
| J3 | Replay of old messages | Confusion, repeated actions | Medium | Medium | Partially mitigated |
| J4 | Flood of messages (DoS) | Joi overwhelmed, memory exhausted | Medium | Medium | |

**Mitigations:**
- J1: ✓ **RESOLVED** - Nebula mesh VPN provides certificate-based mutual authentication
- J2: ✓ **RESOLVED** - Nebula provides encrypted transport with authenticated endpoints
- J3: Sequence numbers, timestamp validation, deduplication window (still needed at app layer)
- J4: Rate limiting, queue depth limits, backpressure

### 4.3 openhab Integration

| ID | Threat | Impact | Likelihood | Risk |
|----|--------|--------|------------|------|
| O1 | Compromised openhab sends malicious events | Prompt injection, false context | Medium | **High** |
| O2 | Event flood overwhelms Joi | DoS, memory exhaustion | Medium | Medium |
| O3 | Joi attempts unauthorized writes | Unintended home automation actions | Low | **High** |
| O4 | Eavesdropping on event stream | Presence patterns leaked | Low | Medium |
| O5 | Attacker spoofs openhab on LAN | Inject arbitrary events | Low | ~~**High**~~ **MITIGATED** |

**Mitigations:**
- O1: Sanitize all event data before LLM, strict schema validation, content length limits
- O2: Rate limiting per event type, queue with bounded size, drop oldest
- O3: Read-only API credentials, policy engine blocks writes, openhab rejects writes
- O4: Use TLS for openhab connection, even on LAN
- O5: ✓ **RESOLVED** - openhab joins Nebula mesh, certificate-based auth

### 4.4 LLM and Agent Behavior

| ID | Threat | Impact | Likelihood | Risk |
|----|--------|--------|------------|------|
| L1 | Prompt injection via Signal message | Joi executes attacker instructions | High | **Critical** |
| L2 | Prompt injection via openhab event | Joi manipulated by crafted device names | Medium | **High** |
| L3 | Hallucination triggers unwanted action | Spam messages, false alerts | High | Medium |
| L4 | Agent loop runaway | Infinite messages, resource exhaustion | Medium | Medium |
| L5 | Model weights backdoored | Persistent compromise | Low | **Critical** |
| L7 | Untrusted model provenance | Supply chain attack | Medium | **High** |
| L6 | Context window overflow | Degraded responses, crashes | Medium | Low |

**Mitigations:**
- L1: Strict input framing, system prompt hardening, output validation
- L2: Never pass raw event data to LLM, use structured templates
- L3: Output sanity checks, rate limits, confidence thresholds
- L4: Circuit breaker (max actions per time window), watchdog process
- L5: Verify model checksums, use trusted sources only
- L7: **POLICY** - Only use models from trusted Western sources (Meta, Google, Microsoft). Chinese models (Qwen, DeepSeek) are BANNED.
- L6: Sliding context window, summarization, hard limits

### 4.5 Memory Store

| ID | Threat | Impact | Likelihood | Risk |
|----|--------|--------|------------|------|
| M1 | Memory store read by attacker (physical) | Conversation history leaked | Medium | **High** |
| M2 | Memory store tampered | Agent behavior manipulated | Low | **High** |
| M3 | Memory poisoning via crafted inputs | Long-term behavioral drift | Medium | Medium |
| M4 | No retention limits | Storage exhaustion, old data exposure | Medium | Medium |

**Mitigations:**
- M1: Encrypt database at rest (SQLCipher, LUKS partition)
- M2: Integrity checks (checksums, append-only log)
- M3: Memory validation, anomaly detection, periodic review
- M4: Retention policies, automatic pruning, size limits

### 4.6 Physical and Local Access

| ID | Threat | Impact | Likelihood | Risk |
|----|--------|--------|------------|------|
| PH1 | Proxmox host stolen | Full data access, key extraction | Low | **High** |
| PH2 | VM disk image copied | Offline attack on data/keys | Low | **High** |
| PH3 | Proxmox console unauthorized access | Direct control of Joi VM | Medium | **High** |
| PH4 | USB attack on host (malicious device) | Code execution | Low | Medium |
| PH5 | Power supply tampering / eGPU disconnect | DoS, hardware damage | Low | Low |
| PH6 | eGPU enclosure theft | GPU loss, potential data on VRAM | Low | Low |

**Mitigations:**
- PH1, PH2: LUKS encryption on Proxmox host, encrypted VM disk images
- PH3: Strong authentication (key-based SSH), Proxmox 2FA, disable password login
- PH4: Disable unused USB ports in BIOS/firmware
- PH5: UPS, tamper detection (optional)
- PH6: Physical security; VRAM is volatile (no persistent data risk)

### 4.7 Network (LAN)

| ID | Threat | Impact | Likelihood | Risk |
|----|--------|--------|------------|------|
| N1 | ARP spoofing / MITM on LAN | Intercept or modify traffic | Medium | **High** |
| N2 | Compromised IoT device attacks RPi | Lateral movement | Medium | Medium |
| N3 | DNS poisoning | Misdirected connections | Low | Medium |
| N4 | Rogue DHCP | Traffic redirection | Low | Medium |

**Mitigations:**
- N1: TLS everywhere, mTLS for critical connections, static ARP (optional)
- N2: VLAN isolation, firewall rules (only allow openhab + Proxy)
- N3: Static DNS or DNS-over-TLS, verify certificates
- N4: Static IP configuration for RPi

## 5. Risk Matrix

```
            │ Low Impact │ Medium Impact │ High Impact │ Critical Impact
────────────┼────────────┼───────────────┼─────────────┼─────────────────
High        │            │ L3            │ L1, L2      │
Likelihood  │            │               │             │
────────────┼────────────┼───────────────┼─────────────┼─────────────────
Medium      │            │ P4, O2, L4    │ P1, P8, P11,│ J1
Likelihood  │            │ M3, M4        │ J2, O1, O5, │
            │            │               │ N1, N2, M1, │
            │            │               │ PH3         │
────────────┼────────────┼───────────────┼─────────────┼─────────────────
Low         │ L6, PH5    │ P6, P9, J3,   │ P3, P5, P7  │ L5
Likelihood  │            │ O4, N3, N4,   │ P10, O3, M2 │
            │            │ PH4           │ PH1, PH2    │
```

> **Note:** P8 (credential exposure via process args) and P11 (signal-cli version expiry) are Medium likelihood/High impact because they are mitigated by using daemon mode and following the update schedule respectively.

## 6. Priority Mitigations

Based on risk assessment, implement in this order:

### Must Have (Before any deployment)

1. ~~**Proxy → Joi authentication** (J1)~~ ✓ **RESOLVED** - Nebula mesh VPN
2. ~~**Prompt injection defenses** (L1, L2)~~ ✓ **RESOLVED** - See prompt-injection-defenses.md
3. ~~**Signal credential protection** (P2)~~ ✓ **RESOLVED** - LUKS + file perms (documented in Joi-architecture-v2.md)
4. ~~**Read-only enforcement** (O3)~~ ✓ **RESOLVED** - Policy engine, see policy-engine.md
5. ~~**Rate limiting on agent actions** (L3, L4)~~ ✓ **RESOLVED** - 60/hr direct, unlimited critical

### Should Have (Before production use)

6. ~~**Disk encryption** (PH1, PH2, M1)~~ ✓ **RESOLVED** - LUKS on host + SQLCipher for DB
7. ~~**Network segmentation** (N1, N2)~~ ✓ **RESOLVED** - Isolated vmbr1 /29 network
8. ~~**TLS on all LAN connections** (N1, O4)~~ ✓ **RESOLVED** - All traffic over Nebula (encrypted)
9. ~~**openhab authentication** (O5)~~ ✓ **RESOLVED** - openhab joins Nebula mesh
10. **Local terminal hardening** (PH3) - Key-based auth only

### Nice to Have (Ongoing hardening)

11. **mTLS replacing HMAC + IP allowlist** (P7)
12. **Hardware security module for keys** (P5)
13. **Anomaly detection on agent behavior** (M3)
14. **Automated security updates** (P1)
15. **Monitoring and alerting** (all)

## 7. Implementation Decisions (Resolved Questions)

### 7.1 Proxy Compromise Recovery
**Q:** What is the recovery procedure if the proxy is compromised?

**A:**
1. Immediately shut down mesh VM (Proxmox console or SSH from trusted host)
2. **Unlink compromised signal-cli device** from primary phone (Signal app → Settings → Linked Devices)
3. Revoke mesh Nebula certificate (requires CA key from offline storage)
4. Re-register Signal number on fresh mesh VM install (new linked device)
5. Issue new Nebula certificate for mesh
6. Update joi to trust new mesh certificate
7. Notify contacts that previous messages may be compromised

> **Note:** signal-cli operates as a linked device. Step 2 is critical - it immediately revokes the compromised instance's ability to send/receive messages, even before you rebuild the VM.

### 7.2 Prompt Injection Detection
**Q:** How do we detect prompt injection attempts?

**A:** Multi-layer detection with logging and alerting:
- **Input layer:** Log all inputs that trigger sanitization (length truncation, pattern removal)
- **Output layer:** Log all outputs that fail validation (blocked patterns, URLs)
- **Alert threshold:** More than 5 sanitization triggers per hour from same source → alert
- **Metrics:** `prompt_injection_attempts_total{layer, source}` counter
- See `prompt-injection-defenses.md` for full defense architecture

### 7.3 Emergency Stop Command
**Q:** Should the owner have an "emergency stop" command that bypasses the agent?

**A:** Yes - primary method is **shutting down mesh VM via Proxmox mobile app**.

| Level | Method | Effect |
|-------|--------|--------|
| **Primary** | Proxmox mobile → stop mesh VM | Cuts all communication. Joi isolated but running (can investigate). |
| Secondary | Proxmox → stop joi VM | Full shutdown of AI. |
| Last resort | Physical power off NUC | Everything stops. |

**Why mesh VM shutdown is preferred:**
- Works even if Joi is in a loop ignoring commands
- No code required - just VM management
- Joi stays running for post-incident investigation
- Simple and reliable

**Optional convenience:** Joi could recognize "STOP" via Signal and enter safe mode, but this is not relied upon for actual emergencies. If Joi is misbehaving badly enough to need stopping, don't trust it to process commands correctly.

### 7.4 Acceptable Latency
**Q:** What is acceptable latency for Proxy → Joi message delivery?

**A:**
- **Target:** < 2 seconds mesh → joi delivery
- **Maximum:** 15 seconds before timeout
- **User-perceived response time:** < 30 seconds total (including LLM inference)
- Protocol: HTTPS over Nebula is sufficient for these latency requirements

### 7.5 External Audit Logs
**Q:** Do we need audit logs accessible externally?

**A:** For PoC: No. Logs stay local on each VM.

For production (future):
- mesh VM could push anonymized log summaries to external syslog (since mesh has WAN)
- joi logs exported via mesh on schedule (daily digest)
- Critical security events (policy denials, rate limit hits) forwarded immediately via mesh

### 7.6 Household Threat Model
**Q:** What is the threat model for the household?

**A:**
| Person | Trust Level | Access | Mitigation |
|--------|-------------|--------|------------|
| Owner | Full trust | Signal, Proxmox, physical | N/A |
| Partner | Trusted adult | May see owner's phone | Acceptable - no secrets in Joi responses |
| Children | Untrusted | Could send Signal from owner's phone | Owner responsibility to secure phone; Joi should not execute dangerous commands regardless |
| Guests | Untrusted | No access expected | No guest WiFi access to vmbr1; physical NUC access = game over anyway |

**Key assumption:** Only owner's phone number is in allowlist. If someone else sends from owner's phone, Joi treats it as owner. This is acceptable for home use - owner is responsible for phone security.

## 8. Review Schedule

This threat model should be reviewed:
- Before initial deployment
- After any architecture changes
- Quarterly during active development
- Annually during maintenance phase

---

*Document version: 1.2*
*Last updated: 2026-02-04*
*Based on: Joi-architecture-v2.md (all VMs on Nebula mesh)*
