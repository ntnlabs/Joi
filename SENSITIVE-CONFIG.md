# Sensitive Configuration

> This file documents configuration that contains secrets or PII.
> These files are NOT in git and must be set up manually on each VM.

## Mesh VM (172.22.22.1)

> **Note: Mesh is stateless.** Policy is pushed from Joi and stored in memory only. No policy files on mesh.

### /etc/default/mesh-signal-worker

```bash
# Signal account phone number (E.164 format)
SIGNAL_ACCOUNT=+<COUNTRY><NUMBER>

# HMAC shared secret (64 hex chars, must match Joi)
MESH_HMAC_SECRET=<64-char-hex>

# Forwarding to Joi
MESH_ENABLE_FORWARD=1
MESH_JOI_URL=http://10.42.0.10:8443
```

See `ENV-REFERENCE.md` for full variable listing and defaults.

**Notes**:
- `MESH_HMAC_SECRET` is the initial seed. Rotated keys are pushed from Joi and stored in memory.
- On mesh restart, it uses env var until Joi pushes current config.

### /var/lib/signal-cli/

Signal account data directory. Contains:
- Account keys and identity
- Contact database
- Group information

**Backup this directory** - losing it means re-registering the Signal account.

---

## Joi VM (172.22.22.2)

### /etc/default/joi-api

Start from `execution/joi/systemd/joi-api.default` and set the secrets:

```bash
# HMAC shared secret (64 hex chars, must match MESH_HMAC_SECRET on Mesh VM)
# Generate with: openssl rand -hex 32
JOI_HMAC_SECRET=<64-char-hex>

# Future: SQLCipher encryption key
# JOI_MEMORY_KEY=<generate-with-openssl-rand-base64-32>
```

### /var/lib/joi/policy/mesh-policy.json

Policy pushed to mesh on startup and changes. Contains sender whitelist, groups, rate limits.

```json
{
  "identity": {
    "bot_name": "Jessica Joi",
    "allowed_senders": ["+<OWNER_PHONE_NUMBER>"],
    "groups": {
      "<GROUP_ID_BASE64>": {
        "participants": ["+<OWNER_PHONE_NUMBER>"],
        "names": []
      }
    }
  },
  "rate_limits": {
    "inbound": { "max_per_hour": 120, "max_per_minute": 20 }
  },
  "validation": {
    "max_text_length": 1500
  }
}
```

**Notes**:
- `bot_name`: Signal profile name for @mention detection
- Group IDs: base64-encoded, get with `signal-cli -a +<ACCOUNT> listGroups`
- `participants`: Who can trigger responses (others are context-only)
- `names`: Per-group @mention name override

### /var/lib/joi/prompts/

Per-user and per-group system prompts. Directory structure:

```
/var/lib/joi/prompts/
├── default.txt              # Default prompt (optional)
├── users/
│   └── +<PHONE>.txt         # Per-user prompt for DMs
└── groups/
    └── <GROUP_ID>.txt       # Per-group prompt
```

**Note**: Group IDs with `/` or `+` characters are converted to `_` and `-` in filenames.

### /var/lib/joi/memory.db

SQLite database containing all conversation history, facts, and system state.

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

### HMAC Secret
Generate: `openssl rand -hex 32`
Set on both VMs: `JOI_HMAC_SECRET` (Joi) and `MESH_HMAC_SECRET` (Mesh).
Use `joi-admin hmac rotate` to rotate without restart.

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

sudo chmod 700 /var/lib/signal-cli
sudo chown -R signal:signal /var/lib/signal-cli

# Joi VM
sudo chmod 640 /etc/default/joi-api
sudo chown root:joi /etc/default/joi-api

sudo chmod 750 /var/lib/joi
sudo chown -R joi:joi /var/lib/joi

sudo chmod 640 /var/lib/joi/policy/mesh-policy.json
sudo chown joi:joi /var/lib/joi/policy/mesh-policy.json
```
