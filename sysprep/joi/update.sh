#!/bin/bash
# Joi VM update control (Ubuntu - uses UFW)
# Enable/disable HTTP/HTTPS/DNS (UDP) outbound for apt updates via gateway/hopper.
#
# Usage: update.sh --enable | --disable | --run
#
# NOTE: joi is air-gapped by default. This temporarily opens egress for updates.

enable_rules() {
    echo "Enabling HTTP/HTTPS/DNS (UDP) outbound for apt..."
    ufw allow out 80/tcp
    ufw allow out 443/tcp
    ufw allow out 53/udp
}

disable_rules() {
    echo "Disabling HTTP/HTTPS/DNS (UDP) outbound..."
    printf 'y\n' | ufw delete allow out 80/tcp >/dev/null 2>&1 || true
    printf 'y\n' | ufw delete allow out 443/tcp >/dev/null 2>&1 || true
    printf 'y\n' | ufw delete allow out 53/udp >/dev/null 2>&1 || true
}

case "$1" in
    --enable)
        enable_rules
        ufw status | grep -E "(80|443|53)"
        echo "Done. Also enable on gateway/hopper, then run 'apt update && apt upgrade'"
        ;;
    --disable)
        disable_rules
        echo "Done."
        ;;
    --run)
        echo "Enabling updates, running apt, then disabling..."
        enable_rules
        apt update && apt upgrade -y
        disable_rules
        echo "Done. Remember to --disable on gateway/hopper too."
        ;;
    --status)
        echo "UFW status:"
        ufw status verbose
        ;;
    *)
        echo "Usage: $0 --enable | --disable | --run | --status"
        echo ""
        echo "  --enable   Allow HTTP/HTTPS/DNS (UDP only) outbound for apt"
        echo "  --disable  Remove outbound rules"
        echo "  --run      Enable, update, upgrade, disable (all-in-one)"
        echo "  --status   Show UFW status"
        echo ""
        echo "NOTE: Gateway/hopper must also have updates enabled!"
        exit 1
        ;;
esac
