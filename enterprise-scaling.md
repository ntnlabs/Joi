# Joi Enterprise Scaling Summary

## Goal
Scale to high message volume (for example 10,000 users) without breaking memory consistency, policy control, or operational safety.

## Core Decision
Keep **Joi Frontend** as the source of truth and control plane.

- Mesh stays transport-focused (receive/send), agnostic to business logic.
- Joi Frontend owns auth, policy, routing, config push to mesh, and orchestration.
- Joi Backends are stateless compute workers (LLM processing only).

## Target Topology
1. `mesh` receives inbound traffic and forwards to `joi-frontend`.
2. `joi-frontend` validates/authenticates and enqueues work.
3. Workers (`joi-backend-*`) consume jobs and run LLM logic.
4. Responses return to `joi-frontend` for final handling and outbound send via mesh.

## Memory Model
Use a **central memory store** (single logical source of truth):

- messages
- user_facts
- context_summaries
- knowledge_chunks / RAG index
- system_state

Backends must not keep their own persistent memory state.

## Consistency Rule (Most Important)
No concurrent processing per `conversation_id`.

- Partition/lock by `conversation_id`.
- Allow only one in-flight job per conversation.
- Different conversations may run on different backend nodes in parallel.
- Same sender/group can land on different nodes over time, as long as ordering is preserved.

## Why This Works
- Horizontal scale for LLM compute.
- Stable memory behavior (no cross-node drift).
- Frontend remains authoritative for system behavior and config management.

## HA Notes
- Start with one writable central DB.
- Later: add replicas/failover, but keep one active writer to avoid split-brain writes.

## Database: SQLCipher → PostgreSQL

SQLCipher is the right choice for the current single-node deployment — encrypted at rest,
zero operational overhead. It becomes a bottleneck at scale:

- Single writer — no horizontal writes, concurrent processes serialize or fail
- No replication or hot standby
- No connection pooling
- Backup is a file copy — fragile under live multi-process writes

**Target: PostgreSQL.** Proper multi-writer ACID, replication, pgBouncer for pooling,
row-level security for tenant isolation. At-rest encryption covered by LUKS (already in
place) + PostgreSQL encryption options. SQLCipher's role goes away.

This is a prerequisite for any multi-user or multi-instance deployment.

## DB Abstraction Layer (Do This Before the Migration)

Raw SQL is currently scattered across `store.py`, `topics.py`, `state.py`, `feedback.py`,
`reminders.py`, and others. Migrating to PostgreSQL in this state means touching every file.

The plan:

1. **Per-domain repository interfaces** — `MessageRepository`, `FactRepository`,
   `ReminderRepository`, `WindStateRepository`, etc. Named methods only, no raw SQL
   outside these files.
2. **SQLite/SQLCipher backend** behind the interface — same behaviour, just reorganised.
   Drop-in for current code.
3. **PostgreSQL backend** behind the same interface — rest of codebase untouched.
4. **Switch via config** — `JOI_DB_BACKEND=sqlite` (default) or `postgres`, connection
   string injected at startup.

This is a mechanical refactor — no logic changes, just boundary drawing. Do it before
the first multi-user deployment or any serious load testing.

## Incremental Rollout
1. Add queue between frontend and backend workers.
2. Enforce per-conversation serialization in frontend/queue layer.
3. Move LLM execution to worker nodes.
4. Keep memory access centralized from day one.
5. Draw DB abstraction layer boundaries.
6. Swap SQLCipher for PostgreSQL behind the abstraction.
