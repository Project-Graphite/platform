#!/bin/sh
set -eu

if [ $# -ne 3 ]; then
  echo "usage: sh migrate-volume.sh <coolify volume> <project slug> <volume name>" >&2
  exit 1
fi

from=$1
project=$2
volume=$3
to="${project}_${volume}"

docker volume inspect "$from" >/dev/null
if docker volume inspect "$to" >/dev/null 2>&1; then
  echo "$to already exists; remove it first if you want to copy again." >&2
  exit 1
fi

docker volume create \
  --label "com.docker.compose.project=$project" \
  --label "com.docker.compose.volume=$volume" \
  "$to" >/dev/null
docker run --rm -v "$from":/from:ro -v "$to":/to alpine:3.22 sh -c 'cp -a /from/. /to/'
echo "Copied $from into $to."
