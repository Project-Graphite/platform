# platform

The private control plane for Project Graphite. One file per project describes what it is, who owns
it, and where it runs. Validation and the public Hub registry are derived from these files.

This repository contains deployment metadata, not credentials. Runtime values live only in
Coolify, Cloudflare, or GitHub secret stores.

## Registry

`people/<handle>.yml` describes a member and creates their portfolio at
`project-graphite.com/members/<handle>`. The registry sync creates one for every organisation
member and refreshes it from their GitHub profile unless it sets `profile: manual`.

`projects/<slug>.yml` drives the Hub card, project page, public hostname, and deployment source.
Set `parent` to the slug of an epic (`kind: epic`) when an entry belongs to it. Child projects
share their epic's repository and appear at `/epics/<parent>/<slug>`.
`tags/<slug>.json` records immutable service image tags published by CI.

## Deployment kinds

`deploy.kind` is one of:

- `compose`: a Git-based Docker Compose application on the shared Coolify VPS
- `pages`: a static Cloudflare Pages build
- `none`: source only

### Compose projects

```yaml
deploy:
  kind: compose
  host: game-forge.project-graphite.com
  source:
    file: compose.production.yaml
    service: web
    port: 3000
  secrets: [DATABASE_PASSWORD]
```

The production Compose file belongs to the project repository. It must use persistent named
volumes for state, define health checks, declare required values with `${NAME:?}`, avoid host port
publishing, and set resource limits suitable for the shared 4 GB VPS. Coolify owns public routing
to the declared service and internal port.

Secret names may be listed here for review, but their values are entered directly in Coolify and
never committed. The initial server is a single failure domain intended for low-traffic portfolio
demos, not highly available production workloads.

### Pages projects

```yaml
deploy:
  kind: pages
  host: project-graphite.com
  source:
    directory: site
    output: dist
    command: npm run build
```

### Source-only projects

```yaml
deploy:
  kind: none
```

## Adding a project

1. Create a repository from `project-template`.
2. Add a Dockerfile for each deployable service and a production Compose file.
3. In the new repository's `.graphite.yml`, replace the `project-template` entry with this
   project's slug and metadata. The Sync registry workflow writes `projects/<slug>.yml` here and
   merges it once the PR checks pass.
4. Merge the project image build and let CI record immutable tags in `tags/<slug>.json`.
5. Add the repository to Coolify, enter its declared secrets, and review the deployment before
   selecting Deploy.

The shared workflow refuses deployment records for projects without a matching platform entry.
Recording a tag does not deploy or mutate the VPS.

## Scripts

```sh
pip install pyyaml jsonschema
python scripts/validate.py
python scripts/compile.py registry.json
```

`validate.py` checks schemas, ownership, host uniqueness, and cross-file contracts. `compile.py`
produces the public registry consumed by Graphite Hub.

## Editor support

```json
{
  "yaml.schemas": {
    "./schema/project.schema.json": "projects/*.yml",
    "./schema/person.schema.json": "people/*.yml"
  }
}
```
