# Mesh Proxy

## Architecture

**Mesh is stateless.** It stores nothing on disk - all config comes from Joi via config push.

On startup:
1. Mesh starts with empty policy (denies all messages)
2. Joi pushes config via `/config/sync` endpoint
3. Mesh applies config in memory and starts processing

On restart:
1. Config is lost (by design - no traces)
2. Joi pushes config again on next scheduler tick or startup

## First Run Setup

1. **Configure environment** in `/etc/default/mesh-signal-worker`:
   - `SIGNAL_ACCOUNT` - Your Signal phone number
   - `MESH_HMAC_SECRET` - Shared secret (must match Joi)
   - `MESH_JOI_INBOUND_URL` - Joi's inbound endpoint

2. **Configure policy on Joi** at `/var/lib/joi/policy/mesh-policy.json`:

```json
{
  "identity": {
    "bot_name": "Your Bot Name",
    "allowed_senders": ["+<YOUR_PHONE>"],
    "groups": {}
  }
}
```

3. **Start mesh first**, then Joi - Joi will push config on startup

## Run API

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./run.sh
```

## Run Signal Worker

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./run-worker.sh
```

## signal-cli JSON-RPC mode (on-connection notifications)

The worker uses notification-driven receive mode and does not poll `receive`.

The worker uses stdio JSON-RPC and spawns `signal-cli` directly:

```bash
signal-cli --config /var/lib/signal-cli jsonRpc --receive-mode=on-connection
```

Note: socket mode is not supported by this build.

## Health

```bash
curl http://127.0.0.1:8444/health
```

## Delivery Tracking

Outbound messages are tracked for delivery confirmation. When Signal sends delivery/read receipts, the tracker updates status.

**Query status for a specific message (by timestamp):**

Note: This endpoint requires HMAC authentication (X-Nonce, X-Timestamp, X-HMAC-SHA256 headers).

```bash
# From Joi (with HMAC headers)
curl -H "X-Nonce: ..." -H "X-Timestamp: ..." -H "X-HMAC-SHA256: ..." \
  "http://mesh:8444/api/v1/delivery/status?timestamp=1234567890123"
```

Response:
```json
{
  "status": "ok",
  "data": {
    "timestamp": 1234567890123,
    "delivered": true,
    "read": false,
    "delivered_at": 1234567891000,
    "read_at": null,
    "sent_at": 1234567890500
  }
}
```

Messages are tracked for 24 hours (configurable via `DeliveryTracker` TTL).

## Test Send (disabled by default)

Enable explicitly:

```bash
export MESH_ENABLE_TEST=1
export SIGNAL_ACCOUNT=+<REDACTED>
```

Then call:

```bash
curl -X POST "http://127.0.0.1:8444/send_test?recipient=+<REDACTED>&message=hello"
```

## Environment Variables

- `SIGNAL_ACCOUNT` (required - Signal phone number)
- `MESH_HMAC_SECRET` (required - shared secret for auth)
- `MESH_WORKER_HTTP_PORT` (default: 8444)
- `MESH_SIGNAL_POLL_SECONDS` (default: 5)
- `SIGNAL_CLI_BIN` (default: /usr/local/bin/signal-cli)
- `SIGNAL_CLI_CONFIG_DIR` (default: /var/lib/signal-cli)
- `MESH_LOG_LEVEL` (default: INFO)

## HMAC Authentication

All requests between Joi and mesh are authenticated with HMAC-SHA256:
- Header format: `X-Nonce`, `X-Timestamp`, `X-HMAC-SHA256`
- Timestamp tolerance: 5 minutes
- Nonce replay protection: 15 minutes

**Key rotation** is handled automatically by Joi (weekly by default). During rotation:
1. Joi pushes new key via config sync
2. Mesh accepts both old and new key during 60-second grace period
3. Old key expires after grace period

Since mesh is stateless, the rotated key is stored in memory only. On restart, mesh uses `MESH_HMAC_SECRET` from environment until Joi pushes the current key.

## Forwarding to Joi (optional)

Set to enable forwarding from the signal worker:

- `MESH_ENABLE_FORWARD=1`
- `MESH_JOI_INBOUND_URL` (default: http://joi:8443/api/v1/message/inbound)
- `MESH_FORWARD_TIMEOUT` (default: 120 seconds)

## Run Worker as systemd (`signal` user)

This is the recommended mode for production on mesh VM.

1. Install unit and env file:

```bash
sudo cp systemd/mesh-signal-worker.service /etc/systemd/system/
sudo cp systemd/mesh-signal-worker.env.example /etc/default/mesh-signal-worker
```

2. Edit account and HMAC secret:

```bash
sudo nano /etc/default/mesh-signal-worker
```

Required settings:
- `SIGNAL_ACCOUNT` - Your Signal phone number
- `MESH_HMAC_SECRET` - Shared secret (must match Joi)
- `MESH_ENABLE_FORWARD=1` - Enable forwarding to Joi
- `MESH_JOI_INBOUND_URL` - Joi's inbound endpoint

3. Stop old `signal-cli` daemon service if it is still enabled:

```bash
sudo systemctl disable --now signal-cli
```

4. Enable/start worker:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mesh-signal-worker
sudo systemctl status mesh-signal-worker
```

5. **Configure policy on Joi** (not on mesh - mesh is stateless):

Policy is managed on Joi at `/var/lib/joi/policy/mesh-policy.json` and pushed to mesh automatically. See the First Run Setup section above.
