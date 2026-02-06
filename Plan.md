# Main Plan: First Good Run (7-turn Signal Conversation)

> **Goal:** A 7-message exchange over Signal between owner and Joi, with the security model intact (policy engine enforced, Joi VM has no WAN, Nebula-authenticated mesh, no openhab required).

## Approach: Vertical Slices

This plan takes a **vertical slice** approach: get a minimal working system end-to-end first, then layer in security hardening. This differs from Alt-Plan which front-loads hardening.

**Rationale:**
- Faster feedback loop - see something working sooner
- Easier to debug integration issues before adding security complexity
- Security layers added incrementally with clear before/after testing

**Trade-off:**
- Early versions are less secure (acceptable for dev environment)
- Must resist temptation to skip hardening phases

## Assumptions

- English-only for first run
- openhab integration out of scope for this milestone
- Implementation in Python
- signal-cli in daemon mode (signald is dead)
- Development on isolated network (no real security exposure during dev)

---

## Phase 1 — Skeleton: End-to-End Message Flow (20-30 hours)

**Goal:** Owner sends Signal message → Joi responds. No security, no persistence, just the pipe.

### 1.1 Infrastructure Setup (6-10h)
- [ ] Create mesh VM (Ubuntu 24 LTS, 2GB RAM)
- [ ] Create joi VM (GPU passthrough configured)
- [ ] Basic networking between VMs (can be simple bridged network initially)
- [ ] Verify GPU passthrough works (Ollama loads model)

### 1.2 signal-cli Setup (4-6h)
- [ ] Install signal-cli on mesh VM
- [ ] Register/link phone number
- [ ] Test send/receive via CLI manually
- [ ] Configure daemon mode with Unix socket
- [ ] Verify: can send and receive messages programmatically

### 1.3 Mesh Proxy Skeleton (4-6h)
- [ ] Python HTTP server (FastAPI or Flask)
- [ ] `/api/v1/message/inbound` - receives from signal-cli, forwards to joi
- [ ] `/api/v1/message/outbound` - receives from joi, sends via signal-cli
- [ ] signal-cli socket client (JSON-RPC)
- [ ] No auth, no validation, no rate limits yet

### 1.4 Joi Core Skeleton (6-8h)
- [ ] Python HTTP server
- [ ] `/api/v1/message/inbound` - receives message, calls LLM, returns response
- [ ] Ollama client (OpenAI-compatible API)
- [ ] Basic system prompt ("You are Joi, a helpful assistant")
- [ ] Call mesh outbound endpoint with response
- [ ] No policy engine, no memory, no persistence yet

### 1.5 Integration Test
- [ ] Send Signal message to bot number
- [ ] Verify Joi responds via Signal
- [ ] **Milestone:** Single message round-trip works

**Exit Criteria:** Can have a basic conversation (no memory, no security)

---

## Phase 2 — Security Foundation (25-35 hours)

**Goal:** Add authentication, encryption, and network isolation. System becomes secure.

### 2.1 Network Isolation (6-10h)
- [ ] Create isolated vmbr1 network in Proxmox (/29 subnet)
- [ ] Move joi VM to vmbr1 (no WAN route)
- [ ] Verify joi cannot reach internet (curl google.com fails)
- [ ] mesh VM has dual NICs: WAN + vmbr1

### 2.2 Nebula Mesh Setup (8-12h)
- [ ] Generate Nebula CA (on air-gapped machine, store securely)
- [ ] Generate certificates for mesh and joi
- [ ] Install Nebula on both VMs
- [ ] Configure lighthouse on mesh
- [ ] Verify mesh ↔ joi communication over Nebula only
- [ ] Block direct vmbr1 traffic (only Nebula allowed)

### 2.3 Transport Security (6-8h)
- [ ] HTTPS on all endpoints (self-signed OK for internal)
- [ ] Add X-Request-ID, X-Timestamp, X-Nonce headers
- [ ] Implement nonce tracking (SQLite table)
- [ ] Implement timestamp validation (±5 minutes)
- [ ] Reject replays

### 2.4 signal-cli Hardening (5-5h)
- [ ] Create dedicated `signal` user
- [ ] Move credentials to `/var/lib/signal-cli/data/`
- [ ] Set permissions 0700
- [ ] Configure systemd service
- [ ] Verify daemon runs as `signal` user

### 2.5 Verification
- [ ] Repeat Phase 1.5 test over secured channel
- [ ] Verify Nebula certificates are required
- [ ] Test replay rejection (resend captured request)
- [ ] **Milestone:** Secure message round-trip works

**Exit Criteria:** Communication is authenticated and encrypted

---

## Phase 3 — Policy Engine (20-30 hours)

**Goal:** Implement all security policies. System enforces rules.

### 3.1 Identity & Allowlists (4-6h)
- [ ] Canonical identity model (owner, not phone number)
- [ ] Identity bindings config (owner → +1555XXXXXXXXX)
- [ ] Sender validation (inbound)
- [ ] Recipient validation (outbound)
- [ ] Reject unknown senders with logging

### 3.2 Rate Limiting (6-8h)
- [ ] Rate limit data structure (sliding window)
- [ ] Inbound: 120/hour
- [ ] Outbound direct: 60/hour, 5s cooldown
- [ ] Outbound critical: unlimited (event), 120/hour (LLM-escalated)
- [ ] Circuit breaker: 120 LLM calls/hour
- [ ] Reject with 429 and retry-after

