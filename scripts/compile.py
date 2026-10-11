import json
import pathlib
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(directory):
    return [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((ROOT / directory).glob("*.yml"))
    ]


def collapse(text):
    return " ".join(text.split())


def listed_on(slug):
    return subprocess.run(
        ["git", "log", "--diff-filter=A", "--format=%as", "--", f"projects/{slug}.yml"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[-1]


def project_card(project):
    host = project["deploy"].get("host")
    showcase = project.get("showcase")
    parent = project.get("parent")
    section = "epics" if project["kind"] == "epic" or parent else "projects"
    slug_path = f"{parent}/{project['slug']}" if parent else project["slug"]
    return {
        "slug": project["slug"],
        "parent": parent,
        "kind": project["kind"],
        "name": project["name"],
        "summary": collapse(project["summary"]),
        "tags": project.get("tags", []),
        "repo": project["repo"],
        "repoUrl": f"https://github.com/{project['repo']}",
        "owner": project["owner"],
        "authors": project["authors"],
        "status": project["status"],
        "paths": project.get("paths", []),
        "releases": project.get("releases", "auto"),
        "pinned": project.get("pinned", False),
        "listedOn": listed_on(project["slug"]),
        "showcase": {
            "problem": collapse(showcase["problem"]),
            "approach": collapse(showcase["approach"]),
            "outcome": collapse(showcase["outcome"]),
            "contributions": [
                {
                    "github": contribution["github"],
                    "summary": collapse(contribution["summary"]),
                }
                for contribution in showcase["contributions"]
            ],
        }
        if showcase
        else None,
        "url": f"https://{host}" if host and project["status"] == "live" else None,
        "path": f"/{section}/{slug_path}",
    }


def person_card(person):
    return {
        "handle": person["handle"],
        "github": person["github"],
        "name": person["name"],
        "headline": person.get("headline"),
        "bio": collapse(person["bio"]) if person.get("bio") else None,
        "stack": person.get("stack", []),
        "links": person.get("links", []),
        "path": f"/members/{person['handle']}",
    }


def main():
    if len(sys.argv) != 2:
        print("usage: compile.py <output-path>", file=sys.stderr)
        return 1

    projects = [project_card(p) for p in load("projects") if p.get("listed", True)]
    projects.sort(key=lambda entry: entry["name"].lower())

    people = [person_card(p) for p in load("people")]
    people.sort(key=lambda entry: entry["name"].lower())

    pathlib.Path(sys.argv[1]).write_text(
        json.dumps({"members": people, "projects": projects}, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Compiled {len(people)} members and {len(projects)} projects to {sys.argv[1]}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
