#!/bin/sh
# NTP VM Initial Setup
# - Firewall (iptables)
# - DNS (resolv.conf)
# - NTP server (chrony) - serves time to internal network
#
# Interactive script - asks for all configuration values.
# Run as root: sh setup.sh

set -e

remove_temp_update_default() {
    # Temporary update route is an internal default preferred over DHCP WAN default.
    # Remove all copies if a previous run was interrupted.
    while ip route show default | grep -q "via $GATEWAY_IP dev $INT_IF"; do
        ip route del default via "$GATEWAY_IP" dev "$INT_IF" 2>/dev/null || break
    done
    ip route del default via "$GATEWAY_IP" dev "$INT_IF" metric 1 2>/dev/null || true
}

cleanup_temp_update_path() {
    # Remove temporary package egress rules (ignore if not present).
    iptables -D OUTPUT -o "$INT_IF" -p tcp --dport 80 -j ACCEPT 2>/dev/null || true
    iptables -D OUTPUT -o "$INT_IF" -p tcp --dport 443 -j ACCEPT 2>/dev/null || true
    iptables -D OUTPUT -o "$INT_IF" -p udp --dport 53 -j ACCEPT 2>/dev/null || true

    # Restore routing preference by removing the temporary internal default.
    remove_temp_update_default
}

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

printf "Gateway/hopper IP [172.22.22.4]: "
read GATEWAY_IP
GATEWAY_IP="${GATEWAY_IP:-172.22.22.4}"

echo ""
echo "Configuration Summary:"
echo "  Hostname:      $HOSTNAME"
echo "  WAN interface: $WAN_IF"
echo "  INT interface: $INT_IF"
echo "  This IP:       $MY_IP"
echo "  Internal net:  $INTERNAL_NET"
echo "  Gateway/hopper:$GATEWAY_IP"
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
echo "$HOSTNAME" > /etc/hostname
hostname "$HOSTNAME"

###########################################
# DISABLE IPV6 (sysctl)
###########################################
echo ""
echo "[2/5] Disabling IPv6..."

cat > /etc/sysctl.d/99-disable-ipv6.conf << 'EOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF

sysctl -p /etc/sysctl.d/99-disable-ipv6.conf >/dev/null

###########################################
# FIREWALL (iptables)
###########################################
echo ""
echo "[3/5] Configuring firewall..."

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

# SSH from gateway/hopper
iptables -A INPUT -i $INT_IF -p tcp -s $GATEWAY_IP --dport 22 -j ACCEPT

# NTP from internal network
iptables -A INPUT -i $INT_IF -p udp -s $INTERNAL_NET --dport 123 -j ACCEPT

# DNS direct via WAN (UDP only by design)
iptables -A OUTPUT -o $WAN_IF -p udp --dport 53 -j ACCEPT
# Upstream NTP direct via WAN
iptables -A OUTPUT -o $WAN_IF -p udp --dport 123 -j ACCEPT

# WAN egress for DHCP
iptables -A OUTPUT -o $WAN_IF -p udp --dport 67 -j ACCEPT
iptables -A INPUT  -i $WAN_IF -p udp --sport 67 --dport 68 -j ACCEPT

# Persist
rc-update add iptables 2>/dev/null || true
/etc/init.d/iptables save

###########################################
# DNS
###########################################
echo ""
echo "[4/5] Configuring DNS (WAN DHCP)..."
# Keep WAN DHCP-provided DNS (do not pin to internal gateway).
# If /etc/resolv.conf was previously pinned manually, refresh it before rerun.

###########################################
# NTP SERVER (chrony)
###########################################
echo ""
echo "[5/5] Configuring NTP server (chrony)..."
echo "NOTE: Router update routing must be enabled before package install (router update.sh --enable)."

# Ensure temporary route/rules are rolled back on error, Ctrl+C, or session disconnect.
trap 'cleanup_temp_update_path' EXIT INT TERM HUP

# Temporary package egress via internal gateway for initial chrony install.
iptables -A OUTPUT -o $INT_IF -p tcp --dport 80 -j ACCEPT
iptables -A OUTPUT -o $INT_IF -p tcp --dport 443 -j ACCEPT
# DNS continues to use WAN resolvers; while internal route is preferred, allow UDP/53
# via the internal path so the router can forward it.
iptables -A OUTPUT -o $INT_IF -p udp --dport 53 -j ACCEPT

remove_temp_update_default
ip route add default via "$GATEWAY_IP" dev "$INT_IF" metric 1 2>/dev/null || \
    ip route replace default via "$GATEWAY_IP" dev "$INT_IF" metric 1

# Fast preflight to avoid hanging on apk when router update routing is not enabled.
APK_REPO_URL="$(grep -v '^[[:space:]]*#' /etc/apk/repositories | grep -v '^[[:space:]]*$' | head -n1)"
if [ -z "$APK_REPO_URL" ] || ! wget -q -T 5 -O /dev/null "${APK_REPO_URL}/" >/dev/null 2>&1; then
    echo "ERROR: Package repo unreachable via internal gateway."
    echo "Enable update routing on router, then rerun setup.sh."
    exit 1
fi

apk add chrony

# Restore default route and close package egress; later updates are controlled via update.sh.
trap - EXIT INT TERM HUP
cleanup_temp_update_path

# Persist final locked-down ruleset.
/etc/init.d/iptables save

cat > /etc/chrony/chrony.conf << EOF
# Upstream NTP sources (direct via WAN by default route)
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

# Serve NTP on internal interface/IP only
bindaddress $MY_IP

# Acquire upstream NTP on WAN interface
bindacqdevice $WAN_IF
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
