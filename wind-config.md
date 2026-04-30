# Wind Configuration Reference

> Tuning guide for the Wind proactive messaging system.
> Config lives in `/var/lib/joi/policy/mesh-policy.json` under the `"wind"` key.
> Changes take effect on next scheduler tick (no restart needed — policy is reloaded each tick).

---

## How Wind Decides to Send

Each scheduler tick (default: every 60s), Wind runs this pipeline per conversation:

```
1. Hard gates   → any fail = skip this tick entirely (no score, no accumulation)
2. Score        → sum of weighted factors (base + silence + topic_pressure + fatigue + engagement)
3. Accumulate   → score added to running accumulator each tick
4. Threshold    → when accumulator >= threshold (with drift), evaluate soft probability
5. Soft trigger → sigmoid probability based on how far above threshold
6. Send         → if triggered: pick topic, generate message, reset accumulator
```

The **accumulator** is the key mechanism — low per-tick scores still build up over time, creating natural variance in send timing.

---

## Global Toggles

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Master switch. Set `true` to activate Wind for this conversation. |
| `shadow_mode` | `true` | If `true`, Wind runs the full pipeline but does **not** send — logs decisions only. Use for tuning before going live. |

---

## Allowlist

| Key | Default | Description |
|-----|---------|-------------|
| `allowlist` | `[]` | List of conversation IDs (phone numbers or group IDs) eligible for Wind. Conversations not in this list are hard-gated. |

> **Note:** Timezone is now per-conversation (stored in `conversation_settings` table), not a Wind config field. Set via Signal command `timezone <tz>`. Defaults to UTC.

---

## Hard Gates

Hard gates run first. Any failure skips the tick entirely — no score computed, accumulator not updated.

| Key | Default | Description |
|-----|---------|-------------|
| `quiet_hours_start` | `23` | Start of quiet window. Accepts an integer hour (`23`), minutes since midnight (`1380`), or `"HH:MM"` string (`"23:30"`). Wind will not send from this time onward. Overridden per-conversation by the adaptive learned quiet start when available. |
| `quiet_hours_end` | `7` | End of quiet window. Same format as `quiet_hours_start`. Wind resumes at this time. Supports overnight ranges (e.g., start=`23`, end=`7`). Config-only — not learned adaptively. |
| `min_cooldown_minutes` | `60` | Minimum minutes between proactive sends. Prevents bursts even if accumulator resets fast. |
| `daily_cap` | `3` | Max proactive messages per rolling 24h window. Each fire expires 24h after it happened, freeing a slot. Hard stop regardless of score. |
| `max_unanswered_streak` | `2` | Stop sending after N consecutive proactives with no user reply. Resets when user responds. |
| `min_silence_minutes` | `30` | Minimum minutes since last user message before Wind is eligible. Prevents interrupting active conversations. During a heated conversation (EMA ≤ `active_convo_gap_minutes`), required silence is `min_silence_minutes + extra`, where `extra` ramps linearly from 30 min (at base ≤ 30) to 120 min (at base ≥ 240). The ramp shape is hard-coded policy — no separate knob. |

**Tuning notes:**
- `min_silence_minutes` is the most impactful gate for responsiveness. Lower it (e.g., 10–15) for more aggressive behavior.
- `daily_cap` interacts with `fatigue_weight` — cap provides a hard stop (rolling 24h), fatigue provides gradual suppression before the cap. Because slots expire 24h after each fire, the cap distributes naturally across the user's active hours over several days.
- `max_unanswered_streak=2` means after 2 ignored proactives, Wind goes quiet until the user engages.

---

## Impulse Score

The per-tick score is the **sum** of all factor contributions, clamped to `[0.0, 1.0]`:

```
score = base + silence + topic_pressure + fatigue + engagement + affinity + novelty + mood
```

This score is added to the accumulator each eligible tick. The accumulator resets to 0 after a send.

### Factor Weights

