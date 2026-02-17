# Environment Variables Reference

Complete list of configurable environment variables for Joi system.

## Joi API (`/etc/default/joi-api`)

### Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_BIND_HOST` | `0.0.0.0` | API bind address |
| `JOI_BIND_PORT` | `8443` | API port |
| `JOI_LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |

### LLM Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_OLLAMA_URL` | `http://localhost:11434` | Ollama API URL |
| `JOI_OLLAMA_MODEL` | `llama3` | Model to use |
| `JOI_LLM_TIMEOUT` | `180` | LLM request timeout in seconds |

### System Prompts

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_PROMPTS_DIR` | `/var/lib/joi/prompts` | Directory for per-user/group prompts |

Prompts directory structure:
```
/var/lib/joi/prompts/
├── default.txt           # Default prompt for all
├── users/
│   └── <phone>.txt       # Per-user prompt (for DMs)
└── groups/
    └── <group_id>.txt    # Per-group prompt
```

### Mesh Communication

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_MESH_URL` | `http://mesh:8444` | Mesh proxy URL for outbound messages |

### Memory Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_MEMORY_DB` | `/var/lib/joi/memory.db` | SQLite database path |
| `JOI_MEMORY_KEY` | (none) | SQLCipher encryption key (future) |
| `JOI_CONTEXT_MESSAGES` | `40` | Recent messages to include in LLM context |
| `JOI_REQUIRE_ENCRYPTED_DB` | `1` | Require encrypted DB (fail startup if unavailable) |

### Memory Consolidation

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_CONSOLIDATION_SILENCE_HOURS` | `1` | Hours of silence before consolidation |
| `JOI_CONSOLIDATION_MAX_MESSAGES` | `200` | Force consolidation at this message count |
| `JOI_CONSOLIDATION_ARCHIVE` | `0` | Set to `1` to archive instead of delete |

### RAG (Knowledge Retrieval)

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_RAG_ENABLED` | `1` | Enable RAG knowledge retrieval |
| `JOI_RAG_MAX_TOKENS` | `500` | Max tokens for RAG context |

### Time Awareness

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_TIME_AWARENESS` | `0` | Inject current datetime into system prompt |
| `JOI_TIMEZONE` | `Europe/Bratislava` | User timezone (IANA format) |

### Scheduler (Wind/Tasks)

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_SCHEDULER_ENABLED` | `1` | Enable background scheduler |
| `JOI_SCHEDULER_INTERVAL` | `60` | Tick interval in seconds |
| `JOI_SCHEDULER_STARTUP_DELAY` | `10` | Seconds to wait before first tick |

---

## Mesh Signal Worker (`/etc/default/mesh-signal-worker`)

### Signal Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `SIGNAL_ACCOUNT` | (required) | Signal phone number (E.164 format) |
| `SIGNAL_CLI_BIN` | `/usr/local/bin/signal-cli` | Path to signal-cli binary |
| `SIGNAL_CLI_CONFIG_DIR` | `/var/lib/signal-cli` | signal-cli config directory |

### Worker Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `MESH_WORKER_HTTP_PORT` | `8444` | HTTP port for outbound API |
| `MESH_SIGNAL_POLL_SECONDS` | `5` | Notification poll interval |
| `MESH_POLICY_FILE` | `/etc/mesh-proxy/policy.json` | Policy file path |

### Forwarding Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `MESH_ENABLE_FORWARD` | `0` | Set to `1` to enable forwarding to Joi |
| `MESH_JOI_INBOUND_URL` | `http://joi:8443/api/v1/message/inbound` | Joi API URL |
| `MESH_FORWARD_TIMEOUT` | `120` | Timeout for Joi requests in seconds |

---

## Example Configurations

### Joi API (`/etc/default/joi-api`)

```bash
# Core
JOI_BIND_HOST=0.0.0.0
JOI_BIND_PORT=8443
JOI_LOG_LEVEL=INFO

# LLM
JOI_OLLAMA_URL=http://localhost:11434
JOI_OLLAMA_MODEL=llama3
JOI_LLM_TIMEOUT=180

# Mesh
JOI_MESH_URL=http://172.22.22.1:8444

# Memory
JOI_MEMORY_DB=/var/lib/joi/memory.db
JOI_CONTEXT_MESSAGES=40

# Consolidation
JOI_CONSOLIDATION_SILENCE_HOURS=1
JOI_CONSOLIDATION_MAX_MESSAGES=200
JOI_CONSOLIDATION_ARCHIVE=0

# Prompts (optional, uses defaults if not set)
# JOI_PROMPTS_DIR=/var/lib/joi/prompts
```

### Mesh Signal Worker (`/etc/default/mesh-signal-worker`)

```bash
# Signal
SIGNAL_ACCOUNT=+<COUNTRY><NUMBER>
SIGNAL_CLI_BIN=/usr/local/bin/signal-cli
SIGNAL_CLI_CONFIG_DIR=/var/lib/signal-cli

# Worker
MESH_WORKER_HTTP_PORT=8444
MESH_POLICY_FILE=/etc/mesh-proxy/policy.json

# Forwarding
MESH_ENABLE_FORWARD=1
MESH_JOI_INBOUND_URL=http://172.22.22.2:8443/api/v1/message/inbound
MESH_FORWARD_TIMEOUT=120
```

---

## Files Reference

### Joi VM

| Path | Purpose |
|------|---------|
| `/etc/default/joi-api` | Environment variables |
| `/var/lib/joi/memory.db` | SQLite database |
| `/var/lib/joi/prompts/` | Per-user and per-group system prompts |
| `/var/lib/joi/prompts/default.txt` | Default system prompt |
| `/var/lib/joi/prompts/users/<phone>.txt` | Per-user prompts for DMs |
| `/var/lib/joi/prompts/groups/<group_id>.txt` | Per-group prompts |

### Mesh VM

| Path | Purpose |
|------|---------|
| `/etc/default/mesh-signal-worker` | Environment variables |
| `/etc/mesh-proxy/policy.json` | Sender whitelist and rate limits |
| `/var/lib/signal-cli/` | Signal account data |
