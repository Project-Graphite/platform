#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ] || [ -z "${DEPLOY_TOKEN:-}" ] || [ -z "${CF_DNS_API_TOKEN:-}" ]; then
  echo "usage: sudo DEPLOY_TOKEN=<token> CF_DNS_API_TOKEN=<token> sh bootstrap.sh" >&2
  exit 1
fi

source=https://raw.githubusercontent.com/project-graphite/platform/main

docker compose version
python3 --version
id deploy >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin --groups docker deploy

install -d -o deploy -g deploy /srv/graphite /srv/graphite/bin /srv/graphite/edge /srv/graphite/edge/routes
curl -fsSL -o /srv/graphite/bin/graphite-up "$source/host/graphite-up"
curl -fsSL -o /srv/graphite/bin/agent.py "$source/host/agent.py"
curl -fsSL -o /srv/graphite/edge/compose.yaml "$source/edge/compose.yaml"
curl -fsSL -o /srv/graphite/edge/traefik.yml "$source/edge/traefik.yml"
curl -fsSL -o /srv/graphite/edge/routes/edge.yml "$source/edge/agent.yml"
curl -fsSL -o /etc/systemd/system/graphite-agent.service "$source/host/graphite-agent.service"
printf 'CF_DNS_API_TOKEN=%s\n' "$CF_DNS_API_TOKEN" > /srv/graphite/edge/secrets.env
chmod 755 /srv/graphite/bin/graphite-up
chown -R deploy:deploy /srv/graphite
chmod 600 /srv/graphite/edge/secrets.env

docker network inspect edge >/dev/null 2>&1 || docker network create edge
bind="$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}')"
printf 'DEPLOY_TOKEN=%s\nBIND=%s\n' "$DEPLOY_TOKEN" "$bind" > /etc/graphite-agent.env
chmod 600 /etc/graphite-agent.env

systemctl daemon-reload
systemctl enable --now graphite-agent
systemctl restart graphite-agent
su deploy -s /bin/sh -c "/srv/graphite/bin/graphite-up edge"
echo "The edge and the deploy agent are running on $bind:9000."
