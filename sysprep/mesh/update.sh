#!/bin/bash
# Mesh VM update control
# Enable/disable HTTP/HTTPS/DNS outbound to gateway for updates.
#
# Usage: update.sh --enable | --disable | --run
#
# CONFIGURATION - Edit these values for your environment:
INT_IF="<INTERNAL_INTERFACE>"  # e.g., eth0

###########################################

# Validate configuration
if echo "$INT_IF" | grep -q "^<"; then
    echo "ERROR: Edit this script and set INT_IF"
    exit 1
fi

RULE_HTTPS="-o $INT_IF -p tcp --dport 443 -j ACCEPT"
RULE_HTTP="-o $INT_IF -p tcp --dport 80 -j ACCEPT"
RULE_DNS="-o $INT_IF -p udp --dport 53 -j ACCEPT"

enable_rules() {
    iptables -A OUTPUT $RULE_HTTPS
    iptables -A OUTPUT $RULE_HTTP
    iptables -A OUTPUT $RULE_DNS
}

disable_rules() {
    iptables -D OUTPUT $RULE_HTTPS 2>/dev/null
    iptables -D OUTPUT $RULE_HTTP 2>/dev/null
    iptables -D OUTPUT $RULE_DNS 2>/dev/null
}

case "$1" in
    --enable)
        echo "Enabling HTTP/HTTPS/DNS outbound via gateway..."
        enable_rules
        echo "Done. Also enable on gateway, then run 'apt update && apt upgrade'"
        ;;
    --disable)
        echo "Disabling HTTP/HTTPS/DNS outbound via gateway..."
        disable_rules
        echo "Done."
        ;;
    --run)
        echo "Enabling updates, running apt, then disabling..."
        enable_rules
        apt update && apt upgrade -y
        disable_rules
        echo "Done. Remember to --disable on gateway too."
        ;;
    *)
        echo "Usage: $0 --enable | --disable | --run"
        echo ""
        echo "  --enable   Open HTTP/HTTPS/DNS to gateway for updates"
        echo "  --disable  Close after done"
        echo "  --run      Open, update, upgrade, close (all-in-one)"
        echo ""
        echo "NOTE: Gateway must also have updates enabled!"
        exit 1
        ;;
esac
