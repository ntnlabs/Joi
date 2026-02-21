# Joi API Contracts

> API specification for communication between mesh, joi, and external systems.
> Version: 1.3
> Last updated: 2026-02-21

## Overview

Joi has two communication channels, both protected by a two-layer security model:

### Two-Layer Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     PROTECTION LAYER                            │
│           (raw automation, LLM has NO control)                  │
│                                                                 │
│  • Rate limiters (hard caps, no override)                       │
│  • Circuit breakers (trip on runaway behavior)                  │
│  • Input/output validation                                      │
│  • Replay protection, watchdogs, emergency stop                 │
│                                                                 │
│  Runs on: Joi VM, mesh VM — independently of LLM                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      LLM AGENT LAYER                            │
│              (trusted for decisions within bounds)              │
│                                                                 │
│  • Decides what to read/write via System Channel                │
│  • Decides what/when to notify owner via Interactive Channel    │
│  • All writes go through LLM (no automated writes)              │
│                                                                 │
│  Runs on: Joi VM — trusted for normal operations                │
└─────────────────────────────────────────────────────────────────┘
```

### Communication Channels

| Channel | Purpose | Direction | Example |
|---------|---------|-----------|---------|
| **Interactive** | Human communication | Bidirectional | Signal messaging |
| **System** | Machine-to-machine | Bidirectional | openhab, Zabbix, actuators |

> **See also:** `system-channel.md` for full System Channel specification.

### API Summary

All APIs use JSON over HTTPS. Authentication via Nebula mesh:

| Channel | Transport | Auth | Port |
|---------|-----------|------|------|
| mesh ↔ joi (Interactive) | Nebula tunnel | Nebula cert + HMAC | 8443/8444 |
| System Channel (inbound) | Nebula tunnel | Nebula cert | 8445 |
| System Channel (outbound) | Nebula tunnel | Nebula cert | varies |

> **Note:** openhab endpoints are being migrated to the generic System Channel API.
> Legacy `/api/v1/openhab/*` endpoints remain as aliases. See `system-channel.md` → "Migration".

> **Architecture Decision:** All three VMs (mesh, joi, openhab) are Nebula mesh nodes. This simplifies auth to a single PKI and ensures all traffic is certificate-authenticated and encrypted.

## Common Headers

All requests include:

```
Content-Type: application/json
X-Request-ID: <uuid>           # For tracing/deduplication
X-Timestamp: <unix-epoch-ms>   # Sender's timestamp
X-Nonce: <uuid>                # Replay protection (mesh ↔ joi only)
X-HMAC-SHA256: <hex>           # Defense-in-depth (mesh ↔ joi only, see below)
```

> **Content-Type Enforcement:** Servers MUST reject requests with `Content-Type` other than `application/json` with HTTP 415 (Unsupported Media Type). This prevents parser confusion attacks from malformed content types.

### HMAC Defense-in-Depth (mesh ↔ joi only)

Nebula provides certificate-based authentication. HMAC adds a second layer of defense:

```
X-HMAC-SHA256: HMAC-SHA256(X-Nonce + X-Timestamp + body, shared_secret)
```

**Validation:**
1. Verify Nebula certificate (primary auth)
2. Verify HMAC matches (secondary auth)
3. Verify nonce not seen before (replay protection)
4. Verify timestamp within 5 minutes (freshness)

**Shared secret location:** See `Joi-architecture-v2.md` → "Challenge-Response Shared Secret"

> **Why both Nebula AND HMAC?** Defense-in-depth. If Nebula has a vulnerability, HMAC
> still protects. If HMAC key leaks, Nebula certs still protect. Both must be
> compromised for an attacker to forge requests.

## Common Response Format

All endpoints return:

```json
{
  "status": "ok" | "error",
  "request_id": "<echoed from request>",
  "timestamp": <unix-epoch-ms>,
  "error": {                   // Only present if status == "error"
    "code": "<error_code>",
    "message": "<human readable>"
  },
  "data": { }                  // Endpoint-specific response data
}
```

## Error Codes

| Code | HTTP Status | Description |
|------|-------------|-------------|
| `invalid_request` | 400 | Malformed JSON or missing required fields |
| `auth_failed` | 401 | Authentication failed (Nebula cert or openhab auth) |
| `forbidden` | 403 | Policy violation (rate limit, disallowed recipient, etc.) |
| `not_found` | 404 | Unknown endpoint |
| `replay_detected` | 409 | Nonce already seen (replay attack) |
| `rate_limited` | 429 | Too many requests |
| `internal_error` | 500 | Server-side failure |

---

## 1. Message Inbound: mesh → joi

When mesh receives a message for the owner (from any transport), it forwards to joi.

### Endpoint

```
POST https://joi:8443/api/v1/message/inbound
```

> **Note:** For PoC, `/api/v1/signal/inbound` is also accepted as alias.

### Request Body

```json
{
  "transport": "signal",           // Transport identifier: "signal", "matrix", "telegram", etc.
  "message_id": "<signal-message-uuid>",
  "sender": {
    "id": "owner",                 // Canonical identity (for authorization)
    "transport_id": "+1555XXXXXXXXX", // Transport-native identifier (for routing/display)
    "display_name": "Owner"        // Optional, from transport profile
  },
  "conversation": {
    "type": "direct",              // "direct" or "group"
    "id": "<conversation-id>"      // For threading
  },
  "priority": "normal",            // "normal" or "critical" (for alerting behavior)
  "content": {
    "type": "text",                // "text", "voice", "image", "file", "reaction"
    "text": "Hello Joi!",          // Present if type == "text"
    "voice_transcription": null,   // STT result if voice (mesh transcribes before sending)
    "voice_transcription_failed": false,
    "voice_failure_reason": null,  // "transcription_timeout", "whisper_error", etc.
    "voice_duration_ms": null,
    "caption": null,               // Optional caption for media
    "media_url": null,             // Local path if media (mesh stores temporarily)
    "reaction": null,              // Emoji if type == "reaction"
    "transport_native": {}         // Transport-specific fields (joi can ignore)
  },
  "metadata": {
    "mesh_received_at": 1706918400100,  // When mesh received from transport
    "original_format": "text"           // Original format before any conversion
  },
  "timestamp": 1706918400000,      // Transport message timestamp
  "quote": {                       // Optional: if replying to a message
    "message_id": "<quoted-msg-id>",
    "text": "<quoted text preview>"
  }
}
```

### Response

```json
{
  "status": "ok",
  "request_id": "abc-123",
  "timestamp": 1706918400500,
  "data": {
    "received": true,
    "will_respond": true           // Hint: joi intends to reply
  }
}
```

### Validation Rules

- `sender.id` must match allowed canonical identity (see Security Invariants)
- `sender.transport_id` must match registered transport identifier for that `sender.id`
- `content.text` max length: 4096 characters (API limit)
- **Signal transport limit:** 1500 characters (enforced at mesh before forwarding)
- `content.type` must be one of: `text`, `voice`, `image`, `file`, `reaction`
- `timestamp` must be within 5 minutes of current time
- If `type` == `voice`: mesh transcribes via STT before forwarding

> **Size limit hierarchy:** Signal messages are capped at 1500 chars at mesh (user-facing).
> The 4096 API limit accommodates metadata/headers in the internal payload.

---

## 2. Message Outbound: joi → mesh

When joi wants to send a message, it calls mesh.

### Endpoint

```
POST https://mesh:8444/api/v1/message/outbound
```

> **Note:** For PoC, `/api/v1/signal/outbound` is also accepted as alias.

### Request Body

```json
{
  "transport": "signal",         // Target transport (must match recipient's registered transport)
  "recipient": {
    "id": "owner",               // Canonical identity (for authorization check)
    "transport_id": "+1555XXXXXXXXX"  // Transport-native identifier (for routing)
  },
  "priority": "normal",          // "normal" or "critical" (affects alerting behavior)
  "delivery": {
    "target": "direct",          // "direct" (DM) or "group"
    "group_id": null             // Transport-specific group ID (if target == "group")
  },
  "conversation_id": "<conversation-id>",  // Optional: for threading
  "content": {
    "type": "text",              // "text" or "voice"
    "text": "Hello! The temperature is 22°C."
  },
  "reply_to": "<message-id>",    // Optional: quote a previous message
  "escalated": false,            // True if Joi judged this urgent (hybrid logic)
  "voice_response": false        // If true, mesh converts text to voice via TTS (future)
}
```

### Response

```json
{
  "status": "ok",
  "request_id": "def-456",
  "timestamp": 1706918401000,
  "data": {
    "message_id": "<transport-message-uuid>",
    "transport": "signal",
    "sent_at": 1706918401000,
    "delivered": false           // Updated async via transport receipts
  }
}
```

### Validation Rules (Policy Engine)

- `recipient.id` must be in canonical allowlist (see Security Invariants)
- `recipient.transport_id` must match registered identifier for that `recipient.id`
- `priority`: "normal" or "critical"
  - `normal`: Standard conversation
  - `critical`: Urgent alerts (may use different notification mechanism per transport)
- `delivery.group_id` required if `delivery.target == "group"`
- `content.text` max length: 2048 characters
- `content.type` must be `text` (media sending not supported initially)

### Rate Limits by Channel

| Channel | Limit | Rationale |
|---------|-------|-----------|
| direct | 60/hour | Prevent runaway agent spam |
| critical (event-triggered) | **NONE** | Safety first - never block true critical alerts |
| critical (LLM-escalated) | 120/hour | 2x direct channel - generous but bounded |

### Channel Selection Logic (Hybrid)

| Scenario | Channel | Escalated | Rate Limited |
|----------|---------|-----------|--------------|
| Normal conversation | direct | false | Yes |
| Proactive chat ("Welcome home!") | direct | false | Yes |
| Known critical event (smoke, storm, security) | critical | false | **No** |
| Joi judges something urgent | critical | true | **Yes (120/hr)** |

> **Security Note:** LLM-escalated messages (`escalated: true`) have a separate rate limit to prevent prompt injection attacks from flooding the critical channel. True critical events from openhab (smoke_alarm, fire_alarm, etc.) bypass all limits.

---

## 3. Control Plane: joi → mesh

Config sync and security management endpoints. Joi pushes config to stateless mesh.

### 3.1 Config Sync

```
POST https://mesh:8444/config/sync
```

Pushes policy config from Joi to mesh. Mesh applies in memory (no disk persistence).

**Request Body:**
```json
{
  "version": 1,
  "timestamp_ms": 1708300000000,
  "identity": {
    "bot_name": "Joi",
    "allowed_senders": ["+1234567890"],
    "groups": { ... }
  },
  "rate_limits": {
    "inbound": { "max_per_hour": 120, "max_per_minute": 20 }
  },
  "validation": {
    "max_text_length": 1500
  },
  "hmac_rotation": {                    // Optional: HMAC key rotation
    "new_secret": "<64-char-hex>",
    "effective_at_ms": 1708300060000,
    "grace_period_ms": 60000
  }
}
```

**Response:**
```json
{
  "status": "ok",
  "data": {
    "config_hash": "<sha256-hex>"
  }
}
```

### 3.2 Config Status

```
GET https://mesh:8444/config/status
```

Returns current config hash. Used by Joi to detect drift.

**Response:**
```json
{
  "status": "ok",
  "data": {
    "config_hash": "<sha256-hex>",
    "applied_at_ms": 1708300000500
  }
}
```

### 3.3 Delivery Status

```
GET https://mesh:8444/api/v1/delivery/status?timestamp=<msg_timestamp>
```

Query delivery/read status for a sent message. Requires HMAC auth.

**Response:**
```json
{
  "status": "ok",
  "data": {
    "timestamp": 1234567890123,
    "delivered": true,
    "read": false,
    "delivered_at": 1234567891000,
    "read_at": null,
    "sent_at": 1234567890500
  }
}
```

### 3.4 Group Membership (Business Mode)

```
GET https://mesh:8444/groups/members
```

Returns all Signal groups with their member lists. Used by Joi in business mode to determine DM access to group knowledge.

**Response:**
```json
{
  "status": "ok",
  "data": {
    "groupId1": ["+1234567890", "+0987654321"],
    "groupId2": ["+1234567890"]
  }
}
```

---

## Joi Endpoints (port 8443)

### Health & Admin

Local-only endpoints (127.0.0.1) for administration.

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/health` | GET | None | Health check with memory/RAG/queue stats |
| `/admin/config/status` | GET | Local | Show config sync status |
| `/admin/config/push` | POST | Local | Force push config to mesh |
| `/admin/hmac/status` | GET | Local | Show HMAC key status |
| `/admin/hmac/rotate` | POST | Local | Manual HMAC key rotation |
| `/admin/security/status` | GET | Local | Show security settings status |
| `/admin/security/privacy-mode` | POST | Local | Enable/disable privacy mode (`?active=true/false`) |
| `/admin/security/kill-switch` | POST | Local | Enable/disable kill switch (`?active=true/false`) |
| `/admin/rag/scopes` | GET | Local | List all knowledge scopes with chunk counts |
| `/admin/rag/search` | GET | Local | Test RAG search (`?q=query&scope=optional`) |

### API Endpoints

Called by mesh over Nebula tunnel.

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/api/v1/message/inbound` | POST | HMAC | Receive message from mesh |
| `/api/v1/document/ingest` | POST | HMAC | Receive document for RAG ingestion |

---

## Mesh Endpoints (port 8444)

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/health` | GET | None | Health check |
| `/api/v1/message/outbound` | POST | HMAC | Send message via Signal |
| `/api/v1/delivery/status` | GET | HMAC | Query delivery/read status |
| `/config/sync` | POST | HMAC | Receive config push from Joi |
| `/config/status` | GET | HMAC | Return current config hash |
| `/groups/members` | GET | HMAC | List groups with members |

---

## 4. OpenHAB Events: openhab → joi

> **Migration Notice:** These endpoints are legacy. New integrations should use the generic
> System Channel API (`POST /api/v1/system/event`). See `system-channel.md` for details.
> These endpoints will continue to work as aliases to the System Channel handler.

openhab pushes events to joi via webhooks. Different event types may use different endpoints for routing/rate limiting.

### Base URL

```
https://joi:8445/api/v1/openhab
```

### 3.1 Presence Event

```
POST /api/v1/openhab/presence
```

```json
{
  "event_id": "<uuid>",
  "event_type": "presence",
  "timestamp": 1706918400000,
  "source": "openhab.homelab.example",
  "data": {
    "entity": "owner",           // "owner", "partner", "guest", "car"
    "state": "home",             // "home", "away", "arriving", "leaving"
    "location": "home",          // Location name
    "changed_at": 1706918400000
  }
}
```

### 3.2 Sensor Event (Batched)

```
POST /api/v1/openhab/sensors
```

```json
{
  "event_id": "<uuid>",
  "event_type": "sensors",
  "timestamp": 1706918400000,
  "source": "openhab.homelab.example",
  "data": {
    "readings": [
      {
        "sensor_id": "living_room_temp",
        "type": "temperature",
        "value": 22.5,
        "unit": "celsius",
        "measured_at": 1706918400000
      },
      {
        "sensor_id": "living_room_humidity",
        "type": "humidity",
        "value": 45,
        "unit": "percent",
        "measured_at": 1706918400000
      }
    ]
  }
}
```

### 3.3 Weather Event

```
POST /api/v1/openhab/weather
```

```json
{
  "event_id": "<uuid>",
  "event_type": "weather",
  "timestamp": 1706918400000,
  "source": "openhab.homelab.example",
  "data": {
    "current": {
      "condition": "partly_cloudy",
      "temperature": 18.5,
      "humidity": 60,
      "wind_speed": 15,
      "wind_unit": "kmh"
    },
    "forecast": [
      {
        "date": "2026-02-04",
        "high": 20,
        "low": 12,
        "condition": "sunny",
        "precipitation_chance": 10
      }
    ],
    "alerts": []                 // Severe weather alerts
  }
}
```

### 3.4 Alert Event (High Priority)

```
POST /api/v1/openhab/alert
```

```json
{
  "event_id": "<uuid>",
  "event_type": "alert",
  "timestamp": 1706918400000,
  "source": "openhab.homelab.example",
  "priority": "high",            // "high", "medium", "low"
  "data": {
    "alert_type": "storm_warning", // "storm_warning", "door_open", "smoke", etc.
    "title": "Storm Warning",
    "message": "Severe thunderstorm expected in 2 hours",
    "expires_at": 1706925600000  // Optional: when alert expires
  }
}
```

### 3.5 Generic State Change

```
POST /api/v1/openhab/state
```

```json
{
  "event_id": "<uuid>",
  "event_type": "state",
  "timestamp": 1706918400000,
  "source": "openhab.homelab.example",
  "data": {
    "item": "front_door_lock",
    "previous_state": "locked",
    "new_state": "unlocked",
    "changed_by": "manual"       // "manual", "automation", "api"
  }
}
```

### Response (All openhab endpoints)

```json
{
  "status": "ok",
  "request_id": "ghi-789",
  "timestamp": 1706918400100,
  "data": {
    "received": true,
    "queued": true               // Event queued for agent processing
  }
}
```

### Validation Rules

- `source` must be `openhab.homelab.example`
- `timestamp` must be within 5 minutes of current time
- `event_id` used for deduplication (reject if seen in last 30 minutes)
- Sensor batches: max 50 readings per request
- Rate limits per endpoint (see below)

---

## 5. System Channel (Generic)

The System Channel provides a unified, type-agnostic interface for machine-to-machine communication. It replaces the openhab-specific endpoints with a generic pattern that supports any registered source.

> **Full specification:** See `system-channel.md`

### Key Concepts

| Concept | Description |
|---------|-------------|
| **Source** | Registered external system (openhab, zabbix, calendar, actuator) |
| **Access Mode** | `read`, `write`, or `read-write` per source |
| **Inbound** | Events FROM systems TO Joi |
| **Outbound** | Actions FROM Joi TO systems |

### Inbound Events (Any Source → Joi)

```
POST https://joi:8445/api/v1/system/event
```

```json
{
  "source": "zabbix",
  "event_id": "<uuid>",
  "event_type": "problem",
  "timestamp": 1707400000000,
  "priority": "high",
  "data": { /* source-specific payload */ }
}
```

### Outbound Actions (Joi → Any Source)

```
POST https://<source-endpoint>:<port>/api/v1/action
```

```json
{
  "action": "acknowledge",
  "action_id": "<uuid>",
  "target": { "id": "12345", "type": "problem" },
  "parameters": { "message": "Acknowledged by Joi" },
  "context": { "triggered_by": "llm_decision" }
}
```

### Write Control

All System Channel writes are **LLM-gated**:
- LLM decides when to perform actions (no automated writes)
- Protection Layer enforces rate limits (LLM cannot override)
- No owner approval required (writes trusted within bounds)

> **See also:** `system-channel.md` → "Two-Layer Architecture" for full security model.

---

## Rate Limits

### Outbound (Joi → Signal)

| Channel | Scope | Limit | Notes |
|---------|-------|-------|-------|
| DM (owner) | per user | 120/hr | Owner gets 2x limit |
| DM (others) | per user | 60/hr | Standard limit |
| Regular group | per group | 60/hr | Standard limit |
| Critical group (event) | - | **unlimited** | Safety first |
| Critical group (LLM-escalated) | - | 120/hr | Prevent prompt injection DoS |

### Inbound (Signal → Joi) — Enforced at mesh

> **Note:** Inbound rate limiting happens at mesh VM, before forwarding to Joi.
> This saves Nebula bandwidth and protects Joi from floods.

| Channel | Scope | Limit | Notes |
|---------|-------|-------|-------|
| DM (owner) | per user | **unlimited** | Owner is primary user |
| DM (others) | per user | 120/hr | Prevent flooding |
| Regular group | per user per group | 120/hr | Prevent one person dominating |
| Critical group | - | **unlimited** | All critical talk matters |
| `/openhab/presence` | 30 | per hour | Presence changes |
| `/openhab/sensors` | 24 | per hour | ~2.5-min batched readings |
| `/openhab/weather` | 4 | per hour | Weather updates |
| `/openhab/alert` | 60 | per hour | High-priority alerts |
| `/openhab/state` | 120 | per hour | Generic state changes |

> **PoC Decision:** Outbound rate limit set to 60/hour. For PoC, only Signal transport is implemented.

Rate limit response:

```json
{
  "status": "error",
  "error": {
    "code": "rate_limited",
    "message": "Rate limit exceeded",
    "retry_after": 300           // Seconds until limit resets
  }
}
```

---

## Replay Protection

For mesh ↔ joi communication:

1. Every request includes `X-Nonce` header (UUID v4)
2. Receiver stores nonces for **15 minutes** (must be ≥ 2× timestamp tolerance to prevent replay after nonce expiry)
3. If nonce seen before → reject with `replay_detected`
4. `X-Timestamp` must be within 5 minutes of server time

> **Security Note:** The nonce retention window (15 min) must always exceed twice the timestamp tolerance (5 min). Otherwise, an attacker could capture a valid nonce, wait for it to expire from the cache, then replay the message while the timestamp is still valid.

> **Centralized Constants (use these everywhere):**
> ```yaml
> security:
>   timestamp_tolerance_minutes: 5
>   nonce_retention_minutes: 15   # MUST be > 2 × timestamp_tolerance
> ```
> If you change timestamp tolerance, you MUST update nonce retention to maintain the invariant: `nonce_retention > 2 × timestamp_tolerance`

> **Clock Sync Requirement:** All VMs (joi, mesh, openhab) must sync to the same NTP source (gateway on vmbr1). See `Joi-architecture-v2.md` → "Time Synchronization (NTP)" for configuration. Without NTP sync, timestamp validation will fail after clock drift exceeds 5 minutes.

> **Nonce Storage:** Nonces are stored in SQLite (`replay_nonces` table), not in-memory. This ensures replay protection survives service restarts. See `memory-store-schema.md` → "replay_nonces".

---

## Security Invariants

**Critical rules that MUST be enforced by both mesh and joi.**

### 1. Authorization Uses Canonical Identity

```
Authorization MUST use sender.id / recipient.id (canonical)
Transport_id is for ROUTING ONLY, never for authorization
```

**Why this matters:**
- Transport identifiers can change (new phone number, new Matrix account)
- Canonical identity represents the actual person/entity
- If you authorize on transport_id, an attacker with a new number could spoof identity

**Correct implementation:**
```python
# CORRECT - authorize on canonical identity
def check_allowlist(message):
    return message["sender"]["id"] in ALLOWED_IDENTITIES  # "owner", "partner"

