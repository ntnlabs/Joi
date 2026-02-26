# Joi Stage 4 Walkthrough (Operational Tuning)

Use this after:

- `sysprep/joi/stage3.md` (Joi App + Ollama)
- `sysprep/mesh/stage3.md` (Mesh Worker)
- Verified end-to-end connectivity (Joi ↔ Mesh ↔ Signal)

This stage configures who can talk to Joi and how Joi behaves.

## What Stage 4 Does

- Create and configure the policy file (allowed senders, groups)
- Set up group configurations with participants and trigger names
- Configure system prompts for Joi's personality (optional)
- Verify end-to-end message flow

## Preconditions

- Joi API running and healthy (`curl http://localhost:8443/health`)
- Mesh worker running and forwarding messages
- HMAC secrets match on both sides
- At least one phone number to allow as sender

---

## 1. Create Policy Directory

On **Joi VM**:

```bash
mkdir -p /var/lib/joi/policy
chown joi:joi /var/lib/joi/policy
```

## 2. Create Policy File

Create `/var/lib/joi/policy/mesh-policy.json`:

```bash
cat > /var/lib/joi/policy/mesh-policy.json << 'EOF'
{
  "version": 1,
  "mode": "business",
  "dm_group_knowledge": false,
  "identity": {
    "bot_name": "Joi",
    "allowed_senders": [],
    "groups": {}
  },
  "rate_limits": {
    "inbound": {
      "max_per_hour": 120,
      "max_per_minute": 20
    }
  },
  "validation": {
    "max_text_length": 1500,
    "max_timestamp_skew_ms": 300000
  },
  "security": {
    "privacy_mode": true,
    "kill_switch": false
  }
}
EOF

chown joi:joi /var/lib/joi/policy/mesh-policy.json
chmod 640 /var/lib/joi/policy/mesh-policy.json
```

## 3. Add Allowed Senders

Edit the policy file and add phone numbers to `allowed_senders`:

```bash
nano /var/lib/joi/policy/mesh-policy.json
```

```json
"allowed_senders": [
  "+421XXXXXXXXX",
  "+421YYYYYYYYY"
]
```

These users can DM Joi directly.

## 4. Add Groups (Optional)

### 4.1 Get Group ID

On **Mesh VM**, list Signal groups:

```bash
sudo -u signal signal-cli --config /var/lib/signal-cli listGroups
```

The group ID is a base64 string like `MJiIQPtAPqfodbXmG8+mKgnXl3dwRfPBs15rdChlV8k=`

### 4.2 Add Group to Policy

Edit policy and add the group:

```json
"groups": {
  "MJiIQPtAPqfodbXmG8+mKgnXl3dwRfPBs15rdChlV8k=": {
    "participants": ["+421XXXXXXXXX", "+421YYYYYYYYY"],
    "names": ["Zuza", "Assistant"]
  }
}
```

**Fields:**
- `participants`: Phone numbers that can trigger Joi responses (others get `store_only` - messages stored but no response)
- `names`: Additional names Joi responds to in this group (merged with global `bot_name`)

### 4.3 How Joi Responds in Groups

Joi responds when:
- Someone @mentions Joi via Signal autocomplete
- Someone types the `bot_name` ("Joi")
- Someone types any name from the group's `names` array

## 5. Apply Configuration

Restart Joi to push config to mesh:

```bash
systemctl restart joi-api
```

Verify config was pushed:

```bash
journalctl -u joi-api --since "1 min ago" | grep -i "config\|push"
```

## 6. Test DM

Send a direct message from an allowed sender to Joi's Signal number. Check logs:

```bash
journalctl -u joi-api -f
```

You should see the message processed and a response generated.

## 7. Test Group

In a configured group, @mention Joi or say one of the trigger names. Joi should respond.

---

## Policy Reference

### Mode

| Mode | Description |
|------|-------------|
| `companion` | Personal assistant - group knowledge never accessible in DMs |
| `business` | Enterprise mode - group knowledge can be shared to DMs (when implemented) |

