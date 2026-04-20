# Repository Guidelines

Guidelines for AI agents and contributors working on the Joi codebase.

## Project Structure

```
execution/
├── joi/                    # Joi VM - AI assistant service
│   ├── api/                # FastAPI server + modules
│   │   ├── server.py       # Main API (~2000 lines)
│   │   ├── scheduler.py    # Background tasks
│   │   ├── admin_routes.py # Admin endpoints
│   │   ├── message_queue.py # Queue + rate limiter
│   │   ├── group_cache.py  # Membership cache
│   │   ├── hmac_auth.py    # HMAC authentication
│   │   ├── hmac_rotator.py # Key rotation
│   │   └── policy_manager.py # Policy storage
│   ├── config/             # Configuration + logging
│   ├── memory/             # SQLCipher database
│   ├── llm/                # Ollama client
│   ├── wind/               # Proactive messaging
│   └── systemd/            # Service files
│
├── mesh/                   # Mesh VM - Signal proxy
│   └── proxy/              # Flask proxy service
│
└── shared/                 # Shared package (pip install -e)
    └── hmac_core.py        # Common HMAC functions

sysprep/                    # Deployment scripts (stage1-4)
```

## Architecture Docs

| Document | Description |
|----------|-------------|
| `Joi-architecture-v3.md` | **Current** - stateless mesh, config push |
| `Joi-threat-model.md` | Threat model and mitigations |
| `api-contracts.md` | API specifications |
| `policy-engine.md` | Security policy rules |

## Development

### Running Locally

```bash
# Joi API
cd execution/joi
pip install -r requirements.txt
pip install -e ../shared
python -m api.server

# Mesh proxy
cd execution/mesh
pip install -r requirements.txt
pip install -e ../shared
python -m proxy.signal_worker
```

### Environment Files

- `/etc/default/joi-api` - Joi configuration
- `/etc/default/mesh-signal-worker` - Mesh configuration

### Testing

```bash
# Syntax check
python -m py_compile execution/joi/api/server.py

# No test framework yet - manual testing via Signal
```

## Coding Style

- **Python**: 4 spaces, type hints encouraged
- **Logging**: Use structured logging with `extra={}` fields
  ```python
  logger.info("Message received", extra={"sender": sender_id, "length": len(text)})
  ```
- **Dependencies**: Use dependency injection (`set_dependencies()`) for modules
- **Naming**: snake_case for files/functions, PascalCase for classes

## Commit Guidelines

- Imperative mood: "Add feature" not "Added feature"
- Co-author tag for AI assistance:
  ```
  Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
  ```

## Security

- No secrets in repo (use env files)
- HMAC authentication between joi ↔ mesh
- SQLCipher encryption for memory database
- Privacy mode redacts PII in logs
- Fail-closed design (deny on error)

## Key Patterns

### Dependency Injection
Modules use `set_dependencies()` to avoid circular imports:
```python
class Scheduler:
    def set_dependencies(self, memory, policy_manager, ...):
        self._memory = memory
        self._policy_manager = policy_manager
```

### Structured Logging
```python
# Text mode shows: "Event [key=value key2=value2]"
# JSON mode outputs full structured records
logger.info("Event", extra={"key": "value", "key2": "value2"})
```

### Admin Endpoints
Read-only endpoints use IP check (localhost only).
Sensitive endpoints (rotate, kill-switch) require HMAC auth.
