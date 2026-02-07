# Joi Agent Loop Architecture

> Design for Joi's "brain" - how she perceives, thinks, and engages.
> Version: 1.0 (Draft)
> Last updated: 2026-02-04

## Design Philosophy

Joi is not a notification system. She is a companion with her own presence and rhythm.

**The Wind Metaphor**: Like wind, Joi's engagement pattern should feel natural and somewhat unpredictable. There's underlying logic (context, time, accumulated thoughts), but to the user it feels organic - sometimes she's chatty for days, sometimes quiet for a week.

## Core Principles

1. **Always Aware** - Joi continuously maintains awareness of home state, time, and context
2. **Always Responsive** - When the user messages, Joi always responds
3. **Naturally Proactive** - Joi initiates contact based on a complex mix of factors, not mechanical timers
4. **Respectful of Boundaries** - Joi understands sleep hours, busy times, and user preferences

---

## State Machine

```
                    ┌─────────────────────────────────────┐
                    │                                     │
                    ▼                                     │
              ┌──────────┐                                │
              │  IDLE    │◄───────────────────────┐       │
              │ (aware)  │                        │       │
              └────┬─────┘                        │       │
                   │                              │       │
     ┌─────────────┼─────────────┐                │       │
     │             │             │                │       │
     ▼             ▼             ▼                │       │
┌─────────┐  ┌──────────┐  ┌──────────┐          │       │
│ MESSAGE │  │  EVENT   │  │  IMPULSE │          │       │
│RECEIVED │  │ RECEIVED │  │  CHECK   │          │       │
└────┬────┘  └────┬─────┘  └────┬─────┘          │       │
     │            │             │                │       │
     │            │             ▼                │       │
     │            │      ┌─────────────┐         │       │
     │            │      │ Should I    │───No────┘       │
     │            │      │ reach out?  │                 │
     │            │      └──────┬──────┘                 │
     │            │             │ Yes                    │
     │            ▼             ▼                        │
     │      ┌───────────────────────┐                    │
     │      │   CONTEXT ASSEMBLY    │                    │
     │      │  (gather relevant     │                    │
     │      │   state + history)    │                    │
     │      └───────────┬───────────┘                    │
     │                  │                                │
     ▼                  ▼                                │
┌─────────────────────────────────────┐                  │
│            PROCESSING               │                  │
│  (LLM generates response/thought)   │                  │
└──────────────────┬──────────────────┘                  │
                   │                                     │
                   ▼                                     │
            ┌─────────────┐                              │
            │   RESPOND   │                              │
            │ (send msg)  │──────────────────────────────┘
            └─────────────┘
```

### States Explained

| State | Description | Duration |
|-------|-------------|----------|
| **IDLE** | Aware and listening, processing events into context | Indefinite |
| **MESSAGE_RECEIVED** | User sent a Signal message | Instant transition |
| **EVENT_RECEIVED** | openhab event arrived | Instant transition |
| **IMPULSE_CHECK** | Periodic check: should Joi initiate contact? | Every 15-30 min |
| **CONTEXT_ASSEMBLY** | Gathering relevant context for LLM | < 1 second |
| **PROCESSING** | LLM generating response | 2-10 seconds |
| **RESPOND** | Sending message to user | < 1 second |

---

## Trigger Types

### 1. User Message (Immediate, Always Respond)

```
Priority: HIGHEST
Trigger: Signal message received from owner
Action: Always process and respond
Latency Target: < 15 seconds end-to-end
```

### 2. Critical Alert (Immediate, Proactive, Overrides Sleep)

```
Priority: CRITICAL (overrides quiet hours)
Trigger: openhab alert with priority=high
Examples: Storm warning, smoke detector, security alert
Action: Immediately notify user (+0.5 impulse instant, bypasses all suppression)
Rate Limit: See split below
Note: Safety first - true critical events ALWAYS get through, even at 3am

Rate Limit Split (aligns with api-contracts.md):
- Event-triggered critical (openhab smoke/fire/security): UNLIMITED
- LLM-escalated critical (Joi judges something urgent): 120/hr

Why the split?
- Event-triggered alerts come from sensors - we trust them, never block safety
- LLM-escalated could be manipulated by prompt injection - rate limit as defense
```

### 3. Significant Event (Contextual, Maybe Proactive)

