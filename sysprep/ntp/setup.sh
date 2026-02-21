#!/bin/sh
# NTP VM Initial Setup
# - Firewall (iptables)
# - DNS (resolv.conf)
# - NTP server (chrony) - serves time to internal network
#
# Interactive script - asks for all configuration values.
# Run as root: sh setup.sh

set -e

echo "=========================================="
echo "NTP VM Initial Setup"
echo "=========================================="
echo ""

# Gather configuration
echo "Network Configuration"
echo "---------------------"
printf "Hostname for this device [ntp]: "
read HOSTNAME
HOSTNAME="${HOSTNAME:-ntp}"

printf "WAN interface [eth0]: "
read WAN_IF
WAN_IF="${WAN_IF:-eth0}"

printf "Internal interface [eth1]: "
read INT_IF
INT_IF="${INT_IF:-eth1}"

printf "This device IP (internal) [172.22.22.3]: "
read MY_IP
MY_IP="${MY_IP:-172.22.22.3}"

printf "Internal network CIDR [172.22.22.0/24]: "
read INTERNAL_NET
INTERNAL_NET="${INTERNAL_NET:-172.22.22.0/24}"

printf "Gateway IP [172.22.22.4]: "
read GATEWAY_IP
GATEWAY_IP="${GATEWAY_IP:-172.22.22.4}"

echo ""
echo "Configuration Summary:"
echo "  Hostname:      $HOSTNAME"
echo "  WAN interface: $WAN_IF"
echo "  INT interface: $INT_IF"
echo "  This IP:       $MY_IP"
echo "  Internal net:  $INTERNAL_NET"
echo "  Gateway:       $GATEWAY_IP"
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
echo "[1/4] Setting hostname..."
echo "$HOSTNAME" > /etc/hostname
hostname "$HOSTNAME"

###########################################
# FIREWALL (iptables)
###########################################
echo ""
echo "[2/4] Configuring firewall..."

iptables -F
iptables -X
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT DROP

# Loopback + established
iptables -A INPUT -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# SSH from gateway
iptables -A INPUT -i $INT_IF -p tcp -s $GATEWAY_IP --dport 22 -j ACCEPT

# NTP from internal network
iptables -A INPUT -i $INT_IF -p udp -s $INTERNAL_NET --dport 123 -j ACCEPT

# WAN egress for NTP + DNS + DHCP
iptables -A OUTPUT -o $WAN_IF -p udp --dport 123 -j ACCEPT
iptables -A OUTPUT -o $WAN_IF -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -o $WAN_IF -p udp --dport 67 -j ACCEPT
iptables -A INPUT  -i $WAN_IF -p udp --sport 67 --dport 68 -j ACCEPT

# Persist
rc-update add iptables 2>/dev/null || true
/etc/init.d/iptables save

###########################################
# DNS
###########################################
echo ""
echo "[3/4] Configuring DNS..."

cat > /etc/resolv.conf << EOF
# Gateway DNS
nameserver $GATEWAY_IP
EOF

###########################################
# NTP SERVER (chrony)
###########################################
echo ""
echo "[4/4] Configuring NTP server (chrony)..."

apk add chrony

cat > /etc/chrony/chrony.conf << EOF
# Upstream NTP sources (via WAN)
pool pool.ntp.org iburst
pool time.cloudflare.com iburst

# Record rate of system clock drift
driftfile /var/lib/chrony/chrony.drift

# Allow stepping clock on first sync
makestep 1.0 3

# Enable RTC sync
rtcsync

# Serve time to internal network
allow $INTERNAL_NET

# Listen on internal interface
bindaddress $MY_IP
EOF

rc-update add chronyd
service chronyd restart

###########################################
# DONE
###########################################
echo ""
echo "=========================================="
echo "NTP setup complete!"
echo "=========================================="
echo ""
echo "Verify with:"
echo "  iptables -L -n -v"
echo "  chronyc sources"
echo "  chronyc clients"
echo "  cat /etc/resolv.conf"
echo ""
echo "This script can now be deleted."
