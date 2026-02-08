# Joi System Channel

> Generic, type-agnostic interface for machine-to-machine communication.
> Version: 1.0 (Draft)
> Last updated: 2026-02-08

## Overview

The System Channel provides a unified interface for Joi to communicate with external systems (openhab, Zabbix, calendars, actuators, etc.) within the trusted Joi ecosystem. Unlike the Interactive Channel (Signal), the System Channel is not exposed externally.

## Two-Layer Architecture

Joi operates with two distinct control layers:

```
┌─────────────────────────────────────────────────────────────────┐
│                     PROTECTION LAYER                            │
│           (raw automation, LLM has NO say)                      │
│                                                                 │
│  Runs on: Joi VM, mesh VM                                       │
│  Purpose: Protect the ecosystem                                 │
│                                                                 │
│  Components:                                                    │
│  • Circuit breakers (trip on runaway behavior)                  │
│  • Rate limiters (hard caps, no override)                       │
│  • Watchdog processes (mesh integrity, heartbeat)               │
│  • Emergency stop (Proxmox-level, Signal STOP keyword)          │
│  • Input validation (size limits, schema enforcement)           │
│  • Replay protection (nonce tracking)                           │
│                                                                 │
│  Key property: These run BEFORE LLM sees anything, and AFTER    │
│  LLM produces output. LLM cannot bypass or influence them.      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ (if allowed through)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      LLM AGENT LAYER                            │
│              (context-aware decision making)                    │
│                                                                 │
│  Runs on: Joi VM                                                │
│  Purpose: Intelligent responses and actions                     │
│                                                                 │
│  Responsibilities:                                              │
│  • Decide what to read from System Channel                      │
│  • Decide what to write to System Channel                       │
│  • Decide what/when to notify owner (Interactive Channel)       │
│  • Decide priority and urgency of communications                │
│                                                                 │
│  Key property: Trusted for normal operations. All writes        │
│  go through LLM. No automated writes bypass the agent.          │
└─────────────────────────────────────────────────────────────────┘
```

**Why two layers?**

| Layer | Trust Model | Override |
|-------|-------------|----------|
| Protection | Zero trust - assumes LLM could be compromised | Cannot be overridden by LLM |
| LLM Agent | Trusted for decisions within bounds | Operates freely within protection limits |

The Protection Layer ensures that even if the LLM is prompt-injected, jailbroken, or malfunctioning, it cannot:
- Send unlimited messages (rate limiters)
- Flood external systems (circuit breakers)
- Bypass authentication (Nebula enforcement)
- Cause cascading failures (watchdog + emergency stop)

## Channel Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                          JOI CORE                               │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                   PROTECTION LAYER                        │  │
│  │   (rate limits, circuit breakers, validation, watchdog)   │  │
│  └───────────────────────────────────────────────────────────┘  │
│                              │                                  │
│                    ┌─────────▼─────────┐                        │
│                    │      LLM AGENT    │                        │
│                    │  (decision maker) │                        │
│                    └──────────┬────────┘                        │
│                               │                                 │
│              ┌────────────────┼────────────────┐                │
│              │                │                │                │
│    ┌─────────▼─────────┐     │     ┌──────────▼──────────┐     │
│    │ INTERACTIVE       │     │     │ SYSTEM CHANNEL      │     │
│    │ CHANNEL           │     │     │                     │     │
│    │                   │     │     │ ┌─────────────────┐ │     │
│    │ mesh ◄──► Signal  │     │     │ │ Source Registry │ │     │
│    │ (human comms)     │     │     │ │                 │ │     │
│    └───────────────────┘     │     │ │ openhab  [R]    │ │     │
│                              │     │ │ zabbix   [RW]   │ │     │
│                              │     │ │ calendar [RW]   │ │     │
│                              │     │ │ actuator [W]    │ │     │
│                              │     │ └─────────────────┘ │     │
│                              │     └─────────────────────┘     │
│  ┌───────────────────────────┴───────────────────────────────┐  │
│  │                   PROTECTION LAYER                        │  │
│  │      (output validation, rate limits, circuit breakers)   │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## Design Principles

