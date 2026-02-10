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
- `SIGNAL_CLI_SOCKET` (default: /var/run/signal-cli/socket)
- `MESH_LOG_DIR` (default: /var/log/mesh-proxy)
- `MESH_ENABLE_TEST` (default: 0)
- `SIGNAL_ACCOUNT` (required for `/send_test` and worker)

## Forwarding to Joi (optional)

Set to enable forwarding from the signal worker:

- `MESH_ENABLE_FORWARD=1`
- `MESH_JOI_INBOUND_URL` (default: http://joi:8443/api/v1/message/inbound)
- `MESH_FORWARD_TIMEOUT` (default: 5 seconds)
