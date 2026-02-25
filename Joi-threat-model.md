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
| System Channel sources | Medium | Medium - operational data | High - false events/writes | Medium |
| LLM Service VMs | Medium | Low - isolated compute | High - malicious output | Medium |
| HMAC keys | Critical | High - auth bypass | High | High |
| Proxmox host hardware | Medium | Physical access = full compromise | High | High |
| GPU (eGPU enclosure) | Low | Physical access to GPU | Medium | Medium |
| Generated images/content | Low | Medium - could contain private info | Low | Low |

### New Assets (System Channel & LLM Services)

| Asset | Description | Risk if Compromised |
|-------|-------------|---------------------|
| System Channel sources | openhab, Zabbix, actuators, calendar | False events, unauthorized writes |
| imagegen VM | Image generation service | Inappropriate content, resource abuse |
| websearch VM | Internet search agent | Fetch malicious content, exfiltration vector |
| tts/stt VMs | Speech services | Audio manipulation |
| codeexec VM | Sandboxed code execution | Sandbox escape, resource abuse |

## 2. Threat Actors

| Actor | Motivation | Capability | Access |
|-------|------------|------------|--------|
| **Remote attacker** | Data theft, botnet, curiosity | High technical skill | Internet only (blocked by design) |
| **LAN attacker** | Lateral movement, data theft | Medium-high skill | Same network segment |
| **Compromised IoT device** | Pivot point | Low (automated) | LAN, possibly System Channel |
| **Malicious household member** | Surveillance, mischief | Low-medium skill | Physical + LAN |
| **Physical intruder** | Hardware theft | Low skill | Physical access |
| **Compromised System Channel source** | Inject false events, intercept writes | Medium (automated) | Direct connection to Joi |
| **Compromised LLM Service VM** | Lateral movement, malicious output | Medium | Nebula mesh only |
| **Supply chain** | Backdoor, cryptomining | High skill | Model weights, dependencies |
| **Malicious web content** | Prompt injection via websearch | Medium | websearch VM only |

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
│  │  │  ┌────────────────────────────────────────┐   │  │       │
│  │  │  │         PROTECTION LAYER               │   │  │       │
│  │  │  │  (rate limits, circuit breakers, etc.) │   │  │       │
│  │  │  └────────────────────────────────────────┘   │  │       │
│  │  │  ┌────────────────────────────────────────┐   │  │       │
│  │  │  │ LLM Agent + Policy Engine + Memory     │   │  │       │
│  │  │  └──────────────────┬─────────────────────┘   │  │       │
│  │  │         ┌───────────┴───────────┐             │  │       │
│  │  │         ▼                       ▼             │  │       │
│  │  │  Interactive Channel     System Channel       │  │       │
│  │  │  (Signal ↔ human)        (machine-to-machine) │  │       │
│  │  └───────────────────────────────────────────────┘  │       │
│  │                         │ TB4                       │       │
│  │                    [eGPU: RTX 3060]                 │       │
│  └─────────────────────────────────────────────────────┘       │
│                            │ Nebula mesh                        │
│          ┌─────────────────┼─────────────────┐                 │
│          ▼                 ▼                 ▼                 │
│   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐        │
│   │ openhab      │  │ Zabbix       │  │ LLM Service  │        │
│   │ [read]       │  │ [read-write] │  │ VMs          │        │
│   └──────────────┘  └──────────────┘  └──────────────┘        │
│                                              │                 │
│                            ┌─────────────────┼─────────────┐   │
│                            ▼                 ▼             ▼   │
│                      ┌──────────┐     ┌──────────┐  ┌────────┐│
│                      │ imagegen │     │websearch │  │  tts   ││
│                      └──────────┘     └────┬─────┘  └────────┘│
│                                            │ (internet)       │
│                                            ▼                   │
│                                     [INTERNET - websearch only]│
└─────────────────────────────────────────────────────────────────┘
```

### Attack Surface Summary

| Surface | Exposure | Protocol | Authentication |
|---------|----------|----------|----------------|
| mesh ↔ joi (Interactive Channel) | Nebula overlay | HTTPS over Nebula | Nebula cert + HMAC |
| System Channel inbound | Nebula overlay | HTTPS over Nebula | Nebula cert |
| System Channel outbound | Nebula overlay | HTTPS over Nebula | Nebula cert |
| LLM Service VMs | Nebula overlay | HTTPS over Nebula | Nebula cert |
| websearch → Internet | Internet | HTTPS | N/A (outbound only) |
| Local terminal | Physical/LAN | TTY/SSH | Key-based SSH |
| Proxmox host disk | Physical | Filesystem | LUKS encryption |
| Signal servers | Internet (via mesh) | Signal Protocol | Signal credentials |

### Two-Layer Security Model

| Layer | What it Protects | LLM Control |
|-------|------------------|-------------|
| **Protection Layer** | Rate limits, circuit breakers, input validation, watchdogs | None - LLM cannot bypass |
| **LLM Agent Layer** | Decision-making for reads, writes, notifications | Trusted within bounds |

> **Key insight:** Even if LLM is compromised (prompt injection, jailbreak), the Protection Layer limits blast radius.

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

### 4.4 System Channel (Generic)

The System Channel is a type-agnostic interface for machine-to-machine communication. Sources can be read-only, write-only, or read-write.

| ID | Threat | Impact | Likelihood | Risk |
|----|--------|--------|------------|------|
| SC1 | Compromised source sends malicious events | Prompt injection, false context | Medium | **High** |
| SC2 | LLM writes to unauthorized system | Unintended actions in external system | Low | **High** |
| SC3 | Replay attack on System Channel | Duplicate writes, confusion | Low | Medium |
| SC4 | Rate limit bypass on writes | Flood external systems | Low | Medium |
| SC5 | Attacker spoofs source identity | Inject false events or intercept writes | Low | **High** |
| SC6 | LLM manipulated to write malicious data | Attack external system via Joi | Medium | **High** |

**Mitigations:**
- SC1: Same as O1 - sanitize all events, strict schema validation
- SC2: Policy engine enforces allowed actions per source; Protection Layer rate limits
- SC3: Nonce/sequence number validation, deduplication window
- SC4: Protection Layer enforces hard limits LLM cannot bypass
- SC5: ✓ **RESOLVED** - All sources on Nebula mesh with certificate auth
- SC6: All writes require `triggered_by: llm_decision`; output validation; rate limits

**Key Protection:** Two-layer architecture ensures that even if LLM is prompt-injected to write malicious data:
1. Protection Layer rate limits cap total writes (e.g., 60/hr)
2. Policy engine validates action is in allowed list for that source
3. Output validation blocks forbidden patterns
4. Circuit breaker trips on rapid-fire writes

### 4.5 LLM Services (Isolated VMs)

LLM Services are compute services on isolated VMs (imagegen, websearch, tts, codeexec).

| ID | Threat | Impact | Likelihood | Risk |
|----|--------|--------|------------|------|
| LS1 | imagegen produces inappropriate content | Offensive/harmful images | Medium | Medium |
| LS2 | websearch fetches malicious content | Prompt injection via web page | Medium | **High** |
| LS3 | websearch used for data exfiltration | Leak conversation/memory via search queries | Low | **High** |
| LS4 | codeexec sandbox escape | Attacker gains control of codeexec VM | Low | **High** |
| LS5 | LLM Service VM compromised | Lateral movement attempt to Joi | Low | Medium |
| LS6 | Resource exhaustion on LLM Service | DoS on image/video generation | Medium | Low |
| LS7 | Malicious output from LLM Service | Trojaned image, malicious code result | Low | Medium |

**Mitigations:**
- LS1: Content policy in Protection Layer blocks forbidden prompts; model-level safety
- LS2: websearch VM isolated; results sanitized before reaching Joi LLM; structured output format
- LS3: Audit log on all search queries; query validation; no raw memory/conversation in queries
- LS4: Minimal sandbox (gVisor/Firecracker); no network; resource limits; isolated VM
- LS5: LLM Service VMs have NO access to Joi core; Nebula mesh only; separate certs
- LS6: Per-service rate limits (e.g., imagegen 10/hr); queue depth limits
- LS7: Validate all output; don't execute returned code without sandbox; image format validation

**websearch Specific Risks:**
```
┌─────────────┐                      ┌─────────────────────────┐
│   Joi VM    │   query (logged)     │   websearch VM          │
│             │─────────────────────►│                         │
│  [no query  │                      │  ┌─────────────────┐    │
│   contains  │                      │  │ Browser Agent   │────┼──► INTERNET
│   secrets]  │                      │  │ (fetch, search) │    │
│             │◄─────────────────────│  └─────────────────┘    │
│             │   results (sanitized)│                         │
└─────────────┘                      └─────────────────────────┘
```

**websearch Mitigations:**
1. Query validation: No raw conversation, no memory content, length limits
2. Result sanitization: Strip scripts, truncate, structured format
3. Isolated VM: Only websearch has internet; Joi cannot reach internet
4. Audit: All queries logged; anomaly detection for exfiltration patterns

### 4.6 Business Mode (DM Group Knowledge Access)

Business mode allows DM users to access RAG knowledge from Signal groups they're members of. This creates a specific attack surface.

| ID | Threat | Impact | Likelihood | Risk |
|----|--------|--------|------------|------|
| BM1 | `/groups/members` endpoint exposes social graph | Attacker with HMAC learns all group memberships | Low | **High** |
| BM2 | Stale membership cache grants access after removal | Ex-member accesses group knowledge via DM | Medium | Medium |
| BM3 | ID format mismatch bypasses membership check | User denied legitimate access | Low | Medium |
| BM4 | signal-cli unavailable, access denied | Legitimate users can't access group knowledge | Medium | Low |

**Mitigations:**
- BM1: **ACCEPTED RISK** - HMAC authentication is sole protection. If HMAC compromised, attacker learns group membership graph. This is by design; endpoint required for feature.
- BM2: Cache refresh interval (default 15 min) limits stale window. Fail-closed: when membership unverifiable, access denied.
- BM3: Mesh returns both phone number AND UUID for each member; client normalizes phone number formats.
- BM4: Fail-closed design - if signal-cli is down and no cache, access denied (security over availability).

**Security Design Decisions:**
- **Companion mode:** DM group knowledge is hardcoded OFF. Config value ignored.
- **Business mode:** DM group knowledge is configurable via `dm_group_knowledge` flag.
- **Fail-closed:** When membership cannot be verified (no cache, signal-cli down), access is denied rather than granted.
- **No policy fallback:** Static policy participants are NOT used as fallback - only live signal-cli membership is trusted.

```
┌─────────────────────────────────────────────────────────────────┐
│                    Business Mode Access Flow                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   DM User ──► Is Business Mode? ──► NO ──► Own scope only       │
│                     │                                            │
│                    YES                                           │
│                     │                                            │
│              dm_group_knowledge? ──► NO ──► Own scope only      │
│                     │                                            │
│                    YES                                           │
│                     │                                            │
│         ┌──────────────────────┐                                │
│         │ Membership Cache     │                                │
│         │ (from signal-cli)    │                                │
│         └──────────┬───────────┘                                │
│                    │                                            │
│            Cache fresh? ──► YES ──► Return user's groups        │
│                    │                                            │
│                   NO                                            │
│                    │                                            │
│         Refresh from mesh ──► Success ──► Return user's groups  │
│                    │                                            │
│                 Failed                                          │
│                    │                                            │
│         Has stale cache? ──► YES ──► Use stale (with warning)   │
│                    │                                            │
│                   NO                                            │
│                    │                                            │
│         FAIL-CLOSED: Return [] (deny access)                    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 4.7 LLM and Agent Behavior

