import json
import pathlib
import sys

import jsonschema
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def validator_for(name):
    schema = json.loads((ROOT / "schema" / name).read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


PROJECT = validator_for("project.schema.json")
PERSON = validator_for("person.schema.json")


def load(directory):
    return [
        (path, yaml.safe_load(path.read_text(encoding="utf-8")))
        for path in sorted((ROOT / directory).glob("*.yml"))
    ]


def schema_problems(validator, path, entry, key):
    if not isinstance(entry, dict):
        yield f"{path.name}: file is empty or is not a YAML mapping"
        return

    for error in sorted(validator.iter_errors(entry), key=lambda e: list(e.path)):
        location = ".".join(str(part) for part in error.path) or "<root>"
        yield f"{path.name}: {location}: {error.message}"

    if entry.get(key) and path.stem != entry[key]:
        yield f"{path.name}: filename must be {entry[key]}.yml to match the {key}"


def project_problems(path, project):
    yield from schema_problems(PROJECT, path, project, "slug")

    if not isinstance(project, dict):
        return

    slug, repo = project.get("slug"), project.get("repo")
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


def uniqueness_problems(entries, field, label):
    claimed = {}
    for path, entry in entries:
        value = entry.get(field) if isinstance(entry, dict) else None
        if not value:
            continue
        if value in claimed:
            yield f"{path.name}: {label} {value} is already claimed by {claimed[value]}"
        claimed[value] = path.name


def ownership_problems(projects, people):
    handles = {person["handle"] for _, person in people if isinstance(person, dict) and "handle" in person}
    for path, project in projects:
        owner = project.get("owner") if isinstance(project, dict) else None
        if owner and owner not in handles:
            yield f"{path.name}: owner {owner} has no entry in people/"


def host_problems(projects):
    claimed = {}
    for path, project in projects:
        host = project.get("deploy", {}).get("host") if isinstance(project, dict) else None
        if not host:
            continue
        if host in claimed:
            yield f"{path.name}: host {host} is already claimed by {claimed[host]}"
        claimed[host] = path.name


def main():
    projects = load("projects")
    people = load("people")

    if not projects:
        print("No registry entries in projects/.", file=sys.stderr)
        return 1
    if not people:
        print("No entries in people/.", file=sys.stderr)
        return 1

    problems = [line for path, person in people for line in schema_problems(PERSON, path, person, "handle")]
    problems.extend(line for path, project in projects for line in project_problems(path, project))
    problems.extend(uniqueness_problems(people, "github", "github account"))
    problems.extend(ownership_problems(projects, people))
    problems.extend(host_problems(projects))

    for line in problems:
        print(f"error: {line}", file=sys.stderr)

    print(f"Checked {len(people)} people and {len(projects)} projects, found {len(problems)} problems.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
