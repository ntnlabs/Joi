# Mesh Stage 2 Walkthrough (Nebula + signal-cli Runtime)

Use this after `sysprep/mesh/setup.sh` (stage 1) is complete.

This is a manual walkthrough on purpose. It avoids brittle download URLs and keeps certificate/linking steps explicit.

## What Stage 2 Does

- Install Nebula from an upstream binary (not Ubuntu `nebula` package `1.6.1`)
- Install a simple `nebula.service` unit (`sysprep/nebula/nebula.service`)
- Install Mesh Nebula config template (`sysprep/mesh/config.yml`)
- Install `signal-cli` (upstream release package or standalone binary)
- Link a Signal device
- Validate `signal-cli` daemon socket runtime

## Why Not Ubuntu Nebula Package

Ubuntu 24.04 currently ships Nebula `1.6.1`, which is too old for this deployment (startup issue seen in lab, fixed in newer Nebula).

Use upstream Nebula binary (`>= 1.7` recommended).

## Preconditions

- Stage 1 completed (`sysprep/mesh/setup.sh`)
- Mesh update window script available and working (`sysprep/mesh/update.sh`)
- `mesh` can reach internet via its own WAN when `./update.sh --enable` is active
- You have these artifacts available locally on the Mesh host:
  - Nebula upstream binary (for Linux amd64), e.g. `/root/nebula`
  - Nebula certs:
    - `ca.crt`
    - `host.crt` (mesh cert)
    - `host.key` (mesh key)
  - `signal-cli` package or standalone binary (for mesh stage 2 runtime setup)

## Files in This Repo Used by Stage 2

- Nebula service unit: `sysprep/nebula/nebula.service`
- Mesh Nebula config template: `sysprep/mesh/config.yml`

## 1. Install Nebula (Upstream Binary)

If Ubuntu `nebula` package is installed, purge it first (recommended):

```bash
apt purge -y nebula
```

Install upstream binary (example path `/root/nebula`):

```bash
install -m 0755 /root/nebula /usr/local/bin/nebula
/usr/local/bin/nebula --version
```

## 2. Install Nebula Service + Config

Copy the custom service file (simple `nebula.service`, not templated `nebula@config`):

```bash
cp /opt/Joi/sysprep/nebula/nebula.service /etc/systemd/system/nebula.service
systemctl daemon-reload
```

Prepare config directory:

```bash
mkdir -p /etc/nebula
```

Install the mesh config template (only if you do not already have a tuned config):

```bash
cp /opt/Joi/sysprep/mesh/config.yml /etc/nebula/config.yml
```

## 3. Install Nebula Certificates

Copy your generated certs to generic names:

```bash
install -m 0644 /path/to/ca.crt /etc/nebula/ca.crt
install -m 0644 /path/to/mesh.crt /etc/nebula/host.crt
install -m 0600 /path/to/mesh.key /etc/nebula/host.key
```

If your files are named differently (for example `gai-mesh.crt` / `gai-mesh.key`), use those source filenames.

## 4. Start Nebula

```bash
systemctl enable --now nebula
systemctl status nebula
journalctl -u nebula -n 100 --no-pager
ip a show tun0
```

Expected:
- `tun0` exists
- Nebula service is `active (running)`

## 5. Verify Nebula Connectivity (Mesh Side)

After Joi Nebula is also configured, verify:

```bash
ping -c2 10.42.0.10
```

## 6. Install signal-cli Runtime

Prepare the service user/runtime directory (idempotent):

```bash
id signal || useradd -r -s /usr/sbin/nologin signal
mkdir -p /var/lib/signal-cli
chown -R signal:signal /var/lib/signal-cli
chmod 0700 /var/lib/signal-cli
```

Install `signal-cli` using one of the two supported approaches:

### Option A: Upstream `.deb` package

```bash
./update.sh --enable
apt install -y /root/signal-cli_*.deb
./update.sh --disable
```

### Option B: Standalone extracted binary (single binary layout)

```bash
mkdir -p /opt/signal-cli/bin
install -m 0755 /path/to/signal-cli /opt/signal-cli/bin/signal-cli
chown root:root /opt/signal-cli/bin/signal-cli
ln -sfn /opt/signal-cli/bin/signal-cli /usr/local/bin/signal-cli
```

Verify install:

```bash
signal-cli --version
sudo -u signal /usr/local/bin/signal-cli --version
```

## 7. Link Signal Device

Run as `signal` user so account state lands with correct ownership:

```bash
sudo -u signal /usr/local/bin/signal-cli --config /var/lib/signal-cli link -n ai-proxy-cli
```

If using terminal QR workflow, keep the `link` process alive and render the exact URI in another terminal:

```bash
printf '%s\n' "sgnl://linkdevice?uuid=...&pub_key=..." | qrencode -t ansiutf8
```

## 8. Validate `signal-cli` Daemon Socket (Stage 2 Endpoint)

Create runtime socket directory (tmpfs-backed, may not exist after reboot):

```bash
mkdir -p /var/run/signal-cli
chown signal:signal /var/run/signal-cli
chmod 0755 /var/run/signal-cli
```

Start daemon manually as `signal` user:

```bash
sudo -u signal /usr/local/bin/signal-cli --config /var/lib/signal-cli daemon --socket /var/run/signal-cli/socket
```

In another shell verify:

```bash
ls -l /var/run/signal-cli/socket
```

Note:

- In `signal-cli 0.13.24`, the daemon socket is for `jsonRpc`.
- Top-level commands like `send` may not support `--socket` in this build.
- For stage 2, a working daemon socket is sufficient. JSON-RPC behavior is exercised in mesh stage 3 via the worker.

This is the Stage 2 completion point on Mesh:
- Nebula up
- signal-cli linked
- signal-cli daemon socket working

## 9. Post-Checks

- `systemctl status nebula`
- `ip a show tun0`
- `sudo -u signal /usr/local/bin/signal-cli --version`
- `ls -ld /var/lib/signal-cli`
- `ls -l /var/run/signal-cli/socket` (during daemon test)

## Notes

- Mesh uses WAN for updates and Signal traffic.
- Internal router/hopper is for SSH management source and internal segmentation.
- Internal NTP server is the only NTP source for Mesh.
- Mesh DNS policy in stage 1 is UDP-only (`53/udp`).
- Run all `signal-cli` operations as user `signal`.
- Mesh worker deployment and Joi integration begin in stage 3 (`sysprep/mesh/stage3.md`).
