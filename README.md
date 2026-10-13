# platform

The control plane for Project Graphite: who the members are, what each project is, and how and
where it runs. Every deployment happens from here.

| Path | What it holds | Written by |
| :--- | :--- | :--- |
| `people/<handle>.yml` | member profiles | registry sync, or by hand with `profile: manual` |
| `projects/<slug>.yml` | the Hub card, host and deploy kind | registry sync from each `.graphite.yml` |
| `deploy/<slug>/values.yaml` | how a project runs in production | contributors, by pull request |
| `tags/<slug>.json` | the image or site build CI last published | CI only |
| `charts/core/` | the chart that renders values into a Compose project and its route | maintainers |
| `edge/` | Traefik (Let's Encrypt through Cloudflare DNS), Sablier and the wake scheduler on the VPS | maintainers |
| `host/` | VPS bootstrap, the deploy agent, `graphite-agent.service` and `graphite-up` | maintainers |

Secret values never live here. They are in the owners-only `secrets` repository, which only an
organisation owner can change and apply.

## How a deploy happens

1. A project's CI publishes its images, or records its static-site commit, in `tags/<slug>.json`.
2. That push runs **Deploy**, as does any push to `main` under `charts/core/`, `deploy/`, `edge/`,
   `host/` or `projects/`. A change to `deploy/<slug>/`, `tags/<slug>.json` or
   `projects/<slug>.yml` deploys that project; a change to `edge/` or `host/` redeploys the edge.
   Deploy renders `deploy/<slug>/values.yaml` with the `core` chart version the values pin and
   posts it, signed with `DEPLOY_TOKEN`, to the deploy agent at `deploy.project-graphite.com`. The
   agent checks the project's secrets exist, runs `docker compose up --wait` and publishes the
   route to Traefik. Deploy then points the hostname at the VPS with a DNS-only record
   (`proxied: true` sends it through Cloudflare instead).
3. Static sites are built here from the recorded commit. `deploy.py pages-site` creates the
   direct-upload Pages project if it is missing, attaches the custom domain and points a proxied
   CNAME at `<slug>.pages.dev`; it refuses a Git-connected project of the same name. The build is
   uploaded with `wrangler pages deploy` (Wrangler 4.149.0).

**PR** runs on every pull request. `deploy.py check` renders every project's values, not the edge,
and merging deploys what changed. A chart-only change tags `core-v<version>` and deploys nothing
until a project bumps `core:`.

## Values

```yaml
core: 1.0.0                # required; core chart version
enabled: true              # true | false
proxied: false             # true | false
secrets: [API_KEY]         # required keys in the secrets repository
sleep:                     # optional
  after: 30m               # duration
  wake: "0 */6 * * *"      # cron | "@every 6h"; requires after
  wakeFor: 15m             # duration; requires wake; default 15m

services:                  # required, at least one
  app:
    image: ghcr.io/project-graphite/<repo>/app   # required
    tag: sha-1a2b3c4        # optional pin; omitted follows CI
    command: [node, server.js]
    entrypoint: [sh, -c]
    port: 3000             # required on the public service
    public: true           # exactly one service when the project has a host
    env:
      API_KEY: ${API_KEY}
      LOG_LEVEL: ${LOG_LEVEL:-info}
    dependsOn: {postgres: healthy}   # healthy | started | completed
    volumes: [data:/data, files:/files:ro]
    files: {/etc/app.conf: "..."}
    health: {test: [CMD, wget, -q, --spider, "http://127.0.0.1:3000/health"], interval: 30s, timeout: 5s, retries: 3, startPeriod: 0s}
    resources: {cpus: "0.5", memory: 512M, reserve: 128M}   # cpus and memory required
    once: false            # true for a run-once job
    init: true
    noNewPrivileges: true
    privileged: false      # true | false
    pid: host              # host
    stopGracePeriod: 10s
```

Deploy injects `slug`, `name` and `host` from `projects/<slug>.yml` and `tags` from
`tags/<slug>.json`; never write them.

An image with `:` or `@` is used as written. Any other image gets `tag`, else the tag in
`tags/<slug>.json`, else `latest`. `${NAME}` in `env` reads the secrets file; a name without a
default must be listed in `secrets`. Write `$$` for a literal `$` in `env` and `command`; `files`
are literal.

`health` needs `test`; the other fields default to the values shown. `init` and
`noNewPrivileges` are on unless set to `false`. `once: true` sets `restart: "no"`.

`sleep.after` puts every service that is not `once` in the project's Sablier group, which stops
after that long without traffic and starts on the next request. `wake` starts the group on a
schedule for `wakeFor`.

A static site's values are only `enabled` and an optional `tag` pin. Its build comes from the
registry's `deploy.source`.

`deploy/host.yaml` holds the VPS memory budget. **PR** fails when the services' `reserve` total
exceeds `memory` minus `system`.

## The core chart

`charts/core` is versioned by `Chart.yaml`. **Deploy** tags `core-v<version>` on the first push of
a new version, and each project moves to it by changing `core:` in its values. A pull request that
changes the chart without bumping its version fails.

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

`charts/core/values.schema.json` checks the values Helm receives, including the injected `slug`,
so it does not validate `deploy/*/values.yaml` as written.
