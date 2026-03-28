# Tool: External Search

## Overview

Joi can look up external information when her training data is insufficient.
The decision to search is made by Llama itself — not by keyword rules or
predefined triggers. This catches any scenario: lyrics, sports scores, local
businesses, recent events, entity lookups, etc.

## Architecture

```
User message
    → Pre-screen LLM call  →  {"search": false}
                                    → main generation (normal path)
                           →  {"search": true, "query": "..."}
                                    → send "looking it up" message to user
                                    → enqueue search request (global queue)
                                    → DDG search on Mesh → fetch top 1-2 pages
                                    → trafilatura extracts content
                                    → results returned to Joi
                                    → inject into context
                                    → main generation (enriched)
                                    → on any failure → explicit fallback
```

## Latency UX

Two full model inference passes (pre-screen + main generation) plus a
multi-hop network round-trip. On a 1650 this is real latency — normal case
~30-60s total, worst case longer. Not fast, but acceptable with the right UX.

When pre-screen triggers a search, Joi sends a brief in-character message
immediately — the "looking it up" message fires when the request enters the
queue, not when it starts processing. User gets immediate feedback even if
waiting behind another search.

Variations (picked randomly):
- *"Let me look that up..."*
- *"One sec, looking that up."*
- *"Give me a moment."*

## Failure Handling

If search fails for any reason (timeout, Mesh unreachable, DDG blocked,
page fetch fails, trafilatura returns garbage), Joi falls back explicitly:

> *"Couldn't find anything on that, but here's what I know..."*

Then answers from training data normally. Transparent, honest, no silent
pretending nothing happened.

Failure triggers:
- Pre-screen exceeds `JOI_LLM_TIMEOUT` (30s default)
- Mesh search + fetch exceeds `JOI_SEARCH_TIMEOUT` (default: 30s)
- All fetched pages return empty/garbage content
- Mesh unreachable (Nebula VPN flap, service down)

## Global Search Queue

Searches run one at a time via a global `queue.Queue` with a single worker
thread in `search.py`. Prevents queuing multiple full inference + network
passes simultaneously on constrained hardware.

- "Looking it up" message fires when request **enters** the queue
- Results delivered via Future when worker completes
- If queue grows (multiple conversations searching simultaneously), each
  waits its turn — no requests dropped
- Worker count can be increased later if hardware improves

## Fact Privacy Classification

Facts passed to the pre-screen LLM must not leak sensitive data externally.
Two columns on `user_facts` control this — see `memory-store-schema.md §
Fact Privacy Classification` for full details.

Summary:
- Only `external_safe = 1` facts are included in the pre-screen context
- `private_fact = 1` facts are never sent externally under any circumstances
- Unclassified (`NULL`) facts are excluded from external context (conservative default)

LLM classification at extraction time is imperfect — occasional misclassification
is expected. Use `joi-admin facts set-privacy <id>` to correct. Spot-check
periodically.

## Pre-screen

Uses the main model (`JOI_OLLAMA_MODEL`) — no separate model needed out of
the box. Optional override via `JOI_SEARCH_MODEL`.

**Input:**
- Short rolling message window (last 3-5 messages) — resolves pronouns and
  references that make no sense from the last message alone
- `external_safe` facts relevant to the message, retrieved via FTS against
  the user message — same pattern as `get_facts_as_context()`. Targeted,
  not a full dump.

**Prompt sketch:**
```
Recent conversation:
User: I've been obsessing over this band lately
Joi: Oh which one?
User: Architects. look them up

What you know about this user (safe to use externally):
- favourite music genre: metalcore

Do you need to look something up externally to answer this well?
Consider: do you know this with confidence, or would fresh data make your
answer meaningfully better (lyrics, current events, a specific person,
prices, etc.)?

Respond with JSON only:
{"search": false}
or
{"search": true, "query": "<concise search query>"}
```

**Env var:** `JOI_SEARCH_MODEL` — optional override, defaults to main model.

## Search Backend

**DuckDuckGo** via HTML endpoint — no API key, no account, no external dependencies.
Privacy-focused. For personal use, rate limiting is not a concern.

Runs on a dedicated **Search VM** — separate from Mesh, which is comms-only
(Signal, and future Telegram/WhatsApp). The Search VM has WAN access, connects
to Joi via Nebula VPN, and exposes a single HMAC-authenticated Flask endpoint.
Unauthenticated requests are rejected.

**Search VM stack:**
- Nebula VPN (Joi → Search VM, no direct WAN exposure to Joi)
- Flask service — single endpoint, receives query, returns extracted content
- `trafilatura`, `httpx` — minimal dependencies, single purpose

## Page Fetch & Content Extraction

DDG returns links + snippets, not content. For most scenarios (lyrics, articles,
entity details) the snippet is insufficient — the top result needs to be fetched
and parsed.

**Fetch tuning:**
- Plain HTTP request — no JS engine, so JS-heavy SPAs return nothing useful.
  Lyrics/article sites are generally server-rendered so this is fine.