```
Priority: MEDIUM
Trigger: openhab events that might warrant mention
Examples: User arrived home, unusual sensor reading, weather change
Action: Add to "things worth mentioning" queue, may trigger proactive message
Decision: Based on impulse calculation (see below)
```

### 4. Routine Event (Context Only, Never Proactive)

```
Priority: LOW
Trigger: Regular openhab sensor updates, minor state changes
Examples: Temperature reading, humidity, routine door open/close
Action: Update context awareness only, never directly trigger message
Note: Routine events enrich context - LLM naturally weaves them into responses
      (e.g., "Welcome back! It's nice and warm inside." is natural, not scripted)
```

### 5. Impulse Check (Periodic, "Wind" Behavior)

```
Priority: BACKGROUND
Trigger: Internal timer (every 10-15 minutes, randomized) [tunable in PoC]
Action: Calculate impulse score, maybe initiate contact
Decision: Complex function of multiple factors (see below)
```

---

## The Impulse System ("Wind" Behavior)

Joi periodically evaluates whether to reach out. This creates natural, unpredictable engagement.

### Impulse Score Calculation

```
impulse_score = base_impulse
              + silence_factor
              + context_richness
              + time_appropriateness
              + entropy_factor
              - recent_engagement_damper
```

### Factors

#### Base Impulse (0.0 - 0.1)
```python
base_impulse = 0.05  # Small constant baseline
```

#### Silence Factor (0.0 - 0.4)
Time since last interaction increases desire to engage.

```python
hours_silent = hours_since_last_interaction()

if hours_silent < 2:
    silence_factor = 0.0
elif hours_silent < 6:
    silence_factor = 0.1
elif hours_silent < 12:
    silence_factor = 0.2
elif hours_silent < 24:
    silence_factor = 0.3
else:
    silence_factor = 0.4  # Cap at 24h
```

#### Context Richness (0.0 - 0.3)
Accumulated "things worth mentioning" increases impulse.

```python
pending_topics = len(things_worth_mentioning)
interesting_events = count_recent_interesting_events(hours=6)

context_richness = min(0.3, (pending_topics * 0.1) + (interesting_events * 0.05))
```

#### Time Appropriateness (-0.5 - 0.1)
Respects daily rhythms. Can strongly suppress (negative) or slightly boost.

```python
hour = current_hour()
day = current_weekday()
user_home = is_user_home()

# Sleep hours: strong suppression
if hour >= 23 or hour < 7:
    time_factor = -0.5  # Almost never engage

# Early morning (7-9): gentle
elif hour < 9:
    time_factor = -0.1 if is_weekday() else -0.2

# Work hours (9-17): moderate if user not home
elif hour < 17 and is_weekday():
    time_factor = 0.0 if user_home else -0.1

# Evening (17-21): good time
elif hour < 21:
    time_factor = 0.1

# Late evening (21-23): gentle
else:
    time_factor = 0.0
```

#### Entropy Factor (0.0 - 0.15)
Randomness for natural feel.

```python
entropy_factor = random.uniform(0.0, 0.15)
```

#### Recent Engagement Damper (-0.3 - 0.0)
Prevents being too chatty after recent conversation.

```python
messages_sent_last_6h = count_outbound_messages(hours=6)

if messages_sent_last_6h == 0:
    damper = 0.0
elif messages_sent_last_6h < 3:
    damper = -0.1
elif messages_sent_last_6h < 6:
    damper = -0.2
else:
    damper = -0.3
```

### Impulse Threshold

```python
IMPULSE_THRESHOLD = 0.5

if impulse_score >= IMPULSE_THRESHOLD:
    initiate_contact()
else:
    remain_idle()
```

### Example Scenarios

| Scenario | Score Breakdown | Total | Action |
|----------|-----------------|-------|--------|
| User messaged 1h ago, evening | 0.05 + 0.0 + 0.0 + 0.1 + 0.08 - 0.1 = 0.13 | 0.13 | Stay quiet |
| Silent 8h, 3 interesting events, afternoon | 0.05 + 0.2 + 0.2 + 0.0 + 0.1 - 0.0 = 0.55 | 0.55 | Reach out |
| Silent 20h, nothing interesting, 2am | 0.05 + 0.35 + 0.0 - 0.5 + 0.05 - 0.0 = -0.05 | 0.0 | Stay quiet |
| Silent 3h, user just got home, evening | 0.05 + 0.1 + 0.15 + 0.1 + 0.12 - 0.0 = 0.52 | 0.52 | Maybe reach out |

