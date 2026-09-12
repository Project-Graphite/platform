import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def card(project):
    host = project["deploy"].get("host")
    return {
        "slug": project["slug"],
        "name": project["name"],
        "summary": " ".join(project["summary"].split()),
        "tags": project.get("tags", []),
        "repo": project["repo"],
        "repoUrl": f"https://github.com/{project['repo']}",
        "authors": project["authors"],
        "status": project["status"],
        "featured": project.get("featured", False),
        "url": f"https://{host}" if host and project["status"] == "live" else None,
    }


def main():
    if len(sys.argv) != 2:
        print("usage: compile.py <output-path>", file=sys.stderr)
        return 1

    projects = [yaml.safe_load(path.read_text()) for path in sorted((ROOT / "projects").glob("*.yml"))]
    cards = [card(project) for project in projects if project.get("listed", True)]
    cards.sort(key=lambda entry: (not entry["featured"], entry["name"].lower()))

    pathlib.Path(sys.argv[1]).write_text(json.dumps(cards, indent=2) + "\n")
    print(f"Compiled {len(cards)} entries to {sys.argv[1]}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
