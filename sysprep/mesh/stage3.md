# Mesh Stage 3 Walkthrough (mesh-signal-worker + Integration)

Use this after:

- `sysprep/mesh/setup.sh` (stage 1)
- `sysprep/mesh/stage2.md` (Nebula)

This stage is manual on purpose. It starts after Mesh stage 2 has a working Nebula tunnel and a working `signal-cli` daemon/socket.

## What Stage 3 Does

- Install Mesh worker Python dependencies on the host
- Install/refresh Mesh worker service unit(s) from the repo
- Configure Mesh environment file(s)
- Start `mesh-signal-worker`
- Verify Mesh listener on port `8444` over Nebula
- Validate Mesh ↔ Joi integration path

## Preconditions

- Mesh stage 2 completed (`sysprep/mesh/stage2.md`)
- Nebula is running and overlay connectivity works
- `signal-cli` is linked and daemon socket works (`/var/run/signal-cli/socket`)
- Joi Nebula node is reachable (for integration tests)

## 1. Install Mesh Worker Python Dependencies (First)

`mesh-signal-worker` runs on the host Python runtime and requires its Python dependencies to be installed before the service can start.

If package installs are needed in this session, open the Mesh update window first:

```bash
./update.sh --enable
```

Install Python tooling and Mesh worker dependencies:

```bash
apt update
apt install -y python3-pip
cd /opt/Joi/execution/mesh/proxy
pip install -r requirements.txt --break-system-packages --ignore-installed
```

Close the update window if you do not need it for the next steps:

```bash
./update.sh --disable
```

## 2. Install / Refresh Mesh Worker Service Unit(s)

```bash
cp /opt/Joi/execution/mesh/proxy/systemd/mesh-signal-worker.service /etc/systemd/system/
systemctl daemon-reload
```

## 3. Configure Mesh Environment File

Review `/etc/default/mesh-signal-worker` and set the host-specific values (Signal socket path, Joi endpoint/HMAC config, policy path, etc.).

### Generate the shared HMAC secret (256-bit)

Generate a 256-bit secret as 64 hex characters (do this once, then use the same value on both Mesh and Joi):

```bash
openssl rand -hex 32
```

Put that value into:

- `/etc/default/mesh-signal-worker` (Mesh)
- `/etc/default/joi-api` (Joi)

Use the same shared secret on both sides.

Protect permissions after editing:

```bash
chmod 640 /etc/default/mesh-signal-worker
chown root:signal /etc/default/mesh-signal-worker
```

## 4. Start mesh-signal-worker

```bash
systemctl enable --now mesh-signal-worker
systemctl status mesh-signal-worker
journalctl -u mesh-signal-worker -n 100 --no-pager
```

## 5. Verify Mesh API Listener (`8444`)

```bash
ss -ltnp | grep 8444
```

## 6. Verify Joi ↔ Mesh Path Over Nebula

From Joi:

```bash
nc -vz 10.42.0.1 8444
```

Expected:
- open/success when `mesh-signal-worker` is running
- earlier `Connection refused` was normal before the worker was started

## 7. Runtime Checks

Signal socket:

```bash
ls -l /var/run/signal-cli/socket
```

Service logs:

```bash
journalctl -u mesh-signal-worker -f
```

## 8. Post-Checks

- `systemctl status mesh-signal-worker`
- `ss -ltnp | grep 8444`
- `ls -l /var/run/signal-cli/socket`
- `systemctl status nebula`

## Notes

- `signal-cli` runtime/linking belongs to stage 2.
- Mesh stage 3 starts at the worker service and integration boundary.
