#!/bin/sh
# Restore: ./scripts/restore.sh backups/xmonitor_20261110_120000.dump
# Stops the app, replaces the database contents with the dump, starts the app again.
set -eu
cd "$(dirname "$0")/.."
dump="${1:?usage: scripts/restore.sh <file.dump>}"
[ -f "$dump" ] || { echo "file not found: $dump"; exit 1; }
printf "This will REPLACE the current database with %s. Continue? [y/N] " "$dump"
read -r answer
[ "$answer" = "y" ] || [ "$answer" = "Y" ] || exit 1
docker compose stop app
docker compose exec -T db sh -c 'pg_restore --clean --if-exists --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$dump"
docker compose start app
echo "Restored from $dump"