1. **Two-Layer Control** - Protection Layer (raw automation) guards the ecosystem; LLM Agent Layer makes intelligent decisions within those bounds. LLM cannot bypass protection.

2. **LLM-Gated Writes** - All *intentional* writes to external systems go through the LLM. The LLM decides when and what to write based on context. Protection Layer automation (rate limits, circuit breakers) is separate and LLM has no say in those.

3. **Trusted LLM for Operations** - Within protection bounds, writes do not require owner approval. The LLM is trusted to make good decisions. Quality comes from good prompts, context, and guardrails.

4. **LLM-Decided Notifications** - If a system event or write result should inform the owner, the LLM decides that. The System Channel never directly notifies humans.

5. **Type-Agnostic** - Same interface pattern for all external systems. Source-specific adapters handle protocol translation.

6. **Internal Only** - System Channel endpoints are only accessible within the Joi ecosystem (Nebula mesh). Never exposed to the internet.

7. **Protection is Autonomous** - Circuit breakers, rate limiters, watchdogs, and emergency stops operate independently. They protect the ecosystem even if LLM is compromised.

## Access Modes

Each registered source has an access mode:

| Mode | Inbound (Read) | Outbound (Write) | Example |
|------|----------------|------------------|---------|
| `read` | System → Joi | - | openhab sensors |
| `write` | - | Joi → System | actuator commands |
| `read-write` | System → Joi | Joi → System | Zabbix alerts + ack |

## Source Registry

Sources are registered in the policy configuration:

```yaml
system_channel:
  sources:
    # Read-only: receives events, cannot be written to
    openhab:
      mode: read
      nebula_name: "openhab"           # Nebula certificate identity
      endpoint: "openhab.homelab.example"
      inbound:
        port: 8445
        event_types:
          - presence
          - sensors
          - weather
          - alert
          - state
        rate_limit: 240/hr

    # Read-write: receives alerts, can acknowledge them
    zabbix:
      mode: read-write
      nebula_name: "zabbix"
      endpoint: "zabbix.homelab.example"
      inbound:
        port: 8446
        event_types:
          - problem
          - resolved
          - info
        rate_limit: 120/hr
      outbound:
        port: 10051                    # Zabbix trapper port
        actions:
          - acknowledge
          - close
          - add_comment
        rate_limit: 60/hr

    # Write-only: receives commands, does not send events
    actuator:
      mode: write
      nebula_name: "actuator-bridge"
      endpoint: "actuator.homelab.example"
      outbound:
        port: 8447
        actions:
          - set_state
          - trigger
        rate_limit: 30/hr

    # Read-write: calendar integration
    calendar:
      mode: read-write
      nebula_name: "calendar-bridge"
      endpoint: "calendar.homelab.example"
      inbound:
        port: 8448
        event_types:
          - event_reminder
          - event_created
          - event_updated
        rate_limit: 60/hr
      outbound:
        port: 8449
        actions:
          - create_event
          - update_event
          - delete_event
        rate_limit: 30/hr
```

---

## API Specification

### Common Headers

All System Channel requests include:

```
Content-Type: application/json
X-Request-ID: <uuid>
X-Timestamp: <unix-epoch-ms>
X-Source: <source-name>              # e.g., "openhab", "zabbix"
```

> **Authentication:** Nebula certificate validation. The `nebula_name` in config must match the certificate CN of the connecting client.

### Common Response Format

```json
{
  "status": "ok" | "error",
  "request_id": "<echoed from request>",
  "timestamp": <unix-epoch-ms>,
  "error": {
    "code": "<error_code>",
    "message": "<human readable>"
  },
  "data": { }
}
```

---

## Inbound API (System → Joi)

### Generic Event Endpoint

```
POST https://joi:8445/api/v1/system/event
```

All sources use the same endpoint. The `source` field identifies the origin.

### Request Body

```json
{
  "source": "zabbix",                  // Registered source name
  "event_id": "<uuid>",                // Unique event ID (for dedup)
  "event_type": "problem",             // Source-specific event type
  "timestamp": 1707400000000,          // Event timestamp
  "priority": "normal",                // "low", "normal", "high", "critical"
  "data": {
    // Source-specific payload (see schemas below)
  },
  "metadata": {
    // Optional source-specific metadata
  }
}
```