| ID | Threat | Impact | Likelihood | Risk |
|----|--------|--------|------------|------|
| L1 | Prompt injection via Signal message | Joi executes attacker instructions | High | **Critical** |
| L2 | Prompt injection via System Channel event | Joi manipulated by crafted data | Medium | **High** |
| L3 | Hallucination triggers unwanted action | Spam messages, false alerts | High | Medium |
| L4 | Agent loop runaway | Infinite messages, resource exhaustion | Medium | Medium |
| L5 | Model weights backdoored | Persistent compromise | Low | **Critical** |
| L6 | Untrusted model provenance | Supply chain attack | Medium | **High** |
| L7 | Context window overflow | Degraded responses, crashes | Medium | Low |
| L8 | LLM manipulated to abuse System Channel writes | Attack external systems | Medium | **High** |

**Mitigations:**
- L1: Strict input framing, system prompt hardening, output validation
- L2: Never pass raw event data to LLM, use structured templates
- L3: Output sanity checks, rate limits, confidence thresholds
- L4: Circuit breaker (max actions per time window), watchdog process
- L5: Verify model checksums, use trusted sources only
- L6: **POLICY** - Only use models from trusted Western sources (Meta, Google, Microsoft). Chinese models (Qwen, DeepSeek) are BANNED.
- L7: Sliding context window, summarization, hard limits
- L8: All writes LLM-gated but Protection Layer enforces hard limits; output validation

