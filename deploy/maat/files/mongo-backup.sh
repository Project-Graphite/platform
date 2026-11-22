#!/bin/bash
set -euo pipefail

login=(--host mongo --username maat --password "$MONGO_PASSWORD" --authenticationDatabase admin)

dump() {
    local work=/tmp/mongo.archive.gz name
    mongodump --quiet "${login[@]}" --db maat --archive="$work" --gzip
    gzip -t "$work"
    mongorestore --quiet "${login[@]}" --archive="$work" --gzip --dryRun
    name="mongo-$(date -u +%Y%m%dT%H%M%SZ).archive.gz"
    cp "$work" "/backup/.$name"
    mv "/backup/.$name" "/backup/$name"
    rm -f "$work"
    find /backup -maxdepth 1 -name 'mongo-*.archive.gz' | sort -r | tail -n +4 | xargs -r rm -f
    touch /tmp/backup-ok
    echo "Dump verified: $name"
}

restore() {
    local file="/backup/$1"
    [ -f "$file" ] || { echo "No such dump: $1" >&2; exit 1; }
    gzip -t "$file"
    mongorestore --quiet "${login[@]}" --archive="$file" --gzip --dryRun
    mongosh --quiet "${login[@]}" maat --eval 'db.dropDatabase()' > /dev/null
    mongorestore --quiet "${login[@]}" --archive="$file" --gzip
    echo "Restored MongoDB from $1"
}

loop() {
    if [ -n "$(find /backup -maxdepth 1 -name 'mongo-*.archive.gz')" ]; then
        touch /tmp/backup-ok
    else
        bash "$0" dump
    fi
    while true; do
        sleep $((3600 - ($(date +%s) + 1800) % 3600))
        bash "$0" dump || echo "The MongoDB dump failed" >&2
    done
}

case "${1:-}" in
    loop) loop ;;
    dump) dump ;;
    list) find /backup -maxdepth 1 -name 'mongo-*.archive.gz' -exec basename {} \; | sort ;;
    restore) restore "${2:?name a dump from list}" ;;
    *) echo "usage: bash $0 loop|dump|list|restore <dump>" >&2; exit 1 ;;
esac
