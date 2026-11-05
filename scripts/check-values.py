import re
import sys

import deploy

REFERENCE = re.compile(r"(?<![$])[$][{]([^}]*)[}]")
SHAPE = re.compile(r"[A-Z_][A-Z0-9_]*(?::?[?].*|:-.*)")
DEFAULT = re.compile(r"(?<![$])[$][{]([A-Z_][A-Z0-9_]*):-([^}]*)[}]")
IMAGE = re.compile(r'"image":\s*"(ghcr\.io/[^"]+)"')
SUPPLIED = {"PROJECT", "HOST"}


def bare_references(path):
    return [
        f"{path.relative_to(deploy.ROOT).as_posix()}: ${{{body}}} needs :? when it is required or :- with its default"
        for body in REFERENCE.findall(deploy.read(path))
        if body not in SUPPLIED and not SHAPE.fullmatch(body)
    ]


def project_problems(slug):
    settings = deploy.values(slug)
    problems = []
    tag = settings.get("IMAGE_TAG")
    if tag is not None and not re.fullmatch(r"sha-[0-9a-f]{7,40}", tag):
        problems.append(f"deploy/{slug}/values: IMAGE_TAG {tag} is not a sha- tag")
    if not deploy.compose_project(slug):
        return problems
    flat = deploy.flatten(slug)
    defaults = dict(DEFAULT.findall(flat))
    problems.extend(
        f"deploy/{slug}/values: {name}={value} repeats its default; remove the line"
        for name, value in settings.items()
        if defaults.get(name) == value
    )
    problems.extend(
        f"deploy/{slug}: {image} must be tagged ${{IMAGE_TAG:?}} or sha-"
        for image in IMAGE.findall(flat)
        if not image.endswith(":${IMAGE_TAG:?}") and ":sha-" not in image
    )
    return problems + bare_references(deploy.DEPLOY / slug / "compose.yaml")


def main():
    problems = [line for path in sorted((deploy.ROOT / "shared").glob("*.yaml")) for line in bare_references(path)]
    for slug in deploy.projects():
        try:
            problems.extend(project_problems(slug))
        except ValueError as error:
            problems.append(str(error))
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    print(f"Checked the values of {len(deploy.projects())} projects, found {len(problems)} problems.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
