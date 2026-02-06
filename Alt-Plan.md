# Alt Plan: First Good Run (7-turn Signal Conversation)

Definition: A 7-message exchange over Signal between owner and Joi, with the security model intact (policy engine enforced, Joi VM has no WAN, Nebula-authenticated mesh, no openhab required).

## Assumptions
- English-only for first run.
- openhab integration is out of scope for this milestone.
- mesh VM is installed and Signal bot is registered and functional (signal-cli in daemon mode).
- Implementation in Python for speed and familiarity.

## Plan Overview
Estimated total: 90–150 hours (single experienced engineer).

## Phase 0 — Baseline Hardening (8–16 hours)
1. Confirm Joi VM has no direct WAN egress (Proxmox network isolation).
2. Validate Nebula mesh connectivity between mesh VM and Joi VM.
3. Baseline firewall rules on mesh VM (only required ports).
4. Define minimal secrets handling (env files, permissions, no commits).

## Phase 1 — Mesh Proxy API (24–40 hours)
1. Implement a minimal HTTPS API on mesh VM:
   - `/api/v1/signal/inbound` (mesh -> Joi)
   - `/api/v1/signal/outbound` (Joi -> mesh)
2. Wire outbound to Signal bot send command (text only).
3. Implement inbound adapter (polling or hook) that forwards owner messages to Joi.
4. Add allowlist + basic rate limits + timestamp validation.
5. Add HMAC + nonce + timestamp layer (defense in depth over Nebula).
6. Logging: request ID, sender/recipient, outcome (no content stored).

### Transport Adapter Contract (Concept)
Goal: make Joi transport-agnostic by keeping a single internal API on the mesh VM, while adapters handle all network-specific details. Joi always speaks the same internal message shape; adding a new network means adding only a new adapter on the mesh VM.

Key idea: treat the mesh VM as a communications hub. Each transport (Signal, Matrix, Telegram, Discord, Slack, etc.) has its own adapter service on the mesh VM that:
- Authenticates and receives messages from the network.
- Normalizes them into a shared internal message format.
- Applies per-transport allowlists, rate limits, and sanity checks.
- Sends outbound messages by translating the internal format back to the transport.

This keeps Joi unchanged even as you add new networks. Joi just talks to the mesh API; it never needs to know how a given network works.

Minimum internal message fields (transport-agnostic):
1. transport
   - Example values: signal, matrix, telegram, discord, slack
   - Purpose: transport-specific logging, allowlists, and policy decisions
2. message_id
   - Unique per transport message
   - Purpose: deduplication, tracing, and reply mapping
3. sender
   - sender.id: transport-specific identifier
   - sender.display: optional human-friendly name
   - Purpose: identity allowlist and audit trail
4. recipient
   - recipient.type: direct or group
   - recipient.id: user ID or room/channel/group ID
   - Purpose: correct routing for outbound replies
5. channel
   - Logical channel: direct or critical
   - Purpose: reuse existing rate limits and escalation rules
6. content
   - content.type: text (for now)
   - content.text: sanitized message text
   - Purpose: consistent LLM input, consistent output handling
7. timestamps
   - timestamps.received_at: mesh receipt time
   - timestamps.sent_at: transport send time (optional)
   - Purpose: replay protection, latency tracing
8. trace_id
   - End-to-end correlation ID (mesh <-> Joi)
   - Purpose: debugging and log stitching

Adapter responsibilities (mesh VM):
- Inbound: parse transport payloads, sanitize text, enforce allowlist, apply rate limits.
- Outbound: enforce per-transport constraints (length, formatting, media rules), send to correct recipient.
- Credential storage: keep transport credentials isolated per adapter, least privilege.
- Logging: log metadata only (no message content), include transport + message_id + trace_id.

Why this works:
- You can add a new network by implementing only a mesh adapter, without touching Joi.
- Joi retains its WAN isolation and policy engine boundaries.
- Transport-specific quirks stay on the mesh VM, which already has WAN access.

Practical note: the API can stay as `/api/v1/signal/*` for the PoC, but should accept a `transport` field. Later, you can rename to `/api/v1/messages/*` once multiple transports are real.

## Phase 2 — Joi Core Minimal Runtime (28–46 hours)
1. Implement Joi API server:
   - `POST /api/v1/signal/inbound`
2. Policy engine (minimal for first run):
   - Identity allowlist (owner only)
   - Content length limits
   - Rate limits (outbound: 60/hr)
   - Default deny on errors
3. LLM call via Ollama OpenAI-compatible API (text in, text out).
4. Minimal agent loop behavior:
   - Always respond to inbound Signal messages
   - No proactive messages in PoC
5. Outbound call to mesh proxy API.
6. **Basic safety (required before testing):**
   - Response cooldown (5s minimum between sends)
   - Circuit breaker (max 120 LLM calls/hr)
   - Single response lock (prevent overlapping)
   - **Emergency stop** documented (shutdown mesh VM via Proxmox mobile)

> **Note:** Items in step 6 were originally Phase 3 but are critical for safe Phase 2 testing. Without them, a bug could cause runaway message loops or LLM spam.

> **Emergency Stop:** Primary method is shutting down mesh VM via Proxmox (owner has mobile access). This cuts Joi's communication path completely and works even if Joi is misbehaving. No code implementation needed - just document the procedure.

## Phase 3 — Conversation Stability (15–25 hours)
1. Ensure 7-turn conversation completes without timeouts.
2. Add request tracing via `X-Request-ID` (end-to-end correlation).
3. Improve error handling and retry logic.
4. Add graceful degradation on LLM timeout/failure.
5. Load testing: verify stability under rapid message bursts.

## Phase 4 — Verification & First Good Run (10–20 hours)
1. Test 7-turn conversation end-to-end.
2. Verify policy logs show all pass/fail outcomes.
3. Confirm no direct WAN from Joi VM.
4. Confirm Nebula auth is the only transport path.
5. **Test policy DENY paths:**
   - Send message from unauthorized phone number → expect DENY
   - Send oversized message (>4096 chars) → expect truncation or DENY
   - Rapid-fire messages to hit rate limit → expect DENY after limit
   - Verify all DENY cases are logged correctly

## Minimal Deliverables
- Mesh proxy API service (Python) with Signal bot integration (signal-cli daemon mode).
- Joi core service (Python) with policy gate and LLM call.
- Documented runbook for starting/stopping both services.
- Evidence of 7-turn successful conversation (timestamps + IDs only).

## Risks / Chokepoints
- Signal bot stability under sustained message exchange.
- GPU passthrough stability (if LLM is GPU-backed).
- Policy enforcement gaps causing bypasses.
- Timeout handling between services.

## Next Milestone After First Run (Not in Scope)
- openhab event ingest + normalization.
- Slovak model evaluation and tuning.
- Proactive agent loop and impulse model.
- Long-term memory store and pruning.