### Validation Rules

1. `source` must be registered in `system_channel.sources`
2. Nebula certificate CN must match `nebula_name` for that source
3. `event_type` must be in source's `inbound.event_types` list
4. Source must have `mode: read` or `mode: read-write`
5. Rate limit per source applies
6. `event_id` used for deduplication (reject if seen in last 30 minutes)

### Response

```json
{
  "status": "ok",
  "request_id": "abc-123",
  "timestamp": 1707400000500,
  "data": {
    "received": true,
    "queued": true
  }
}
```

---

## Outbound API (Joi → System)

### Generic Action Endpoint

Joi calls the target system's endpoint to perform actions.

```
POST https://<endpoint>:<port>/api/v1/action
```

### Request Body

```json
{
  "action": "acknowledge",             // Action name from source config
  "action_id": "<uuid>",               // Unique ID for this action
  "timestamp": 1707400000000,
  "target": {
    // Target-specific identifier (e.g., alert ID, device ID)
    "id": "zabbix-problem-12345",
    "type": "problem"
  },
  "parameters": {
    // Action-specific parameters
    "message": "Acknowledged by Joi",
    "close": false
  },
  "context": {
    // Optional context for the action
    "triggered_by": "llm_decision",
    "related_event_id": "<original-event-id>"
  }
}
```

### Validation Rules (Policy Engine)

Before Joi sends an outbound action:

1. Target `source` must be registered with `mode: write` or `mode: read-write`
2. `action` must be in source's `outbound.actions` list
3. Rate limit for outbound actions applies
4. LLM must have explicitly decided to perform this action (no automation)

### Expected Response

```json
{
  "status": "ok",
  "action_id": "<echoed>",
  "timestamp": 1707400000500,
  "data": {
    "executed": true,
    "result": {
      // Action-specific result
    }
  }
}
```

---

## Source-Specific Schemas

### openhab (read-only)

Unchanged from current `api-contracts.md`. Events use:
- `/api/v1/system/event` with `source: "openhab"`
- Event types: `presence`, `sensors`, `weather`, `alert`, `state`

### Zabbix (read-write)

**Inbound Events:**

```json
{
  "source": "zabbix",
  "event_id": "zabbix-evt-12345",
  "event_type": "problem",
  "timestamp": 1707400000000,
  "priority": "high",
  "data": {
    "problem_id": "12345",
    "host": "webserver01",
    "trigger": "CPU usage > 90%",
    "severity": "high",
    "status": "problem",              // "problem" or "resolved"
    "started_at": 1707399000000,
    "value": 95.2
  }
}
```

**Outbound Actions:**

```json
{
  "action": "acknowledge",
  "action_id": "<uuid>",
  "target": {
    "id": "12345",
    "type": "problem"
  },
  "parameters": {
    "message": "Acknowledged by Joi. Owner notified.",
    "close": false
  }
}
```

### Actuator (write-only)

**Outbound Actions:**

```json
{
  "action": "set_state",
  "action_id": "<uuid>",
  "target": {
    "id": "living_room_lights",
    "type": "switch"
  },
  "parameters": {
    "state": "on",
    "brightness": 80              // Optional, device-specific
  }
}
```

### Calendar (read-write)

**Inbound Events:**

```json
{
  "source": "calendar",
  "event_id": "cal-reminder-xyz",
  "event_type": "event_reminder",
  "timestamp": 1707400000000,
  "priority": "normal",
  "data": {
    "calendar_event_id": "xyz",
    "title": "Team Meeting",
    "start_time": 1707408000000,
    "end_time": 1707411600000,
    "location": "Conference Room A",
    "reminder_minutes": 15
  }
}
```

**Outbound Actions:**

```json
{
  "action": "create_event",
  "action_id": "<uuid>",
  "target": {
    "id": "primary",               // Calendar ID
    "type": "calendar"
  },
  "parameters": {
    "title": "Follow-up call",
    "start_time": 1707494400000,
    "end_time": 1707498000000,
    "description": "Created by Joi based on conversation"
  }
}
```

---

## LLM Integration

### Available Tools for LLM

The LLM agent has access to System Channel through defined tools:

