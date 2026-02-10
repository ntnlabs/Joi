# Mesh Proxy (Skeleton)

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

## signal-cli JSON-RPC mode (manual receive)

For the worker to poll `receive`, run signal-cli in JSON-RPC mode with manual receive:

The worker uses stdio JSON-RPC and spawns `signal-cli` directly:

```bash
signal-cli --config /var/lib/signal-cli jsonRpc --receive-mode=manual
```

Note: socket mode is not supported by this build.

## Health

```bash
curl http://127.0.0.1:8444/health
```

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

- `MESH_BIND_HOST` (default: 0.0.0.0)
- `MESH_BIND_PORT` (default: 8444)
- `MESH_SIGNAL_MODE` (default: stdio; `socket` enables `/send_test`)
- `SIGNAL_CLI_SOCKET` (default: /var/run/signal-cli/socket; only used in socket mode)
- `MESH_LOG_DIR` (default: /var/log/mesh-proxy)
- `MESH_ENABLE_TEST` (default: 0)
- `SIGNAL_ACCOUNT` (required for `/send_test` and worker)
- `SIGNAL_CLI_BIN` (default: /usr/local/bin/signal-cli)
- `SIGNAL_CLI_CONFIG_DIR` (default: /var/lib/signal-cli)
- `MESH_POLICY_FILE` (default: /etc/mesh-proxy/policy.json)

## Forwarding to Joi (optional)

Set to enable forwarding from the signal worker:

- `MESH_ENABLE_FORWARD=1`
- `MESH_JOI_INBOUND_URL` (default: http://joi:8443/api/v1/message/inbound)
- `MESH_FORWARD_TIMEOUT` (default: 5 seconds)

## Run Worker as systemd (`signal` user)

This is the recommended mode for production on mesh VM.

1. Install unit and env file:

```bash
sudo cp systemd/mesh-signal-worker.service /etc/systemd/system/
sudo cp systemd/mesh-signal-worker.env.example /etc/default/mesh-signal-worker
sudo mkdir -p /etc/mesh-proxy
sudo cp config/policy.example.json /etc/mesh-proxy/policy.json
```

2. Edit account and options:

```bash
sudo nano /etc/default/mesh-signal-worker
```

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
