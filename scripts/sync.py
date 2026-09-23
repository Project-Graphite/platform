import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
ORGANIZATION = "Project-Graphite"


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
            return yaml.safe_load(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def load_projects():
    return {
        project["slug"]: project
        for path in sorted((ROOT / "projects").glob("*.yml"))
        if isinstance(project := yaml.safe_load(path.read_text(encoding="utf-8")), dict)
    }


def sync():
    existing = load_projects()
    seen = {}
    synced = set()
    changed = []

    for repository in repositories():
        source = manifest(repository["name"])
        if source is None:
            continue
        repo = f"project-graphite/{repository['name']}"
        synced.add(repo)
        entries = source.get("projects") if isinstance(source, dict) else None
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"{repository['name']}: .graphite.yml must contain a non-empty projects list")

        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("slug"), str):
                raise ValueError(f"{repository['name']}: every project needs a slug")
            project = {**entry, "repo": repo}
            slug = project["slug"]
            if slug in seen:
                raise ValueError(f"{slug}: also declared by {seen[slug]}")
            seen[slug] = repository["name"]
            if existing.get(slug) == project:
                continue
            (ROOT / "projects" / f"{slug}.yml").write_text(
                yaml.safe_dump(project, sort_keys=False, allow_unicode=True, width=100),
                encoding="utf-8",
            )
            changed.append(slug)

    for slug, project in existing.items():
        if slug not in seen and project.get("repo") in synced:
            (ROOT / "projects" / f"{slug}.yml").unlink()
            changed.append(slug)

    if changed:
        print(f"Updated {len(changed)} registry entries: {', '.join(sorted(changed))}.")
    else:
        print("Registry entries already match repository manifests.")


if __name__ == "__main__":
    try:
        sync()
    except (OSError, RuntimeError, ValueError, yaml.YAMLError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