# WRONG - never do this
def check_allowlist_bad(message):
    return message["sender"]["transport_id"] in ALLOWED_PHONES  # +1555...
```

### 2. Transport_id Must Match Registered Binding

Even though authorization uses canonical ID, the `transport_id` must match the registered binding for that identity:

```yaml
# identity_bindings.yaml (mesh config)
identities:
  owner:
    language: en                     # For system messages (rate limit, errors)
    signal: "+1555XXXXXXXXX"
    matrix: "@owner:matrix.example"  # future
  partner:
    language: sk                     # Slovak system messages
    signal: "+1555YYYYYYYYY"
```

**Validation logic:**
```python
def validate_sender(message):
    canonical = message["sender"]["id"]
    transport = message["transport"]
    transport_id = message["sender"]["transport_id"]

    # 1. Check canonical identity is allowed
    if canonical not in ALLOWED_IDENTITIES:
        return False, "unknown_identity"

    # 2. Check transport_id matches the registered binding
    expected = IDENTITY_BINDINGS[canonical].get(transport)
    if transport_id != expected:
        return False, "transport_id_mismatch"

    return True, None
```

### 3. Defense in Depth: Validate at Both Layers

| Layer | Validates |
|-------|-----------|
| **mesh** | Transport_id exists in bindings, basic sanitization |
| **joi** | Canonical identity in allowlist, full policy check |

mesh normalizes and verifies transport_id binding. joi trusts canonical ID but still validates it against its own allowlist. Neither layer alone is sufficient.

### 3.1 Unknown Senders (Spam/Phishing Protection)

Joi is visible in the Signal ecosystem as a regular user. Spammers, phishers, or random people may attempt to message her.

**mesh rejects unknown senders before they reach Joi:**

```python
def handle_incoming_signal(signal_message):
    """Called when signal-cli receives any message."""
    sender_phone = signal_message["source"]  # e.g., "+123456789"

    # Look up phone in ALL identity bindings
    canonical_id = lookup_canonical_id(sender_phone)

    if canonical_id is None:
        # Unknown sender - reject at mesh level
        log_security_event("INFO", "unknown_sender_rejected", {
            "transport": "signal",
            "transport_id": sender_phone,  # Log actual number for forensics
            "action": "dropped"
        })
        return  # Do NOT forward to Joi

    # Known sender - proceed with normal flow
    forward_to_joi(canonical_id, sender_phone, signal_message)


