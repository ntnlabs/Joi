#!/bin/bash
# Joi VM Initial Setup
# - Firewall (UFW)
# - DNS (resolv.conf)
# - NTP client (chrony)
#
# Interactive script - asks for all configuration values.
# Run as root: bash setup.sh

set -e

echo "=========================================="
echo "Joi VM Initial Setup"
echo "=========================================="
echo ""

# Gather configuration
echo "Network Configuration"
echo "---------------------"
printf "Hostname for this device [joi]: "
read HOSTNAME
HOSTNAME="${HOSTNAME:-joi}"

printf "Internal interface [eth0]: "
read INT_IF
INT_IF="${INT_IF:-eth0}"

printf "Gateway IP [172.22.22.4]: "
read GATEWAY_IP
GATEWAY_IP="${GATEWAY_IP:-172.22.22.4}"

printf "NTP server IP [172.22.22.3]: "
read NTP_IP
NTP_IP="${NTP_IP:-172.22.22.3}"

echo ""
echo "Nebula Configuration"
echo "--------------------"
printf "Mesh Nebula IP [10.42.0.1]: "
read MESH_NEBULA_IP
MESH_NEBULA_IP="${MESH_NEBULA_IP:-10.42.0.1}"

echo ""
echo "Setup Options"
echo "-------------"
printf "Allow Docker bridge traffic? [Y/n]: "
read ALLOW_DOCKER
case "$ALLOW_DOCKER" in
    n|N|no|NO) ALLOW_DOCKER=0 ;;
    *) ALLOW_DOCKER=1 ;;
esac

printf "Allow temporary egress (53/80/443) for initial setup? [Y/n]: "
read ALLOW_TMP
case "$ALLOW_TMP" in
    n|N|no|NO) ALLOW_TMP=0 ;;
    *) ALLOW_TMP=1 ;;
esac

echo ""
echo "Configuration Summary:"
echo "  Hostname:       $HOSTNAME"
echo "  INT interface:  $INT_IF"
echo "  Gateway:        $GATEWAY_IP"
echo "  NTP server:     $NTP_IP"
echo "  Mesh Nebula IP: $MESH_NEBULA_IP"
echo "  Docker bridge:  $([ $ALLOW_DOCKER -eq 1 ] && echo 'yes' || echo 'no')"
echo "  Temp egress:    $([ $ALLOW_TMP -eq 1 ] && echo 'yes' || echo 'no')"
echo ""
printf "Proceed? [y/N]: "
read CONFIRM
case "$CONFIRM" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; exit 1 ;;
esac

###########################################
# HOSTNAME
###########################################
echo ""
echo "[1/5] Setting hostname..."
hostnamectl set-hostname "$HOSTNAME"

###########################################
# JOI USER + DIRECTORIES
###########################################
echo ""
echo "[2/5] Creating joi account and directories..."

if ! id -u joi >/dev/null 2>&1; then
    useradd -r -s /usr/sbin/nologin -d /var/lib/joi joi
fi

mkdir -p /var/lib/joi
mkdir -p /opt/joi

# Data dir must be writable by the joi service user.
chown joi:joi /var/lib/joi
chmod 750 /var/lib/joi

###########################################
# FIREWALL (UFW)
###########################################
echo ""
echo "[3/5] Configuring firewall (UFW)..."

ufw --force reset
ufw default deny incoming
ufw default deny outgoing

# Loopback
ufw allow in on lo
ufw allow out on lo

# Docker bridge
if [ "$ALLOW_DOCKER" = "1" ]; then
    ufw allow in on docker0
    ufw allow out on docker0
fi

# SSH from gateway
ufw allow from "$GATEWAY_IP" to any port 22 proto tcp

# NTP to internal NTP server
ufw allow out to "$NTP_IP" port 123 proto udp

# Nebula transport
ufw allow 4242/udp
ufw allow out 4242/udp

# joi <-> mesh API over Nebula
ufw allow from "$MESH_NEBULA_IP" to any port 8443 proto tcp
ufw allow out to "$MESH_NEBULA_IP" port 8444 proto tcp

# Temporary egress for setup
if [ "$ALLOW_TMP" = "1" ]; then
    ufw allow out 53
    ufw allow out 80/tcp
    ufw allow out 443/tcp
fi

ufw --force enable

###########################################
# DNS
###########################################
echo ""
echo "[4/5] Configuring DNS..."

# Disable systemd-resolved if present
if systemctl is-active --quiet systemd-resolved 2>/dev/null; then
    systemctl stop systemd-resolved
    systemctl disable systemd-resolved
    rm -f /etc/resolv.conf
fi

cat > /etc/resolv.conf << EOF
# Gateway DNS
nameserver $GATEWAY_IP
EOF

chattr +i /etc/resolv.conf 2>/dev/null || true

###########################################
# NTP (chrony)
###########################################
echo ""
echo "[5/5] Configuring NTP client (chrony)..."

apt-get update
apt-get install -y chrony

cat > /etc/chrony/chrony.conf << EOF
# Use internal NTP server
server $NTP_IP iburst

# Record rate of system clock drift
driftfile /var/lib/chrony/chrony.drift

# Allow stepping clock on first sync
makestep 1.0 3

# Enable RTC sync
rtcsync
EOF

systemctl enable chrony
systemctl restart chrony

###########################################
# DONE
###########################################
echo ""
echo "=========================================="
echo "Joi setup complete!"
echo "=========================================="
echo ""
echo "Verify with:"
echo "  ufw status verbose"
echo "  chronyc sources"
echo "  cat /etc/resolv.conf"
echo ""
if [ "$ALLOW_TMP" = "1" ]; then
    echo "WARNING: Temporary egress (53/80/443) is ENABLED."
    echo "After setup, run: ufw delete allow out 53"
    echo "                  ufw delete allow out 80/tcp"
    echo "                  ufw delete allow out 443/tcp"
    echo ""
fi
echo "This script can now be deleted."
