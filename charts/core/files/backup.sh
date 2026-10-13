#!/bin/sh
set -eu
while true; do
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  dump="/backups/$PGDATABASE-$stamp.dump"
  pg_dump -Fc -f "$dump.partial"
  pg_restore --list "$dump.partial" > /dev/null
  mv "$dump.partial" "$dump"
  for volume in /data/*; do
    if [ -d "$volume" ]; then
      tar -czf "/backups/$(basename "$volume")-$stamp.tar.gz" -C /data "$(basename "$volume")"
    fi
  done
  find /backups -type f -mtime "+$BACKUP_KEEP_DAYS" -delete
  sleep 86400
done
