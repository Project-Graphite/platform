import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.request

import yaml

import agent
import cloudflare

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
EDGE = ROOT / "edge"
DEPLOY_HOST = "deploy.project-graphite.com"
CHANGE = re.compile(r"^(?:deploy|projects)/([a-z0-9-]+)(?:/|[.]yml$)")
REQUIRED = re.compile(r"(?<![$])[$][{]([A-Z_][A-Z0-9_]*):?[?]")
LINE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")


def read(path):
    return path.read_text(encoding="utf-8")


def entry(slug):
    path = ROOT / "projects" / f"{slug}.yml"
    return yaml.safe_load(read(path)) if path.exists() else None


def values(slug):
    path = DEPLOY / slug / "values"
    found = {}
    for number, line in enumerate(read(path).splitlines() if path.exists() else [], 1):
        match = LINE.match(line)
        if not match:
            raise ValueError(f"deploy/{slug}/values line {number} is not NAME=value")
        found[match[1]] = match[2]
    return found


def projects():
    return sorted(path.name for path in DEPLOY.iterdir() if path.is_dir())


def compose_project(slug):
    return (DEPLOY / slug / "compose.yaml").exists()


def environment(slug):
    return {**values(slug), "PROJECT": slug, "HOST": entry(slug)["deploy"]["host"]}


def env_file(pairs):
    return "".join(f"{name}={value}\n" for name, value in pairs.items())


def compose(slug, *args, env=()):
    with tempfile.TemporaryDirectory() as workdir:
        options = []
        for index, pairs in enumerate(env):
            path = pathlib.Path(workdir) / f"{index}.env"
            path.write_text(env_file(pairs), encoding="utf-8")
            options += ["--env-file", str(path)]
        command = ["docker", "compose", "-p", slug, "-f", str(DEPLOY / slug / "compose.yaml"), *options, *args]
        supplied = {name for pairs in env for name in pairs}
        result = subprocess.run(command, text=True, capture_output=True, env={name: value for name, value in os.environ.items() if name not in supplied})
    if result.returncode:
        raise ValueError(f"deploy/{slug}: {result.stderr.strip()}")
    return result.stdout


def flatten(slug):
    return compose(slug, "config", "--no-interpolate", "--format", "json").replace(str(DEPLOY / slug) + os.sep, "./")


def required(text):
    return sorted(set(REQUIRED.findall(text)))


def render(slug):
    flat = flatten(slug)
    env = environment(slug)
    secrets = [name for name in required(flat) if name not in env]
    return json.loads(compose(slug, "config", "--format", "json", env=[env, dict.fromkeys(secrets, "stub")])), flat, secrets


def route(slug, config):
    host = entry(slug)["deploy"]["host"]
    for service in config["services"].values():
        if slug in (((service.get("networks") or {}).get("edge") or {}).get("aliases") or []):
            return json.dumps({
                "http": {
                    "routers": {slug: {"rule": f"Host(`{host}`)", "entryPoints": ["websecure"], "service": slug, "tls": {"certResolver": "letsencrypt"}}},
                    "services": {slug: {"loadBalancer": {"servers": [{"url": f"http://{slug}:{service['expose'][0]}"}]}}},
                }
            }, indent=2)
    raise ValueError(f"deploy/{slug}: no service joins the edge network as {slug}")


def run(*command, capture=False):
    return subprocess.run(command, check=True, text=True, capture_output=capture).stdout


def git(*args):
    return run("git", "-C", str(ROOT), *args, capture=True).strip()


def github(path):
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "User-Agent": "project-graphite-deploy",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def changes(before):
    if not before or set(before) == {"0"} or subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "-e", f"{before}^{{commit}}"], capture_output=True
    ).returncode:
        return None
    return git("diff", "--name-only", before, "HEAD").splitlines()


def plan():
    project = os.environ.get("PROJECT", "")
    everything = projects()
    if project:
        chosen = everything if project == "all" else [] if project == "edge" else [project]
        edge = project in ("all", "edge")
    else:
        changed = changes(os.environ.get("BEFORE", ""))
        if changed is None:
            chosen, edge = everything, True
        else:
            chosen = sorted({match.group(1) for path in changed if (match := CHANGE.match(path))})
            if any(path.startswith("shared/") for path in changed):
                chosen = sorted({*chosen, *filter(compose_project, everything)})
            edge = any(path.startswith(("edge/", "host/")) for path in changed)

    compose_projects, pages, down = [], [], []
    for slug in chosen:
        registered = entry(slug)
        kind = registered["deploy"]["kind"] if registered else None
        if not (DEPLOY / slug).is_dir() or values(slug).get("ENABLED") == "false":
            if kind == "compose":
                down.append(slug)
        elif kind == "compose" and compose_project(slug):
            compose_projects.append(slug)
        elif kind == "pages":
            pages.append(slug)
    return {"edge": edge, "compose": compose_projects, "down": down, "pages": pages}


