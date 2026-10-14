# platform

The control plane for Project Graphite: who the members are, what each project is, and how and
where it runs. Every deployment happens from here.

| Path | What it holds | Written by |
| :--- | :--- | :--- |
| `people/<handle>.yml` | member profiles | registry sync, or by hand with `profile: manual` |
| `projects/<slug>.yml` | the Hub card, host and deploy kind | registry sync from each `.graphite.yml` |
| `deploy/<slug>/core.yaml` | what a project runs: its services and built-in components | contributors, by pull request |
| `deploy/<slug>/values.yaml` | its settings: environment, secret names, memory | contributors, by pull request |
| `deploy/<slug>/files/` | scripts and config files its services mount | contributors, by pull request |
| `deploy/chart.yaml` | the `core` chart version every project uses | maintainers |
| `tags/<slug>.json` | the image or site build CI last published | CI only |
| `charts/core/` | the chart that renders a project into a Compose project and its route; its `values.yaml` lists every option and default | maintainers |
| `edge/` | Traefik (Let's Encrypt through Cloudflare DNS), Sablier and the wake scheduler on the VPS | maintainers |
| `host/` | VPS bootstrap, the deploy agent, `graphite-agent.service` and `graphite-up` | maintainers |

Secret values never live here. They are in the owners-only `secrets` repository, which only an
organisation owner can change and apply.

## How a deploy happens

1. A project's CI publishes its images, or records its static-site commit, in `tags/<slug>.json`.
2. That push runs **Deploy**, as does any push to `main` under `charts/core/`, `deploy/`, `edge/`,
   `host/` or `projects/`. A change to `deploy/<slug>/`, `tags/<slug>.json` or
   `projects/<slug>.yml` deploys that project; a change to `edge/` or `host/` redeploys the edge.
   Deploy merges `deploy/<slug>/core.yaml` and `values.yaml`, renders them with the `core` chart
   version in `deploy/chart.yaml` and posts the result, signed with `DEPLOY_TOKEN`, to the deploy agent at `deploy.project-graphite.com`. The
   agent checks the project's secrets exist, runs `docker compose up --wait` and publishes the
   route to Traefik. Deploy then points the hostname at the VPS with a DNS-only record
   (`proxied: true` sends it through Cloudflare instead).
3. Static sites are built here from the recorded commit. `deploy.py pages-site` creates the
   direct-upload Pages project if it is missing, attaches the custom domain and points a proxied
   CNAME at `<slug>.pages.dev`; it refuses a Git-connected project of the same name. The build is
   uploaded with `wrangler pages deploy` (Wrangler 4.149.0).

**PR** runs on every pull request. `deploy.py check` renders every project, not the edge, and
merging deploys what changed. A chart change tags `core-v<version>` and deploys nothing until
`deploy/chart.yaml` moves to it, which redeploys every project.

## Projects

A project on the VPS is a folder:

```
deploy/strata/
  core.yaml      what runs
  values.yaml    how it is configured
  files/         scripts its services mount, if any
```

The smallest project is one service with a port, which gets the project's hostname:

```yaml
# core.yaml
services:
  app:
    image: ghcr.io/project-graphite/<repo>/app
    port: 3000
    health: /health
```

```yaml
# values.yaml
app:
  memory: 256M
  env:
    APP_URL: https://<slug>.project-graphite.com
  secrets: [API_KEY]
```

`values.yaml` uses the names from `core.yaml`, a service or a built-in component, and its fields
are merged over them; it can also set `enabled: false`. **PR** fails when it names something
`core.yaml` does not define. Either file can hold any field: by convention, what a project is goes in
`core.yaml` and what you tune goes in `values.yaml`.

Every option and its default is in [`charts/core/values.yaml`](charts/core/values.yaml). A project
writes only what differs.

| Key in `core.yaml` | Gives |
| :--- | :--- |
| `services.<name>` | any container: image, port, health, env, secrets, volumes, files, command and the rest |
| `postgres` | a PostgreSQL server; every service gets `DATABASE_URL` and `POSTGRES_*` and waits for it |
| `redis` | a Redis server; every service gets `REDIS_URL` and waits for it |
| `backup` | a daily `pg_dump` into the `backups` volume, plus a tar of the volumes it lists |
| `sleep` | stops the project after `after` without requests; `wake` starts it on a schedule |
| `proxied` | sends the hostname through Cloudflare's proxy |

A service's `secrets` must exist in `secrets/<slug>.env` and are passed through under the same
name; `optional` names are passed when present. `env` holds plain settings, and `${NAME}` or
`${NAME:-default}` in it reads the secrets file; a name without a default must be in some service's
`secrets`. `files` mounts a file from `deploy/<slug>/files/`. With several ported services, set
`public: true` on the one that gets the hostname.

A service can answer on more hostnames with `hosts: [api.<slug>.project-graphite.com]`; Deploy
points each at the VPS and routes it to that service's `port`, and **PR** fails when a hostname is
outside `project-graphite.com` or already used. `jobs` runs commands inside a running service on a
cron schedule, in UTC:

```yaml
worker:
  image: ghcr.io/project-graphite/<repo>/worker
  jobs:
    - {name: nightly, schedule: "0 3 * * *", command: [python, -m, reports]}
```

The edge's scheduler (Ofelia) runs them with `docker exec`, as root unless the job sets `user`. A
sleeping project can't have jobs, since its containers are stopped when they fall due.

An image with `:` or `@` is used as written. Any other image gets `tag`, else the tag in
`tags/<slug>.json`, else `latest`. To roll back, set `<service>: {tag: sha-1a2b3c4}` in
`values.yaml`.

`deploy/strata/` shows `postgres`, `redis` and `backup`; `deploy/pixelpocket/` shows plain services
with mounted scripts; `deploy/graphite-message-lab/` shows `sleep`.

A static site needs only `deploy/<slug>/values.yaml` with `enabled: true`, and an optional `tag`
pin. Its build comes from the registry's `deploy.source`.

`deploy/host.yaml` holds the VPS memory budget. **PR** fails when the rendered reservations exceed
`memory` minus `system`.

## The core chart

`charts/core` is versioned by `Chart.yaml`. **Deploy** tags `core-v<version>` on the first push of
a new version, and `deploy/chart.yaml` picks the version every project renders with. A project can
stay behind with `core: <version>` in its `core.yaml`. A pull request that changes the chart without
bumping its version fails.

## Workflows

| Workflow | Runs on | What it does |
| :--- | :--- | :--- |
| **Main** | push to `main` under `projects/`, `people/`, `schema/` or `scripts/` | validates and compiles the registry and commits it to `graphite-hub` as `site/src/data/registry.json` |
| **Sync registry** | `sync-registry` repository dispatch, or by hand | syncs each repository's `.graphite.yml` and members' GitHub profiles into `projects/` and `people/`, opens a pull request and merges it once **PR** passes |
| **Deploy** | push to `main` (see above), or by hand | releases the core chart, deploys the VPS, builds and uploads static sites |
| **PR** | every pull request | validates the registry, runs `deploy.py check`, scans for secrets and runs the shared PR checks |

## Settings

The `production` environment only deploys from `main`.

| Secret | Value |
| :--- | :--- |
| `DEPLOY_TOKEN` | the deploy agent's token, the same as on the VPS and in `secrets` |
| `CLOUDFLARE_API_TOKEN` | Zone → DNS → Edit and Account → Cloudflare Pages → Edit |

| Variable | Value |
| :--- | :--- |
| `DEPLOY_URL` | `https://deploy.project-graphite.com` |
| `VPS_IPV4` | the VPS address the DNS records point at |
| `CLOUDFLARE_ACCOUNT_ID` | the account that holds the Pages projects |
| `CLOUDFLARE_ZONE_ID` | the `project-graphite.com` zone |

Deploy skips the VPS when `DEPLOY_URL` is empty and static sites when `CLOUDFLARE_ACCOUNT_ID` is
empty. The repository secrets `PLATFORM_APP_CLIENT_ID` and `PLATFORM_APP_PRIVATE_KEY` are the
platform GitHub App's credentials, used by **Main** and **Sync registry**.

## Setting up the VPS

The VPS needs Docker with the Compose plugin and `python3`.

```sh
curl -fsSLO https://raw.githubusercontent.com/project-graphite/platform/main/host/bootstrap.sh
sudo DEPLOY_TOKEN=<token> CF_DNS_API_TOKEN=<certificate token> sh bootstrap.sh
```

The certificate token is the one Traefik uses for Let's Encrypt DNS challenges and needs
Zone → Zone → Read and Zone → DNS → Edit. The script creates the `deploy` user,
`/srv/graphite/{bin,edge,edge/routes}`, `/etc/graphite-agent.env` with `DEPLOY_TOKEN` and `BIND`,
`edge/secrets.env`, the `edge` network and `graphite-agent.service`, then starts the edge. The
agent listens on the Docker bridge gateway at port 9000, and Traefik routes
`deploy.project-graphite.com` to it (`edge/agent.yml`).

## Running it

```sh
pip install pyyaml jsonschema
python scripts/validate.py
python scripts/compile.py registry.json
git fetch --tags
python scripts/deploy.py check
```

`deploy.py check` needs Helm 3, Docker and the `core-v*` tags. Deploy uses the other subcommands:
`release-core`, `plan`, `apply`, `pages-info <slug>` and `pages-site <slug>`.
`python scripts/agent.py secrets <name> < secrets.env` sends a secrets file to the agent; it needs
`DEPLOY_TOKEN` and `DEPLOY_URL`. Deploy runs on demand from Actions → **Deploy** → Run workflow,
with a project slug, `edge` or `all`.

## Editor support

```json
{
  "yaml.schemas": {
    "./schema/project.schema.json": "projects/*.yml",
    "./schema/person.schema.json": "people/*.yml"
  }
}
```

`charts/core/values.schema.json` checks a project after Deploy merges its two files and adds `slug`,
so it does not validate `core.yaml` or `values.yaml` on their own.
