# Joi Stage 3 Walkthrough (Joi App + Ollama Deployment)

Use this after:

- `sysprep/joi/setup.sh` (stage 1)
- `sysprep/joi/stage2.md` (Nebula)

This stage remains manual on purpose. Joi is a physical host in this deployment and Docker/Ollama setup should be explicit.

## What Stage 3 Does

- Deploy/update the Joi repository checkout on the host (`/opt/Joi`)
- Install Joi Python dependencies (current project method)
- Install and configure `joi-api` systemd service
- Run/update the Ollama container workload (GPU-enabled)
- Verify Joi API + Ollama runtime

## Preconditions

- Joi stage 1 completed and UFW baseline is active
- Joi stage 2 completed (`sysprep/joi/stage2.md`)
- Nebula is up (if required by your current deployment sequence)
- Docker Engine (`docker.io`) is installed and running
- NVIDIA Container Toolkit is installed and configured
- GPU smoke test in Docker has already passed
- `joi` service user exists (created in stage 1)
- `/var/lib/joi` exists and is owned by `joi:joi`

## 1. Open Joi Update Window (If Needed)

Use this only if you still need package installs or a fresh git clone on this session:

```bash
./update.sh --enable
```

## 2. Repository Checkout / Update (`/opt/Joi`)

Initial clone (new host):

```bash
cd /opt
git clone https://github.com/ntnlabs/Joi.git Joi
```

Update existing checkout:

```bash
cd /opt/Joi
git pull
```

## 3. Install Joi Python Dependencies

Current project method (system Python with pip, as used in lab):

```bash
apt install -y python3-pip
cd /opt/Joi/execution/joi
pip install -r requirements.txt --break-system-packages --ignore-installed
```

Install SQLCipher CLI/dev libs used by Joi memory/admin tooling, then install the Python SQLCipher binding if your current runtime still requires it:

```bash
apt install -y sqlcipher libsqlcipher-dev
pip install sqlcipher3-binary --break-system-packages
```

If additional packages are still needed in your current branch/runtime, install them explicitly here (keep this section aligned with real usage and trim later when packaging improves).

## 4. Install / Refresh Joi API Service Unit

Install the systemd unit from the repo:

```bash
cp /opt/Joi/execution/joi/systemd/joi-api.service /etc/systemd/system/
systemctl daemon-reload
```

Ensure Joi data dir ownership (usually already correct from stage 1):

```bash
chown -R joi:joi /var/lib/joi
chmod 750 /var/lib/joi
```

## 5. Configure Joi Environment File

Review and edit `/etc/default/joi-api` for this host (Nebula endpoints, HMAC, model settings, etc.), then lock down permissions:

```bash
chmod 640 /etc/default/joi-api
chown root:joi /etc/default/joi-api
```

## 6. Configure Docker NVIDIA Runtime as Default

Docker may fall back to CPU even with `--gpus all` if NVIDIA runtime is not the default. Check and fix:

```bash
docker info | grep -i "default runtime"
```

If it shows `runc` instead of `nvidia`, configure the daemon:

```bash
cat > /etc/docker/daemon.json << 'EOF'
{
  "default-runtime": "nvidia",
  "runtimes": {
    "nvidia": {
      "path": "nvidia-container-runtime",
      "runtimeArgs": []
    }
  }
}
EOF

systemctl restart docker
docker info | grep -i "default runtime"
```

Should now show `Default Runtime: nvidia`.

## 7. Deploy / Start Ollama Container (GPU)

Run Ollama with GPU access:

```bash
docker stop ollama 2>/dev/null || true
docker rm ollama 2>/dev/null || true
docker run -d --gpus all \
  -v ollama:/root/.ollama \
  -p 11434:11434 \
  --name ollama \
  --restart unless-stopped \
  ollama/ollama
```

Verify container state:

```bash
docker ps
docker logs --tail 100 ollama
docker exec ollama nvidia-smi
```

## 8. Pull / Verify Model in Ollama

Primary business-mode deployment model for this host:
- `phi4:14b-q4_K_M`

Pull and verify the model:

```bash
docker exec ollama ollama pull phi4:14b-q4_K_M
docker exec ollama ollama list
```

Quick generation smoke test:

```bash
time curl -s http://localhost:11434/api/generate \
  -d '{"model":"phi4:14b-q4_K_M","prompt":"hi","stream":false}' | head -c 200
```

## 9. Configure Joi Model Selection (Important)

Set the Joi model in `/etc/default/joi-api` to the exact model pulled into Ollama.

Example (business-mode deployment on A2000):

```bash
docker exec -it ollama ollama list
sed -i 's/JOI_OLLAMA_MODEL=.*/JOI_OLLAMA_MODEL=phi4:14b-q4_K_M/' /etc/default/joi-api
```

Optional context window override (append only if not already managed elsewhere):

```bash
grep -q '^JOI_OLLAMA_NUM_CTX=' /etc/default/joi-api \
  && sed -i 's/^JOI_OLLAMA_NUM_CTX=.*/JOI_OLLAMA_NUM_CTX=4096/' /etc/default/joi-api \
  || echo 'JOI_OLLAMA_NUM_CTX=4096' >> /etc/default/joi-api
```

Verify:

```bash
grep -E '^JOI_OLLAMA_MODEL=|^JOI_OLLAMA_NUM_CTX=' /etc/default/joi-api
```

## 10. Start / Restart Joi API

Enable/start the service:

```bash
systemctl enable joi-api
systemctl restart joi-api
systemctl status joi-api
```

## 11. Verify Joi API Health

```bash
curl http://127.0.0.1:8443/health
```

If you are testing across Nebula, also verify Mesh-facing health from the appropriate peer path.

## 12. Live Logs / Runtime Checks

Useful checks during rollout:

```bash
journalctl -u joi-api -f
docker ps
docker exec ollama ollama ps
```

Optional admin/debug checks:

```bash
ufw status verbose
cd /opt/Joi/execution/joi/scripts
```

## 13. Close Joi Update Window (If Opened)

```bash
./update.sh --disable
```

## 14. Post-Checks

- `docker ps` (Ollama running)
- `docker exec ollama nvidia-smi` (GPU visible in container)
- `curl http://127.0.0.1:11434/api/tags`
- `curl http://127.0.0.1:8443/health`
- `systemctl status joi-api`

## Notes

- Joi is intended to route external traffic via gateway/hopper, not direct WAN.
- If GNOME is active on Joi, `Xorg` / `gnome-shell` may consume VRAM on the NVIDIA GPU. That is separate from Ollama correctness, but relevant for capacity/performance.
- Docker/NVIDIA runtime substrate setup belongs to stage 2.
- Nebula installation/configuration belongs to stage 2. Do not repeat Nebula steps here.
- Standard path in this repo is `/opt/Joi` (uppercase `J`).
- Current business-mode model target for this host: `phi4:14b-q4_K_M`.
- Do not open ad-hoc UFW ports manually for package installs on Joi; use `./update.sh --enable` / `--disable` so temporary egress stays consistent.