**Behavior Mode Impact on Threats:**

| Threat | companion mode | assistant mode |
|--------|---------------|----------------|
| L3 (Hallucination spam) | Higher risk - proactive messages | Lower risk - no proactive |
| L4 (Runaway loop) | Higher risk - impulse system | Lower risk - request-response only |
| Proactive abuse | Possible | **Eliminated** |
| Attack surface | Larger (impulse, proactive queue) | Smaller |

> **Note:** `assistant` mode reduces attack surface by eliminating proactive behavior. Consider using it for high-security deployments.

### 4.9 Memory Store

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

### 4.10 Physical and Local Access

| ID | Threat | Impact | Likelihood | Risk |
|----|--------|--------|------------|------|
| PH1 | Proxmox host stolen | Full data access, key extraction | Low | **High** |
| PH2 | VM disk image copied | Offline attack on data/keys | Low | **High** |
| PH3 | Proxmox console unauthorized access | Direct control of Joi VM | Medium | **High** |
| PH4 | USB attack on host (malicious device) | Code execution | Low | Medium |
| PH5 | Power supply tampering / eGPU disconnect | DoS, hardware damage | Low | Low |
| PH6 | eGPU enclosure theft | GPU loss, potential data on VRAM | Low | Low |
| PH7 | Maintenance USB key stolen/cloned | Attacker can disable heartbeat protection | Low | **High** |
| PH8 | Forged maintenance USB (wrong label) | Attempt to trigger maintenance mode | Low | Low |

**Mitigations:**
- PH1, PH2: LUKS encryption on Proxmox host, encrypted VM disk images
- PH3: Strong authentication (key-based SSH), Proxmox 2FA, disable password login
- PH4: Disable unused USB ports in BIOS/firmware (except for maintenance key port)
- PH5: UPS, tamper detection (optional)
- PH6: Physical security; VRAM is volatile (no persistent data risk)
- PH7: Ed25519 cryptographic verification (USB ID alone is not sufficient); keep maintenance key in secure location; backup key in separate safe
- PH8: Cryptographic verification rejects invalid keys; logs all attempts

### 4.11 Network (LAN)

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
Low         │ L6, PH5,   │ P6, P9, J3,   │ P3, P5, P7  │ L5
Likelihood  │ PH8        │ O4, N3, N4,   │ P10, O3, M2 │
            │            │ PH4           │ PH1, PH2,   │
            │            │               │ PH7         │
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

*Document version: 1.3*
*Last updated: 2026-02-08*
*Based on: Joi-architecture-v2.md, system-channel.md (System Channel, LLM Services, behavior modes)*
