#!/bin/sh
# Manual backup: ./scripts/backup.sh  ->  backups/xmonitor_YYYYmmdd_HHMMSS.dump
set -eu
cd "$(dirname "$0")/.."
mkdir -p backups
file="backups/xmonitor_$(date +%Y%m%d_%H%M%S).dump"
docker compose exec -T db sh -c 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$file"
echo "Backup written to $file ($(du -h "$file" | cut -f1))"