def lookup_canonical_id(transport_id: str) -> Optional[str]:
    """Reverse lookup: find canonical ID for a transport identifier."""
    for identity, bindings in IDENTITY_BINDINGS.items():
        if bindings.get("signal") == transport_id:
            return identity
    return None  # Not found = unknown sender
```

**Key behaviors:**
- Unknown senders are **dropped silently** (no response to spammer)
- Logged with actual phone number (forensics > spammer privacy)
- Joi never sees the message (reduced attack surface)
- No LLM resources wasted on spam

**Contrast with rate-limited known senders:**

| Scenario | Response |
|----------|----------|
| Unknown sender | Silent drop (don't confirm number is active) |
| Known sender, rate limited | Drop + notify via Signal: "Message not delivered. Wait X minutes." |

The notification for rate-limited known users is important UX - otherwise they think Joi froze.

**Signal-specific consideration:**
- Signal has "message requests" for unknown contacts
- signal-cli may need configuration to even receive these
- Default behavior: accept all messages, filter at mesh level

### 3.2 Group Message Handling

Joi uses two channels:
- **Direct (DM):** Private conversation with owner
- **Critical (Group):** Alert group for urgent notifications

**Group messages are processed if sender is in identity_bindings:**

```
Owner sends message in critical group
    ↓
