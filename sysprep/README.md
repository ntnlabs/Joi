# Sysprep Stages

This directory is split into rollout stages by scope, not by host complexity.

## Stage 1: OS Baseline (Host Prep)

Stage 1 is host-level setup.

Typical content:

- hostname
- firewall baseline
- DNS baseline
- NTP baseline
- OS users/directories required by host services
- update-gating scripts (`update.sh`)

Goal:

- host is hardened
- host networking model is correct
- host can be safely updated in a controlled way

Examples in this repo:

- `router/setup.sh`, `router/update.sh`
- `ntp/setup.sh`, `ntp/update.sh`
- `mesh/setup.sh`, `mesh/update.sh`
- `joi/setup.sh`, `joi/update.sh`

## Stage 2: Infrastructure Runtime Substrate

Stage 2 is for shared runtime building blocks that are above OS setup, but not yet Joi application deployment.

Typical content:

- overlay networking runtime (Nebula)
- transport/runtime dependencies (for example `signal-cli`)
- container runtime substrate (Docker, NVIDIA runtime/toolkit) if treated as platform
- cert placement and runtime service wiring for those building blocks

Goal:

- host can participate in the secure transport/control plane
- runtime dependencies are installed and validated
- application deployment can start without debugging OS/network basics

Examples in this repo:

- `mesh/stage2.md` (Nebula)
- `joi/stage2.md` (Nebula + Docker/NVIDIA runtime substrate)

## Stage 3: Project/Application Deployment

Stage 3 is Joi-project-specific deployment and integration.

Typical content:

- `signal-cli` account linking / daemon workflow for Mesh transport integration
- Ollama deployment on Joi (after Docker/NVIDIA runtime is already in place)
- Joi API / Mesh worker service deployment
- app config/env/secrets wiring (HMAC, endpoints, service units)
- end-to-end integration checks

Goal:

- Joi stack is deployed
- Joi and Mesh communicate over Nebula
- project runtime is operational

Examples in this repo:

- `mesh/stage3.md` (`signal-cli`)
- `joi/stage3.md` (Ollama deployment)

## Practical Rule

If a step can fail because OS networking/firewall is wrong, it belongs in stage 1.

If a step is reusable infrastructure/runtime plumbing, it belongs in stage 2.

If a step is specifically wiring or operating Joi/Mesh application behavior, it belongs in stage 3.
