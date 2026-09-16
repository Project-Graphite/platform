# platform

The control plane for Project Graphite. One file per project describes what it is, where it runs,
and how it is exposed. Validation, the public Hub registry, and Kubernetes resources are derived
from those files.

This repository is private. It contains deployment topology, not credentials. Kubernetes Secrets
and the Cloudflare Tunnel token are created directly in the cluster and never committed.

## Registry

`people/<handle>.yml` describes a member and creates their portfolio at
`project-graphite.com/members/<handle>`.

`projects/<slug>.yml` describes a project and drives:

- its Hub card and project page
- the immutable container images used by each service
- its Kubernetes namespace, workloads, dependencies, quotas, and network policies
- the internal gateway route for its hostname
- the configuration and secret names required at runtime

`tags/<slug>.json` records each service's deployed image tag. CI writes these files; people do not.

## Deployment kinds

`deploy.kind` is one of:

- `kubernetes`: containers on the shared GKE cluster
- `pages`: a static Cloudflare Pages build
- `none`: source only

### Kubernetes projects

```yaml
deploy:
  kind: kubernetes
  host: game-forge.project-graphite.com
  config:
    LOG_LEVEL: info
  secrets: [DATABASE_PASSWORD]
  dependencies:
    postgres:
      storage: 5Gi
  migration:
    service: server
    command: [python, manage.py, migrate, --noinput]
  services:
    frontend:
      port: 80
      route: /
      size: tiny
    server:
      port: 3000
      route: /api
      size: small
      env: [LOG_LEVEL, DATABASE_PASSWORD]
    worker:
      route: none
      size: small
      command: [python, worker.py]
      env: [DATABASE_PASSWORD]
```

Each service key must match a container image published as
`ghcr.io/project-graphite/<repository>/<service>`. Routes are served internally by the shared
gateway and exposed only through Cloudflare Tunnel. No project receives a public load balancer or
NodePort.

| Service field | Meaning |
| :--- | :--- |
| `port` | Container port; required for a routed service |
| `route` | Path prefix, or `none` for a worker |
| `size` | `tiny`, `small`, or `medium` resource preset |
| `replicas` | One to three; defaults to one |
| `env` | Names declared in `config` or `secrets` |
| `command` | Array overriding the image command |
| `health` | Optional readiness and liveness HTTP paths |

Every Kubernetes project gets its own namespace, service account, quota, default limits, config,
default-deny network policy, and explicitly allowed traffic. Secret values are deliberately absent
from generated resources.

The starter `e2-small` node is sized for the Hub plus one small full-stack lab at a time. The
namespace model supports more projects without redesign, but additional always-on workloads require
a deliberate node-capacity and cost review.

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
2. Add a Dockerfile for each deployable service.
3. Add and review `projects/<slug>.yml` here.
4. Merge the project image build and let CI record immutable tags in `tags/<slug>.json`.
5. Render the cluster resources, create the declared Secrets, and review the diff before applying.

The shared deployment workflow refuses projects without a matching platform entry.

## Scripts

```sh
pip install pyyaml jsonschema
python scripts/validate.py
python scripts/test_render_kubernetes.py
python scripts/render_kubernetes.py ./rendered
python scripts/compile.py registry.json
```

`validate.py` checks schemas and cross-file ownership, host, service, route, and environment
contracts. `render_kubernetes.py` produces cluster and per-project YAML. Generated output is a
review artifact, not committed state.

## Editor support

```json
{
  "yaml.schemas": {
    "./schema/project.schema.json": "projects/*.yml",
    "./schema/person.schema.json": "people/*.yml"
  }
}
```
