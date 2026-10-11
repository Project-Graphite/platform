#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ] || [ $# -ne 1 ] || [ -z "${TUNNEL_TOKEN:-}" ]; then
  echo "usage: sudo TUNNEL_TOKEN=<token> sh bootstrap.sh '<deploy ssh public key>'" >&2
  exit 1
fi

source=https://raw.githubusercontent.com/project-graphite/platform/main

docker compose version
id deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash --groups docker deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
printf '%s\n' "$1" > /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys

install -d -o deploy -g deploy /srv/graphite /srv/graphite/bin /srv/graphite/edge /srv/graphite/edge/routes
curl -fsSL -o /srv/graphite/bin/graphite-up "$source/host/graphite-up"
curl -fsSL -o /srv/graphite/edge/compose.yaml "$source/edge/compose.yaml"
curl -fsSL -o /srv/graphite/edge/traefik.yml "$source/edge/traefik.yml"
chmod 755 /srv/graphite/bin/graphite-up
printf 'TUNNEL_TOKEN=%s\n' "$TUNNEL_TOKEN" > /srv/graphite/edge/secrets.env
chown -R deploy:deploy /srv/graphite
chmod 600 /srv/graphite/edge/secrets.env

docker network inspect edge >/dev/null 2>&1 || docker network create edge
su deploy -c "/srv/graphite/bin/graphite-up edge"
echo "The edge is running. Next: configure the tunnel and Access, then run the platform Deploy workflow."
