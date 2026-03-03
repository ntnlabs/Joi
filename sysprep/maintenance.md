# Maintenance Guide

Recurring maintenance tasks for Joi and Mesh VMs.

## Docker Ecosystem Update

Updates Docker Engine, containerd, and plugins while preserving all volumes and data.

**Safe to update:** Volumes (`/var/lib/docker/volumes/`) persist across upgrades. Ollama models, container configs, and NVIDIA runtime settings survive.

### Pre-flight

```bash
docker --version
docker volume ls
docker ps
```

### Update Procedure

```bash
# 1. Stop containers gracefully
docker stop ollama

# 2. Update Docker packages
sudo apt update
sudo apt install --only-upgrade docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 3. Restart Docker daemon
sudo systemctl restart docker

# 4. Verify NVIDIA runtime still default (Joi VM)
docker info | grep -i "default runtime"
# Should show: Default Runtime: nvidia

# 5. Verify volumes survived
docker volume ls
# Should show: ollama

# 6. Start containers
docker start ollama

# 7. Verify models intact
docker exec ollama ollama list
```

### If NVIDIA Runtime Lost

If `Default Runtime` shows `runc` instead of `nvidia`, restore the config:

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

sudo systemctl restart docker
docker info | grep -i "default runtime"
```

### Rollback (if needed)

```bash
# Check available versions
apt list -a docker-ce

# Install specific version
sudo apt install docker-ce=<version> docker-ce-cli=<version>
```

---

## NVIDIA Driver Update

Update NVIDIA drivers on Joi VM (physical host with GPU).

### Pre-flight

```bash
nvidia-smi
docker exec ollama nvidia-smi
```

### Update Procedure

```bash
# 1. Stop GPU workloads
docker stop ollama

# 2. Update driver
sudo apt update
sudo apt install --only-upgrade nvidia-driver-<version>

# 3. Reboot required
sudo reboot

# 4. After reboot, verify
nvidia-smi
docker start ollama
docker exec ollama nvidia-smi
```

---

## System Updates (General)

Regular package updates for both VMs.

### Safe Updates (no reboot)

```bash
sudo apt update
sudo apt upgrade
```

### Kernel Updates (requires reboot)

```bash
sudo apt update
sudo apt full-upgrade
sudo reboot
```

### After Reboot Checklist

**Joi VM:**
```bash
systemctl status joi-api
docker ps
docker exec ollama nvidia-smi
curl http://127.0.0.1:8443/health
```

**Mesh VM:**
```bash
systemctl status mesh-signal-worker
curl http://127.0.0.1:8444/health
```

---

## Ollama Container Update

Updates the Ollama container image while preserving all models (stored in volume).

### Check Current Version

```bash
docker exec ollama ollama --version
```

### Check for Updates

```bash
docker pull ollama/ollama
# "Image is up to date" = already latest
# Downloads layers = newer version available, continue with update
```

### Update Procedure

```bash
# 1. Pull latest image (if not already done above)
docker pull ollama/ollama

# 2. Stop and remove old container
docker stop ollama
docker rm ollama

# 3. Run new container with same parameters
docker run -d --gpus all \
  -v ollama:/root/.ollama \
  -p 11434:11434 \
  --name ollama \
  --restart unless-stopped \
  ollama/ollama

# 4. Verify models intact
docker exec ollama ollama list
docker exec ollama nvidia-smi
```

**Note:** The `-v ollama:/root/.ollama` volume mount preserves all models across container recreations.

---

## Ollama Model Management

### List Models

```bash
docker exec ollama ollama list
```

### Pull New Model

```bash
docker exec ollama ollama pull <model>:<tag>
```

### Remove Model

```bash
docker exec ollama ollama rm <model>:<tag>
```

### Check Disk Usage

```bash
docker system df
du -sh /var/lib/docker/volumes/ollama/
```

---

## Log Management

### View Logs

```bash
# Joi API
journalctl -u joi-api -f

# Mesh worker
journalctl -u mesh-signal-worker -f

# Docker/Ollama
docker logs --tail 100 -f ollama
```

### Clear Old Logs

```bash
# Vacuum journald logs older than 7 days
sudo journalctl --vacuum-time=7d

# Or by size (keep last 500MB)
sudo journalctl --vacuum-size=500M
```

---

## Database Maintenance

### Check Database Size

```bash
ls -lh /var/lib/joi/memory.db*
```

### Vacuum (reclaim space after deletions)

```bash
# Stop service first
sudo systemctl stop joi-api

# Vacuum
sqlite3 /var/lib/joi/memory.db "VACUUM;"

# Or with SQLCipher (if encrypted)
sqlcipher /var/lib/joi/memory.db
> PRAGMA key = '<your-key>';
> VACUUM;
> .quit

# Restart
sudo systemctl start joi-api
```

---

## Backup Checklist

### Critical Files (Joi VM)

| Path | Contents |
|------|----------|
| `/etc/default/joi-api` | All secrets (HMAC, DB key) |
| `/var/lib/joi/memory.db` | Conversation history, facts |
| `/var/lib/joi/prompts/` | Per-user/group configs |
| `/var/lib/joi/policy/` | Mesh policy |
| `/var/lib/joi/knowledge/` | RAG knowledge base |
| `/etc/nebula/` | Nebula keys and config |

### Critical Files (Mesh VM)

| Path | Contents |
|------|----------|
| `/etc/default/mesh-signal-worker` | HMAC secret, account |
| `/var/lib/signal-cli/` | Signal account data |
| `/etc/nebula/` | Nebula keys and config |

### Docker Volumes

```bash
# List volumes
docker volume ls

# Backup Ollama models (large!)
docker run --rm -v ollama:/data -v $(pwd):/backup alpine tar czf /backup/ollama-backup.tar.gz /data
```
