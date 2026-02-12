#!/bin/bash
# Generate SQLCipher encryption key for Joi memory database
#
# Usage: sudo ./generate-memory-key.sh
#
# This creates /etc/joi/memory.key with a random 64-character key.
# The file is owned by root with mode 600 (readable only by root).
# The joi service should run as root or the file owner should be adjusted.

set -euo pipefail

KEY_FILE="${JOI_MEMORY_KEY_FILE:-/etc/joi/memory.key}"
KEY_DIR=$(dirname "$KEY_FILE")

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    echo "Error: This script must be run as root (sudo)"
    exit 1
fi

# Check if key already exists
if [[ -f "$KEY_FILE" ]]; then
    echo "Error: Key file already exists at $KEY_FILE"
    echo "To regenerate, first remove the existing key (this will make existing database unreadable!):"
    echo "  sudo rm $KEY_FILE"
    exit 1
fi

# Create directory if needed
if [[ ! -d "$KEY_DIR" ]]; then
    echo "Creating directory: $KEY_DIR"
    mkdir -p "$KEY_DIR"
    chmod 700 "$KEY_DIR"
fi

# Generate random key (64 hex characters = 256 bits)
echo "Generating 256-bit encryption key..."
KEY=$(openssl rand -hex 32)

# Write key to file with strict permissions
umask 077
echo "$KEY" > "$KEY_FILE"
chmod 600 "$KEY_FILE"

echo "Key written to: $KEY_FILE"
echo "Permissions: $(stat -c '%a' "$KEY_FILE")"
echo ""
echo "IMPORTANT: Back up this key securely! Without it, the database cannot be decrypted."
echo ""
echo "Next steps:"
echo "  1. Install SQLCipher: pip install sqlcipher3-binary"
echo "  2. Restart joi-api service"
echo "  3. The database will be encrypted on first write"
echo ""
echo "To migrate an existing unencrypted database, see docs/sqlcipher-migration.md"