### Security Flags

| Flag | Effect |
|------|--------|
| `privacy_mode: true` | Redacts phone numbers and content from logs |
| `kill_switch: true` | Emergency stop - drops all messages |

### Rate Limits

```json
"rate_limits": {
  "inbound": {
    "max_per_hour": 120,
    "max_per_minute": 20
  }
}
```

Per-sender limits. Exceeding sends a rate limit notice to the user.

---

## 8. System Prompts (Optional)

System prompts define Joi's personality per user or group.

### 8.1 Create Prompts Directory

```bash
mkdir -p /var/lib/joi/prompts
chown joi:joi /var/lib/joi/prompts
```

### 8.2 Default Prompt

Create `/var/lib/joi/prompts/default.txt`:

```
You are Joi, an AI assistant.
You are helpful, professional, and concise.
```

### 8.3 Per-User Prompt

Create `/var/lib/joi/prompts/+421XXXXXXXXX.txt` (filename is the phone number):

```
You are Joi, a personal assistant for this user.
Be friendly and remember their preferences.
```

### 8.4 Per-Group Prompt

Create `/var/lib/joi/prompts/groups/<GROUP_ID>.txt`

**Important:** The filename must be sanitized:
- Replace `+` with `-`
- Replace `/` with `_`

Example: Group ID `MJiIQPtAPqfodbXmG8+mKgnXl3dwRfPBs15rdChlV8k=`
Filename: `MJiIQPtAPqfodbXmG8-mKgnXl3dwRfPBs15rdChlV8k=.txt`

Helper to generate correct filename:
```bash
GROUP_ID="MJiIQPtAPqfodbXmG8+mKgnXl3dwRfPBs15rdChlV8k="
SAFE_ID=$(echo "$GROUP_ID" | tr '+/' '-_')
echo "Create: /var/lib/joi/prompts/groups/${SAFE_ID}.txt"
```

Example prompt:
```
You are Zuza, the team assistant for this project group.
When someone addresses "Zuza" or @mentions you, they are talking to you.
Keep responses brief and action-oriented.
```

No restart needed - prompts are loaded dynamically.

---

## 9. Consolidation Model (Optional)

By default, Joi uses the main model for memory consolidation (fact extraction, summarization).

To use a separate lighter model:

```bash
# Pull a smaller model
docker exec ollama ollama pull llama3:8b

# Add to /etc/default/joi-api
echo 'JOI_CONSOLIDATION_MODEL=llama3:8b' >> /etc/default/joi-api

# Restart
systemctl restart joi-api
```

For consistency, using the same model for everything is recommended.

---

## 10. Post-Checks

- [ ] DM from allowed sender gets response
- [ ] DM from non-allowed sender is ignored (or store_only)
- [ ] Group message with @mention gets response
- [ ] Group message with trigger name gets response
- [ ] Privacy mode redacts logs (no message content visible)
- [ ] `curl http://localhost:8443/health` shows healthy status

## Troubleshooting

### Message not forwarded to Joi

Check mesh logs:
```bash
journalctl -u mesh-signal-worker -f
```

Common issues:
- Sender not in `allowed_senders` (for DMs)
- Sender not in group `participants`
- HMAC mismatch between mesh and Joi

### Joi not responding in group

- Verify the group ID matches exactly (base64, case-sensitive)
- Check if using correct trigger name or @mention
- Check Joi logs for "not addressing joi" messages

### Config not pushing

```bash
journalctl -u joi-api | grep -i "push\|config\|mesh"
```

Verify mesh is reachable:
```bash
curl http://10.42.0.1:8444/health
```

---

## Notes

- Policy file is pushed from Joi to Mesh on startup and periodically
- Mesh is stateless - config lives in memory only
- If mesh restarts, Joi auto-pushes config within ~60 seconds
- Phone numbers must include country code (e.g., `+421...`)
- Group IDs are case-sensitive base64 strings
