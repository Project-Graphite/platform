import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

import yaml

import validate

ROOT = pathlib.Path(__file__).resolve().parent.parent
ORGANIZATION = "Project-Graphite"
SLUG = json.loads((ROOT / "schema" / "project.schema.json").read_text(encoding="utf-8"))["properties"]["slug"]["pattern"]


def request(path, accept="application/vnd.github+json"):
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise RuntimeError("GH_TOKEN is required")
    return urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Accept": accept,
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "project-graphite-registry-sync",
        },
    )


def repositories():
    page = 1
    while True:
        with urllib.request.urlopen(
            request(f"/orgs/{ORGANIZATION}/repos?type=all&per_page=100&page={page}")
        ) as response:
            entries = json.load(response)
        yield from entries
        if len(entries) < 100:
            return
        page += 1


def manifest(repository):
    try:
        with urllib.request.urlopen(
            request(
                f"/repos/{ORGANIZATION}/{repository}/contents/.graphite.yml",
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


def apply(name, text, projects, organisation, people):
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
    existing = set(validate.registry_problems(entries(projects), people))
    problems.extend(
        line for line in validate.registry_problems(entries(updated), people) if line not in existing
    )
    return updated, problems


def report(name, problems):
    for line in problems:
        print(f"error: {name}: {line}", file=sys.stderr)


def sync():
    existing = registry()
    projects = existing
    people = validate.load("people")
    listed = list(repositories())
    organisation = {repository["name"].lower() for repository in listed}
    skipped = []

    for repository in listed:
        text = manifest(repository["name"])
        if text is None:
            continue
        updated, problems = apply(repository["name"], text, projects, organisation, people)
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
