# platform

The control plane for Project Graphite. One file per project describes what it is, where it runs
and how it is exposed; everything else is derived from that file.

This repository is private. It holds hostnames, deployment topology and Cloudflare configuration.
Nothing secret is committed — credentials live on the host, outside git.

## The registry

`people/<handle>.yml` describes a person and creates their portfolio at
`project-graphite.com/members/<handle>`. It includes projects they maintain or are credited on.

`projects/<slug>.yml` is the single source of truth for a project. It drives:

- the card on [project-graphite.com](https://project-graphite.com) and the project page at
  `/projects/<slug>`
- which container images get built and where they are pushed
- the generated compose file and Traefik routing
- the DNS record
- which environment variables must exist before the project can start

`tags/<slug>.json` records the image tag currently deployed for each service. **It is written by
CI, never by hand.** Keeping it out of the registry file means a bot deploy never conflicts with a
human edit, and a `git log` on `tags/` reads as a deployment history.

## Fields

| Field | Required | Notes |
| :--- | :--- | :--- |
| `slug` | yes | Lowercase. Must equal the filename and the repository name. Becomes the subdomain. |
| `name` | yes | Display name, 2–60 characters. |
| `summary` | yes | 20–240 characters. One or two sentences, present tense, no marketing. |
| `tags` | no | Up to six lowercase technology tags. |
| `repo` | yes | `project-graphite/<name>`. |
| `owner` | yes | Handle of the member responsible for maintaining it. Must exist in `people/`. |
| `authors` | yes | GitHub usernames. Everyone who built it, in the order they should be credited. An author with a `people/` entry links to their page; anyone else links to their GitHub profile. |
| `status` | yes | `live`, `wip`, `archived` or `source-only`. |
| `featured` | no | Sorts the entry to the top of the portfolio. |
| `listed` | no | `false` hides it from the portfolio. For platform pieces only. |
| `deploy.kind` | yes | `compose`, `pages` or `none`. |

### `kind: compose`

Runs on the shared host. One Docker Compose project, one container per service.

```yaml
deploy:
  kind: compose
  host: game-forge.project-graphite.com
  database: postgres
  services:
    frontend:
      port: 80
      route: /
      size: small
    server:
      port: 3000
      route: /api
      size: small
      env: [OPENAI_API_KEY]
    worker:
      route: none
      size: medium
```

Each key under `services` must name a top-level folder in the repository that contains a
`Dockerfile`. That is the same convention CI uses to decide what to build, so the two cannot drift.

| Service field | Notes |
| :--- | :--- |
| `port` | Container port. Required when `route` is a path. |
| `route` | Path prefix such as `/` or `/api`, or `none` for a service with no public route. |
| `size` | `small` (512 MB, 0.5 CPU) or `medium` (1 GB, 1 CPU). Fixed presets — the host is shared. |
| `replicas` | 1–3. Defaults to 1. |
| `env` | Names of variables the service needs. Values live on the host, never here. |
| `writable` | Paths that need to be writable. Containers run read-only apart from `/tmp`. |
| `command` | Overrides the image entrypoint command. |

At least one service must have a path route, and no two may claim the same one.

### `kind: pages`

A static build on Cloudflare Pages. No host resources, free, with preview deployments per pull
request.

```yaml
deploy:
  kind: pages
  host: project-graphite.com
  source:
    directory: site
    output: dist
    command: npm run build
```

### `kind: none`

Source published, nothing deployed. Use it for archived projects and for work that is not a web
application.

## Adding a project

1. Create the repository from
   [project-template](https://github.com/project-graphite/project-template).
2. Build it. Every deployable folder gets a `Dockerfile`.
3. Open a pull request here adding `projects/<slug>.yml`.
4. It is reviewed for name, summary, resource size and the secrets it asks for.
5. On merge the DNS record appears, the portfolio rebuilds, and the project goes live.

A repository cannot deploy until its registry entry exists — `reusable-deploy` fails with a clear
message rather than silently doing nothing.

## People

`people/<handle>.yml`:

```yaml
handle: amr
github: AmrMsCLL
name: Amr El-Sheraey
headline: Full-stack engineer, Cairo
bio: >
  Two or three sentences. What you work on and what you are interested in.
links:
  - label: github
    url: https://github.com/AmrMsCLL
  - label: linkedin
    url: https://www.linkedin.com/in/amrsherieycll
```

| Field | Required | Notes |
| :--- | :--- | :--- |
| `handle` | yes | Lowercase. Becomes the URL segment and must match the filename. |
| `github` | yes | GitHub username. Used to link `authors` entries to this page. |
| `name` | yes | Display name. |
| `headline` | no | One line, shown under the name. |
| `bio` | no | Up to 600 characters. |
| `links` | no | Up to six labelled links. Must be `https://` or `mailto:`. |

**`owner` and `authors` are different things.** `owner` is the member responsible for maintaining
the project in Graphite; `authors` is who actually wrote it. A project appears on a member page
when they maintain it or their GitHub username is credited in `authors`.

## Scripts

| Script | Purpose |
| :--- | :--- |
| `scripts/validate.py` | Check every entry against the schema, plus the cross-checks the schema cannot express |
| `scripts/compile.py` | Render people and projects to the `registry.json` the portfolio consumes |

Both need `pyyaml` and `jsonschema`:

```sh
pip install pyyaml jsonschema
python scripts/validate.py
```

`validate.py` catches what a schema alone cannot: filename against slug or handle, repository
against slug, an `owner` with no `people/` entry, duplicate hostnames across projects, duplicate
GitHub accounts across people, and two services claiming the same route.

## Editor support

Point your editor at the schema for completion and inline errors. In VS Code, `settings.json`:

```json
{
  "yaml.schemas": {
    "./schema/project.schema.json": "projects/*.yml",
    "./schema/person.schema.json": "people/*.yml"
  }
}
```

## Not built yet

The host side is deliberately unwritten until there is a host to run it on:

- `deploy/` — the generated compose files
- `host/` — the reconcile agent, Traefik and cloudflared, shared Postgres
- `terraform/` — Cloudflare DNS records derived from the registry

Cloudflare Pages manages its own DNS, so none of this is needed while every project is
`kind: pages`.
