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
| `edge/` | Traefik, Sablier, cloudflared and the wake scheduler on the VPS | maintainers |
| `host/` | VPS bootstrap and helper scripts | maintainers |

Secret values never live here. They are in the owners-only `secrets` repository, which only an
organisation owner can change and apply.

## How a deploy happens

1. A project's CI publishes its images, or records its static-site commit, in `tags/<slug>.json`.
2. That push runs **Deploy**. It renders `deploy/<slug>/values.yaml` with the `core` chart version
   the values pin, checks the project's secrets exist on the VPS, copies the Compose file over SSH
   through Cloudflare Access, runs `docker compose up --wait`, publishes the route to Traefik and
   points the hostname at the tunnel.
3. Static sites are built here from the recorded commit and uploaded to Cloudflare Pages.

A pull request that changes values, the chart or the edge runs the same rendering in **PR**, and
merging it deploys.

## Values

```yaml
core: 1.0.0                # core chart version
enabled: true              # true | false
secrets: [API_KEY]         # required keys in the secrets repository
sleep:                     # optional
  after: 30m               # duration
  wake: "0 */6 * * *"      # cron | "@every 6h"
  wakeFor: 15m             # duration

services:
  app:
    image: ghcr.io/project-graphite/<repo>/app
    tag: sha-1a2b3c4        # optional pin; omitted follows CI
    command: [node, server.js]
    entrypoint: [sh, -c]
    port: 3000
    public: true           # exactly one service
    env:
      API_KEY: ${API_KEY}
      LOG_LEVEL: ${LOG_LEVEL:-info}
    dependsOn: {postgres: healthy}   # healthy | started | completed
    volumes: [data:/data, files:/files:ro]
    files: {/etc/app.conf: "..."}
    health: {test: [CMD, wget, -q, --spider, "http://127.0.0.1:3000/health"], interval: 30s, timeout: 5s, retries: 3, startPeriod: 20s}
    resources: {cpus: "0.5", memory: 512M, reserve: 128M}
    once: false            # true for a run-once job
    init: true
    noNewPrivileges: true
    stopGracePeriod: 10s
```

An image with `:` or `@` is used as written. Any other image gets `tag`, else the tag in
`tags/<slug>.json`, else `latest`. `${NAME}` in `env` reads the secrets file; a name without a
default must be listed in `secrets`. Write `$$` for a literal `$` in `env` and `command`; `files`
are literal.

A static site's values are only `enabled` and an optional `tag` pin. Its build comes from the
registry's `deploy.source`.

`deploy/host.yaml` holds the VPS memory budget. **PR** fails when the services' `reserve` total
exceeds `memory` minus `system`.

## The core chart

`charts/core` is versioned by `Chart.yaml`. **Deploy** tags `core-v<version>` on the first push of
a new version, and each project moves to it by changing `core:` in its values. A pull request that
changes the chart without bumping its version fails.

## Running it

```sh
pip install pyyaml jsonschema
python scripts/validate.py
python scripts/compile.py registry.json
python scripts/deploy.py check
```

`deploy.py check` needs Helm 3 and Docker. Deploy runs on demand from Actions → **Deploy** → Run
workflow, with a project slug, `edge` or `all`.

## Editor support

```json
{
  "yaml.schemas": {
    "./schema/project.schema.json": "projects/*.yml",
    "./schema/person.schema.json": "people/*.yml",
    "./charts/core/values.schema.json": "deploy/*/values.yaml"
  }
}
```