```yaml
llm_tools:
  # Read current state from a system
  system_read:
    description: "Read current state or query data from a registered system"
    parameters:
      source: string        # Registered source name
      query_type: string    # Source-specific query type
      query: object         # Query parameters

  # Write/act on a system
  system_write:
    description: "Perform an action on a registered system"
    parameters:
      source: string        # Registered source name
      action: string        # Action from source's allowed actions
      target: object        # Target identifier
      parameters: object    # Action parameters

  # List available systems and their capabilities
  system_list:
    description: "List registered systems and their capabilities"
    parameters: {}
```

### Example LLM Decision Flow

```
1. Zabbix sends problem event: "CPU > 90% on webserver01"
   └─► Event queued for agent processing

2. Agent loop picks up event, assembles context:
   - Recent events from this host
   - Owner's availability (presence data)
   - Time of day (quiet hours?)
   - Related alerts

3. LLM decides:
   "This is a high-severity alert on a production server.
    I should:
    a) Acknowledge in Zabbix so the team knows it's being handled
    b) Notify owner via Signal (high priority)"

4. Agent executes:
   └─► system_write(source="zabbix", action="acknowledge", ...)
   └─► send_message(channel="direct", priority="high", ...)

5. Both actions go through Policy Engine before execution
```

### Context for LLM Decisions

When the LLM evaluates system events, it receives:

```yaml
context:
  event:
    source: "zabbix"
    event_type: "problem"
    priority: "high"
    data: { ... }

  related_state:
    # Recent events from same source
    recent_events: [...]
    # Current state of related items
    current_state: { ... }

  owner_context:
    presence: "away"
    quiet_hours: false
    last_interaction: 1707390000000

  system_capabilities:
    zabbix:
      mode: "read-write"
      available_actions: ["acknowledge", "close", "add_comment"]
```

---

## Policy Engine Updates

### Source Validation

```python
def enforce_system_inbound(event: dict) -> PolicyResult:
    """Enforce policy on incoming system event."""

    source = event.get('source')

    # 1. Source must be registered
    if source not in get_registered_sources():
        return PolicyResult.DENY("Unknown source", log_level="WARN")

    # 2. Verify Nebula identity matches
    source_config = get_source_config(source)
    if not verify_nebula_identity(source_config['nebula_name']):
        return PolicyResult.DENY("Nebula identity mismatch", log_level="WARN")

    # 3. Source must allow reads
    if source_config['mode'] not in ['read', 'read-write']:
        return PolicyResult.DENY("Source is write-only", log_level="INFO")

    # 4. Event type must be allowed
    allowed_types = source_config['inbound']['event_types']
    if event['event_type'] not in allowed_types:
        return PolicyResult.DENY("Event type not allowed", log_level="INFO")

    # 5. Rate limit
    if is_rate_limited(f"system.inbound.{source}"):
        return PolicyResult.DENY("Rate limited", log_level="INFO")

    # 6. Deduplication
    if is_duplicate_event(event['event_id']):
        return PolicyResult.DENY("Duplicate event", log_level="DEBUG")

    return PolicyResult.ALLOW()


def enforce_system_outbound(action: dict) -> PolicyResult:
    """Enforce policy on outgoing system action."""

    source = action.get('source')

    # 1. Source must be registered
    if source not in get_registered_sources():
        return PolicyResult.DENY("Unknown target source", log_level="WARN")

    source_config = get_source_config(source)

    # 2. Source must allow writes
    if source_config['mode'] not in ['write', 'read-write']:
        return PolicyResult.DENY("Source is read-only", log_level="WARN")

    # 3. Action must be allowed
    allowed_actions = source_config['outbound']['actions']
    if action['action'] not in allowed_actions:
        return PolicyResult.DENY("Action not allowed", log_level="WARN")

    # 4. Rate limit
    if is_rate_limited(f"system.outbound.{source}"):
        return PolicyResult.DENY("Rate limited", log_level="INFO")

    # 5. Verify LLM decision (no automated writes)
    if not action.get('context', {}).get('triggered_by') == 'llm_decision':
        log_security_event("CRITICAL", "Non-LLM write attempt blocked")
        return PolicyResult.DENY("Only LLM-initiated writes allowed", log_level="CRITICAL")

    return PolicyResult.ALLOW()
```

