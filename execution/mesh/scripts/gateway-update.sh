#!/bin/sh
# Enable/disable APT updates on locked-down gateway VM
# Usage: gateway-update.sh --enable | --disable | --run

RULE_HTTPS="-o eth0 -p tcp --dport 443 -j ACCEPT"
RULE_HTTP="-o eth0 -p tcp --dport 80 -j ACCEPT"
RULE_DNS="-o eth0 -p udp --dport 53 -j ACCEPT"

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
        echo "Enabling HTTP/HTTPS/DNS outbound..."
        enable_rules
        echo "Done. Run 'apt update && apt upgrade' then --disable"
        ;;
    --disable)
        echo "Disabling HTTP/HTTPS/DNS outbound..."
        disable_rules
        echo "Done."
        ;;
    --run)
        echo "Enabling updates, running apt, then disabling..."
        enable_rules
        apt update && apt upgrade -y
        disable_rules
        echo "Done."
        ;;
    *)
        echo "Usage: $0 --enable | --disable | --run"
        echo ""
        echo "  --enable   Open HTTP/HTTPS/DNS for manual apt commands"
        echo "  --disable  Close after done"
        echo "  --run      Open, update, upgrade, close (all-in-one)"
        exit 1
        ;;
esac
