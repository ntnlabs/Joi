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

## Incremental Rollout
1. Add queue between frontend and backend workers.
2. Enforce per-conversation serialization in frontend/queue layer.
3. Move LLM execution to worker nodes.
4. Keep memory access centralized from day one.