| Key | Default | Factor | Range | Description |
|-----|---------|--------|-------|-------------|
| `base_impulse` | `0.1` | base | `[0, 1]` | Constant per-tick contribution. Ensures accumulator always grows when gates pass. |
| `silence_weight` | `0.3` | silence | `[0, weight]` | Max contribution from silence. Scales linearly from `min_silence_minutes` to `silence_cap_hours`. |
| `silence_cap_hours` | `24.0` | silence | — | Silence stops contributing beyond this many hours. |
| `topic_pressure_weight` | `0.2` | topic_pressure | `[0, weight]` | Boost when there are queued topics ready to send. Higher = more eager to send when topics exist. |
| `fatigue_weight` | `0.3` | fatigue | `[-weight, 0]` | **Negative** damper. Scales with rolling 24h fire count / `daily_cap`. At cap, full weight is subtracted. |
| `engagement_weight` | `0.2` | engagement | `[-weight, +weight]` | Boost/dampen based on engagement score (0.5 = neutral). Engaged users get more proactives; disengaged get fewer. |
| `mood_weight` | `0.15` | mood | `[-weight, +weight]` | Phase 4d: impulse contribution from Joi's mood. Positive moods boost, negative moods dampen. Scales with `mood_intensity`. |
| `momentum_nudge` | `0.05` | — | — | Phase 4d: intensity nudge applied per heated message. Amplifies Joi's current mood during fast conversations. Set 0 to disable. Env override: `JOI_WIND_MOMENTUM_NUDGE`. |

**Silence factor formula:**
```
silence_contribution = ((elapsed_hours - min_silence_hours) / (silence_cap_hours - min_silence_hours)) * silence_weight
```
Capped at `silence_weight`. Zero if elapsed < `min_silence_minutes`.

**Fatigue factor formula:**
```
recent_count = fires in proactive_fire_times within last 24h
fatigue_damper = -(recent_count / daily_cap) * fatigue_weight
```
At `daily_cap` fires in the rolling window, the full `fatigue_weight` is subtracted from score each tick.

**Engagement factor formula:**
```
engagement_contribution = (engagement_score - 0.5) * engagement_weight * 2
```
`engagement_score` is 0.5 neutral, 1.0 fully engaged, 0.0 fully disengaged (per-conversation, updated by Phase 4a).

---

## Impulse Threshold & Accumulation

| Key | Default | Description |
|-----|---------|-------------|
| `impulse_threshold` | `0.6` | Base threshold the accumulator must reach to trigger a send. |
| `threshold_drift_min` | `-0.1` | How far below baseline the threshold can drift (random walk). |
| `threshold_drift_max` | `0.1` | How far above baseline the threshold can drift. |
| `threshold_drift_step` | `0.01` | Max change in threshold offset per tick. |
| `threshold_mean_reversion` | `0.01` | Pull-back toward baseline per tick (1% of current offset). |
| `soft_trigger_steepness` | `10.0` | Sigmoid steepness for soft probability. Higher = sharper step at threshold; lower = more gradual. |

**How the accumulator works:**
- Each eligible tick: `accumulator += score`
- When `accumulator >= current_threshold`: evaluate sigmoid probability
- On send: `accumulator` resets to 0
- On gate failure: accumulator **not** updated (gates protect the signal)

**Threshold drift** adds natural variance — the effective threshold wanders within `[base + drift_min, base + drift_max]`, so Wind doesn't fire at perfectly predictable intervals. Mean reversion prevents it from drifting too far.

**Estimating time-to-fire** (rough guide):
```
ticks_needed ≈ threshold / avg_score_per_tick
minutes ≈ ticks_needed * scheduler_interval_seconds / 60
```
With `impulse_threshold=0.4`, `base_impulse=0.1`, `silence_weight=0.2`, `silence_cap_hours=2`:
- After 20+ min silence: avg score ≈ 0.02–0.05/tick → fires in ~10–30 min
- With active topic pressure: avg score ≈ 0.05–0.15/tick → fires faster

---

## Engagement Tracking (Phase 4a)

| Key | Default | Description |
|-----|---------|-------------|
| `ignore_timeout_hours` | `12.0` | If a proactive message gets no response within this window, it is classified as ignored (negative feedback). |

The engagement score per conversation starts at 0.5 (neutral) and shifts based on how the user responds to proactive messages. This feeds back into the `engagement` impulse factor.

