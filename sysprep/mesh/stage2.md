# Mesh Stage 2 Walkthrough (Nebula)

Use this after `sysprep/mesh/setup.sh` (stage 1) is complete.

This is a manual walkthrough on purpose. It avoids brittle download URLs and keeps certificate/linking steps explicit.

## What Stage 2 Does

- Install Nebula from an upstream binary (not Ubuntu `nebula` package `1.6.1`)
- Install a simple `nebula.service` unit (`sysprep/nebula/nebula.service`)
- Install Mesh Nebula config template (`sysprep/mesh/config.yml`)

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

## 6. Post-Checks

- `systemctl status nebula`
- `ip a show tun0`

## Notes

- Mesh uses WAN for updates and Signal traffic.
- Internal router/hopper is for SSH management source and internal segmentation.
- Internal NTP server is the only NTP source for Mesh.
- Mesh DNS policy in stage 1 is UDP-only (`53/udp`).
- `signal-cli` installation/linking is stage 3 (`sysprep/mesh/stage3.md`).
