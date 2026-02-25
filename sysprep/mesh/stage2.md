# Mesh Stage 2 Walkthrough (Nebula + signal-cli)

Use this after `sysprep/mesh/setup.sh` (stage 1) is complete.

This is a manual walkthrough on purpose. It avoids brittle download URLs and keeps certificate/linking steps explicit.

## What Stage 2 Does

- Install Nebula from an upstream binary (not Ubuntu `nebula` package `1.6.1`)
- Install a simple `nebula.service` unit (`sysprep/nebula/nebula.service`)
- Install Mesh Nebula config template (`sysprep/mesh/config.yml`)
- Install `signal-cli` (recommended: upstream release package/binary)
- Prepare `signal` runtime user/data directory (if not already present)

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
  - `signal-cli` package or extracted binary (see signal-cli section)

## Files in This Repo Used by Stage 2

- Nebula service unit: `sysprep/nebula/nebula.service`
- Mesh Nebula config template: `sysprep/mesh/config.yml`

## 1. Prepare Runtime User and Directories

Mesh service expects `signal-cli` data under `/var/lib/signal-cli`.

```bash
id signal || useradd -r -s /usr/sbin/nologin signal
mkdir -p /var/lib/signal-cli
chown -R signal:signal /var/lib/signal-cli
chmod 0700 /var/lib/signal-cli
```

## 2. Install Nebula (Upstream Binary)

If Ubuntu `nebula` package is installed, purge it first (recommended):

```bash
apt purge -y nebula
```

Install upstream binary (example path `/root/nebula`):

```bash
install -m 0755 /root/nebula /usr/local/bin/nebula
/usr/local/bin/nebula --version
```

## 3. Install Nebula Service + Config

Copy the custom service file (simple `nebula.service`, not templated `nebula@config`):

```bash
cp /opt/joi/sysprep/nebula/nebula.service /etc/systemd/system/nebula.service
systemctl daemon-reload
```

Prepare config directory:

```bash
mkdir -p /etc/nebula
```

Install the mesh config template (only if you do not already have a tuned config):

```bash
cp /opt/joi/sysprep/mesh/config.yml /etc/nebula/config.yml
```

## 4. Install Nebula Certificates

Copy your generated certs to generic names:

```bash
install -m 0644 /path/to/ca.crt /etc/nebula/ca.crt
install -m 0644 /path/to/mesh.crt /etc/nebula/host.crt
install -m 0600 /path/to/mesh.key /etc/nebula/host.key
```

If your files are named differently (for example `gai-mesh.crt` / `gai-mesh.key`), use those source filenames.

## 5. Start Nebula

```bash
systemctl enable --now nebula
systemctl status nebula
journalctl -u nebula -n 100 --no-pager
ip a show tun0
```

Expected:
- `tun0` exists
- Nebula service is `active (running)`

## 6. Verify Nebula Connectivity (Mesh Side)

After Joi Nebula is also configured, verify:

```bash
ping -c2 10.42.0.10
```

## 7. Install signal-cli (Recommended: Upstream Release)

Two supported approaches are common in this project:

### Option A (Preferred on Ubuntu): Upstream `.deb` package

Open update window first:

```bash
./update.sh --enable
```

Install local `.deb`:

```bash
apt install -y /root/signal-cli_*.deb
```

Close update window:

```bash
./update.sh --disable
```

### Option B: Extracted upstream binary release (manual layout)

If you extracted a release tarball already, install it under `/opt/signal-cli` and link it:

```bash
rm -rf /opt/signal-cli
mkdir -p /opt/signal-cli
cp -a /path/to/extracted/signal-cli-*/* /opt/signal-cli/
chmod 0755 /opt/signal-cli/bin/signal-cli
chown -R root:root /opt/signal-cli
ln -sfn /opt/signal-cli/bin/signal-cli /usr/local/bin/signal-cli
```

Verify:

```bash
signal-cli --version
sudo -u signal /usr/local/bin/signal-cli --version
```

## 8. Link signal-cli Account (Phone in Hand)

Link as a secondary device (recommended workflow):

```bash
sudo -u signal signal-cli --config /var/lib/signal-cli link -n mesh-bot
```

Scan the QR code with the phone.

Alternative (register primary number) is possible, but link mode is the normal path for this deployment.

## 9. Start signal-cli Daemon (Manual Test First)

Before wiring final service config, test manually:

```bash
sudo -u signal signal-cli --config /var/lib/signal-cli daemon --socket /var/run/signal-cli/socket
```

In another shell:

```bash
ls -l /var/run/signal-cli/socket
```

## 10. Post-Checks

- `signal-cli --version`
- `id signal`
- `ls -ld /var/lib/signal-cli`
- `systemctl status nebula`
- `ip a show tun0`

## Notes

- Mesh uses WAN for updates and Signal traffic.
- Internal router/hopper is for SSH management source and internal segmentation.
- Internal NTP server is the only NTP source for Mesh.
- Mesh DNS policy in stage 1 is UDP-only (`53/udp`).
