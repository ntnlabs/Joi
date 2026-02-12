#!/bin/bash
# Migrate existing unencrypted Joi database to SQLCipher encrypted format
#
# Usage: sudo ./migrate-to-encrypted.sh
#
# Prerequisites:
#   1. sqlcipher CLI tool installed: apt install sqlcipher
#   2. Key file exists: /etc/joi/memory.key
#   3. joi-api service is stopped
#
# This script:
#   1. Creates a backup of the original database
#   2. Creates a new encrypted database
#   3. Copies all data from old to new
#   4. Replaces the original with the encrypted version

set -euo pipefail

DB_PATH="${JOI_MEMORY_DB:-/var/lib/joi/memory.db}"
KEY_FILE="${JOI_MEMORY_KEY_FILE:-/etc/joi/memory.key}"
BACKUP_PATH="${DB_PATH}.unencrypted.backup"
TEMP_PATH="${DB_PATH}.encrypted.tmp"

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    echo "Error: This script must be run as root (sudo)"
    exit 1
fi

# Check sqlcipher is installed
if ! command -v sqlcipher &> /dev/null; then
    echo "Error: sqlcipher CLI not installed"
    echo "Install with: apt install sqlcipher"
    exit 1
fi

# Check key file exists
if [[ ! -f "$KEY_FILE" ]]; then
    echo "Error: Key file not found at $KEY_FILE"
    echo "Generate one with: ./generate-memory-key.sh"
    exit 1
fi

# Check database exists
if [[ ! -f "$DB_PATH" ]]; then
    echo "Error: Database not found at $DB_PATH"
    echo "Nothing to migrate - the database will be created encrypted on first run."
    exit 0
fi

# Check service is stopped
if systemctl is-active --quiet joi-api 2>/dev/null; then
    echo "Error: joi-api service is running"
    echo "Stop it first: sudo systemctl stop joi-api"
    exit 1
fi

# Read key
KEY=$(cat "$KEY_FILE")

echo "=== SQLCipher Migration ==="
echo "Database: $DB_PATH"
echo "Key file: $KEY_FILE"
echo ""

# Create backup
echo "1. Creating backup..."
cp "$DB_PATH" "$BACKUP_PATH"
# Also backup WAL and SHM if they exist
[[ -f "${DB_PATH}-wal" ]] && cp "${DB_PATH}-wal" "${BACKUP_PATH}-wal"
[[ -f "${DB_PATH}-shm" ]] && cp "${DB_PATH}-shm" "${BACKUP_PATH}-shm"
echo "   Backup created at: $BACKUP_PATH"

# Remove temp file if exists from previous failed attempt
rm -f "$TEMP_PATH" "${TEMP_PATH}-wal" "${TEMP_PATH}-shm"

# Create encrypted copy using sqlcipher
echo "2. Creating encrypted database..."
sqlcipher "$DB_PATH" <<EOF
ATTACH DATABASE '$TEMP_PATH' AS encrypted KEY '$KEY';
SELECT sqlcipher_export('encrypted');
DETACH DATABASE encrypted;
EOF

# Verify the encrypted database works
echo "3. Verifying encrypted database..."
TABLE_COUNT=$(sqlcipher "$TEMP_PATH" "PRAGMA key = '$KEY'; SELECT count(*) FROM sqlite_master WHERE type='table';" 2>/dev/null | tail -1)
if [[ -z "$TABLE_COUNT" || "$TABLE_COUNT" -lt 1 ]]; then
    echo "Error: Encrypted database verification failed"
    rm -f "$TEMP_PATH"
    exit 1
fi
echo "   Verification passed ($TABLE_COUNT tables)"

# Replace original with encrypted
echo "4. Replacing original database..."
rm -f "$DB_PATH" "${DB_PATH}-wal" "${DB_PATH}-shm"
mv "$TEMP_PATH" "$DB_PATH"

echo ""
echo "=== Migration Complete ==="
echo ""
echo "Original database backed up to: $BACKUP_PATH"
echo "Database is now encrypted with SQLCipher."
echo ""
echo "Next steps:"
echo "  1. Restart joi-api: sudo systemctl start joi-api"
echo "  2. Verify it works, then optionally delete the backup:"
echo "     sudo rm $BACKUP_PATH"
