# Joi Stage 2 Walkthrough (Nebula)

Use this after `sysprep/joi/setup.sh` (stage 1) is complete.

This is a manual walkthrough on purpose. Joi is a physical device in this deployment, so stage 2 should remain explicit and operator-driven.

## What Stage 2 Does

- Install Nebula from an upstream binary (not Ubuntu `nebula` package `1.6.1`)
- Install a simple `nebula.service` unit (`sysprep/nebula/nebula.service`)
- Install Joi Nebula config template (`sysprep/joi/config.yml`)
- Install Nebula certs (`ca.crt`, `host.crt`, `host.key`)

## Why Not Ubuntu Nebula Package

Ubuntu 24.04 currently ships Nebula `1.6.1`, which is too old for this deployment (known startup issue seen in testing; use newer upstream Nebula).

Use upstream Nebula binary (`>= 1.7` recommended).

## Preconditions

- Stage 1 completed (`sysprep/joi/setup.sh`)
- Joi internal routing/DNS is working for the current session
- You have these artifacts available locally on the Joi host:
  - Nebula upstream binary (for Linux amd64), e.g. `/root/nebula`
  - Nebula certs:
    - `ca.crt`
    - `host.crt` (Joi cert)
    - `host.key` (Joi key)

## Files in This Repo Used by Stage 2

- Nebula service unit: `sysprep/nebula/nebula.service`
- Joi Nebula config template: `sysprep/joi/config.yml`

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
cp /opt/joi/sysprep/nebula/nebula.service /etc/systemd/system/nebula.service
systemctl daemon-reload
```

Prepare config directory:

```bash
mkdir -p /etc/nebula
```

Install the Joi config template (only if you do not already have a tuned config):

```bash
cp /opt/joi/sysprep/joi/config.yml /etc/nebula/config.yml
```

## 3. Install Nebula Certificates

Copy your generated certs to generic names:

```bash
install -m 0644 /path/to/ca.crt /etc/nebula/ca.crt
install -m 0644 /path/to/joi.crt /etc/nebula/host.crt
install -m 0600 /path/to/joi.key /etc/nebula/host.key
```

If your files are named differently (for example `gai-ai.crt` / `gai-ai.key`), use those source filenames.

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

## 5. Verify Nebula Connectivity (Joi Side)

```bash
ping -c2 10.42.0.1
```

## 6. Post-Checks

- `systemctl status nebula`
- `ip a show tun0`
- `ping -c2 10.42.0.1`

## Notes

- Joi Nebula is internal-only in this deployment (no WAN exposure).
- Joi DNS and general egress policy still follow stage 1 (via gateway/hopper).
- Joi default route may be intentionally non-persistent in this environment (fail-closed). Add it manually before Nebula startup if required for your current test plan.
