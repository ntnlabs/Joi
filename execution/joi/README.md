# Joi API

The core Joi AI assistant API running on the Joi VM.

## Quick Start

```bash
# Install dependencies (system-wide)
sudo apt install python3-pip
sudo pip3 install flask requests

# Run
./run.sh
```

## Ollama Setup

Ollama runs in Docker with GPU support:

```bash
# Install nvidia-container-toolkit (if not already)
sudo apt install nvidia-container-toolkit
sudo systemctl restart docker

# Run Ollama with GPU
docker run -d --gpus all \
  -v ollama:/root/.ollama \
  -p 11434:11434 \
  --name ollama \
  --restart unless-stopped \
  ollama/ollama

# Pull the model
docker exec ollama ollama pull llama3

# Verify GPU is being used
docker exec ollama nvidia-smi
docker exec ollama ollama ps  # Should show GPU in PROCESSOR column
```

**Note:** Without `--gpus all`, Ollama runs on CPU which is significantly slower and may cause timeouts with larger context windows.

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

### Mesh
| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_MESH_URL` | http://mesh:8444 | Mesh proxy URL for outbound |

### Memory
| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_MEMORY_DB` | /var/lib/joi/memory.db | SQLite database path |
| `JOI_CONTEXT_MESSAGES` | 40 | Recent messages in LLM context |

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