---

## Migration from Current openhab API

The current openhab-specific endpoints can be migrated gradually:

| Current Endpoint | New Endpoint | Notes |
|------------------|--------------|-------|
| `POST /api/v1/openhab/presence` | `POST /api/v1/system/event` | `source: "openhab"`, `event_type: "presence"` |
| `POST /api/v1/openhab/sensors` | `POST /api/v1/system/event` | `source: "openhab"`, `event_type: "sensors"` |
| `POST /api/v1/openhab/weather` | `POST /api/v1/system/event` | `source: "openhab"`, `event_type: "weather"` |
| `POST /api/v1/openhab/alert` | `POST /api/v1/system/event` | `source: "openhab"`, `event_type: "alert"` |
| `POST /api/v1/openhab/state` | `POST /api/v1/system/event` | `source: "openhab"`, `event_type: "state"` |

**Migration strategy:**
1. Implement generic `/api/v1/system/event` endpoint
2. Keep legacy `/api/v1/openhab/*` endpoints as aliases (internally route to system event handler)
3. Update openhab integration to use new format when convenient
4. Deprecate legacy endpoints after transition

---

## Security Considerations

### Trust Model

```
┌────────────────────────────────────────────────────────────────┐
│                       TRUST BOUNDARIES                         │
│                                                                │
│  UNTRUSTED                    TRUSTED                          │
│  ─────────                    ───────                          │
│                                                                │
│  Internet ──X──► mesh VM ──► Joi VM ◄──► System Channel        │
│                    │                         │                 │
│                    │                    ┌────┴────┐            │
│                    │                    │ openhab │            │
│                    ▼                    │ zabbix  │            │
│               Signal only               │ etc.    │            │
│              (authenticated)            └─────────┘            │
│                                         All Nebula-            │
│                                         authenticated          │
└────────────────────────────────────────────────────────────────┘
```

### Two-Layer Security Model

```
┌────────────────────────────────────────────────────────────────┐
│                     PROTECTION LAYER                           │
│                (LLM has NO control over this)                  │
│                                                                │
│  Location: Joi VM + mesh VM                                    │
│  Trust: Zero - assumes LLM could be compromised                │
│                                                                │
│  Mechanisms:                                                   │
│  ┌──────────────────┬────────────────────────────────────────┐ │
│  │ Rate Limiters    │ Hard caps on messages/actions per hour │ │
│  │ Circuit Breakers │ Trip on rapid-fire behavior            │ │
│  │ Input Validation │ Size limits, schema enforcement        │ │
│  │ Output Validation│ Block forbidden patterns               │ │
│  │ Replay Protection│ Nonce tracking, timestamp validation   │ │
│  │ Mesh Watchdog    │ Challenge-response, integrity checks   │ │
│  │ Emergency Stop   │ Proxmox shutdown, STOP keyword         │ │
│  └──────────────────┴────────────────────────────────────────┘ │
│                                                                │
│  Key property: These CANNOT be influenced, bypassed, or        │
│  disabled by the LLM. They run as separate processes/code.     │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│                      LLM AGENT LAYER                           │
│                (trusted for normal operations)                 │
│                                                                │
│  Location: Joi VM (agent loop)                                 │
│  Trust: High - trusted to make good decisions                  │
│                                                                │
│  Responsibilities:                                             │
│  ┌──────────────────┬────────────────────────────────────────┐ │
│  │ Read decisions   │ What to query from systems             │ │
│  │ Write decisions  │ What actions to take on systems        │ │
│  │ Notify decisions │ What/when to tell the owner            │ │
│  │ Priority         │ Urgency assessment                     │ │
│  │ Context          │ Uses memory, presence, history         │ │
│  └──────────────────┴────────────────────────────────────────┘ │
│                                                                │
│  Key property: Operates freely WITHIN protection bounds.       │
│  All writes go through LLM. No owner approval needed.          │
└────────────────────────────────────────────────────────────────┘
```

### Why This Design?

**Problem:** LLM could be prompt-injected, jailbroken, or malfunction.

