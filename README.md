# platform

The control plane for Project Graphite: what each project is, and how and where it runs. Every
deployment happens from here.

| Path | What it holds | Written by |
| :--- | :--- | :--- |
| `projects/<slug>.yml` | the registry: the Hub card, host and deploy kind | the registry sync from each repository's `.graphite.yml` |
| `schema/project.schema.json` | the one definition of a registry entry, used here and by the Hub | maintainers |
| `deploy/<slug>/compose.yaml` | what a project runs, extending the fragments in `shared/` | contributors, by pull request |
| `deploy/<slug>/values` | `IMAGE_TAG`, and only the settings that differ from a default | CI writes `IMAGE_TAG`; contributors the rest |
| `deploy/<slug>/files/` | scripts and config files its services mount | contributors, by pull request |
| `deploy/host.yaml` | the VPS memory budget | maintainers |
| `shared/` | `app.yaml`, `postgres.yaml`, `redis.yaml`, `redis-password.yaml`: the hardening, limits, health checks and defaults every service extends | maintainers |
| `edge/` | Traefik (Let's Encrypt through Cloudflare DNS), Sablier, and the shared backup service | maintainers |
| `host/` | VPS bootstrap, the deploy agent, `graphite-agent.service`, `graphite-up` and `graphite-report` | maintainers |
| `scripts/` | `deploy.py`, `agent.py`, `cloudflare.py`, `check-memory.py`, `check-values.py`, `sync.py`, `validate.py`, `uptime.py` | maintainers |

Secret values never live here. They are entered in the console's Secrets page and reach the VPS as
`/etc/graphite/<slug>.env`; a compose file names them as `${NAME:?}`.

## How a deploy happens

1. A project's CI publishes `ghcr.io/project-graphite/<repo>/<service>:sha-<short>` after its
   checks pass, then commits `IMAGE_TAG=sha-<short>` into `deploy/<slug>/values`.
2. That push runs **Deploy**, as does any push to `main` under `deploy/`, `edge/`, `host/` or
   `shared/`. A change under `deploy/<slug>/` deploys that project, a change under `shared/`
   deploys every compose project, and a change under `edge/` or `host/` redeploys the edge and
   the backup service. An automated push may change only `IMAGE_TAG` and `ENABLED=false` lines.
3. For a compose project, `deploy.py` renders `compose.yaml` with `docker compose config`, with
   `PROJECT` from the folder name, `HOST` from the registry and every `${NAME:?}` it cannot
   supply stubbed, and posts the rendered file, the values, the mounted files, the Traefik route
   and the list of required secret names to the deploy agent at `deploy.project-graphite.com`,
   signed with `DEPLOY_TOKEN`. No secret value is read or sent.
4. The agent verifies the signature, refuses a compose file that asks for host access, and
   answers `waiting on secrets: …` without touching anything when `/etc/graphite/<slug>.env`
   lacks a required name. Otherwise it writes the folder under `/srv/graphite/<slug>/`, runs
   `graphite-up`, which is `docker compose up -d --wait --remove-orphans` with the values and
   the env file, publishes the route to Traefik, and reports the rollout to the Hub.
5. **Deploy** points the hostname at the VPS: a DNS-only A record, or proxied through
   Cloudflare when the values set `PROXIED=true`, as the apex and the console do.

A project whose values hold `ENABLED=false`, or whose `deploy/<slug>/` folder is gone while the
registry still says `compose`, is taken down: the agent removes the route and stops its containers.
Its volumes and env file stay.

Static sites still deploy to Cloudflare Pages: `deploy.py pages-info` resolves the recorded
`IMAGE_TAG` to a commit, the workflow builds the registry's `deploy.source` on Node 24, and
`deploy.py pages-site` creates the direct-upload Pages project if it is missing, attaches the host
and points a proxied CNAME at `<slug>.pages.dev` before `wrangler pages deploy`. Only
`digital-to-physical` uses this path.

## Projects

A project on the VPS is a folder:

```
deploy/strata/
  compose.yaml   what runs, extending shared/
  values         IMAGE_TAG and the settings that differ from a default
  files/         scripts its services mount, if any
```

The smallest project is one service that extends `shared/app.yaml`:

```yaml
# compose.yaml
services:
  app:
    extends: {file: ../../shared/app.yaml, service: app}
    image: ghcr.io/project-graphite/<repo>/app:${IMAGE_TAG:?}
    environment:
      API_KEY: ${API_KEY:?}
    healthcheck:
      test: [CMD, wget, -qO-, http://127.0.0.1:3000/health]

networks:
  edge: {external: true}
```

```
# values
IMAGE_TAG=sha-1a2b3c4
APP_MEMORY=512M
```

`shared/app.yaml` gives the `app` service `init`, `restart: unless-stopped`, rotated logs,
`no-new-privileges`, a health check every 30 s, `APP_URL=https://${HOST}`, the `edge` network
under the project's name, and a limit of `APP_MEMORY` (256M), `APP_RESERVE` (64M) and `APP_CPUS`
(0.5) on port `APP_PORT` (3000). The service that joins the edge network as `${PROJECT}` gets the
registry host. `shared/postgres.yaml` adds PostgreSQL 18 as `${PROJECT}` with `POSTGRES_PASSWORD`,
labelled for backup; `shared/redis.yaml` adds Redis 8 with `REDIS_MAXMEMORY` (48mb) and
`REDIS_POLICY` (`allkeys-lru`), and `shared/redis-password.yaml` the same behind `REDIS_PASSWORD`.
Each has its own `*_MEMORY`, `*_RESERVE` and `*_CPUS`.

Rules the fragments set, which **PR** enforces:

- A secret or setting is `${NAME:?}` where it is required and `${NAME:-default}` where it is not,
  so `docker compose config` enforces presence and a default is written once.
- A `values` line equal to its default is refused; `IMAGE_TAG` must be a `sha-` tag, and a
  `ghcr.io` image is tagged `${IMAGE_TAG:?}` or `sha-`.
- Every service has a memory limit, and the reservations fit `deploy/host.yaml`.
- A compose file may not use `privileged`, `pid`, `ports`, `build`, `env_file`, host paths or
  external volumes; the agent refuses them. The console, the edge and the backup service are the
  exceptions.
- Backups are labels: `graphite.backup.postgres: "true"` on a PostgreSQL container, and
  `graphite.backup.volumes: name,name` on a service for the named volumes.

`deploy/strata/` shows PostgreSQL, Redis with a password and a files volume; `deploy/pixelpocket/`
shows plain services with mounted scripts; `deploy/graphite-hub/` shows a proxied host and Redis
with `noeviction`.

A static site needs only `deploy/<slug>/values` with `IMAGE_TAG`, and `ENABLED=false` to stop
new uploads. Its build comes from the registry's `deploy.source`.

## The edge and backups

`edge/compose.yaml` runs Traefik on 80 and 443 with HTTP/3, redirecting HTTP, with certificates
from Let's Encrypt through a Cloudflare DNS challenge (`CF_DNS_API_TOKEN`) and one route file per
project from `/srv/graphite/edge/routes/`; and Sablier, which no route uses at present. `edge/agent.yml`
is the route to the deploy agent.

`edge/backup.yaml` runs `backup.py` as its own compose project. Every day at 03:00 UTC, or on
the schedule set on the console's Backups page, it dumps every container labelled
`graphite.backup.postgres` with `pg_dump` through `docker exec`, packs every volume a service
lists under `graphite.backup.volumes`, taking a consistent copy of any SQLite file inside, and
writes `/var/lib/graphite/backups/<project>/<stamp>.tar` with a manifest of sizes and checksums.
Each archive is reopened and verified after it is written: checksums, `pg_restore --list`,
`PRAGMA integrity_check` on every SQLite copy, every tar listed to its end. A failed check marks
the archive failed and raises a console alert. Archives are kept 14 days. The console orders runs,
verifications and restores through files under `.requests/`.

## Workflows

| Workflow | Runs on | What it does |
| :--- | :--- | :--- |
| **Deploy** | push to `main` under `deploy/`, `edge/`, `host/` or `shared/`, or by hand with a slug, `edge` or `all` | plans what changed, deploys the VPS through the agent, builds and uploads static sites, points DNS |
| **PR** | every pull request | `validate.py` on the registry, `deploy.py check`, `check-memory.py`, `check-values.py`, gitleaks, and the shared PR checks |
| **Sync registry** | every hour, a `sync-registry` repository dispatch from the Hub when a `.graphite.yml` changes, or by hand | rereads every repository's `.graphite.yml` into `projects/`, opens `automation/sync-project-registry`, waits for **PR** and merges it with the app's bypass |
| **Uptime** | every 10 minutes | probes every enabled host and the agent from GitHub and opens or closes a `Down: <host>` issue |

## Settings

The `production` environment only deploys from `main`.

| Secret | Value |
| :--- | :--- |
| `DEPLOY_TOKEN` | the deploy agent's token, the same as `/agent/DEPLOY_TOKEN` in the console |
| `CLOUDFLARE_API_TOKEN` | Zone → DNS → Edit and Account → Cloudflare Pages → Edit |

| Variable | Value |
| :--- | :--- |
| `DEPLOY_URL` | `https://deploy.project-graphite.com` |
| `VPS_IPV4` | the VPS address the DNS records point at |
| `CLOUDFLARE_ACCOUNT_ID` | the account that holds the Pages projects |
| `CLOUDFLARE_ZONE_ID` | the `project-graphite.com` zone |

Deploy skips the VPS when `DEPLOY_URL` is empty and static sites when `CLOUDFLARE_ACCOUNT_ID` is
empty. The organisation secrets `PLATFORM_APP_CLIENT_ID` and `PLATFORM_APP_PRIVATE_KEY` are the
platform GitHub App's credentials, used by **PR** and **Sync registry** to read the organisation.

## Setting up the VPS

The VPS needs Docker with the Compose plugin and `python3`.

```sh
curl -fsSLO https://raw.githubusercontent.com/project-graphite/platform/main/host/bootstrap.sh
sudo DEPLOY_TOKEN=<token> CF_DNS_API_TOKEN=<certificate token> sh bootstrap.sh
```

The certificate token is the one Traefik uses for Let's Encrypt DNS challenges and needs
Zone → Zone → Read and Zone → DNS → Edit. The script creates the `deploy` user,
`/srv/graphite/{bin,edge,edge/routes}`, `/etc/graphite/agent.env` with `DEPLOY_TOKEN` and `BIND`,
the edge's `secrets.env`, the `edge` network and `graphite-agent.service`, then starts the edge.
The agent listens on the Docker bridge gateway at port 9000, and Traefik routes
`deploy.project-graphite.com` to it. Run the script again to update the agent; `host/` files reach
the VPS only through it.

## Running it

```sh
pip install pyyaml jsonschema
python scripts/validate.py
python scripts/deploy.py check
python scripts/check-memory.py
python scripts/check-values.py
```

`validate.py` needs `GH_TOKEN` to check owners against the organisation; the others need Docker.
Deploy uses the other subcommands: `plan`, `apply`, `pages-info <slug>` and `pages-site <slug>`.
Deploy runs on demand from Actions → **Deploy** → Run workflow, with a project slug, `edge` or `all`.

## Editor support

```json
{
  "yaml.schemas": {
    "./schema/project.schema.json": "projects/*.yml"
  }
}
```