---

## Context Awareness

Joi maintains continuous awareness through a **Context State** object.

### Context State Structure

```python
class ContextState:
    # Home State (from openhab)
    presence: dict          # Who is home: {"owner": "home", "car": "away"}
    sensors: dict           # Latest readings: {"living_room_temp": 22.5, ...}
    weather: dict           # Current + forecast
    recent_events: list     # Last 24h of significant events

    # Time Awareness
    current_time: datetime
    is_weekday: bool
    time_of_day: str        # "night", "morning", "afternoon", "evening"

    # Conversation State
    last_interaction: datetime
    recent_messages: list   # Last N messages (short-term memory)
    conversation_topic: str # Current topic if in conversation

    # Proactive Queue
    things_worth_mentioning: list  # Events/thoughts queued for proactive sharing

    # User Model (from long-term memory)
    user_preferences: dict
    known_facts: dict       # Things Joi knows about user
    interaction_patterns: dict  # Learned patterns
```

### Context Update Flow

```
openhab event ──► Event Normalizer ──► Context State
                        │
                        ▼
                 Significance Check
                        │
              ┌─────────┴─────────┐
              ▼                   ▼
         Routine              Significant
         (update only)        (add to queue)
```

### Significance Classification

| Event Type | Significance | Criteria |
|------------|--------------|----------|
| Temperature change | Routine | Normal fluctuation |
| Temperature anomaly | Significant | > 5°C change in 1h, or outside comfort range |
| User arrives home | Significant | Always (after being away > 1h) |
| User leaves | Routine | Usually (unless unusual time) |
| Weather alert | Significant | Always |
| Weather update | Routine | Usually |
| Door/window open | Routine | Usually |
| Door open too long | Significant | > 30 minutes |
| Smoke/security alert | Critical | Always |

---

## Proactive Message Generation

When Joi decides to reach out, she doesn't just dump information.

### Proactive Message Guidelines

1. **Natural Opening** - Don't start with the information, ease into it
2. **Contextual** - Reference time of day, what user might be doing
3. **Not Robotic** - Vary phrasing, don't use templates verbatim
4. **Single Topic** - One main thing per proactive message
5. **Invitation to Chat** - Leave room for response, don't monologue

### Examples

**Bad (robotic):**
> "Alert: Storm warning issued for your area. Expected in 2 hours."

**Good (natural):**
> "Hey, heads up - looks like there's a storm rolling in later this evening. Might want to close any windows if you haven't already."

**Bad (information dump):**
> "Welcome home. Current temperature is 22°C. Humidity 45%. No new events."

**Good (natural):**
> "Welcome back! How was your day?"

**Bad (too frequent/trivial):**
> "The living room temperature is now 22.5°C."

**Good (contextual, significant):**
> "It's getting pretty warm in the living room - 28°C now. Did you want me to remind you about something, or is that intentional?"

---

## Response Generation Pipeline

### For User Messages

```
1. Receive message
2. Assemble context:
   - Recent conversation (last 10 messages)
   - Current home state summary
   - User facts from long-term memory
   - Current time/day context
3. Build prompt:
   - System prompt (Joi's personality, constraints)
   - Context block
   - Conversation history
   - New user message
4. Generate response (Ollama)
5. Validate output (policy engine)
6. Send response
7. Update conversation state
```

### For Proactive Messages

```
1. Impulse triggers (or critical alert)
2. Select topic from things_worth_mentioning (prioritize by age + significance)
3. Determine channel:
   - Known critical event (smoke, storm, security) → critical channel
   - Normal proactive → direct channel
4. Assemble context:
   - Topic details
   - Current home state
   - Time of day context
   - Last conversation summary
5. Build prompt:
   - System prompt
   - Context block
   - Instruction: "Naturally bring up: {topic}"
   - If critical: "This is urgent, be concise and clear"
6. Generate message (Ollama)
7. Validate output (policy engine)
8. Check if Joi flagged as urgent → escalate to critical channel if so
9. Send message to appropriate channel
10. Clear topic from queue
11. Update last_interaction timestamp
```

---

