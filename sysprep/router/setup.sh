#!/bin/sh
# Router/Gateway VM Initial Setup
# - Firewall (iptables)
# - DNS forwarding (dnsmasq)
# - NTP client (chrony)
#
# Interactive script - asks for all configuration values.
# Run as root: sh setup.sh

set -e

echo "=========================================="
echo "Router/Gateway VM Initial Setup"
echo "=========================================="
echo ""

# Gather configuration
echo "Network Configuration"
echo "---------------------"
printf "Hostname for this device [router]: "
read HOSTNAME
HOSTNAME="${HOSTNAME:-router}"

printf "WAN interface [eth0]: "
read WAN_IF
WAN_IF="${WAN_IF:-eth0}"

printf "Internal interface [eth1]: "
read INT_IF
INT_IF="${INT_IF:-eth1}"

printf "This device IP (internal) [172.22.22.4]: "
read MY_IP
MY_IP="${MY_IP:-172.22.22.4}"

printf "Internal network CIDR [172.22.22.0/24]: "
read INTERNAL_NET
INTERNAL_NET="${INTERNAL_NET:-172.22.22.0/24}"

printf "NTP server IP [172.22.22.3]: "
read NTP_IP
NTP_IP="${NTP_IP:-172.22.22.3}"

printf "WAN management subnet (for SSH) [172.18.200.0/24]: "
read MGMT_SUBNET
MGMT_SUBNET="${MGMT_SUBNET:-172.18.200.0/24}"

printf "VPN subnet (for SSH) [10.0.10.0/24]: "
read VPN_SUBNET
VPN_SUBNET="${VPN_SUBNET:-10.0.10.0/24}"

echo ""
echo "Upstream DNS servers (for forwarding)"
printf "Primary DNS [1.1.1.1]: "
read DNS1
DNS1="${DNS1:-1.1.1.1}"

printf "Secondary DNS [8.8.8.8]: "
read DNS2
DNS2="${DNS2:-8.8.8.8}"

echo ""
echo "Configuration Summary:"
echo "  Hostname:      $HOSTNAME"
echo "  WAN interface: $WAN_IF"
echo "  INT interface: $INT_IF"
echo "  This IP:       $MY_IP"
echo "  Internal net:  $INTERNAL_NET"
echo "  NTP server:    $NTP_IP"
echo "  Mgmt subnet:   $MGMT_SUBNET"
echo "  VPN subnet:    $VPN_SUBNET"
echo "  DNS upstream:  $DNS1, $DNS2"
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
iptables -A FORWARD -m state --state ESTABLISHED,RELATED -j ACCEPT

# SSH from internal
iptables -A INPUT -i $INT_IF -p tcp -s $INTERNAL_NET --dport 22 -j ACCEPT

# SSH from management subnet
iptables -A INPUT -i $WAN_IF -p tcp -s $MGMT_SUBNET --dport 22 -j ACCEPT

# SSH from VPN subnet
iptables -A INPUT -i $WAN_IF -p tcp -s $VPN_SUBNET --dport 22 -j ACCEPT

# NTP to internal NTP server
iptables -A OUTPUT -o $INT_IF -p udp -d $NTP_IP --dport 123 -j ACCEPT

# SSH to internal VMs (jump host)
iptables -A OUTPUT -o $INT_IF -p tcp -d $INTERNAL_NET --dport 22 -j ACCEPT

# DNS queries from internal network
iptables -A INPUT -i $INT_IF -p udp -s $INTERNAL_NET --dport 53 -j ACCEPT
iptables -A INPUT -i $INT_IF -p tcp -s $INTERNAL_NET --dport 53 -j ACCEPT

# WAN egress for DNS (forwarding)
iptables -A OUTPUT -o $WAN_IF -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -o $WAN_IF -p tcp --dport 53 -j ACCEPT

# Persist
rc-update add iptables 2>/dev/null || true
/etc/init.d/iptables save

###########################################
# DNS (dnsmasq)
###########################################
echo ""
echo "[3/4] Configuring DNS forwarding (dnsmasq)..."

apk add dnsmasq

cat > /etc/dnsmasq.conf << EOF
# Listen only on internal interface
interface=$INT_IF
bind-interfaces

# Don't read /etc/resolv.conf
no-resolv

# Upstream DNS servers
server=$DNS1
server=$DNS2

# Local domain
local=/internal/

# Don't forward short names
domain-needed
bogus-priv

# Cache size
cache-size=1000
EOF

echo "nameserver 127.0.0.1" > /etc/resolv.conf

rc-update add dnsmasq
service dnsmasq restart

###########################################
# NTP (chrony)
###########################################
echo ""
echo "[4/4] Configuring NTP client (chrony)..."

apk add chrony

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

rc-update add chronyd
service chronyd restart

###########################################
# DONE
###########################################
echo ""
echo "=========================================="
echo "Router setup complete!"
echo "=========================================="
echo ""
echo "Verify with:"
echo "  iptables -L -n -v"
echo "  chronyc sources"
echo "  dig @127.0.0.1 google.com"
echo ""
echo "This script can now be deleted."