---

## Learning & Pursuit (Phase 4b)

### Symmetric Decay + Novelty

| Key | Default | Description |
|-----|---------|-------------|
| `interest_decay_rate` | `0.02` | Daily decay for `interest_weight` (2%/day). Slower than rejection decay to allow interest to persist longer. |
| `novelty_weight` | `0.1` | Impulse bonus when the best pending topic is from an unexplored family (never engaged before). Prevents high-interest families from permanently crowding out new topics. |

### Affinity Bonus

| Key | Default | Description |
|-----|---------|-------------|
| `affinity_weight` | `0.15` | Max impulse contribution from topic affinity. High `interest_weight` families surface more readily. |

### Pursuit Back-off

| Key | Default | Description |
|-----|---------|-------------|
| `pursuit_backoff_hours` | `[4, 12, 24]` | Retry delay schedule (hours). retry 1 → 4h, retry 2 → 12h, retry 3+ → 24h. Prevents the same topic from immediately re-surfacing after being ignored. |

### Cooldown Anti-periodicity

| Key | Default | Description |
|-----|---------|-------------|
| `cooldown_days` | `9` | Center of cooldown window (days). Replaces the old fixed 7-day value. |
| `cooldown_jitter_days` | `2` | Random ±N days applied to cooldown duration. Actual cooldown is `[7, 11]` days with defaults. Prevents predictable weekly pattern. |

### Undertaker

Topics that accumulate enough rejection or that are explicitly deflected on a ghost probe reach the **undertaker** state — permanently blocked. Undertaker families never surface again unless manually cleared by an admin.

| Key | Default | Description |
|-----|---------|-------------|
| `undertaker_threshold` | `2.0` | `rejection_weight` at which auto-promotion occurs. Not reachable via normal deflections (cap is 1.0); exists as a safety valve. The primary path to undertaker is via ghost probe deflection. |

Admin management:
```bash
joi-admin wind show-feedback +1234567890        # UNDERTAKER shown in status column
joi-admin wind undertaker-clear +1234567890 health  # Remove permanent block
```

### Ghost Probe

After a topic family has been deeply rejected and silent for `ghost_probe_days`, Wind queues a low-priority ghost probe — a gentle re-check. If the user engages, the family is restored. If deflected again, it escalates to undertaker.

| Key | Default | Description |
|-----|---------|-------------|
| `ghost_probe_days` | `60` | Days of inactivity before a ghost probe is generated. |
| `ghost_probe_priority` | `20` | Priority of ghost probe topics (very low — surfaces only when nothing else is pending). |

Ghost probe lifecycle:
- **engaged** → family restored, topic marked as engaged
- **ignored** → 90-day cooldown applied
- **deflected** → undertaker promotion (family permanently blocked)

### Topic Priority Decay (Phase 5)

Each end-of-day maintenance pass, pending topics lose priority points. The decay rate scales
with queue depth so neglected topics sink faster when the backlog is large, letting the topics
the user engages with naturally float to the top.

Formula: `points = max(base, round(base × sqrt(pending_count / reference)))`

Topics created today are excluded — freshly mined topics are not immediately penalised.
Priority floors at 0. Only `pending`, non-expired topics are affected.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `topic_priority_decay_points` | `4` | Base decay points per day. |
| `topic_priority_decay_reference` | `8` | Queue depth at which decay equals base exactly. Set to `0` to disable. |

Example decay at base=4, reference=8: 8 topics→4 pts/day, 30 topics→8 pts/day, 100 topics→14 pts/day.

After decay, an **affinity protection** pass partially restores priority for topics from families
the user likes. Restore formula: `round(points × affinity_factor × min(1.0, preference_score))`
where `preference_score = interest_weight - rejection_weight`. Neutral or disliked families get
no restoration. Topics created today are excluded from both passes.

If a family is in the undertaker but `preference_score` has climbed above
`undertaker_release_threshold` (user started engaging with it organically), the family is
released from the undertaker automatically — user-driven signal trumps the permanent block.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `topic_priority_affinity_factor` | `0.5` | Fraction of decayed points restored for liked families. `0` disables affinity protection entirely. |
| `topic_priority_undertaker_release_threshold` | `0.5` | Preference score at which an undertaker family is organically released. |