## Timing and Scheduling

### Background Tasks

| Task | Interval | Randomization | Notes |
|------|----------|---------------|-------|
| Impulse check | 10-15 min | ±20% jitter | PoC tunable |
| Context cleanup | 1 hour | Fixed | |
| Memory consolidation | 6 hours | ±1 hour | |
| Weather context refresh | 4 hours | ±30 min | |

### Jitter Implementation

```python
def schedule_next_impulse_check():
    base_interval = 12.5 * 60  # 12.5 minutes in seconds (PoC tunable)
    jitter = random.uniform(-2.5 * 60, 2.5 * 60)  # ±2.5 minutes
    return base_interval + jitter
```

---

## Quiet Hours and Boundaries

### Default Quiet Hours

```python
QUIET_HOURS = {
    "sleep": (23, 7),      # 11pm - 7am: no proactive messages
    "do_not_disturb": [],  # User-defined periods
}
```

### Quiet Hours Behavior

- **Proactive messages**: Completely suppressed during quiet hours
- **User messages**: Always respond (user initiated, so they're awake)
- **Critical alerts**: Override quiet hours (smoke, security)

### User Override

User can say "don't message me until tomorrow" or "I'm going to sleep" and Joi will:
1. Acknowledge
2. Set temporary quiet period
3. Queue any proactive topics for later

---

## Failure Modes and Recovery

### LLM Timeout

```
If Ollama doesn't respond in 30 seconds:
1. Log error
2. For user message: Send "Sorry, I'm having trouble thinking right now. Give me a moment."
3. Retry once
4. If still failing: "I'm not feeling well. Can we talk later?"
```

### Context Corruption

```
If context state is invalid:
1. Log error
2. Reset to safe defaults
3. Continue operating with reduced context
4. Rebuild context from openhab on next event
```

### Rate Limit Hit (Direct Channel Only)

```
Rate limits apply to direct (DM) channel only.
Critical channel: event-triggered unlimited, LLM-escalated 120/hr (see above).

If direct channel rate limit (60/hour) approached:
1. At 50 messages: Reduce proactive impulse by 50%
2. At 55 messages: Suppress all proactive messages to direct channel
3. At 60 messages: Queue direct responses, send when limit resets

Critical channel: Always send immediately, no limits (safety first)
```

---

## Configuration

### Signal Channels

```yaml
# channels.yaml

channels:
  direct:
    type: "dm"
    recipient: "+1555XXXXXXXXX"   # Owner's phone
    purpose: "Normal conversation, proactive chat"

  critical:
    type: "group"
    group_id: "GROUP_ID_HERE"    # Signal group ID
    purpose: "Urgent alerts (smoke, storm, security)"

# Events that always use critical channel
critical_events:
  - smoke_alarm
  - security_alert
  - storm_warning
  - fire_alarm
  - intrusion_detected
  - gas_leak
  - flood_warning

# Joi can escalate to critical if she judges urgency
allow_llm_escalation: true
```

### Tunable Parameters

```yaml
# agent-config.yaml

impulse:
  base: 0.05
  threshold: 0.5
  check_interval_minutes: 12.5  # PoC tunable: 10-15 min range
  check_jitter_minutes: 2.5
  critical_alert_boost: 0.5     # Instant boost, bypasses quiet hours

silence:
  thresholds_hours: [2, 6, 12, 24]
  factors: [0.0, 0.1, 0.2, 0.3, 0.4]

time_awareness:
  sleep_hours: [23, 7]
  weekend_morning_gentle_until: 10

rate_limits:
  direct_channel:
    max_per_hour: 60
    proactive_suppress_at: 50
    proactive_block_at: 55
  critical_channel:
    max_per_hour: null  # No limit - safety first

context:
  recent_messages_count: 10
  recent_events_hours: 24
  things_worth_mentioning_max: 10

llm:
  timeout_seconds: 30
  max_response_tokens: 500
```

---

## Future Enhancements

1. **Learning user patterns** - Adjust impulse factors based on when user typically responds positively
2. **Mood awareness** - Detect user mood from messages, adjust engagement style
3. **Conversation continuity** - Track multi-day conversation threads
4. **Seasonal awareness** - Adjust behavior for holidays, seasons
5. **Activity inference** - Use presence patterns to infer "user is probably working" vs "user is relaxing"