mesh: sender phone in identity_bindings? YES (owner)
    ↓
Forward to Joi with conversation.type = "group"
    ↓
Joi processes and responds IN THE GROUP (not DM)
```

**Joi has full context awareness:**

| Field | Tells Joi |
|-------|-----------|
| `sender.id` | Who is talking (owner, partner, etc.) |
| `conversation.type` | Where: "direct" (DM) or "group" |
| `conversation.id` | Which conversation/group |

**Response routing rule:** Joi responds in the same channel where the message originated.

```python
def determine_response_channel(inbound_message):
    """Response goes back to same channel as inbound."""
    if inbound_message["conversation"]["type"] == "group":
        return {
            "target": "group",
            "group_id": inbound_message["conversation"]["id"]
        }
    else:
        return {
            "target": "direct",
            "group_id": None
        }
```

**Use case - Critical alert follow-up:**
```
Joi (to group): 🔥 FIRE ALARM triggered in kitchen!
Owner (in group): Joi, is anyone at home?
Joi (in group): Based on presence data, no one is home.
Owner (in group): What's the temperature reading?
Joi (in group): Kitchen sensor shows 28°C. May be false alarm.
```

**Group members who are NOT in identity_bindings:**
- Can receive messages (Signal group membership)
- Cannot talk to Joi (messages dropped at mesh)
- This is intentional: observation ≠ command authority

### 3.3 Group Addressing Behavior

Different channels have different response rules:

| Channel | Joi Responds To | Rationale |
|---------|-----------------|-----------|
| **DM** | Everything | You're obviously talking to Joi |
| **Critical group** | Everything | Critical = all talk is relevant, no filtering |
| **Regular group** (future) | Only when addressed by name | Avoid interrupting human conversations |

**Current scope (PoC):** Only DM and critical group exist. Both get full attention.

**Critical group rationale:**
- This is not a casual chat group
- If someone is talking in critical, it's about a critical situation
- Joi should listen to all messages and respond helpfully
- "No sacrifice is too big for critical"

**Future regular groups (if added):**
Would require name trigger ("Joi, ...") to avoid noise. Config:
```yaml
group_addressing:
  critical_groups: always_respond      # Current behavior
  regular_groups: require_name_trigger # Future behavior
  trigger_patterns:
    - "^joi[,:]?"
    - "^hey joi"
