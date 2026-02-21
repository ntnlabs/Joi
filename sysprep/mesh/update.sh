#!/bin/bash
# Mesh VM update control (Ubuntu - uses UFW)
# Enable/disable HTTP outbound for apt updates.
#
# Usage: update.sh --enable | --disable | --run
#
# NOTE: mesh already has 443/tcp and 53/udp permanently allowed for Signal.
#       This script only controls 80/tcp for apt repositories.

enable_rules() {
    echo "Enabling HTTP outbound for apt..."
    ufw allow out 80/tcp
}

disable_rules() {
    echo "Disabling HTTP outbound..."
    ufw delete allow out 80/tcp 2>/dev/null
}

case "$1" in
    --enable)
        enable_rules
        ufw status | grep "80/tcp"
        echo "Done. Also enable on gateway, then run 'apt update && apt upgrade'"
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
        echo "Done. Remember to --disable on gateway too."
        ;;
    --status)
        echo "UFW status:"
        ufw status verbose
        ;;
    *)
        echo "Usage: $0 --enable | --disable | --run | --status"
        echo ""
        echo "  --enable   Allow HTTP (80) outbound for apt"
        echo "  --disable  Remove HTTP outbound rule"
        echo "  --run      Enable, update, upgrade, disable (all-in-one)"
        echo "  --status   Show UFW status"
        echo ""
        echo "NOTE: 443/tcp and 53/udp are permanently allowed for Signal."
        echo "NOTE: Gateway must also have updates enabled!"
        exit 1
        ;;
esac
