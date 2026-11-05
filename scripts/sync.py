import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

import yaml

import validate

ROOT = pathlib.Path(__file__).resolve().parent.parent
SLUG = json.loads((ROOT / "schema" / "project.schema.json").read_text(encoding="utf-8"))["properties"]["slug"]["pattern"]


def repositories():
    return validate.paginate(f"/orgs/{validate.ORGANIZATION}/repos?type=all")


def manifest(repository):
    try:
        with urllib.request.urlopen(
            validate.request(
                f"/repos/{validate.ORGANIZATION}/{repository}/contents/.graphite.yml",
                "application/vnd.github.raw+json",
            )
        ) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def registry():
    return {path.stem: project for path, project in validate.load("projects")}


def entries(projects):
    return [(ROOT / "projects" / f"{slug}.yml", project) for slug, project in sorted(projects.items())]


def apply(name, text, projects, organisation, members):
    try:
        source = yaml.safe_load(text)
    except yaml.YAMLError as error:
        return projects, [f".graphite.yml is not valid YAML: {error}"]

    declared = source.get("projects") if isinstance(source, dict) else None
    if not isinstance(declared, list) or not declared:
        return projects, [".graphite.yml must contain a non-empty projects list"]

    repo = f"project-graphite/{name}"
    candidates = {}
    problems = []
    for entry in declared:
        if not isinstance(entry, dict) or not isinstance(entry.get("slug"), str):
            problems.append("every project needs a slug")
        elif not re.fullmatch(SLUG, entry["slug"]):
            problems.append(f"{entry['slug']!r} is not a valid slug")
        elif entry["slug"] in candidates:
            problems.append(f"{entry['slug']} is declared more than once")
        else:
            candidates[entry["slug"]] = {**entry, "repo": repo}

    for slug in candidates:
        claimant = projects[slug].get("repo") if isinstance(projects.get(slug), dict) else None
        if (
            isinstance(claimant, str)
            and claimant.lower() != repo.lower()
            and claimant.split("/")[-1].lower() in organisation
        ):
            problems.append(f"{slug} is registered to {claimant}; choose a slug no other repository uses")

    updated = {
        slug: project
        for slug, project in projects.items()
        if not (isinstance(project, dict) and project.get("repo") == repo)
    } | candidates
    existing = set(validate.registry_problems(entries(projects), members))
    problems.extend(
        line for line in validate.registry_problems(entries(updated), members) if line not in existing
    )
    return updated, problems


def report(name, problems):
    for line in problems:
        print(f"error: {name}: {line}", file=sys.stderr)


def sync():
    existing = registry()
    projects = existing
    members = validate.members()
    listed = list(repositories())
    organisation = {repository["name"].lower() for repository in listed}
    skipped = []

    for repository in listed:
        text = manifest(repository["name"])
        if text is None:
            continue
        updated, problems = apply(repository["name"], text, projects, organisation, members)
        if problems:
            report(repository["name"], problems)
            skipped.append(repository["name"])
        else:
            projects = updated

    changed = []
    for slug, project in projects.items():
        if existing.get(slug) == project:
            continue
        (ROOT / "projects" / f"{slug}.yml").write_text(
            yaml.safe_dump(project, sort_keys=False, allow_unicode=True, width=100),
            encoding="utf-8",
        )
        changed.append(slug)

    for slug in existing.keys() - projects.keys():
        (ROOT / "projects" / f"{slug}.yml").unlink()
        changed.append(slug)

    if changed:
        print(f"Updated {len(changed)} registry entries: {', '.join(sorted(changed))}.")
    else:
        print("Registry entries already match repository manifests.")

    if skipped:
        print(f"Skipped invalid manifests in {', '.join(skipped)}; their entries are unchanged.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(sync())
