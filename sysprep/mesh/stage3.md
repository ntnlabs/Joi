# Mesh Stage 3 Walkthrough (signal-cli)

Use this after:

- `sysprep/mesh/setup.sh` (stage 1)
- `sysprep/mesh/stage2.md` (Nebula)

This stage is manual on purpose because Signal device linking is interactive and operator-driven.

## What Stage 3 Does

- Prepare `signal` service user and runtime directory
- Install `signal-cli` (upstream release package or standalone binary)
- Link a Signal device (recommended) or register/verify a number
- Validate basic `signal-cli` runtime under the `signal` user

## Preconditions

- Mesh networking is working
- Mesh outbound `443/tcp` and `53/udp` are allowed (stage 1 baseline)
- You can open the mesh update window (`./update.sh --enable`) for package installs if needed
- You have the Signal device in hand (for linking QR scan)

## 1. Prepare Runtime User and Directory

```bash
id signal || useradd -r -s /usr/sbin/nologin signal
mkdir -p /var/lib/signal-cli
chown -R signal:signal /var/lib/signal-cli
chmod 0700 /var/lib/signal-cli
```

## 2. Install signal-cli

Two valid approaches are used in this project. Pick one.

### Option A: Upstream `.deb` package (Ubuntu-friendly)

Open update window:

```bash
./update.sh --enable
```

Install local `.deb` (downloaded in advance from upstream GitHub release):

```bash
apt install -y /root/signal-cli_*.deb
```

Close update window:

```bash
./update.sh --disable
```

### Option B: Standalone extracted binary (single binary layout)

If you downloaded the native Linux binary and extracted it as a single `signal-cli` binary:

```bash
mkdir -p /opt/signal-cli/bin
install -m 0755 /path/to/signal-cli /opt/signal-cli/bin/signal-cli
chown root:root /opt/signal-cli/bin/signal-cli
ln -sfn /opt/signal-cli/bin/signal-cli /usr/local/bin/signal-cli
```

## 3. Verify Install (as root and as signal user)

```bash
signal-cli --version
sudo -u signal /usr/local/bin/signal-cli --version
```

## 4. Link Device (Recommended Workflow)

Run `signal-cli link` as the `signal` user so all account state lands with correct ownership:

```bash
sudo -u signal /usr/local/bin/signal-cli --config /var/lib/signal-cli link -n ai-proxy-cli
```

This prints an `sgnl://linkdevice?...` URI.

### QR workflow (terminal)

If you use the manual QR method, keep the `link` command running and render the exact URI in another terminal:

```bash
printf '%s\n' "sgnl://linkdevice?uuid=...&pub_key=..." | qrencode -t ansiutf8
```

Important:

- Keep the `signal-cli link` process alive while scanning
- Paste the full URI exactly
- Quote the URI (contains `&`)
- Do not manually re-encode/decode `%` sequences

## 5. Ownership Repair (Only if Anything Was Run as Root)

If you accidentally ran link/register as root:

```bash
chown -R signal:signal /var/lib/signal-cli
chmod -R go-rwx /var/lib/signal-cli
```

## 6. Manual Daemon Test (Before Final Service Wiring)

```bash
sudo -u signal /usr/local/bin/signal-cli --config /var/lib/signal-cli daemon --socket /var/run/signal-cli/socket
```

In another shell:

```bash
ls -l /var/run/signal-cli/socket
```

## 7. Optional Send Test (Socket Mode)

Replace placeholders with real numbers:

```bash
sudo -u signal /usr/local/bin/signal-cli \
  --socket /var/run/signal-cli/socket \
  --config /var/lib/signal-cli \
  -u <YOUR_SIGNAL_NUMBER> \
  send -m "test from mesh" <TARGET_NUMBER>
```

## 8. Post-Checks

- `id signal`
- `ls -ld /var/lib/signal-cli`
- `sudo -u signal /usr/local/bin/signal-cli --version`
- linked account data exists under `/var/lib/signal-cli`

## Notes

- On some iOS devices, linking may fail while Android succeeds for the same URI/QR flow. Treat that as client-app behavior, not Mesh/Nebula/network failure.
- Keep all `signal-cli` runtime operations under the `signal` user.
