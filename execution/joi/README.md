# Joi API

The core Joi AI assistant API running on the Joi VM.

## Quick Start

```bash
# Create virtualenv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run
./run.sh
```

## Environment Variables

See [ENV-REFERENCE.md](../../ENV-REFERENCE.md) for complete documentation.

### Core
| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_BIND_HOST` | 0.0.0.0 | API bind address |
| `JOI_BIND_PORT` | 8443 | API port |
| `JOI_LOG_LEVEL` | INFO | Logging level |

### LLM
| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_OLLAMA_URL` | http://localhost:11434 | Ollama API URL |
| `JOI_OLLAMA_MODEL` | llama3 | Model to use |
| `JOI_LLM_TIMEOUT` | 180 | LLM request timeout (seconds) |
| `JOI_NAMES` | Joi | Names to respond to in groups (comma-separated) |

### Mesh
| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_MESH_URL` | http://mesh:8444 | Mesh proxy URL for outbound |

### Memory
| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_MEMORY_DB` | /var/lib/joi/memory.db | SQLite database path |
| `JOI_CONTEXT_MESSAGES` | 10 | Recent messages in LLM context |

### Consolidation
| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_CONSOLIDATION_SILENCE_HOURS` | 1 | Hours of silence before consolidation |
| `JOI_CONSOLIDATION_MAX_MESSAGES` | 200 | Force consolidation at message count |
| `JOI_CONSOLIDATION_ARCHIVE` | 0 | Set to 1 to archive instead of delete |

### RAG
| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_RAG_ENABLED` | 1 | Enable knowledge retrieval |
| `JOI_RAG_MAX_TOKENS` | 500 | Max tokens for RAG context |

### Prompts
| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_PROMPTS_DIR` | /var/lib/joi/prompts | Directory for per-user/group prompts |

## Endpoints

### Health Check

```bash
curl http://localhost:8443/health
```

### Inbound Message (from mesh)

```bash
curl -X POST http://localhost:8443/api/v1/message/inbound \
  -H "Content-Type: application/json" \
  -d '{
    "transport": "signal",
    "message_id": "test-123",
    "sender": {"id": "owner", "transport_id": "+<REDACTED>"},
    "conversation": {"type": "direct", "id": "+<REDACTED>"},
    "content": {"type": "text", "text": "Hello!"},
    "timestamp": 1234567890000
  }'
```

## Architecture

```
mesh (Signal) ──► POST /api/v1/message/inbound ──► Joi API
                                                      │
                                                      ▼
                                                   Ollama
                                                      │
                                                      ▼
mesh (Signal) ◄── POST /api/v1/message/outbound ◄── Joi API
```