---

## Environment Variables

These variables configure Wind behaviour at the process level (set in `joi-api.default`, not in `mesh-policy.json`).

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_WIND_OUTCOME_TTL_DAYS` | `90` | How long wind topic outcome summaries stay active for deduplication (days). Resolved outcome summaries older than this are excluded when building the topic-selection context. |
| `JOI_WIND_OUTCOME_HISTORY_DAYS` | `180` | How far back to search conversation history when generating topic outcome summaries (days). Controls the RAG window used to enrich outcome narratives. |

---

## Wake-Up Procedure (Phase 5)

When a conversation has been silent for an extended period, Wind runs a clean re-entry procedure
and schedules a proactive re-engagement message for a random time in the next non-quiet window.

The silence threshold is `max(floor, min(cap, convo_gap_ema * multiplier))`. With defaults:
- Daily users (EMA≈2h): max(72h, min(96h, 6h)) = **72h** (3 days)
- Weekly users (EMA≈168h): max(72h, min(96h, 504h)) = **96h** (4 days)

The procedure runs **once per silence gap**, gated by `last_wakeup_at > last_user_interaction_at`.

**Procedure steps:**
1. Compact context (reuse existing method — same as pre-Wind send)
2. Purge expired facts (hard-delete facts whose TTL has passed)
3. Inject gap marker into context summaries (`[JOI-PAUSE duration=Xd dates=YYYY-MM-DD→YYYY-MM-DD]`)
4. Reset Wind impulse to 0 (prevents double-fire once user returns)
5. Schedule proactive — pick a random UTC time in the **next full non-quiet window** (tomorrow's `quiet_hours_end` → `quiet_hours_start`), store as `wakeup_send_at`

**Proactive message** uses core (important=True) facts + gap duration + last observed user mood.
It does **not** count toward `proactive_fire_times` or the rolling daily cap — it is a one-time
re-engagement event, not a regular Wind proactive.

**Cancellation** — if the user messages before `wakeup_send_at` arrives, `record_user_interaction()`
automatically clears it. The gap marker is already in context, so the reactive response is
naturally gap-aware without any extra handling.

| Key | Default | Description |
|-----|---------|-------------|
| `wakeup_floor_hours` | `72.0` | Minimum silence (hours) before wake-up triggers. Wake-up never fires sooner than this. |
| `wakeup_cap_hours` | `96.0` | Maximum threshold (hours). Wake-up always fires within 4 days regardless of EMA. |
| `wakeup_ema_multiplier` | `3.0` | EMA gap multiplier. Nudges threshold between floor and cap for users with varying frequency. |

---

## Example Configs

### Active companion (evening/night, responsive)
```json
{
  "enabled": true,
  "shadow_mode": false,
  "quiet_hours_start": 0,
  "quiet_hours_end": 7,
  "min_cooldown_minutes": 10,
  "min_silence_minutes": 20,
  "daily_cap": 4,
  "max_unanswered_streak": 2,
  "impulse_threshold": 0.4,
  "base_impulse": 0.1,
  "silence_weight": 0.2,
  "silence_cap_hours": 2.0,
  "fatigue_weight": 0.2,
  "engagement_weight": 0.2
}
```

### Conservative (fewer interruptions, longer silence required)
```json
{
  "enabled": true,
  "shadow_mode": false,
  "quiet_hours_start": 22,
  "quiet_hours_end": 9,
  "min_cooldown_minutes": 60,
  "min_silence_minutes": 60,
  "daily_cap": 2,
  "max_unanswered_streak": 1,
  "impulse_threshold": 0.7,
  "base_impulse": 0.05,
  "silence_weight": 0.3,
  "silence_cap_hours": 12.0,
  "fatigue_weight": 0.4,
  "engagement_weight": 0.2
}
```

### Shadow mode (tuning / observation only)
```json
{
  "enabled": true,
  "shadow_mode": true,
  "allowlist": ["+1234567890"]
}
```