```

### 3.4 Multi-User / Multi-Conversation Support

The design supports multiple simultaneous conversations:

**Multiple DMs:**
```
Owner DM ──────► Joi ◄────── Partner DM
(conversation_id: abc)    (conversation_id: xyz)
```

**Multiple Groups:**
```
Critical Group ──────► Joi ◄────── Family Group (future)
(conversation_id: 123)           (conversation_id: 456)
```

**Key design decisions:**

| Aspect | Scope | Rationale |
|--------|-------|-----------|
| Rate limits | Per-conversation | Owner and partner don't share limits |
| Cooldown | Per-conversation | Rapid chat with one person doesn't block another |
| Memory/context | Per-conversation | Each conversation has its own history |
| LLM circuit breaker | Global | Protect total LLM resources |

**Identity vs Conversation:**
- `sender.id` = WHO is talking (owner, partner)
- `conversation.id` = WHERE they're talking (which DM or group)

Both are tracked. Joi can:
- Know who's talking (for permissions, personalization)
- Know which conversation (for context, routing replies)
- Maintain separate context per conversation
- Apply rate limits per conversation

### 4. Outbound: Same Rules Apply

When joi sends a message:
- `recipient.id` must be in canonical allowlist
- `recipient.transport_id` is looked up from bindings (joi doesn't need to know it)
- mesh validates the binding before sending

---

## Retry Strategy

Clients should retry on:
- `5xx` errors: Retry with exponential backoff (1s, 2s, 4s, max 30s)
- `429` rate limited: Wait for `retry_after` seconds
- Network errors: Retry with exponential backoff

Do NOT retry on:
- `4xx` errors (except 429): Fix the request first
- `replay_detected`: Generate new nonce

Max retries: 3 attempts, then log and alert.

---

## Health Check Endpoints

Each service exposes:

```
GET /health
```

Response:

```json
{
  "status": "healthy",
  "service": "joi",
  "version": "0.1.0",
  "timestamp": 1706918400000
}
```

---

## Emergency Stop

### Primary Method: Shutdown mesh VM via Proxmox

Owner has Proxmox mobile access. Emergency stop = shutdown mesh VM.

**Why this is better than Signal keywords:**
- Works even if Joi is in a loop ignoring commands
- Cuts communication path completely (mesh is the only egress)
- No code required - just VM management
- Joi continues running but is isolated (can investigate later)

**Procedure:**
1. Open Proxmox mobile app
2. Stop `mesh` VM
3. Joi is now isolated - cannot send or receive Signal messages
4. Investigate via Proxmox console to joi VM if needed
5. Restart mesh VM when resolved

### Secondary Method: Joi Safe Mode (optional convenience)

If implemented, owner can send `STOP` via Signal for graceful shutdown:

**Behavior:**
1. Joi recognizes keyword, enters safe mode
2. Stops all proactive messages and LLM calls
3. Responds: "Safe mode active. Restart mesh VM to resume."
4. Logs event

> **Note:** This is convenience, not security. If Joi is misbehaving badly enough to need stopping, don't trust it to process the stop command correctly. Use Proxmox.

### System Reset API (joi internal, optional)

```
POST /api/v1/system/reset
```

Resets circuit breakers and rate limit counters after resolving issues.

```json
{
  "reset": ["circuit_breaker", "rate_limits"]
}
```

> **Access Control:** Localhost-only via Proxmox console. Not exposed via Nebula.

---

## Future Considerations

- WebSocket for real-time events (reduce polling)
- Media message support (images from Signal)
- Delivery receipts callback from mesh to joi
- Bulk event ingestion endpoint for openhab

---

## Future: Web Access via HTTP Proxy

Planned capability for Joi to access the web via HTTP proxy on mesh VM.

### Architecture

- mesh VM runs Squid proxy on vmbr1 (10.99.0.1:3128)
- joi uses mesh as HTTP proxy for allowed web requests
- Proxy enforces domain allowlist (only approved APIs)
- No custom API endpoint - joi uses standard HTTP client with proxy

### joi Configuration

```python
# joi uses mesh as HTTP proxy
import requests

PROXY = "http://10.99.0.1:3128"

def web_search(query: str) -> dict:
    response = requests.get(
        "https://api.duckduckgo.com/",
        params={"q": query, "format": "json"},
        proxies={"http": PROXY, "https": PROXY},
        timeout=10
    )
    return response.json()
```

### mesh Proxy Config (Squid)

```squid
# Only joi can use the proxy
acl joi_vm src 10.99.0.2
http_access allow joi_vm
http_access deny all

# Only allowed domains
acl allowed_domains dstdomain .duckduckgo.com .openweathermap.org
http_access allow allowed_domains
http_access deny all
```

### Security Notes

- Domain allowlist on proxy (not arbitrary browsing)
- Results sanitized by joi before reaching LLM
- Rate limiting enforced by Policy Engine (before making request)
- All proxy requests logged on mesh
- Results treated as CONTEXT only (in `<search_results>` tags)
