import json
import pathlib
import sys

import jsonschema
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
VALIDATOR = jsonschema.Draft202012Validator(
    json.loads((ROOT / "schema" / "project.schema.json").read_text())
)


def entry_problems(path, project):
    if not isinstance(project, dict):
        yield f"{path.name}: file is empty or is not a YAML mapping"
        return

    for error in sorted(VALIDATOR.iter_errors(project), key=lambda e: list(e.path)):
        location = ".".join(str(part) for part in error.path) or "<root>"
        yield f"{path.name}: {location}: {error.message}"

    slug = project.get("slug")
    if slug and path.stem != slug:
        yield f"{path.name}: filename must be {slug}.yml to match the slug"

    repo = project.get("repo")
    if slug and repo and repo.split("/")[-1].lower() != slug:
        yield f"{path.name}: repo {repo} does not match slug {slug}"

    deploy = project.get("deploy")
    if not isinstance(deploy, dict) or deploy.get("kind") != "compose":
        return

    routes = [
        service.get("route")
        for service in deploy.get("services", {}).values()
        if isinstance(service, dict)
    ]
    exposed = [route for route in routes if route and route != "none"]

    if not exposed:
        yield f"{path.name}: a compose project needs at least one service with a route"
    if len(exposed) != len(set(exposed)):
        yield f"{path.name}: two services claim the same route {sorted(exposed)}"


def host_collisions(entries):
    claimed = {}
    for path, project in entries:
        host = project.get("deploy", {}).get("host") if isinstance(project, dict) else None
        if not host:
            continue
        if host in claimed:
            yield f"{path.name}: host {host} is already claimed by {claimed[host]}"
        claimed[host] = path.name


def main():
    entries = [
        (path, yaml.safe_load(path.read_text()))
        for path in sorted((ROOT / "projects").glob("*.yml"))
    ]

    if not entries:
        print("No registry entries in projects/.", file=sys.stderr)
        return 1

    problems = [line for path, project in entries for line in entry_problems(path, project)]
    problems.extend(host_collisions(entries))

    for line in problems:
        print(f"error: {line}", file=sys.stderr)

    print(f"Checked {len(entries)} entries, found {len(problems)} problems.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
