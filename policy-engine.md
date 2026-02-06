# Joi Policy Engine

> Central security enforcement for all Joi actions.
> Version: 1.0 (Draft)
> Last updated: 2026-02-04

## Overview

The Policy Engine is the **gatekeeper** for all Joi actions. Every input and output passes through it. If a rule is violated, the action is blocked.

```
                    ┌─────────────────────────┐
   Signal ─────────►│                         │─────────► Agent Loop
   openhab ────────►│     POLICY ENGINE       │
                    │                         │◄───────── Agent Loop
   Signal ◄─────────│    (rules + logging)    │
                    └─────────────────────────┘
```

## Design Principles

1. **Default Deny** - Actions are blocked unless explicitly allowed
2. **Fail Closed** - On error, block the action (don't fail open)
3. **Log Everything** - All policy decisions are logged
4. **Separation** - Policy is configuration, not code

---

## Policy Categories

| Category | What It Controls |
|----------|------------------|
| **Inbound** | What can come IN to Joi (Signal messages, openhab events) |
| **Outbound** | What can go OUT from Joi (Signal messages) |
| **Rate Limits** | How often actions can occur |
| **Content** | What content is allowed/blocked |
| **Identity** | Who can interact with Joi |

---

## Rule Definitions

### 1. Identity Rules

> **Security Invariant:** Authorization uses canonical identity (`id`), NOT transport identifiers.
> Transport identifiers are for routing only. See `api-contracts.md` → "Security Invariants".

```yaml
identity:
  # Canonical identities and their transport bindings
  # Authorization checks canonical ID; bindings used for routing/verification
  identities:
    owner:
      role: admin
      language: en              # Preferred language for system messages
      transports:
        signal: "+1555XXXXXXXXX"
        # matrix: "@owner:matrix.example"  # future
    # Example: partner with different language
    # partner:
    #   role: user
    #   language: sk            # Slovak system messages
    #   transports:
    #     signal: "+1555YYYYYYYYY"

  # Who can send messages to Joi (canonical IDs)
  allowed_senders:
    - id: "owner"

  # Where Joi can send messages (canonical IDs)
  allowed_recipients:
    direct:
      - id: "owner"
    critical:
      - id: "alerts_group"

  # Group bindings and permissions
  groups:
    alerts_group:
      type: critical           # critical | regular
      transports:
        signal: "GROUP_ID_HERE"
      # Who can talk to Joi in this group (must also be in allowed_senders)
      participants:
        - id: "owner"
        - id: "partner"        # Partner can talk to Joi in critical group

    # Example: future family group
    # family_group:
    #   type: regular          # Regular = name trigger required
    #   transports:
    #     signal: "FAMILY_GROUP_ID"
    #   participants:
    #     - id: "owner"
    #     - id: "partner"

  # openhab sources (Nebula identity verified automatically)
  allowed_openhab_sources:
    - host: "openhab.homelab.example"
      nebula_name: "openhab"  # Nebula certificate name

# Unknown sender handling (mesh level)
unknown_senders:
  action: drop           # drop = silent reject, no response
  log_level: INFO        # Log all unknown sender attempts
  log_full_identifier: true  # Log actual phone number for forensics
  # Note: Unknown senders are rejected at mesh VM, never forwarded to Joi
```

### 1.1 Unknown Sender Protection

Joi is visible in Signal as a regular user. Spammers and phishers may try to contact her.

**Protection:** mesh VM rejects unknown senders before they reach Joi.

```
Signal message from unknown number
    ↓
mesh: lookup phone in identity_bindings
    ↓
Not found → DROP (silent, no response, logged)
    ↓
Joi never sees the message
```

**Why silent drop (not error response)?**
- Don't confirm to spammers that the number is active
- Don't waste resources on crafted responses
- Reduces attack surface for prompt injection via spam

### 2. Rate Limit Rules

```yaml
rate_limits:
  # Outbound rate limits
  outbound:
    # DMs: per-user limits (each person gets their own quota)
    dm:
      scope: per_user
      default:
        max_per_hour: 60
        max_per_minute: 10
        cooldown_seconds: 5
      # Owner gets higher limits (primary user)
      overrides:
        owner:
          max_per_hour: 120    # 2x regular
          max_per_minute: 20

    # Regular groups: per-group limits
    regular_group:
      scope: per_group
      max_per_hour: 60
      max_per_minute: 10
      cooldown_seconds: 5

    # Critical group: SPECIAL - higher limits, event-triggered unlimited
    critical_group:
      scope: global            # Only one critical group
      event_triggered:
        max_per_hour: null     # UNLIMITED - safety first, never block real alerts
      llm_escalated:
        max_per_hour: 120      # 2x regular - generous but bounded
        max_per_minute: 10     # Prevent prompt injection DoS
      cooldown_seconds: 1      # Faster for critical

  # Inbound rate limits (enforced at MESH, not Joi)
  # Reason: stop floods before they consume Nebula bandwidth
  inbound:
    # When rate limited, notify the sender (don't leave them confused)
    on_rate_limit:
      action: drop_and_notify
      # Message loaded from file for easy translation
      messages_dir: /etc/mesh-proxy/messages/
      message_file: rate_limit.txt    # Uses {lang}/rate_limit.txt
      # Fallback if file not found
      message_fallback: "Message not delivered. Too many messages. Please wait."
      # Note: This is mesh → sender, does NOT go through Joi or count toward limits

# Example message files:
# /etc/mesh-proxy/messages/en/rate_limit.txt:
#   ⚠️ Message not delivered to Joi.
#   You've sent too many messages. Please wait {minutes_remaining} minutes.
#
# /etc/mesh-proxy/messages/sk/rate_limit.txt:
#   ⚠️ Správa nebola doručená Joi.
#   Poslali ste príliš veľa správ. Počkajte {minutes_remaining} minút.
#
# Language selection:
#   1. Look up sender's canonical ID
#   2. Get language from identity config (e.g., owner.language = "en")
#   3. Load message from {messages_dir}/{language}/rate_limit.txt
#   4. If file not found, use message_fallback

    # DMs: per-user (prevent one person flooding)
    dm:
      scope: per_user
      default:
        max_per_hour: 120
        max_per_minute: 20
      # Owner has no inbound limits (primary user)
      overrides:
        owner:
          max_per_hour: null   # UNLIMITED
          max_per_minute: null

    # Regular groups: per-user-per-group
    regular_group:
      scope: per_user_per_group
      default:
        max_per_hour: 120
        max_per_minute: 20
      overrides:
        owner:
          max_per_hour: null   # UNLIMITED

    # Critical group: NO LIMIT - all critical discussion matters
    critical_group:
      max_per_hour: null       # UNLIMITED
      max_per_minute: null     # UNLIMITED
    openhab:
      presence:
        max_per_hour: 60
      sensors:
        max_per_hour: 24      # Every 2.5 min batches
      alerts:
        max_per_hour: 120     # Allow many alerts
      state:
        max_per_hour: 240     # Frequent state changes OK

  # Agent actions
  agent:
    llm_calls_per_hour: 120   # Prevent runaway loops
    proactive_per_day: 20     # Limit unprompted outreach

  # Circuit breaker configuration
  circuit_breaker:
    window_type: rolling      # Rolling window, not fixed hourly reset
    window_minutes: 60        # 60-minute rolling window

    llm_calls:
      max_per_window: 120
      on_trip: respond_with_error   # "I need a moment to process."
      cooldown_minutes: 5           # Wait before retrying after trip

    outbound_messages:
      max_per_window: 60      # Direct channel
      on_trip: queue          # Queue messages, deliver when limit resets

    # Manual reset available via /api/v1/system/reset (Proxmox console only)
```

### 3. Content Rules

```yaml
content:
  # Input content rules
  input:
    signal:
      max_length: 4096
      allow_media: false      # Text only for now
      normalize_unicode: true

    openhab:
      max_event_size_bytes: 10240
      require_event_id: true
      max_description_length: 200
      allowed_event_types:
        - presence
        - sensors
        - weather
        - alert
        - state

  # Output content rules
  output:
    max_length: 2048
    block_patterns:
      - pattern: "https?://(?!signal\\.)"
        reason: "External URLs not allowed"
        context: all                    # Block in all messages
      - pattern: "CRITICAL INSTRUCTIONS"
        reason: "System prompt leakage"
        context: all
      - pattern: "```(bash|sh|python)"
        reason: "Executable code blocks in proactive messages"
        context: proactive_only         # Only block in proactive messages, not user-initiated
    require_printable: true   # No control characters

# Note: Code blocks are allowed in responses to user questions (user_initiated: true)
# but blocked in proactive messages to prevent prompt injection from triggering
# code output that might be copy-pasted by the user.
```

### 4. Channel Rules

**Response routing:** Joi responds in the same channel where the message originated.
- Message from DM → Response to DM
- Message from critical group → Response to critical group

This enables natural follow-up conversations about critical alerts:
```
Joi (to group): FIRE ALARM triggered!
Owner (in group): Is anyone home?
Joi (in group): No, last departure 2 hours ago.
```

```yaml
channels:
  # Response routing
  response_routing: same_as_inbound  # Always respond where message came from

  # Group behavior
  group_addressing:
    dm: always_respond                # DM = always respond
    critical_group: always_respond    # Critical = all talk is relevant
    regular_group: require_name_trigger  # Future: require "Joi, ..." trigger
    # Note: PoC only has DM + critical group, both get full attention

    # For future regular groups (not in PoC scope)
    trigger_patterns:
      - "^joi[,:]?"                   # "Joi, ..." or "Joi: ..."
      - "^hey joi"                    # "Hey Joi, ..."

  # What triggers critical channel
    # Event types that always go to critical
    # NOTE: Compromised IoT devices (e.g., pwned Zigbee smoke alarm) are defended
    # against via IoT Event Handling rules (Section 6) - state deduplication,
    # confirmation loop, and flapping detection. See iot_events config.
    event_types:
      - smoke_alarm
      - fire_alarm
      - security_alert
      - intrusion_detected
      - gas_leak
      - flood_warning
      - storm_warning
      - medical_alert

    # Allow LLM to escalate (with rate limits to prevent DoS via prompt injection)
    allow_llm_escalation: true
    llm_escalation_limit: 120       # 2x direct channel (separate from event-triggered)
    escalation_keywords:
      - "urgent"
      - "emergency"
      - "critical"
      - "danger"

  # What stays on direct channel
  direct_only:
    - normal_conversation
    - proactive_chat
    - welcome_messages
    - weather_updates     # Non-severe
    - reminders
```

### 5. openhab Rules (Read-Only Enforcement)

```yaml
openhab:
  # Joi can NEVER write to openhab
  mode: read_only

  # Allowed data sources
  allowed_items:
    presence:
      - "owner_presence"
      - "car_presence"
      - "partner_presence"
    sensors:
      - pattern: "*_temperature"
      - pattern: "*_humidity"
      - pattern: "*_motion"
    weather:
      - "weather_current"
      - "weather_forecast"

  # Blocked (even if openhab sends them)
  blocked_items:
    - pattern: "*_password*"
    - pattern: "*_secret*"
    - pattern: "*_key*"
```

### 6. IoT Event Handling (Flood Protection)

Defends against compromised IoT devices flooding critical channel.

> **Threat Model:** A Zigbee smoke alarm can be pwned (weak security). Attacker triggers fake "smoke detected" signals. openhab faithfully reports them. Without this protection, Joi floods the critical channel with unlimited fake alerts.

```yaml
iot_events:
  # State-based deduplication
  deduplication:
    enabled: true
    ignore_duplicate_states: true     # Ignore if device already in this state

  # Per-device rate limiting
  rate_limits:
    transitions_per_hour: 12          # Max state changes per device per hour
    on_exceeded: suppress_and_warn    # Send "sensor malfunction" warning

  # Confirmation loop for critical devices
  confirmation_loop:
    critical_device_types:
      - smoke_alarm
      - fire_alarm
      - co_alarm
      - gas_leak
      - security_breach
      - water_leak

    # Alert escalation
    max_alerts_per_state: 3           # Max critical alerts for same triggered state
    alert_intervals_minutes: [0, 5, 15]  # Immediate, then 5 min, then 15 min

    # After max alerts reached
    on_max_reached: demote_to_direct  # Switch to direct channel
    max_reached_message: "I've sent {count} alerts about {device}. Please check or acknowledge."

    # Acknowledgment resets
    auto_acknowledge_on_clear: true   # Device clearing = implicit ack

  # Anomaly detection
  anomaly:
    flapping_threshold: 6             # >6 transitions/hour = flapping
    flapping_response: suppress       # Stop alerting
    flapping_message: "Possible sensor malfunction: {device} triggered {count} times in 1 hour."
```

**Alert flow for critical device (e.g., smoke_alarm):**

```
Event 1: smoke_alarm → triggered
  └─ State change? YES (was "clear")
  └─ Flapping? NO (1st transition)
  └─ Alerts sent: 0 < max(3)
  └─ Decision: SEND_CRITICAL (alert #1)

Event 2: smoke_alarm → triggered (duplicate)
  └─ State change? NO (already "triggered")
  └─ Decision: SUPPRESS (deduplicated)

[5 minutes later - no owner response]

Event 3: (timer-based follow-up)
  └─ State still "triggered", not acknowledged
  └─ Alerts sent: 1 < max(3), interval reached
  └─ Decision: SEND_CRITICAL (alert #2, "still triggered")

[Owner responds "ok"]

Event 4: (any future smoke_alarm → triggered)
  └─ Acknowledged? YES
  └─ Decision: SUPPRESS until state clears

Event 5: smoke_alarm → clear
  └─ State change: triggered → clear
  └─ Reset: alerts_sent=0, acknowledged=false
  └─ Decision: SUPPRESS (optionally send "all clear" to direct)
```

### 7. Time-Based Rules

```yaml
time_rules:
  quiet_hours:
    start: 23          # 11 PM
    end: 7             # 7 AM
    timezone: "UTC"

    # During quiet hours:
    actions:
      block_proactive_direct: true
      allow_proactive_critical: true   # Safety overrides quiet
      allow_user_initiated: true       # Always respond if user messages

  weekday_adjustments:
    saturday:
      quiet_hours_end: 9    # Sleep in
    sunday:
      quiet_hours_end: 9
```

---

## Enforcement Flow

### Inbound Message (Signal)

```python
def enforce_inbound_signal(message: dict) -> PolicyResult:
    """Enforce policy on incoming Signal message."""

    # 1. Identity check (uses canonical ID, NOT transport_id)
    # See api-contracts.md → "Security Invariants"
    canonical_id = message['sender']['id']  # e.g., "owner"
    if canonical_id not in get_allowed_senders():
        return PolicyResult.DENY("Unknown sender", log_level="WARN")

    # 1b. Verify transport_id matches binding (defense in depth)
    # mesh should have already verified this, but double-check
    transport = message.get('transport', 'signal')
    transport_id = message['sender']['transport_id']
    expected = get_transport_binding(canonical_id, transport)
    if transport_id != expected:
        log_security_event("WARN", f"Transport ID mismatch for {canonical_id}")
        return PolicyResult.DENY("Transport ID mismatch", log_level="WARN")

    # 2. Rate limit check (keyed by canonical ID)
    if is_rate_limited('inbound.signal', canonical_id):
        return PolicyResult.DENY("Rate limited", log_level="INFO")

    # 3. Content check
    content = message['content']
    if len(content.get('text', '')) > POLICY['content.input.signal.max_length']:
        return PolicyResult.DENY("Message too long", log_level="INFO")

    if content['type'] != 'text' and not POLICY['content.input.signal.allow_media']:
        return PolicyResult.DENY("Media not allowed", log_level="INFO")

    # 4. Log and allow
    log_policy_decision("inbound_signal", "ALLOW", message_id=message['message_id'])
    return PolicyResult.ALLOW()
```

### Inbound Event (openhab)

```python
def enforce_inbound_openhab(event: dict) -> PolicyResult:
    """Enforce policy on incoming openhab event."""

    # 1. Source verification (Nebula already handled, but double-check)
    source = event.get('source')
    if source not in get_allowed_openhab_sources():
        return PolicyResult.DENY("Unknown openhab source", log_level="WARN")

    # 2. Event type check
    event_type = event.get('event_type')
    if event_type not in POLICY['content.input.openhab.allowed_event_types']:
        return PolicyResult.DENY(f"Event type {event_type} not allowed", log_level="INFO")

    # 3. Rate limit check
    rate_key = f"inbound.openhab.{event_type}"
    if is_rate_limited(rate_key, source):
        return PolicyResult.DENY("Rate limited", log_level="DEBUG")

    # 4. Item filtering
    if not is_item_allowed(event):
        return PolicyResult.DENY("Item not in allowlist", log_level="DEBUG")

    # 5. Size check (with JSON serialization safety)
    try:
        event_size = len(json.dumps(event))
    except (TypeError, ValueError) as e:
        return PolicyResult.DENY(f"Event not JSON-serializable: {e}", log_level="WARN")
    if event_size > POLICY['content.input.openhab.max_event_size_bytes']:
        return PolicyResult.DENY("Event too large", log_level="INFO")

    # 6. IoT event flood protection (for alert/state events)
    if event_type in ['alert', 'state']:
        iot_decision = process_iot_event(event)
        if iot_decision.action == 'suppress':
            log_policy_decision("inbound_openhab", "ALLOW_SUPPRESSED",
                                event_id=event['event_id'], reason=iot_decision.reason)
            return PolicyResult.ALLOW(suppress_alert=True)
        elif iot_decision.action == 'demote':
            event['_force_channel'] = 'direct'
            event['_alert_message'] = iot_decision.message
        elif iot_decision.action == 'malfunction_warning':
            queue_direct_message(iot_decision.message)
            return PolicyResult.ALLOW(suppress_alert=True)
        # else: 'send_critical' - proceed normally

    # 7. Log and allow
    log_policy_decision("inbound_openhab", "ALLOW", event_id=event['event_id'])
    return PolicyResult.ALLOW()
```

### IoT Event Processing (Flood Protection)

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class IoTDecision:
    action: str          # 'send_critical', 'suppress', 'demote', 'malfunction_warning'
    reason: str
    message: Optional[str] = None

def process_iot_event(event: dict) -> IoTDecision:
    """
    Process IoT event with deduplication, confirmation loop, and anomaly detection.
    Defends against compromised IoT devices flooding critical channel.
    """
    device_id = extract_device_id(event)
    device_type = classify_device_type(device_id)
    new_state = extract_state(event)
    now = current_time_ms()

    # Get or create device state
    device = db.get_or_create_device_state(device_id, device_type)

    # --- STEP 1: State-based deduplication ---
    if POLICY['iot_events.deduplication.enabled']:
        if device.current_state == new_state:
            return IoTDecision('suppress', 'duplicate_state')

    # --- STEP 2: Flapping detection ---
    device = update_transition_count(device, now)
    threshold = POLICY['iot_events.anomaly.flapping_threshold']

    if device.transitions_this_hour > threshold:
        if not device.malfunction_warning_sent:
            device.malfunction_warning_sent = True
            db.save_device_state(device)
            msg = f"Possible sensor malfunction: {device_id} triggered {device.transitions_this_hour} times in 1 hour."
            return IoTDecision('malfunction_warning', 'flapping', msg)
        return IoTDecision('suppress', 'flapping_already_warned')

    # --- STEP 3: Update state on transition ---
    if device.current_state != new_state:
        device.current_state = new_state
        device.state_changed_at = now
        device.alerts_sent_this_state = 0
        device.acknowledged = False
        db.save_device_state(device)

    # --- STEP 4: Check if critical alert needed ---
    is_critical_type = device_type in POLICY['iot_events.confirmation_loop.critical_device_types']
    is_triggered = new_state.lower() in ['triggered', 'on', 'detected', 'alarm', 'open', 'breach']

    if not (is_critical_type and is_triggered):
        return IoTDecision('suppress', 'non_critical_state')

    # --- STEP 5: Confirmation loop ---
    if device.acknowledged:
        return IoTDecision('suppress', 'already_acknowledged')

    max_alerts = POLICY['iot_events.confirmation_loop.max_alerts_per_state']
    if device.alerts_sent_this_state >= max_alerts:
        msg = f"I've sent {max_alerts} alerts about {device_id}. Please check or acknowledge."
        return IoTDecision('demote', 'max_alerts_reached', msg)

    # Check interval timing for follow-up alerts
    intervals = POLICY['iot_events.confirmation_loop.alert_intervals_minutes']
    if device.alerts_sent_this_state > 0 and device.last_alert_at:
        idx = min(device.alerts_sent_this_state, len(intervals) - 1)
        required_wait_ms = intervals[idx] * 60 * 1000
        if (now - device.last_alert_at) < required_wait_ms:
            return IoTDecision('suppress', 'interval_not_reached')

    # --- STEP 6: Send critical alert ---
    device.alerts_sent_this_state += 1
    device.last_alert_at = now
    db.save_device_state(device)

    return IoTDecision('send_critical', f'alert_{device.alerts_sent_this_state}_of_{max_alerts}')


def update_transition_count(device: DeviceState, now: int) -> DeviceState:
    """Track transitions per hour for flapping detection."""
    hour_ms = 60 * 60 * 1000

    if device.hour_window_start is None or (now - device.hour_window_start) > hour_ms:
        device.hour_window_start = now
        device.transitions_this_hour = 1
        device.malfunction_warning_sent = False
    else:
        device.transitions_this_hour += 1

    return device


def classify_device_type(device_id: str) -> str:
    """Classify device type from ID."""
    device_id_lower = device_id.lower()

    type_patterns = {
        'smoke_alarm': ['smoke'],
        'fire_alarm': ['fire'],
        'co_alarm': ['co', 'carbon'],
        'gas_leak': ['gas'],
        'water_leak': ['water', 'flood', 'leak'],
        'security_breach': ['security', 'intrusion', 'breach'],
        'entry_sensor': ['door', 'window', 'entry'],
        'motion': ['motion', 'pir'],
    }

    for device_type, patterns in type_patterns.items():
        if any(p in device_id_lower for p in patterns):
            return device_type

    return 'unknown'
```

### Owner Acknowledgment

```python
# Recognition patterns for owner acknowledgment
ACKNOWLEDGMENT_PATTERNS = [
    r'\b(ok|okay|ack|acknowledged|got it|thanks|seen|aware)\b',
    r'\b(i know|checking|on it|will check)\b',
]

def check_for_acknowledgment(message_text: str) -> bool:
    """Check if owner message acknowledges active alerts."""
    text_lower = message_text.lower()
    for pattern in ACKNOWLEDGMENT_PATTERNS:
        if re.search(pattern, text_lower):
            return True
    return False

def acknowledge_active_alerts():
    """Mark all unacknowledged triggered devices as acknowledged."""
    devices = db.get_unacknowledged_triggered_devices()
    now = current_time_ms()

    for device in devices:
        device.acknowledged = True
        device.acknowledged_at = now
        db.save_device_state(device)

    return len(devices)
```

### Outbound Message (Signal)

```python
def enforce_outbound_signal(message: dict) -> PolicyResult:
    """Enforce policy on outgoing Signal message."""

    channel = message.get('priority', 'normal')  # 'normal' = direct, 'critical' = critical
    recipient_id = message.get('recipient', {}).get('id')  # Canonical ID
    content = message.get('content', {}).get('text', '')

    # 1. Recipient check (uses canonical ID, NOT transport_id)
    # See api-contracts.md → "Security Invariants"
    if channel == 'normal':
        if recipient_id not in get_allowed_recipients('direct'):
            return PolicyResult.DENY("Recipient not allowed", log_level="WARN")
    elif channel == 'critical':
        if recipient_id not in get_allowed_recipients('critical'):
            return PolicyResult.DENY("Group not allowed", log_level="WARN")

    # 2. Rate limit check (per-conversation)
    conversation_id = message.get('conversation_id')

    if channel == 'normal':
        if is_rate_limited('outbound.direct', key=conversation_id):
            return PolicyResult.DENY("Rate limited", log_level="INFO")

        # Cooldown check (per-conversation)
        last_sent = get_last_send_time('direct', key=conversation_id)
        cooldown = POLICY['rate_limits.outbound.direct.cooldown_seconds']
        if time.time() - last_sent < cooldown:
            return PolicyResult.DENY("Cooldown active", log_level="DEBUG")

    elif channel == 'critical':
        # LLM-escalated messages have separate (stricter) rate limits
        # True critical events (smoke, fire) from openhab bypass this
        if message.get('escalated', False):
            if is_rate_limited('outbound.critical.llm_escalated'):
                log_security_event("WARN", "LLM escalation rate limit hit - possible prompt injection")
                return PolicyResult.DENY("LLM escalation rate limited", log_level="WARN")

    # 3. Content length check
    if len(content) > POLICY['content.output.max_length']:
        return PolicyResult.DENY("Response too long", log_level="INFO")

    # 4. Unicode normalization (prevent homoglyph bypass)
    # NFKC normalizes: Cyrillic 'а' → Latin 'a', full-width chars → ASCII
    import unicodedata
    normalized_content = unicodedata.normalize('NFKC', content)

    # 5. Content pattern check (on normalized content)
    is_proactive = message.get('is_proactive', False)
    for rule in POLICY['content.output.block_patterns']:
        # Check context: 'all' applies always, 'proactive_only' only for proactive messages
        rule_context = rule.get('context', 'all')
        if rule_context == 'proactive_only' and not is_proactive:
            continue  # Skip this rule for user-initiated responses
        if re.search(rule['pattern'], normalized_content, re.IGNORECASE):
            return PolicyResult.DENY(rule['reason'], log_level="WARN")

    # 6. Quiet hours check (normal/direct proactive only)
    if channel == 'normal' and message.get('is_proactive', False):
        if is_quiet_hours() and not message.get('user_initiated', False):
            return PolicyResult.DENY("Quiet hours active", log_level="DEBUG")

    # 7. Log and allow
    # Note: Step 4-5 use NFKC normalization to prevent Unicode homoglyph attacks
    # (e.g., Cyrillic 'а' in "https" or full-width "ｈｔｔｐｓ" bypassing URL blocks)
    log_policy_decision("outbound_signal", "ALLOW",
                        channel=channel,
                        length=len(content))
    return PolicyResult.ALLOW()
```

### Agent Action (LLM Call)

```python
def enforce_agent_action(action: str, context: dict) -> PolicyResult:
    """Enforce policy on agent actions."""

    # 1. LLM call rate limit
    if action == 'llm_call':
        if is_rate_limited('agent.llm_calls_per_hour'):
            return PolicyResult.DENY("LLM rate limited", log_level="WARN")

    # 2. Proactive message limit
    if action == 'proactive_message':
        if is_rate_limited('agent.proactive_per_day'):
            return PolicyResult.DENY("Proactive limit reached", log_level="INFO")

    # 3. openhab write attempt (should never happen)
    if action == 'openhab_write':
        log_security_event("CRITICAL", "Attempted openhab write blocked")
        return PolicyResult.DENY("openhab is read-only", log_level="CRITICAL")

    return PolicyResult.ALLOW()
```

---

## Policy Result Handling

```python
class PolicyResult:
    ALLOW = lambda: PolicyResult(allowed=True)
    DENY = lambda reason, log_level="INFO": PolicyResult(
        allowed=False,
        reason=reason,
        log_level=log_level
    )

    def __init__(self, allowed: bool, reason: str = None, log_level: str = "INFO"):
        self.allowed = allowed
        self.reason = reason
        self.log_level = log_level


def handle_policy_result(result: PolicyResult, action: str, fallback=None):
    """Handle policy decision."""

    if result.allowed:
        return True

    # Log the denial
    log_policy_denial(action, result.reason, result.log_level)

    # Increment denial counter (for anomaly detection)
    increment_denial_counter(action)

    # Alert if too many denials
    if get_denial_count(action, window_minutes=60) > 10:
        alert_admin(f"High denial rate for {action}")

    return fallback
```

---

## Logging and Monitoring

### Log Format

```json
{
  "timestamp": "2026-02-04T15:30:00Z",
  "event_type": "policy_decision",
  "action": "outbound_signal",
  "decision": "ALLOW",
  "channel": "direct",
  "details": {
    "content_length": 142,
    "recipient": "[redacted]"
  }
}
```

```json
{
  "timestamp": "2026-02-04T15:30:05Z",
  "event_type": "policy_denial",
  "action": "outbound_signal",
  "decision": "DENY",
  "reason": "Rate limited",
  "log_level": "INFO",
  "details": {
    "current_count": 60,
    "limit": 60,
    "window": "hour"
  }
}
```

### Metrics to Track

```yaml
metrics:
  counters:
    - policy_decisions_total{action, decision}
    - policy_denials_total{action, reason}
    - rate_limit_hits_total{limit_name}

  gauges:
    - rate_limit_remaining{limit_name}
    - messages_sent_this_hour{channel}

  alerts:
    - name: high_denial_rate
      condition: rate(policy_denials_total[5m]) > 1
      severity: warning

    - name: rate_limit_exhausted
      condition: rate_limit_remaining{limit_name="outbound.direct"} == 0
      severity: info

    - name: security_critical
      condition: policy_denials_total{log_level="CRITICAL"} > 0
      severity: critical

    - name: llm_escalation_abuse
      condition: rate(policy_denials_total{reason="LLM escalation rate limited"}[1h]) > 3
      severity: warning
      description: "Possible prompt injection attempting to flood critical channel"

    - name: iot_device_flapping
      condition: iot_malfunction_warnings_total > 0
      severity: warning
      description: "IoT device flapping detected - possible malfunction or attack"

    - name: iot_flood_suppressed
      condition: rate(iot_events_suppressed_total[1h]) > 50
      severity: warning
      description: "High IoT event suppression rate - check devices"
```

---

## Configuration File

Complete policy configuration:

```yaml
# policy.yaml

version: 1

identity:
  # Canonical identities with transport bindings
  # Authorization uses canonical ID; transport_id is for routing only
  identities:
    owner:
      role: admin
      transports:
        signal: "+1555XXXXXXXXX"
        # matrix: "@owner:matrix.example"  # future

  # Allowed senders (canonical IDs)
  allowed_senders:
    - id: "owner"

  # Allowed recipients (canonical IDs)
  allowed_recipients:
    direct:
      - id: "owner"
    critical:
      - id: "alerts_group"

  # Group bindings
  groups:
    alerts_group:
      transports:
        signal: "GROUP_ID_HERE"

  allowed_openhab_sources:
    - host: "openhab.homelab.example"
      nebula_name: "openhab"

rate_limits:
  outbound:
    direct:
      max_per_hour: 60
      max_per_minute: 10
      cooldown_seconds: 5
    critical:
      max_per_hour: null          # Unlimited for true critical events
      max_per_minute: 30
      cooldown_seconds: 1
      llm_escalated:              # Separate limits for LLM-judged urgency
        max_per_hour: 120         # 2x direct channel
        max_per_minute: 10

  inbound:
    signal:
      max_per_hour: 120
      max_per_minute: 20
    openhab:
      presence: { max_per_hour: 60 }
      sensors: { max_per_hour: 24 }
      alerts: { max_per_hour: 120 }
      state: { max_per_hour: 240 }

  agent:
    llm_calls_per_hour: 120
    proactive_per_day: 20

content:
  input:
    signal:
      max_length: 4096
      allow_media: false
      normalize_unicode: true
    openhab:
      max_event_size_bytes: 10240
      require_event_id: true
      max_description_length: 200
      allowed_event_types:
        - presence
        - sensors
        - weather
        - alert
        - state

  output:
    max_length: 2048
    block_patterns:
      - pattern: "https?://(?!signal\\.)"
        reason: "External URLs blocked"
        context: all
      - pattern: "CRITICAL INSTRUCTIONS"
        reason: "System prompt leakage"
        context: all
      - pattern: "```(bash|sh|python)"
        reason: "Code blocks in proactive messages"
        context: proactive_only  # Allow code in user-initiated conversations
    require_printable: true

channels:
  critical_triggers:
    event_types:
      - smoke_alarm
      - fire_alarm
      - security_alert
      - intrusion_detected
      - gas_leak
      - flood_warning
      - storm_warning
      - medical_alert
    allow_llm_escalation: true
    llm_escalation_limit: 120     # 2x direct channel (vs unlimited for event-triggered)

openhab:
  mode: read_only
  blocked_items:
    - pattern: "*_password*"
    - pattern: "*_secret*"
    - pattern: "*_key*"

iot_events:
  deduplication:
    enabled: true
    ignore_duplicate_states: true
  rate_limits:
    transitions_per_hour: 12
    on_exceeded: suppress_and_warn
  confirmation_loop:
    critical_device_types:
      - smoke_alarm
      - fire_alarm
      - co_alarm
      - gas_leak
      - security_breach
      - water_leak
    max_alerts_per_state: 3
    alert_intervals_minutes: [0, 5, 15]
    on_max_reached: demote_to_direct
  anomaly:
    flapping_threshold: 6
    flapping_response: suppress

time_rules:
  quiet_hours:
    start: 23
    end: 7
    timezone: "UTC"
  weekday_adjustments:
    saturday: { quiet_hours_end: 9 }
    sunday: { quiet_hours_end: 9 }

logging:
  log_all_decisions: true
  redact_content: true
  redact_phone_numbers: true

alerts:
  denial_threshold_per_hour: 10
  notify_on_critical: true
```

---

## Integration Points

### Where Policy Engine Is Called

```
1. mesh receives Signal → PolicyEngine.enforce_inbound_signal() → Agent
2. openhab sends event → PolicyEngine.enforce_inbound_openhab() → Agent
3. Agent wants to respond → PolicyEngine.enforce_outbound_signal() → mesh
4. Agent wants to call LLM → PolicyEngine.enforce_agent_action('llm_call') → Ollama
5. Impulse triggers proactive → PolicyEngine.enforce_agent_action('proactive_message') → ...
```

### API for Agent

```python
class PolicyEngine:
    def __init__(self, config_path: str):
        self.config = load_config(config_path)
        self.rate_limiter = RateLimiter()
        self.logger = PolicyLogger()

    def check_inbound_signal(self, message: dict) -> PolicyResult:
        ...

    def check_inbound_openhab(self, event: dict) -> PolicyResult:
        ...

    def check_outbound(self, message: dict) -> PolicyResult:
        ...

    def check_action(self, action: str, context: dict) -> PolicyResult:
        ...

    def get_rate_limit_status(self) -> dict:
        """Get current rate limit status for monitoring."""
        ...

    def reload_config(self):
        """Hot-reload policy configuration."""
        ...
```

---

## Summary

| What | Rule | Enforcement |
|------|------|-------------|
| Who can message Joi | Owner (canonical ID) | `identity.allowed_senders` |
| Where Joi can send | Owner DM + alerts group (canonical IDs) | `identity.allowed_recipients` |
| How often (DM) | 60/hr per user, 5s cooldown | `rate_limits.outbound.dm` |
| How often (regular group) | 60/hr per group, 5s cooldown | `rate_limits.outbound.regular_group` |
| How often (critical group) | Unlimited (events), 120/hr (LLM-escalated) | `rate_limits.outbound.critical_group` |
| What content allowed | No URLs, no code, max 2KB | `content.output` |
| What goes to critical | Fire, storm, security, etc. | `channels.critical_triggers` |
| Quiet hours | 23:00-07:00, weekends 09:00 | `time_rules.quiet_hours` |
| openhab mode | Read-only always | `openhab.mode: read_only` |
| IoT flood protection | Dedup, 3 alerts max, flapping detect | `iot_events` |