- `User-Agent`: mobile browser UA — leaner pages, fewer ads, simpler layout.
  Alternatively a text browser UA (`Lynx`, `w3m`) for maximum simplicity on
  sites that respect it.
- No `Cookie` header — avoids cookie consent walls on many sites.
- `Accept-Language: en` — avoids locale redirects.
- `DNT: 1` — fits the privacy theme.

**Content extraction:**
- `trafilatura` — Readability-style extraction, strips nav/ads/boilerplate,
  returns clean article/lyric text. Use `favor_recall=True` for aggressive
  extraction, `include_tables=False` to skip tabular junk.
- DDG snippet used as fallback if fetch fails or returns garbage.

**Fetch up to 2 results** — if first page fails or content is too short,
try the second.

## Result Injection

Extracted page content (not DDG snippets) is injected as a context block
before main generation. Truncated to `JOI_SEARCH_MAX_CHARS` per result.

**Sketch:**
```
[Search results for: "<query>"]
--- Result 1: <title> ---
<extracted content, truncated>

--- Result 2: <title> ---
<extracted content, truncated>
```

**Max results:** minimum 2, upper bound controlled by `JOI_SEARCH_MAX_RESULTS`
(default: 4). Each result truncated to `JOI_SEARCH_MAX_CHARS` (default: 2000)
— placeholder value. The right limit depends on the loaded model's context
window and what else is already in the prompt (facts, summaries, RAG).
Needs revisiting during implementation.

## Per-Conversation Toggle

Search is opt-in per conversation — same pattern as Wind's allowlist.
A `search_allowlist` field in the policy JSON controls which conversation
IDs are eligible. `search.py` checks this before firing the pre-screen.

Conversations not in the allowlist take the normal path silently — no
error, no indication to the user.

No new joi-admin commands needed — managed by editing the policy file
directly, same as Wind's allowlist.

## Logging

Follows Joi's standard structured logging pattern:

- `DEBUG` — full detail: query, URLs fetched, extracted content, full injection block
- `INFO` — query, URLs fetched, result count, total chars injected

If it's going out to an external network anyway, logging it locally adds no
additional privacy risk.

## Schema Migration

`user_facts` requires two new columns — schema v13:

```sql
ALTER TABLE user_facts ADD COLUMN external_safe INTEGER DEFAULT NULL;
ALTER TABLE user_facts ADD COLUMN private_fact INTEGER DEFAULT NULL;
```

## Search in Wind & Reminders

Wind proactive messages and reminders can also benefit from search — Joi
enriches her own outbound messages with fresh external context before sending.

**Wind:** topic content drives the pre-screen. If Joi wants to follow up on
a music artist, a sports result, or a news story, she can search for current
info before composing the message. The pre-screen prompt shifts from
"the user said X" to "Joi wants to bring up X":

```
Joi is about to send a proactive message about: "<topic title> — <topic summary>"

Would fresh external information make this message meaningfully better?
(e.g. recent news, current stats, lyrics, latest release)

Respond with JSON only:
{"search": false}
or
{"search": true, "query": "<concise search query>"}
```

**Reminders:** most reminders don't need search ("dentist appointment").
But outcome-curiosity followups might — "checking how the concert went"
could benefit from a setlist search. Same pre-screen, topic-driven.

**Key difference from user-triggered search:** no "looking it up" message
is sent to the user — Joi is composing proactively, the search happens
silently before the message is generated. The user just receives a
well-informed message.

Same global search queue, same HMAC channel to Search VM, same injection
pattern. Pre-screen prompt is the only thing that changes.

### Reminder Pre-Search

Reminders fire at a specific time — search cannot block them. Instead,
search runs ahead of time:

1. Scheduler detects a reminder due within `JOI_SEARCH_REMINDER_ADVANCE_MINUTES`
   (default: 15) — fires a background search for that reminder
2. Results cached in memory (dict keyed on reminder ID, no files)
3. At fire time — cached results injected into generation, reminder fires on time
4. If search hasn't completed by fire time (queue backed up, slow network)
   — fall back gracefully, fire without enrichment. Punctuality over enrichment.

Cache TTL: slightly longer than the advance window to cover minor timing
variance. Evicted after reminder fires.

## Files to Create/Modify

- `execution/search/` — new Search VM service (Flask, DDG, trafilatura, HMAC auth)
- `execution/search/systemd/` — systemd unit + defaults file for search service
- `execution/joi/api/search.py` — pre-screen logic, global search queue, result injection
- `execution/joi/api/server.py` — call into `search.py`, inject results into prompt
- `execution/joi/memory/store.py` — schema v13 migration, `external_safe`/`private_fact` columns
- `execution/joi/systemd/joi-api.default` — `JOI_SEARCH_ENABLED`, `JOI_SEARCH_MODEL`, `JOI_SEARCH_TIMEOUT`, `JOI_SEARCH_MAX_RESULTS`, `JOI_SEARCH_MAX_CHARS`
- `sysprep/` — stage scripts for Search VM provisioning
