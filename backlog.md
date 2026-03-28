# Joi Backlog — Fix Soon

Items that are real bugs or quality issues but not urgent enough to block current work.
Address these before the next major feature phase.

---

## Mesh

- **Synchronous blocking in mesh receive loop** (`execution/mesh/proxy/signal_worker.py`)
  Attachment processing and typing indicator forwarding do blocking HTTP POSTs to Joi
  inline in the main Signal receive loop. While Joi is slow (mid-inference), no other
  messages are received. Fix: fire attachment and typing forwards in background threads.

- **Consolidation scans all conversations instead of current one** (`execution/joi/api/server.py`, `execution/joi/memory/consolidation.py`)
  `run_consolidation()` fetches all distinct conversation IDs and checks each one on every
  message. Only the conversation that just received a message can have crossed the threshold.
  Fix: pass `conversation_id` through `_maybe_run_consolidation()` into `run_consolidation()`
  and skip the full scan. Likely leftover from single-conversation v1 code.

- **Rewrite all FTS/RAG/facts fallback logic** (`execution/joi/api/server.py:2196, 2249`)
  When FTS returns nothing (e.g. short message like "ok", "lol"), the current fallback dumps
  ALL facts (no limit) and 10 days of summaries into the prompt. This was designed assuming
  no keywords meant "need everything" — but it's the wrong trade-off. A short conversational
  message doesn't need 10 days of context, it needs nothing or a very small recent slice.
  Redesign: if FTS returns empty, inject nothing (or at most a small fixed-size recent
  slice). Do not fall back to unbounded dumps for any path — facts, summaries, or RAG.

- **FTS query sanitizer too aggressive for Slovak** (`execution/joi/memory/store.py:1500-1501`)
  `len(w) > 2` strips short Slovak prepositions and particles that carry real meaning
  (`vo`, `na`, `zo`, `po`, `ku`). Stopword list is English-only — Slovak words that
  should be filtered aren't, and fall through to the length filter instead.
  Result: weaker FTS matches for Slovak queries, more frequent fallback-to-everything triggers.
  Fix: lower length threshold, add Slovak stopwords, consider unicode-aware tokenization.

- **Race condition in send cache cleanup** (`execution/joi/api/server.py:348, 2910`)
  `_last_send_times` is written under per-conversation lock (`_get_send_lock`) but read
  and mutated during cleanup under `_send_locks_lock`. Two different locks — concurrent
  send + cleanup can cause `RuntimeError: dictionary changed size during iteration`.
  Fix: protect all access to `_last_send_times` with `_send_locks_lock` consistently,
  or use a `threading.RLock` and always acquire it before touching the dict.

- **Scheduler bypasses MessageQueue for LLM calls** (`execution/joi/api/server.py:2037`, `execution/joi/api/scheduler.py`)
  Wind and reminder generation call `llm.chat()` directly from the scheduler thread,
  bypassing the `MessageQueue`. Ollama serializes so no crash or OOM, but there is no
  priority control — user responses can be delayed by background Wind/reminder inference.
  Fix: route Wind and reminder LLM calls through `message_queue` with `is_owner=False`
  so owner messages always take priority over proactive generation.

- **Unreliable worker shutdown in mesh forwarder** (`execution/mesh/proxy/forwarder.py:154`)
  `_shutdown_workers()` sends `None` sentinel via `put_nowait()`. If a queue is full
  (Joi is down, messages piling up), `queue.Full` is silently swallowed and that worker
  never receives the shutdown signal. Workers are daemon threads so process exits eventually,
  but graceful shutdown fails — in-flight messages in full queues are lost without processing.
  Fix: replace sentinel approach with a `threading.Event` stop flag that workers check
  alongside the queue, independent of queue capacity.

- **Group membership cache not truly fail-closed** (`execution/joi/api/group_cache.py:178-181`)
  The docstring claims fail-closed behavior, but when a refresh fails and stale cache exists,
  access is granted from stale data. A revoked group member retains cross-group knowledge
  access until the next successful refresh. Only relevant in business mode + dm_group_knowledge.
  Fix: either deny on any refresh failure (true fail-closed), or allow stale cache only within
  a grace period (e.g. 2x TTL) and deny beyond that. Also fix the misleading docstring.

- **Missing docstrings on _as_dict / _as_list_of_dicts** (`execution/mesh/proxy/signal_worker.py:1013-1017`)
  Two one-liner helper functions lack docstrings. Add a one-line description to each.

- **logging.basicConfig() in main() is dead code** (`execution/mesh/proxy/signal_worker.py:1403`)
  `configure_logging()` is called at module import time (line 17), so `basicConfig` in `main()`
  is always a no-op. Safe to delete.

- **Rate limiter not thread-safe** (`execution/mesh/proxy/rate_limiter.py`)
  `check_and_add` has no locking. Two concurrent threads for the same sender key could both
  pass the limit check before either records the event. No real race today since the Signal
  receive loop is single-threaded, but the class is not safe if that changes.
  Fix: add a `threading.Lock` acquired in `check_and_add`.

- **jsonrpc_client single recv(65536) may truncate large responses** (`execution/mesh/proxy/jsonrpc_client.py:24`)
  `s.recv(65536)` returns at most 64KB in one call. Large signal-cli responses (e.g. big group
  member lists) may arrive in multiple chunks — only the first is read, causing a JSON parse
  error or silent data loss. Fix: read in a loop until EOF, then decode the full buffer.

- **transport_native raw Signal envelope forwarded to Joi** (`execution/mesh/proxy/signal_worker.py:1324`)
  The normalized payload includes `content.transport_native` — the full raw Signal envelope
  (sourceUuid, serverGuid, device info, raw dataMessage, etc.). Mesh uses it internally for
  attachment processing and mention re-checking, both of which happen before `forward_to_joi`.
  Joi receives this data unnecessarily and it may end up in logs.
  Fix: `payload["content"].pop("transport_native", None)` after Mesh processing, before forward.

---

## Sysprep

- **iptables cutover before package installs, no rollback trap** (`sysprep/router/setup.sh:105`)
  Firewall is flushed and default-DROP applied at step 3, but `apk add dnsmasq/chrony` runs
  at steps 4-5. If `apk` fails mid-run, `set -e` exits and the machine is left with DROP-all
  rules saved to disk. SSH rules are included so lockout is unlikely, but not guaranteed.
  Fix: move `iptables -P INPUT/FORWARD/OUTPUT DROP` to after all rules are added, or add a
  `trap` that restores a permissive policy on failure before exiting.

---

## To triage

_(items from security review not yet categorized here)_
