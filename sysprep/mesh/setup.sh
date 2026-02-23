#!/bin/bash
# Mesh VM Initial Setup
# - Firewall (UFW)
# - DNS (via WAN DHCP / systemd-resolved)
# - NTP client (chrony)
# - Service user/data dir prep for mesh-signal-worker (signal-cli runtime)
#
# Interactive script - asks for all configuration values.
# Run as root: bash setup.sh

set -e

echo "=========================================="
echo "Mesh VM Initial Setup"
echo "=========================================="
echo ""

# Gather configuration
echo "Network Configuration"
echo "---------------------"
printf "Hostname for this device [mesh]: "
read HOSTNAME
HOSTNAME="${HOSTNAME:-mesh}"

printf "Internal interface [eth0]: "
read INT_IF
INT_IF="${INT_IF:-eth0}"

printf "WAN interface [eth1]: "
read WAN_IF
WAN_IF="${WAN_IF:-eth1}"

printf "Gateway/hopper IP [172.22.22.4]: "
read GATEWAY_IP
GATEWAY_IP="${GATEWAY_IP:-172.22.22.4}"

printf "NTP server IP [172.22.22.3]: "
read NTP_IP
NTP_IP="${NTP_IP:-172.22.22.3}"

echo ""
echo "Nebula Configuration"
echo "--------------------"
printf "Joi Nebula IP [10.42.0.10]: "
read JOI_NEBULA_IP
JOI_NEBULA_IP="${JOI_NEBULA_IP:-10.42.0.10}"

echo ""
echo "Configuration Summary:"
echo "  Hostname:      $HOSTNAME"
echo "  INT interface: $INT_IF"
echo "  WAN interface: $WAN_IF"
echo "  Gateway/hopper:$GATEWAY_IP"
echo "  NTP server:    $NTP_IP"
echo "  Joi Nebula IP: $JOI_NEBULA_IP"
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
# SIGNAL USER / DATA DIR
###########################################
echo ""
echo "[2/5] Preparing signal service user and data dir..."

if ! id -u signal >/dev/null 2>&1; then
    useradd -r -s /usr/sbin/nologin signal
fi

mkdir -p /var/lib/signal-cli
chown signal:signal /var/lib/signal-cli
chmod 0700 /var/lib/signal-cli

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

# SSH from gateway/hopper
ufw allow from "$GATEWAY_IP" to any port 22 proto tcp

# NTP to internal NTP server
ufw allow out to "$NTP_IP" port 123 proto udp
# Allow replies / server packets from internal NTP server (matches lab profile)
ufw allow from "$NTP_IP" to any port 123 proto udp

# Nebula transport
ufw allow 4242/udp
ufw allow out 4242/udp

# joi <-> mesh API over Nebula
ufw allow from "$JOI_NEBULA_IP" to any port 8444 proto tcp
ufw allow out to "$JOI_NEBULA_IP" port 8443 proto tcp

# WAN egress for Signal and DNS (UDP only by design)
ufw allow out 443/tcp
ufw allow out 53/udp

ufw --force enable

###########################################
# DNS
###########################################
echo ""
echo "[4/5] Configuring DNS (WAN DHCP)..."

# Mesh uses WAN DHCP-provided DNS. On reruns, undo older script behavior that
# pinned /etc/resolv.conf to the internal gateway and restore systemd-resolved.
chattr -i /etc/resolv.conf 2>/dev/null || true
if systemctl list-unit-files 2>/dev/null | grep -q '^systemd-resolved\.service'; then
    systemctl enable systemd-resolved >/dev/null 2>&1 || true
    systemctl restart systemd-resolved >/dev/null 2>&1 || true
    if [ -e /run/systemd/resolve/stub-resolv.conf ]; then
        ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
    elif [ -e /run/systemd/resolve/resolv.conf ]; then
        ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf
    fi
else
    echo "WARNING: systemd-resolved not available; ensure WAN DHCP DNS is configured."
fi

###########################################
# NTP (chrony)
###########################################
echo ""
echo "[5/5] Configuring NTP client (chrony)..."

# Temporary HTTP egress for Ubuntu apt repositories during initial setup.
ufw allow out 80/tcp

apt-get update
apt-get install -y chrony

# Close HTTP egress again; later updates are controlled via update.sh.
printf 'y\n' | ufw delete allow out 80/tcp >/dev/null 2>&1 || true

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
echo "Mesh setup complete!"
echo "=========================================="
echo ""
echo "Verify with:"
echo "  ufw status verbose"
echo "  chronyc sources"
echo "  cat /etc/resolv.conf"
echo ""
echo "This script can now be deleted."