**Solution:** Protection Layer limits blast radius.

| Scenario | Without Protection | With Protection |
|----------|-------------------|-----------------|
| Prompt injection floods messages | Unlimited spam to owner | Rate limited to 60/hr |
| Runaway agent loop | Infinite LLM calls | Circuit breaker trips at 120/hr |
| Compromised LLM writes everywhere | Unrestricted system access | Only allowed actions to registered sources |
| Attacker replays old requests | Duplicate actions executed | Nonce rejection |

### Why Writes are Trusted (Within Bounds)

1. **LLM-Gated:** Every intentional write goes through LLM decision. Protection automation is separate.

2. **Policy Enforced:** Policy Engine validates every action before execution.

3. **Rate Limited:** Protection Layer caps outbound actions regardless of LLM intent.

4. **Logged:** All actions are logged with full context for audit.

5. **Nebula Only:** System Channel is only accessible within the Nebula mesh. No external exposure.

### LLM Quality Guardrails

Since writes are trusted within bounds, LLM quality is critical:

1. **Clear System Prompts:** Define what actions are appropriate and when.

2. **Context Awareness:** LLM receives full context (owner presence, time, history).

3. **Action Confirmation in Prompt:** Before executing, LLM should "think" about consequences.

4. **Audit Log:** All LLM decisions leading to writes are logged for review.

```yaml
llm_guardrails:
  # Require LLM to explain decision before action
  require_reasoning: true

  # High-impact actions require extra consideration
  high_impact_actions:
    - source: actuator
      action: set_state
      require_context:
        - owner_presence
        - time_of_day
```

### Protection Layer Components

These run independently of LLM and cannot be overridden:

```yaml
protection_layer:
  # Joi VM components
  joi_vm:
    rate_limiters:
      outbound_messages: 60/hr
      llm_calls: 120/hr
      system_writes: 60/hr
    circuit_breakers:
      llm_calls:
        window: 60min
        max: 120
        cooldown: 5min
      system_writes:
        window: 60min
        max: 60
        cooldown: 5min
    input_validation:
      max_message_length: 4096
      max_event_size: 10240
    output_validation:
      block_patterns: [...]
      max_length: 2048

  # mesh VM components
  mesh_vm:
    rate_limiters:
      inbound_messages: 120/hr  # Per non-owner user
      signal_sends: 60/hr
    watchdog:
      heartbeat_interval: 10s
      challenge_response: true
      on_failure: shutdown_mesh
    input_validation:
      max_signal_length: 1500
      unknown_sender: drop

  # Both VMs
  shared:
    replay_protection:
      nonce_retention: 15min
      timestamp_tolerance: 5min
    emergency_stop:
      proxmox_shutdown: true
      signal_keyword: "STOP"
```

---

## Rate Limits Summary

| Direction | Scope | Default | Notes |
|-----------|-------|---------|-------|
| Inbound (per source) | per source | 120/hr | Configurable per source |
| Outbound (per source) | per source | 60/hr | Configurable per source |
| Outbound (global) | all sources | 120/hr | Total writes across all sources |

---

## Future Considerations

1. **Query API:** Allow LLM to query current state from systems (not just receive events).

2. **Webhook Registration:** Dynamic registration of new sources via API.

3. **Schema Validation:** JSON Schema per source for stricter event validation.

4. **Async Actions:** Long-running actions with callback on completion.

5. **Action Batching:** Multiple related actions in a single request.

---

## Summary

| Aspect | Design |
|--------|--------|
| **Architecture** | Two layers: Protection (automation) + LLM Agent (decisions) |
| **Protection Layer** | Raw automation, LLM has no control, guards ecosystem |
| **LLM Agent Layer** | Trusted for operations within protection bounds |
| **Access Modes** | read, write, read-write per source |
| **Write Control** | LLM-gated for intentional writes; Protection automated separately |
| **Owner Approval** | Not required, writes are trusted within bounds |
| **Notifications** | LLM decides if/when to inform owner |
| **Authentication** | Nebula certificates |
| **Rate Limits** | Protection Layer enforces hard caps (LLM cannot override) |
| **Migration** | Gradual, legacy openhab endpoints as aliases |
