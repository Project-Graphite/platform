import json
import pathlib
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


def topology(deploy):
    if deploy["kind"] == "compose":
        return {
            "kind": "compose",
            "database": deploy.get("database", "none"),
            "services": [
                {
                    "name": name,
                    "route": None if service.get("route", "none") == "none" else service["route"],
                    "port": service.get("port"),
                    "size": service.get("size", "small"),
                }
                for name, service in deploy["services"].items()
            ],
        }

    if deploy["kind"] == "pages":
        return {
            "kind": "pages",
            "database": "none",
            "services": [{"name": "static", "route": "/", "port": None, "size": "small"}],
        }

    return None


def project_card(project):
    deploy = project["deploy"]
    host = deploy.get("host")
    return {
        "slug": project["slug"],
        "name": project["name"],
        "summary": collapse(project["summary"]),
        "tags": project.get("tags", []),
        "repo": project["repo"],
        "repoUrl": f"https://github.com/{project['repo']}",
        "owner": project["owner"],
        "authors": project["authors"],
        "status": project["status"],
        "featured": project.get("featured", False),
        "url": f"https://{host}" if host and project["status"] == "live" else None,
        "path": f"/{project['owner']}/{project['slug']}",
        "topology": topology(deploy),
    }


def person_card(person):
    return {
        "handle": person["handle"],
        "github": person["github"],
        "githubUrl": f"https://github.com/{person['github']}",
        "name": person["name"],
        "headline": person.get("headline"),
        "bio": collapse(person["bio"]) if person.get("bio") else None,
        "links": person.get("links", []),
        "path": f"/{person['handle']}",
    }


def main():
    if len(sys.argv) != 2:
        print("usage: compile.py <output-path>", file=sys.stderr)
        return 1

    projects = [project_card(p) for p in load("projects") if p.get("listed", True)]
    projects.sort(key=lambda entry: (not entry["featured"], entry["name"].lower()))

    people = [person_card(p) for p in load("people")]
    people.sort(key=lambda entry: entry["name"].lower())

    registry = {"people": people, "projects": projects}
    pathlib.Path(sys.argv[1]).write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
    print(f"Compiled {len(people)} people and {len(projects)} projects to {sys.argv[1]}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
