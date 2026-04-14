#!/bin/bash
# Re-encrypt Joi memory database with a new SQLCipher key.
#
# Usage: sudo ./rekey-memory.sh [SERVICE_USER]
#
# What this does:
#   1. Stops joi-api
#   2. Generates a new 256-bit hex key
#   3. Re-encrypts the database in-place (PRAGMA rekey — no data loss)
#   4. Saves the old key to <key_file>.old
#   5. Writes the new key to <key_file>
#   6. Starts joi-api
#
# The old key is kept at <key_file>.old for emergency recovery.
# Once you've verified Joi starts cleanly, remove it:
#   sudo rm /etc/joi/memory.key.old

set -euo pipefail

KEY_FILE="${JOI_MEMORY_KEY_FILE:-/etc/joi/memory.key}"
DB_FILE="${JOI_MEMORY_DB:-/var/lib/joi/memory.db}"
SERVICE_USER="${1:-joi}"
SQLCIPHER_BIN="${JOI_SQLCIPHER_BIN:-sqlcipher}"

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    echo "Error: This script must be run as root (sudo)"
    exit 1
fi

# Verify service user exists
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "Error: User '$SERVICE_USER' does not exist"
    exit 1
fi

# Verify sqlcipher is available
if ! command -v "$SQLCIPHER_BIN" &>/dev/null; then
    echo "Error: sqlcipher not found (looked for: $SQLCIPHER_BIN)"
    echo "Install with: apt install sqlcipher"
    exit 1
fi

# Verify key file exists
if [[ ! -f "$KEY_FILE" ]]; then
    echo "Error: Key file not found at $KEY_FILE"
    echo "Use generate-memory-key.sh for a fresh install."
    exit 1
fi

# Verify database exists
if [[ ! -f "$DB_FILE" ]]; then
    echo "Error: Database not found at $DB_FILE"
    exit 1
fi

OLD_KEY=$(tr -d '\r\n' < "$KEY_FILE")
if [[ -z "$OLD_KEY" ]]; then
    echo "Error: Key file is empty"
    exit 1
fi

echo "=== Joi Memory Rekey ==="
echo "Database:  $DB_FILE"
echo "Key file:  $KEY_FILE"
echo "Old key:   ${OLD_KEY:0:8}...(truncated)"
echo ""

# Verify old key actually opens the database before touching anything
echo "Verifying old key opens database..."
if ! "$SQLCIPHER_BIN" "$DB_FILE" <<EOF >/dev/null 2>&1
PRAGMA key = '$OLD_KEY';
SELECT count(*) FROM sqlite_master WHERE type='table';
EOF
then
    echo "Error: Could not open database with current key — aborting."
    exit 1
fi
echo "OK"

# Stop service
echo "Stopping joi-api..."
systemctl stop joi-api

# Generate new key
echo "Generating new 256-bit key..."
NEW_KEY=$(openssl rand -hex 32)

# Rekey the database in-place
echo "Rekeying database..."
if ! "$SQLCIPHER_BIN" "$DB_FILE" <<EOF
PRAGMA key = '$OLD_KEY';
PRAGMA rekey = '$NEW_KEY';
EOF
then
    echo "Error: Rekey failed — database unchanged. Starting joi-api with old key."
    systemctl start joi-api
    exit 1
fi

# Verify new key opens the database before committing it to file
echo "Verifying new key opens database..."
if ! "$SQLCIPHER_BIN" "$DB_FILE" <<EOF >/dev/null 2>&1
PRAGMA key = '$NEW_KEY';
SELECT count(*) FROM sqlite_master WHERE type='table';
EOF
then
    echo "Error: New key verification failed."
    echo "Attempting to restore old key via rekey..."
    if "$SQLCIPHER_BIN" "$DB_FILE" <<EOF2 >/dev/null 2>&1
PRAGMA key = '$NEW_KEY';
PRAGMA rekey = '$OLD_KEY';
EOF2
    then
        echo "Restored old key. Starting joi-api with old key."
    else
        echo "CRITICAL: Could not restore old key. Old key is still at $KEY_FILE."
        echo "Manual recovery needed."
    fi
    systemctl start joi-api
    exit 1
fi
echo "OK"

# Backup old key, write new key
echo "Saving old key to ${KEY_FILE}.old..."
cp -p "$KEY_FILE" "${KEY_FILE}.old"
chmod 600 "${KEY_FILE}.old"

umask 077
echo "$NEW_KEY" > "$KEY_FILE"
chmod 600 "$KEY_FILE"
chown "$SERVICE_USER:$SERVICE_USER" "$KEY_FILE"

echo "New key written to: $KEY_FILE"
echo "Old key saved to:   ${KEY_FILE}.old"
echo ""

# Start service
echo "Starting joi-api..."
systemctl start joi-api

echo ""
echo "=== Rekey complete ==="
echo "New key: ${NEW_KEY:0:8}...(truncated)"
echo ""
echo "Once you've verified Joi starts cleanly, remove the old key:"
echo "  sudo rm ${KEY_FILE}.old"
