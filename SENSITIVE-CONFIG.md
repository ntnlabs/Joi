# Sensitive Configuration

> This file documents configuration that contains secrets or PII.
> These files are NOT in git and must be set up manually on each VM.

## Mesh VM (172.22.22.1)

### /etc/default/mesh-signal-worker

```bash
# Signal account phone number (E.164 format)
SIGNAL_ACCOUNT=+<COUNTRY><NUMBER>

# Path to signal-cli binary and config
SIGNAL_CLI_BIN=/usr/local/bin/signal-cli
SIGNAL_CLI_CONFIG_DIR=/var/lib/signal-cli

# Policy file location
MESH_POLICY_FILE=/etc/mesh-proxy/policy.json

# HTTP port for outbound API
MESH_WORKER_HTTP_PORT=8444
```

### /etc/mesh-proxy/policy.json

```json
{
  "allowed_senders": [
    "+<OWNER_PHONE_NUMBER>"
  ],
  "rate_limits": {
    "max_per_minute": 10,
    "max_per_hour": 120
  }
}
```

### /var/lib/signal-cli/

Signal account data directory. Contains:
- Account keys and identity
- Contact database
- Group information

**Backup this directory** - losing it means re-registering the Signal account.

---

## Joi VM (172.22.22.2)

### /etc/default/joi-api

```bash
# API settings
JOI_BIND_HOST=0.0.0.0
JOI_BIND_PORT=8443

# Ollama LLM
JOI_OLLAMA_URL=http://localhost:11434
JOI_OLLAMA_MODEL=llama3

# Mesh proxy (Nebula IP)
JOI_MESH_URL=http://172.22.22.1:8444

# Memory database
JOI_MEMORY_DB=/var/lib/joi/memory.db
JOI_CONTEXT_MESSAGES=10

# Logging
JOI_LOG_LEVEL=INFO

# Future: SQLCipher encryption key
# JOI_MEMORY_KEY=<generated-key>
```

### /var/lib/joi/memory.db

SQLite database containing:
- All conversation history
- User messages and Joi responses
- System state

**Contains sensitive conversation content** - encrypt at rest when SQLCipher is enabled.

---

## Secrets Checklist

Before deploying, verify:

- [ ] No phone numbers in git: `git grep -E "\+[0-9]{10,}"`
- [ ] No API keys in git: `git grep -iE "(api_key|apikey|secret)"`
- [ ] /etc/default files have correct permissions (0600 or 0640)
- [ ] /var/lib/signal-cli owned by signal user
- [ ] /var/lib/joi owned by joi user
- [ ] Backups exclude or encrypt sensitive paths

---

## Regenerating Secrets

### Signal Account
If compromised, re-register on a new number. Old conversations are lost.

### SQLCipher Key (future)
Generate: `openssl rand -base64 32`
Store in: `/etc/default/joi-api` as `JOI_MEMORY_KEY`
Note: Changing key requires re-creating the database.

---

## File Permissions Reference

```bash
# Mesh VM
sudo chmod 640 /etc/default/mesh-signal-worker
sudo chown root:signal /etc/default/mesh-signal-worker

sudo chmod 640 /etc/mesh-proxy/policy.json
sudo chown root:signal /etc/mesh-proxy/policy.json

sudo chmod 700 /var/lib/signal-cli
sudo chown -R signal:signal /var/lib/signal-cli

# Joi VM
sudo chmod 640 /etc/default/joi-api
sudo chown root:joi /etc/default/joi-api

sudo chmod 750 /var/lib/joi
sudo chown -R joi:joi /var/lib/joi
```