### 3.3 Content Validation (4-6h)
- [ ] Input length limits (4096 chars)
- [ ] Output length limits (2048 chars)
- [ ] Unicode NFKC normalization
- [ ] Block patterns (URLs, system prompt leakage)
- [ ] Sanitize before LLM, validate after LLM

### 3.4 Safety Mechanisms (6-10h)
- [ ] Response cooldown (5s minimum between sends)
- [ ] Single response lock (prevent overlapping)
- [ ] Circuit breaker implementation
- [ ] Graceful degradation on LLM timeout
- [ ] Default deny on any error

### 3.5 Verification
- [ ] Test: unknown sender → DENY
- [ ] Test: oversized message → truncate/DENY
- [ ] Test: rapid messages → rate limit kicks in
- [ ] Test: blocked pattern in output → DENY
- [ ] **Milestone:** Policy engine blocks bad behavior

**Exit Criteria:** All policy rules enforced and logged

---

## Phase 4 — Persistence & Stability (15-25 hours)

**Goal:** Add memory, improve reliability. System is production-ready.

### 4.1 Memory Store (8-12h)
- [ ] SQLite database with SQLCipher encryption
- [ ] Conversation history table
- [ ] Context summaries table
- [ ] Replay nonces table (if not done in 2.3)
- [ ] Retention policies (auto-prune old data)

### 4.2 Context Management (4-6h)
- [ ] Sliding context window
- [ ] Summarization for long conversations
- [ ] Include relevant history in LLM prompt
- [ ] Hard limit on context size

### 4.3 Error Handling & Recovery (3-7h)
- [ ] Retry logic with exponential backoff
- [ ] Timeout handling (LLM, network)
- [ ] Service restart recovery (state survives restart)
- [ ] Health check endpoints

### 4.4 Verification
- [ ] 7-turn conversation without errors
- [ ] Restart services mid-conversation, verify recovery
- [ ] **Milestone:** Stable multi-turn conversation

**Exit Criteria:** Can hold sustained conversation with memory

---

## Phase 5 — Verification & First Good Run (8-15 hours)

**Goal:** Prove the system works. Document evidence.

### 5.1 End-to-End Testing (4-8h)
- [ ] 7-turn conversation test (record timestamps + IDs)
- [ ] Verify all messages logged correctly
- [ ] Verify policy decisions logged (ALLOW/DENY)
- [ ] Verify no policy bypasses

### 5.2 Security Verification (2-4h)
- [ ] Confirm joi VM has no WAN access
- [ ] Confirm Nebula is only transport path
- [ ] Test DENY paths:
  - [ ] Unauthorized phone number → DENY
  - [ ] Oversized message → truncation or DENY
  - [ ] Rapid-fire to hit rate limit → DENY
- [ ] Verify all DENY cases logged

### 5.3 Documentation (2-3h)
- [ ] Runbook: starting/stopping services
- [ ] Runbook: emergency stop procedure
- [ ] Evidence: 7-turn conversation log (timestamps + IDs only)
- [ ] Known issues / future work

**Exit Criteria:** First Good Run achieved and documented

---

## Deliverables

1. **mesh VM** - Ubuntu 24 LTS with signal-cli daemon, Nebula, proxy API
2. **joi VM** - GPU-enabled with Ollama, policy engine, core API
3. **Documentation:**
   - Service runbook (start/stop/restart)
   - Emergency stop procedure
   - 7-turn conversation evidence
4. **Logs:** Policy decisions, anonymized message metadata

---

## Time Estimates

| Phase | Optimistic | Pessimistic | Notes |
|-------|------------|-------------|-------|
| Phase 1: Skeleton | 20h | 30h | Depends on signal-cli quirks |
| Phase 2: Security | 25h | 35h | Nebula setup can be tricky |
| Phase 3: Policy | 20h | 30h | Core security logic |
| Phase 4: Persistence | 15h | 25h | SQLCipher + context mgmt |
| Phase 5: Verification | 8h | 15h | Testing and documentation |
| **Total** | **88h** | **135h** | ~11-17 working days |

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| signal-cli daemon mode issues | Medium | High | Fallback to per-command (less secure, document risk) |
| GPU passthrough instability | Medium | Medium | CPU fallback documented |
| Nebula configuration errors | Medium | Medium | Test incrementally, keep working configs |
| LLM response quality | Low | Medium | System prompt iteration |
| Policy bypass discovered | Low | High | Extensive DENY path testing |

---

## Comparison with Alt-Plan

| Aspect | Main Plan (this) | Alt-Plan |
|--------|------------------|----------|
| **Approach** | Vertical slices (working system first) | Horizontal layers (security first) |
| **First milestone** | Message round-trip (insecure) | Hardened infrastructure |
| **Security timing** | Added in Phase 2-3 | Front-loaded in Phase 0-1 |
| **Feedback speed** | Faster (see it work sooner) | Slower (more setup before first test) |
| **Risk profile** | Early versions insecure | More secure throughout |
| **Estimate** | 88-135h | 90-150h |

**Recommendation:** Use Main Plan for faster iteration during development, but ensure Phase 2-3 are completed before any real use. Alt-Plan is better if you prefer "do it right the first time" approach.

---

## Next Milestone After First Run (Not in Scope)

- openhab event ingest + normalization
- Slovak model evaluation and tuning
- Proactive agent loop and impulse model
- Long-term memory store and pruning
- Voice message support (STT/TTS)

---

*Document version: 1.0*
*Created: 2026-02-05*
*Complements: Alt-Plan.md*
