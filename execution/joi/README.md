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

| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_BIND_HOST` | 0.0.0.0 | API bind address |
| `JOI_BIND_PORT` | 8443 | API port |
| `JOI_OLLAMA_URL` | http://localhost:11434 | Ollama API URL |
| `JOI_OLLAMA_MODEL` | llama3 | Model to use |
| `JOI_MESH_URL` | http://mesh:8444 | Mesh proxy URL for outbound |
| `JOI_LOG_LEVEL` | INFO | Logging level |

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
