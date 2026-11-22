#!/bin/sh
set -eu

live() {
    redis-cli --no-auth-warning -h redis "$@"
}

dump() {
    work=/tmp/redis.rdb
    rm -f "$work"
    log=$(live --rdb "$work" 2>&1) || { echo "$log" >&2; exit 1; }
    redis-check-rdb "$work" > /dev/null
    name="redis-$(date -u +%Y%m%dT%H%M%SZ).rdb"
    cp "$work" "/backup/.$name"
    mv "/backup/.$name" "/backup/$name"
    rm -f "$work"
    find /backup -maxdepth 1 -name 'redis-*.rdb' | sort -r | tail -n +4 | xargs -r rm -f
    touch /tmp/backup-ok
    echo "Dump verified: $name"
}

restore() {
    file="/backup/$1"
    [ -f "$file" ] || { echo "No such dump: $1" >&2; exit 1; }
    redis-check-rdb "$file" > /dev/null

    work=$(mktemp -d)
    cp "$file" "$work/dump.rdb"
    redis-server --port 6380 --bind 0.0.0.0 --dir "$work" --dbfilename dump.rdb --appendonly no --save '' \
        --repl-diskless-sync-delay 0 --requirepass "$REDISCLI_AUTH" > "$work/server.log" 2>&1 &
    server=$!
    trap 'live replicaof no one > /dev/null; live config set masterauth "" > /dev/null; kill "$server"; rm -rf "$work"' EXIT

    tries=0
    until [ "$(redis-cli --no-auth-warning -p 6380 ping 2> /dev/null)" = PONG ]; do
        tries=$((tries + 1))
        [ "$tries" -lt 60 ] || { echo "The dump did not load" >&2; exit 1; }
        sleep 1
    done

    live config set masterauth "$REDISCLI_AUTH" > /dev/null
    live replicaof "$(hostname -i | cut -d' ' -f1)" 6380 > /dev/null
    tries=0
    until live info replication | grep -q 'master_link_status:up'; do
        tries=$((tries + 1))
        [ "$tries" -lt 120 ] || { echo "Redis did not take the dump" >&2; exit 1; }
        sleep 1
    done

    live replicaof no one > /dev/null
    tries=0
    while live info persistence | grep -qE 'aof_rewrite_in_progress:1|aof_rewrite_scheduled:1'; do
        tries=$((tries + 1))
        [ "$tries" -lt 120 ] || { echo "Redis did not save the restored data" >&2; exit 1; }
        sleep 1
    done
    echo "Restored Redis from $1"
}

loop() {
    if [ -n "$(find /backup -maxdepth 1 -name 'redis-*.rdb')" ]; then
        touch /tmp/backup-ok
    else
        sh "$0" dump
    fi
    while true; do
        sleep $((3600 - ($(date +%s) + 1800) % 3600))
        sh "$0" dump || echo "The Redis dump failed" >&2
    done
}

case "${1:-}" in
    loop) loop ;;
    dump) dump ;;
    list) find /backup -maxdepth 1 -name 'redis-*.rdb' -exec basename {} \; | sort ;;
    restore) restore "${2:?name a dump from list}" ;;
    *) echo "usage: sh $0 loop|dump|list|restore <dump>" >&2; exit 1 ;;
esac
