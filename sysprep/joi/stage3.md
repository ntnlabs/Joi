# Joi Stage 3 Walkthrough (Docker + Ollama)

Use this after:

- `sysprep/joi/setup.sh` (stage 1)
- `sysprep/joi/stage2.md` (Nebula)

This stage remains manual on purpose. Joi is a physical host in this deployment and Docker/Ollama setup should be explicit.

## What Stage 3 Does

- Validate Joi network prerequisites for package/container pulls
- Install Docker Engine (`docker.io`) if not already installed
- Install NVIDIA container runtime/toolkit pieces (if required)
- Run or update the Ollama container workload
- Verify GPU visibility and model runtime

## Preconditions

- Joi stage 1 completed and UFW baseline is active
- Joi can reach the gateway/hopper and DNS via the internal network
- If Joi default route is intentionally non-persistent (fail-closed), add it manually for this session before continuing
- GPU driver is installed and `nvidia-smi` works

## 1. Session Network Check (Joi)

Confirm route and DNS first:

```bash
ip route
cat /etc/resolv.conf
getent hosts archive.ubuntu.com
```

If default route is missing (intentional fail-closed design in this environment), add it manually:

```bash
ip route add default via 172.22.22.4 dev eno1
```

Adjust interface/IPs to your actual Joi box if different.

## 2. Open Joi Update Window

```bash
./update.sh --enable
```

This should open temporary outbound:

- `53/udp`
- `80/tcp`
- `443/tcp`

## 3. Install / Update Docker and Related Packages

This stage standardizes on Ubuntu `docker.io` for now (not Podman).

Install/update Docker Engine and verify:

```bash
apt install -y docker.io
```

Then verify:

```bash
docker --version
systemctl status docker
```

## 4. Verify NVIDIA Runtime Availability

Confirm GPU driver on host:

```bash
nvidia-smi
```

If using NVIDIA container runtime/toolkit, verify it is installed and configured:

```bash
nvidia-container-toolkit --version || true
```

## 5. Deploy / Start Ollama Container

Use your project-specific Docker command / compose file. The exact invocation is intentionally not hardcoded here because it varies by deployment iteration.

Minimum checks after startup:

```bash
docker ps
docker logs --tail 100 <ollama_container_name>
```

## 6. Verify Ollama Reachability

From Joi host:

```bash
curl -s http://127.0.0.1:11434/api/tags | head
```

If your deployment binds Ollama elsewhere, use that address instead.

## 7. Model Check (Optional)

If the `ollama` CLI is available in the container:

```bash
docker exec <ollama_container_name> ollama list
```

## 8. Close Joi Update Window

```bash
./update.sh --disable
```

## 9. Post-Checks

- `nvidia-smi` (GPU visible, expected processes)
- `docker ps` (containers healthy)
- `curl http://127.0.0.1:11434/api/tags`

## Notes

- Joi is intended to route external traffic via gateway/hopper, not direct WAN.
- If GNOME is active on Joi, `Xorg` / `gnome-shell` may consume VRAM on the NVIDIA GPU. That is separate from Ollama correctness, but relevant for capacity/performance.
- Podman is not covered by this stage walkthrough.
