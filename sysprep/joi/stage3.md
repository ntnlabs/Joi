# Joi Stage 3 Walkthrough (Ollama Deployment)

Use this after:

- `sysprep/joi/setup.sh` (stage 1)
- `sysprep/joi/stage2.md` (Nebula)

This stage remains manual on purpose. Joi is a physical host in this deployment and Docker/Ollama setup should be explicit.

## What Stage 3 Does

- Run or update the Ollama container workload
- Verify Ollama service/model runtime

## Preconditions

- Joi stage 1 completed and UFW baseline is active
- Joi stage 2 completed (`sysprep/joi/stage2.md`)
- Nebula is up (if required by your current deployment sequence)
- Docker Engine (`docker.io`) is installed and running
- NVIDIA Container Toolkit is installed and configured
- GPU smoke test in Docker has already passed

## 1. Deploy / Start Ollama Container

Use your project-specific Docker command / compose file. The exact invocation is intentionally not hardcoded here because it varies by deployment iteration.

Minimum checks after startup:

```bash
docker ps
docker logs --tail 100 <ollama_container_name>
```

## 2. Verify Ollama Reachability

From Joi host:

```bash
curl -s http://127.0.0.1:11434/api/tags | head
```

If your deployment binds Ollama elsewhere, use that address instead.

## 3. Model Check (Optional)

If the `ollama` CLI is available in the container:

```bash
docker exec <ollama_container_name> ollama list
```

## 4. Post-Checks

- `docker ps` (containers healthy)
- `curl http://127.0.0.1:11434/api/tags`
- `docker logs --tail 100 <ollama_container_name>`

## Notes

- Joi is intended to route external traffic via gateway/hopper, not direct WAN.
- If GNOME is active on Joi, `Xorg` / `gnome-shell` may consume VRAM on the NVIDIA GPU. That is separate from Ollama correctness, but relevant for capacity/performance.
- Docker/NVIDIA runtime substrate setup belongs to stage 2.