def show_plan():
    for key, value in plan().items():
        print(f"{key}={json.dumps(value)}")


def deploy_compose(slug):
    config, flat, secrets = render(slug)
    env = environment(slug)
    folder = DEPLOY / slug / "files"
    files = {"compose.yaml": flat, "values": env_file(env)}
    if folder.is_dir():
        files.update({f"files/{path.name}": read(path) for path in sorted(folder.iterdir())})
    print(agent.send({"name": slug, "action": "up", "files": files, "route": route(slug, config), "require": secrets}))
    cloudflare.point_at_vps(env["HOST"], values(slug).get("PROXIED") == "true")
    print(f"Deployed {slug} at https://{env['HOST']}.")


def take_down(slug):
    print(agent.send({"name": slug, "action": "down"}))


def deploy_edge():
    cloudflare.point_at_vps(DEPLOY_HOST)
    print(agent.send({
        "name": "edge",
        "action": "up",
        "files": {name: read(EDGE / name) for name in ("compose.yaml", "traefik.yml")},
        "route": read(EDGE / "agent.yml"),
        "require": ["CF_DNS_API_TOKEN"],
    }))
    print(agent.send({
        "name": "backup",
        "action": "up",
        "files": {"compose.yaml": read(EDGE / "backup.yaml"), "files/backup.py": read(EDGE / "backup.py")},
    }))


def apply():
    steps = plan()
    failed = []
    for name, step in (
        [("edge", deploy_edge)] * steps["edge"]
        + [(slug, lambda slug=slug: deploy_compose(slug)) for slug in steps["compose"]]
        + [(slug, lambda slug=slug: take_down(slug)) for slug in steps["down"]]
    ):
        print(f"::group::{name}")
        try:
            step()
        except (SystemExit, subprocess.CalledProcessError, RuntimeError, ValueError, OSError) as error:
            print(f"::error::{name}: {error}")
            failed.append(name)
        print("::endgroup::")
    return 1 if failed else 0


def pages_info(slug):
    registered = entry(slug)
    tag = values(slug).get("IMAGE_TAG")
    if not tag:
        sys.exit(f"error: {slug} has no IMAGE_TAG in deploy/{slug}/values; push to its main branch")
    repository, source = registered["repo"], registered["deploy"]["source"]
    commit = github(f"/repos/{repository}/commits/{tag.removeprefix('sha-')}")["sha"]
    print(f"repository={repository}")
    print(f"commit={commit}")
    print(f"directory={source['directory']}")
    print(f"output={source['directory']}/{source['output']}")
    print(f"command={source.get('command', 'npm run build')}")


def pages_site(slug):
    cloudflare.pages_site(slug, entry(slug)["deploy"]["host"])


def check():
    problems = []
    for slug in projects():
        registered = entry(slug)
        kind = registered["deploy"]["kind"] if registered else None
        try:
            if kind == "pages":
                problems.extend(f"deploy/{slug}/values: {name} is not used by a Pages site" for name in set(values(slug)) - {"IMAGE_TAG", "ENABLED"})
            elif kind == "compose" and compose_project(slug):
                route(slug, render(slug)[0])
            else:
                problems.append(f"deploy/{slug}: needs compose.yaml and deploy.kind compose in projects/{slug}.yml, or deploy.kind pages")
        except ValueError as error:
            problems.append(str(error))
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    print(f"Checked {len(projects())} projects, found {len(problems)} problems.")
    return 1 if problems else 0


COMMANDS = {
    "plan": show_plan,
    "apply": apply,
    "check": check,
    "pages-info": pages_info,
    "pages-site": pages_site,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(f"usage: deploy.py {{{'|'.join(COMMANDS)}}} [project]", file=sys.stderr)
        return 1
    return COMMANDS[sys.argv[1]](*sys.argv[2:]) or 0


if __name__ == "__main__":
    sys.exit(main())
