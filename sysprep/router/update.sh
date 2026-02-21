#!/bin/sh
# Router/Gateway update routing control
# Enable/disable:
# - HTTP/HTTPS/DNS forwarding for internal VM updates
# - HTTP/HTTPS egress for router self-updates (OUTPUT policy is DROP by default)
#
# Usage: update.sh --enable | --disable | --status
#
# CONFIGURATION - Edit these values for your environment:
WAN_IF="<WAN_INTERFACE>"           # e.g., eth0
INT_IF="<INTERNAL_INTERFACE>"      # e.g., eth1
INTERNAL_NET="<INTERNAL_NETWORK>"  # e.g., 172.22.22.0/24

# Update ports
UPDATE_PORTS_TCP="80,443"
UPDATE_PORTS_UDP="53"

###########################################

# Validate configuration
if echo "$WAN_IF" | grep -q "^<"; then
    echo "ERROR: Edit this script and set WAN_IF, INT_IF, INTERNAL_NET"
    exit 1
fi

enable_updates() {
    echo "Enabling update routing for $INTERNAL_NET..."

    # Forward HTTP/HTTPS from internal to WAN
    iptables -A FORWARD -i $INT_IF -o $WAN_IF -s $INTERNAL_NET -p tcp -m multiport --dports $UPDATE_PORTS_TCP -j ACCEPT

    # Forward DNS from internal to WAN
    iptables -A FORWARD -i $INT_IF -o $WAN_IF -s $INTERNAL_NET -p udp --dport $UPDATE_PORTS_UDP -j ACCEPT

    # Allow router itself to reach HTTP/HTTPS on WAN (apk update/upgrade)
    iptables -A OUTPUT -o $WAN_IF -p tcp -m multiport --dports $UPDATE_PORTS_TCP -j ACCEPT

    # NAT masquerade (if not already present)
    iptables -t nat -C POSTROUTING -s $INTERNAL_NET -o $WAN_IF -j MASQUERADE 2>/dev/null || \
        iptables -t nat -A POSTROUTING -s $INTERNAL_NET -o $WAN_IF -j MASQUERADE

    # Enable IP forwarding
    echo 1 > /proc/sys/net/ipv4/ip_forward

    echo "Update routing ENABLED."
    echo "Internal VMs can now reach HTTP/HTTPS/DNS via this gateway."
    echo "Run --disable when done."
}

disable_updates() {
    echo "Disabling update routing..."

    # Remove forward rules (ignore errors if not present)
    iptables -D FORWARD -i $INT_IF -o $WAN_IF -s $INTERNAL_NET -p tcp -m multiport --dports $UPDATE_PORTS_TCP -j ACCEPT 2>/dev/null
    iptables -D FORWARD -i $INT_IF -o $WAN_IF -s $INTERNAL_NET -p udp --dport $UPDATE_PORTS_UDP -j ACCEPT 2>/dev/null
    iptables -D OUTPUT -o $WAN_IF -p tcp -m multiport --dports $UPDATE_PORTS_TCP -j ACCEPT 2>/dev/null

    echo "Update routing DISABLED."
}

show_status() {
    echo "=== Router Update Routing Status ==="
    echo ""
    echo "FORWARD chain:"
    iptables -L FORWARD -n -v --line-numbers
    echo ""
    echo "OUTPUT chain:"
    iptables -L OUTPUT -n -v --line-numbers
    echo ""
    echo "NAT POSTROUTING:"
    iptables -t nat -L POSTROUTING -n -v
    echo ""
    echo "IP forwarding: $(cat /proc/sys/net/ipv4/ip_forward)"
}

case "$1" in
    --enable)
        enable_updates
        ;;
    --disable)
        disable_updates
        ;;
    --status)
        show_status
        ;;
    *)
        echo "Usage: $0 --enable | --disable | --status"
        echo ""
        echo "  --enable   Allow internal VMs to reach HTTP/HTTPS/DNS"
        echo "  --disable  Block update traffic (default state)"
        echo "  --status   Show current forwarding rules"
        exit 1
        ;;
esac
