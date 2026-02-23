#!/bin/sh
# NTP VM update control
# Enable/disable package update egress via internal gateway/hopper.
#
# Usage: update.sh --enable | --disable | --run
#
# CONFIGURATION - Edit these values for your environment:
INT_IF="<INTERNAL_INTERFACE>"  # e.g., eth1
GATEWAY_IP="<GATEWAY_IP>"      # e.g., 172.22.22.4

###########################################

# Validate configuration
if echo "$INT_IF" | grep -q "^<" || echo "$GATEWAY_IP" | grep -q "^<"; then
    echo "ERROR: Edit this script and set INT_IF, GATEWAY_IP"
    exit 1
fi

RULE_HTTPS="-o $INT_IF -p tcp --dport 443 -j ACCEPT"
RULE_HTTP="-o $INT_IF -p tcp --dport 80 -j ACCEPT"
# DNS is actually destined to the router/gateway/hopper on the internal interface.
# Use a comment marker so --disable removes only this update rule (not baseline DNS).
RULE_DNS_UDP="-o $INT_IF -p udp -d $GATEWAY_IP --dport 53 -m comment --comment ntp-update-dns -j ACCEPT"

remove_temp_update_default() {
    while ip route show default | grep -q "via $GATEWAY_IP dev $INT_IF"; do
        ip route del default via "$GATEWAY_IP" dev "$INT_IF" 2>/dev/null || break
    done
    ip route del default via "$GATEWAY_IP" dev "$INT_IF" metric 1 2>/dev/null || true
}

enable_rules() {
    iptables -A OUTPUT $RULE_HTTPS
    iptables -A OUTPUT $RULE_HTTP
    iptables -A OUTPUT $RULE_DNS_UDP
    remove_temp_update_default
    ip route add default via "$GATEWAY_IP" dev "$INT_IF" metric 1 2>/dev/null || \
        ip route replace default via "$GATEWAY_IP" dev "$INT_IF" metric 1
}

disable_rules() {
    iptables -D OUTPUT $RULE_HTTPS 2>/dev/null
    iptables -D OUTPUT $RULE_HTTP 2>/dev/null
    iptables -D OUTPUT $RULE_DNS_UDP 2>/dev/null
    remove_temp_update_default
}

case "$1" in
    --enable)
        echo "Enabling DNS (UDP)/HTTP/HTTPS outbound via internal gateway/hopper..."
        enable_rules
        echo "Done. Internal gateway/hopper route preferred for updates (metric 1)."
        echo "Also enable on gateway/hopper, then run 'apk update && apk upgrade'"
        ;;
    --disable)
        echo "Disabling DNS (UDP)/HTTP/HTTPS outbound via internal gateway/hopper..."
        disable_rules
        echo "Done. Temporary internal update route removed."
        ;;
    --run)
        echo "Enabling updates, running apk, then disabling..."
        enable_rules
        apk update && apk upgrade
        disable_rules
        echo "Done. Remember to --disable on gateway/hopper too."
        ;;
    *)
        echo "Usage: $0 --enable | --disable | --run"
        echo ""
        echo "  --enable   Open DNS (UDP to gateway/hopper) + HTTP/HTTPS via internal gateway/hopper for updates"
        echo "  --disable  Close after done"
        echo "  --run      Open, update, upgrade, close (all-in-one)"
        echo ""
        echo "NOTE: Gateway/hopper must also have updates enabled!"
        exit 1
        ;;
esac
