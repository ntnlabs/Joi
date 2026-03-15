# Joi User Commands

Commands you can send to Joi via Signal. All matching is case-insensitive.

---

## Wind Snooze (owner, DM only)

Silence proactive Wind messages without affecting the topic queue or daily cap.

**Snooze:**

| Message | Effect |
|---|---|
| `quiet` / `shh` / `hush` | Snooze Wind for 4 hours (default) |
| `snooze` / `mute` / `pause` | Same |
| `quiet 2h` / `shh 30m` / `pause 1d` | Snooze for a specific duration |
| `quiet tonight` | Snooze until next `quiet_hours_end` in configured timezone |

Duration suffixes: `Nh` or `N hours`, `Nm` or `N min`, `Nd` or `N days`.
Limits: minimum 5 minutes, maximum 7 days. Max message length: 8 words.

**Resume:**

| Message | Effect |
|---|---|
| `wake` / `resume` | Cancel snooze immediately |
| `unsnooze` / `unmute` | Same |

---

## Fact Storage

Joi automatically detects fact-storing intent. No special syntax required — just say it naturally.

**Trigger phrases** (must appear in the message):

- `remember` / `don't forget` / `never forget` / `always remember`
- `note that` / `keep in mind`
- `call me [name]` / `my name is [name]`

**Examples:**

```
Remember I'm allergic to shellfish.
My name is Alex.
Note that I prefer metric units.
Never forget: I hate surprise meetings.
```

Facts prefixed with `always remember` / `never forget` or in personal categories (name, partner, profession) are automatically marked important.

---

## Group Addressing

In group chats, Joi only responds when directly addressed:

```
@Joi what's the weather like?
```

The `@Joi` mention must appear at the start or after whitespace, followed by whitespace or punctuation.
