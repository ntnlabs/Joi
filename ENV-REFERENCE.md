# Environment Variables Reference

Complete list of configurable environment variables for Joi system.

## Joi API (`/etc/default/joi-api`)

### Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_BIND_HOST` | `10.42.0.10` | API bind address (Nebula IP) |
| `JOI_BIND_PORT` | `8443` | API port |
| `JOI_LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `JOI_LOG_JSON` | `0` | Set to `1` for JSON structured logs |

### Message Limits

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_MAX_INPUT_LENGTH` | `1500` | Max inbound message length (chars) |
| `JOI_MAX_OUTPUT_LENGTH` | `2000` | Max outbound message length (chars) |
| `JOI_SIGNAL_FORMAT_ENABLED` | `1` | Set to `0` to disable Unicode bold conversion for Signal |

### LLM Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_OLLAMA_URL` | `http://localhost:11434` | Ollama API URL |
| `JOI_OLLAMA_MODEL` | `llama3` | Model to use |
| `JOI_LLM_TIMEOUT` | `180` | LLM request timeout in seconds |
| `JOI_LLM_KEEP_ALIVE` | `30m` | How long to keep model in VRAM after request |
| `JOI_OLLAMA_NUM_CTX` | (unset) | Override context window size (prefer setting in Modelfile) |

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
| `JOI_MESH_URL` | `http://10.42.0.1:8444` | Mesh proxy URL for outbound messages |
| `JOI_MESH_POLICY_PATH` | `/var/lib/joi/policy/mesh-policy.json` | Mesh policy file path |

### Memory Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_MEMORY_DB` | `/var/lib/joi/memory.db` | SQLite database path |
| `JOI_MEMORY_KEY` | (none) | SQLCipher encryption key (inline) |
| `JOI_MEMORY_KEY_FILE` | `/etc/joi/memory.key` | SQLCipher encryption key file path |
| `JOI_NONCE_DB` | `/var/lib/joi/nonces.db` | Nonce replay-protection database |
| `JOI_CONTEXT_MESSAGES` | `50` | Recent messages to include in LLM context |
| `JOI_REQUIRE_ENCRYPTED_DB` | `1` | Require encrypted DB (fail startup if unavailable) |

### Memory Consolidation / Compaction

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_COMPACT_BATCH_SIZE` | `20` | Messages to compact when context overflows |
| `JOI_CONSOLIDATION_MODEL` | (unset) | Optional separate model for consolidation (e.g. `joi-consolidator`) |
| `JOI_MESSAGE_RETENTION_DAYS` | `0` | Days before fully-processed messages are hard-deleted (0 = keep forever, max 90) |

### Wind Engagement

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_ENGAGEMENT_MODEL` | (unset) | Optional model for engagement classification (e.g. `joi-engagement`) |

### RAG (Knowledge Retrieval)

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_RAG_ENABLED` | `1` | Enable RAG knowledge retrieval |
| `JOI_RAG_MAX_TOKENS` | `500` | Max tokens for RAG context |

### Facts & Summaries FTS

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_FACTS_FTS_ENABLED` | `1` | Enable facts full-text search context injection |
| `JOI_FACTS_FTS_MAX_TOKENS` | `300` | Max tokens for facts FTS context |
| `JOI_SUMMARIES_FTS_ENABLED` | `1` | Enable summaries full-text search context injection |
| `JOI_SUMMARIES_FTS_MAX_TOKENS` | `400` | Max tokens for summaries FTS context |

### Time Awareness

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_TIME_AWARENESS` | `0` | Inject current datetime into system prompt |
| `JOI_TIMEZONE` | `Europe/Bratislava` | User timezone (IANA format) |

### Response Cooldown

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_RESPONSE_COOLDOWN_SECONDS` | `5.0` | Min seconds between responses in DMs |
| `JOI_RESPONSE_COOLDOWN_GROUP_SECONDS` | `2.0` | Min seconds between responses in groups |
| `JOI_OUTBOUND_MAX_PER_HOUR` | `120` | Max outbound messages per hour across all conversations |

### Group Membership

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_MEMBERSHIP_REFRESH_MINUTES` | `15` | How often to refresh group membership cache |

### Knowledge Ingestion

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_INGESTION_DIR` | `/var/lib/joi/ingestion` | Directory watched for new knowledge files |
| `JOI_INGESTION_KEEP_FILES` | `0` | Set to `1` to keep source files after ingestion |
| `JOI_INGESTION_CHUNK_SIZE` | `500` | Chunk size for RAG ingestion (tokens) |
| `JOI_INGESTION_OVERLAP` | `50` | Chunk overlap for RAG ingestion (tokens) |
| `JOI_MAX_DOCUMENT_SIZE` | `1048576` | Max document size in bytes; Joi's ingest guard is 2x this (should match `MESH_MAX_DOCUMENT_SIZE`) |

### HMAC Authentication

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_HMAC_SECRET` | (required) | Shared secret — must match `MESH_HMAC_SECRET` on Mesh VM |
| `JOI_HMAC_TIMESTAMP_TOLERANCE_MS` | `300000` | Timestamp tolerance in ms (default: 5 minutes) |
| `JOI_HMAC_SECRET_FILE` | `/var/lib/joi/hmac.secret` | Path to rotated HMAC secret file (managed by joi-admin) |

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
| `SIGNAL_CLI_SOCKET` | `/var/run/signal-cli/socket` | signal-cli socket path (socket mode) |

### Worker Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `MESH_WORKER_HTTP_PORT` | `8444` | HTTP port for outbound API |
| `MESH_BIND_HOST` | `0.0.0.0` | Bind address |
| `MESH_SIGNAL_MODE` | `stdio` | Signal CLI mode (`stdio` or `socket`) |
| `MESH_LOG_LEVEL` | `INFO` | Logging level |
| `MESH_LOG_JSON` | `0` | Set to `1` for JSON structured logs |
| `MESH_LOG_DIR` | `/var/log/mesh-proxy` | Log directory |

### Forwarding Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `MESH_MAX_DOCUMENT_SIZE` | `1048576` | Max attachment size in bytes before forwarding to Joi (should match `JOI_MAX_DOCUMENT_SIZE`) |
| `MESH_ENABLE_FORWARD` | `0` | Set to `1` to enable forwarding to Joi |
| `MESH_JOI_URL` | (required) | Joi API base URL (e.g. `http://10.42.0.10:8443`) |
| `MESH_FORWARD_TIMEOUT` | `120` | Timeout for Joi requests in seconds |
| `MESH_HMAC_SECRET` | (required) | Shared secret — must match `JOI_HMAC_SECRET` on Joi VM |

---

## Files Reference

### Joi VM

| Path | Purpose |
|------|---------||
| `/etc/default/joi-api` | Environment variables |
| `/var/lib/joi/memory.db` | SQLite database |
| `/var/lib/joi/nonces.db` | Nonce replay-protection database |
| `/var/lib/joi/prompts/` | Per-user and per-group system prompts |
| `/var/lib/joi/prompts/default.txt` | Default system prompt |
| `/var/lib/joi/prompts/users/<phone>.txt` | Per-user prompts for DMs |
| `/var/lib/joi/prompts/groups/<group_id>.txt` | Per-group prompts |
| `/var/lib/joi/policy/mesh-policy.json` | Mesh policy (pushed from Joi to Mesh) |

### Mesh VM

| Path | Purpose |
|------|---------||
| `/etc/default/mesh-signal-worker` | Environment variables (incl. initial HMAC secret) |
| `/var/lib/signal-cli/` | Signal account data |

Note: Mesh is stateless - policy and rotated HMAC keys are pushed from Joi and stored in memory only.
