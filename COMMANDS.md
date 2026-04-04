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

## Reminders (owner, DM only)

Joi creates reminders from natural language. No special syntax required.

**Trigger phrases:**

- `remind me` / `remind me in` / `remind me at` / `remind me tonight`

**Examples:**

```
remind me in 5m to check the oven        → fires in 5 minutes
remind me in 2h to take meds             → fires in 2 hours
remind me at 3pm to submit the form      → fires at 15:00 local time
remind me tonight to call mom            → fires at 9pm local time
```

**Implicit (no "remind me"):**

```
tonight I need to install a security camera    → reminder at 9pm
I have to call the bank before 5pm            → reminder at 4:30pm
```

**After a reminder fires, you can snooze it:**

```
remind me again in 30 minutes
snooze
```

**Listing reminders:**

```
what reminders do I have?
what's on my agenda?
show me my upcoming reminders
```

---

## Notes (owner, DM only)

Personal named notes — longer-form text, searchable, editable.

**Create:**

```
take a note: trip ideas / Vienna in spring, budget €500
note this: call dentist before end of month
write a note called "book list": starts with Dune
```

**Create with a reminder:**

```
note this for Friday: submit expense report
```

**Append to a note:**

```
add "also bring umbrella" to my trip ideas note
```

**Replace a note's content:**

```
update my trip ideas note: go to Vienna in June instead
```

**List notes:**

```
what notes do I have?
show my notes
```

**Read a note:**

```
show me my trip ideas note
what did I write about Vienna?
open note "book list"
```

**Delete a note:**

```
delete my trip ideas note
remove the book list note
```

**Add a reminder to an existing note:**

```
remind me about my trip ideas note on Friday
```

**Admin (joi-admin):**

```
joi-admin notes list                      # list all notes in DB
joi-admin notes list --conversation +123  # notes for one conversation
joi-admin notes list --archived           # include archived notes
joi-admin notes show <id>                 # show full content of a note
joi-admin notes delete <id>              # soft-delete a note
joi-admin notes delete-all               # archive all notes
```

---

## Group Addressing

In group chats, Joi only responds when directly addressed:

```
@Joi what's the weather like?
```

The `@Joi` mention must appear at the start or after whitespace, followed by whitespace or punctuation.
