#!/bin/bash
# Mesh VM update control (Ubuntu - uses UFW)
# Enable/disable HTTP outbound for apt updates.
#
# Usage: update.sh --enable | --disable | --run
#
# NOTE: mesh already has 443/tcp (Signal) and 53/udp (DNS) permanently allowed.
#       This script only controls 80/tcp for apt repositories.

enable_rules() {
    echo "Enabling HTTP outbound for apt..."
    ufw allow out 80/tcp
}

disable_rules() {
    echo "Disabling HTTP outbound..."
    printf 'y\n' | ufw delete allow out 80/tcp >/dev/null 2>&1 || true
}

pip_install() {
    echo "Installing Python dependencies..."
    pip3 install -r /opt/Joi/execution/mesh/proxy/requirements.txt --break-system-packages
}

case "$1" in
    --enable)
        enable_rules
        ufw status | grep "80/tcp"
        echo "Done. Run 'apt update && apt upgrade'"
        ;;
    --disable)
        disable_rules
        echo "Done."
        ;;
    --pip)
        pip_install
        echo "Done."
        ;;
    --run)
        echo "Enabling updates, running apt + pip, then disabling..."
        enable_rules
        apt update && apt upgrade -y
        disable_rules
        pip_install
        echo "Done. Remember to --disable on gateway too."
        ;;
    --status)
        echo "UFW status:"
        ufw status verbose
        ;;
    *)
        echo "Usage: $0 --enable | --disable | --pip | --run | --status"
        echo ""
        echo "  --enable   Allow HTTP (80) outbound for apt"
        echo "  --disable  Remove HTTP outbound rule"
        echo "  --pip      Install/update Python dependencies (443 always open)"
        echo "  --run      Enable, update apt + pip, disable (all-in-one)"
        echo "  --status   Show UFW status"
        echo ""
        echo "NOTE: 443/tcp (Signal) and 53/udp (DNS) are permanently allowed."
        exit 1
        ;;
esac
