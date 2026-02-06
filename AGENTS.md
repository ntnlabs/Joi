# Repository Guidelines

This repository currently contains early architecture notes for the Joi project. Use this guide to keep contributions consistent as the codebase grows.

## Project Structure & Module Organization
- `Joi-architecture-v2.md`: **Current authoritative architecture** (security-hardened, Nebula mesh, Proxmox VM).
- `Joi-architecture.md`: Original high-level architecture (superseded by v2).
- `Joi-threat-model.md`: Threat model, risks, and mitigations.
- `PROJECT_SUMMARY.md`: Quick reference for project overview.
- Future code should live under `src/` (runtime, adapters, and agents) and `tests/` (unit/integration).
- If you add assets or diagrams, use `assets/` and keep filenames descriptive (e.g., `assets/proxy-flow.png`).

## Build, Test, and Development Commands
There are no build or test scripts yet. If you add tooling, document it here with examples, for instance:
- `make dev` — run the local dev loop.
- `npm test` — execute unit tests.
- `pytest` — run Python test suite.

## Coding Style & Naming Conventions
No enforced style yet. When adding code:
- Use 2 spaces for YAML/JSON and 4 spaces for Python (if used).
- Prefer clear, descriptive names (e.g., `openhab_event_adapter.py`, `signal_proxy.go`).
- Add minimal comments only where behavior is non-obvious.

## Testing Guidelines
No test framework is configured. When tests are introduced:
- Place them under `tests/`.
- Name files to mirror sources (e.g., `tests/test_openhab_events.py`).
- Document how to run tests in the section above.

## Commit & Pull Request Guidelines
No commit conventions are established in this repo yet. Until then:
- Use concise, imperative messages (e.g., “Add proxy webhook spec”).
- PRs should include: purpose, scope, and any design decisions or tradeoffs.

## Security & Configuration Tips
- Do not store secrets in this repo.
- For config, prefer local `.env` files (not committed) or documented placeholders.
- Maintain the security boundaries described in `Joi-architecture.md`.
