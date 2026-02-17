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
| `JOI_REQUIRE_ENCRYPTED_DB` | 1 | Require encrypted DB (fail if not available) |

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

### Time Awareness
| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_TIME_AWARENESS` | 0 | Inject current datetime into system prompt |
| `JOI_TIMEZONE` | Europe/Bratislava | User timezone (IANA format) |

### Scheduler
| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_SCHEDULER_ENABLED` | 1 | Enable background scheduler |
| `JOI_SCHEDULER_INTERVAL` | 60 | Tick interval in seconds |
| `JOI_SCHEDULER_STARTUP_DELAY` | 10 | Startup delay in seconds |

### Prompts
| Variable | Default | Description |
|----------|---------|-------------|
| `JOI_PROMPTS_DIR` | /var/lib/joi/prompts | Directory for per-user/group prompts |

## Custom Models (Modelfile)

Ollama Modelfiles let you create custom model variants with baked-in personality and parameters.

### Creating a Custom Model

```bash
# 1. Copy and customize the template
cp ollama/Modelfile.example ollama/Modelfile
vim ollama/Modelfile

# 2. Build the model
docker exec joi-brain ollama create joi -f /opt/joi/execution/joi/ollama/Modelfile

# 3. Test it
docker exec -it joi-brain ollama run joi "Hey"

# 4. Use it (global default)
# In /etc/default/joi-api:
JOI_OLLAMA_MODEL=joi
```

### What Gets Baked In

| Setting | Effect |
|---------|--------|
| `FROM` | Base model to customize |
| `PARAMETER temperature` | Creativity (0.0-1.0) |
| `PARAMETER top_p` | Vocabulary diversity |
| `PARAMETER num_ctx` | Context window size |
| `SYSTEM` | Base personality prompt |

Changes require re-running `ollama create` to take effect.

### Multiple Model Variants

Create different models for different use cases:

```bash
ollama create joi -f Modelfile.joi           # Default personality
ollama create joi-creative -f Modelfile.creative  # More creative
ollama create joi-formal -f Modelfile.formal      # Business-like
```

## Per-User/Group Configuration

Different users or groups can use different models and context sizes via config files.

### Directory Structure

```
/var/lib/joi/prompts/
├── default.txt              # Default prompt (fallback)
├── default.model            # Default model (optional)
├── default.context          # Default context message count (optional)
├── users/
│   ├── +1234567890.txt      # User's extra prompt (optional)
│   ├── +1234567890.model    # User's model: joi-creative
│   └── +1234567890.context  # User's context size: 20
└── groups/
    ├── ABC123.txt           # Group's extra prompt (optional)
    ├── ABC123.model         # Group's model: joi-formal
    └── ABC123.context       # Group's context size: 60
```

### File Types

| File | Contains | Fallback |
|------|----------|----------|
| `.txt` | System prompt text | default.txt → hardcoded |
| `.model` | Model name (e.g., `joi-creative`) | default.model → `JOI_OLLAMA_MODEL` |
| `.context` | Number of messages (e.g., `20`) | default.context → `JOI_CONTEXT_MESSAGES` |

### Model/Prompt Combinations

| Has .model? | Has .txt? | Result |
|-------------|-----------|--------|
| No | No | Default model + default prompt |
| No | Yes | Default model + user's prompt |
| Yes | No | User's model + NO prompt (Modelfile handles it) |
| Yes | Yes | User's model + user's prompt (additions) |

### Example Setup

```bash
# User gets creative model with smaller context (for smaller model)
echo "joi-creative" > /var/lib/joi/prompts/users/+1234567890.model
echo "20" > /var/lib/joi/prompts/users/+1234567890.context

# Group gets formal model with larger context (for bigger model)
echo "joi-formal" > /var/lib/joi/prompts/groups/ABC123.model
echo "60" > /var/lib/joi/prompts/groups/ABC123.context
echo "This is a work group. Keep responses professional." > /var/lib/joi/prompts/groups/ABC123.txt
```

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

## Admin Tools

### joi-admin

Memory and key management utility. **Safe by default** - no flags = no action.

```bash
sudo joi-admin purge [FLAGS]
```

#### Data Flags

| Flag | Deletes |
|------|---------|
| `--contexts` | Messages and context summaries |
| `--facts` | Learned user facts |
| `--knowledge` | RAG knowledge chunks |
| `--all-data` | All of the above |

#### Scope Flags

| Flag | Effect |
|------|--------|
| `--conversation ID` | Limit to specific conversation (phone/group ID) |
| *(none)* | Affects ALL conversations |

#### Key Flags

| Flag | Effect |
|------|--------|
| `--hmac-key` | Regenerate HMAC signing key |
| `--nebula-keys` | Remove Nebula host keys (requires re-enrollment) |
| `--all-keys` | Both HMAC and Nebula keys |

#### Nuclear Options

| Flag | Effect |
|------|--------|
| `--everything` | `--all-data` + `--all-keys` |
| `--nebula-ca` | Also remove Nebula CA cert (true factory reset) |

#### Common Use Cases

```bash
# Clear conversation history (//reload equivalent)
sudo joi-admin purge --contexts --conversation +1234567890

# Clear history + facts (//restart equivalent)
sudo joi-admin purge --contexts --facts --conversation +1234567890

# Full conversation reset (//reset equivalent)
sudo joi-admin purge --all-data --conversation +1234567890

# Customer handoff (wipe everything)
sudo joi-admin purge --all-data --all-keys

# True factory reset
sudo joi-admin purge --everything --nebula-ca
```
