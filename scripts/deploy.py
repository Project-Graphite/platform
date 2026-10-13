import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

import yaml

import agent
import cloudflare

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
CHART = ROOT / "charts" / "core"
CURL = "curlimages/curl:8.22.0"
DEPLOY_HOST = "deploy.project-graphite.com"
COMPONENTS = ("postgres", "redis", "backup")
CHANGE = re.compile(r"^(?:deploy|tags|projects)/([a-z0-9-]+)(?:/|[.]json$|[.]yml$)")
REFERENCE = re.compile(r"(?<![$])[$][{]([A-Z][A-Z0-9_]*)[}]")
MEMORY = re.compile(r"^([0-9]+)([MG])$")


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None


def entry(slug):
    return load(ROOT / "projects" / f"{slug}.yml")


def values(slug):
    return load(DEPLOY / slug / "values.yaml") or {}


def merge(base, override):
    if isinstance(base, dict) and isinstance(override, dict):
        return {**base, **{key: merge(base.get(key), value) for key, value in override.items()}}
    return override


def spec(slug):
    folder = DEPLOY / slug
    shape = load(folder / "core.yaml") or {}
    settings = values(slug)
    services = shape.get("services") or {}
    components = [name for name in COMPONENTS if name in shape]
    unknown = set(settings) - {"enabled", *components, *services}
    if unknown:
        raise ValueError(f"deploy/{slug}/values.yaml sets {', '.join(sorted(unknown))}, which core.yaml does not define")
    merged = {key: value for key, value in shape.items() if key != "services"}
    merged["core"] = shape.get("core") or load(DEPLOY / "chart.yaml")["core"]
    merged["enabled"] = settings.get("enabled", True)
    for name in components:
        merged[name] = {**merge(shape[name] or {}, settings.get(name) or {}), "enabled": True}
    merged["services"] = {name: merge(service or {}, settings.get(name) or {}) for name, service in services.items()}
    for service in merged["services"].values():
        if "files" in service:
            service["files"] = {
                target: (folder / "files" / source).read_text(encoding="utf-8") for target, source in service["files"].items()
            }
    return merged


def required(project):
    names = {name for service in project["services"].values() for name in service.get("secrets", [])}
    if "postgres" in project:
        names.add("POSTGRES_PASSWORD")
    if project.get("redis", {}).get("password"):
        names.add("REDIS_PASSWORD")
    return sorted(names)


def tags(slug):
    path = ROOT / "tags" / f"{slug}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def compose_project(slug):
    return (DEPLOY / slug / "core.yaml").exists()


def projects():
    return sorted(path.name for path in DEPLOY.iterdir() if path.is_dir())


def mebibytes(memory):
    amount, unit = MEMORY.match(memory).groups()
    return int(amount) * (1024 if unit == "G" else 1)


def run(*command, capture=False, stdin=None):
    return subprocess.run(command, check=True, text=True, capture_output=capture, input=stdin).stdout


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


def core_version():
    return load(CHART / "Chart.yaml")["version"]


def released(version):
    return bool(git("tag", "--list", f"core-v{version}"))


def chart_at(version, workdir):
    target = workdir / f"core-{version}"
    if not target.exists():
        archive = subprocess.run(
            ["git", "-C", str(ROOT), "archive", f"core-v{version}", "charts/core"],
            check=True,
            capture_output=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(target, filter="data")
    return target / "charts" / "core"


def render(slug, project, chart, out):
    registered = entry(slug)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    given = out / "values.json"
    given.write_text(
        json.dumps({
            **project,
            "slug": slug,
            "name": registered["name"],
            "host": registered["deploy"].get("host", ""),
            "tags": tags(slug),
        }),
        encoding="utf-8",
    )
    run("helm", "template", slug, str(chart), "--values", str(given), "--output-dir", str(out), capture=True)
    templates = out / "core" / "templates"
    return templates / "compose.yaml", templates / "route.yaml"


def release_core():
    version = core_version()
    if released(version):
        print(f"core {version} is already released.")
        return
    git("tag", f"core-v{version}")
    git("push", "origin", f"core-v{version}")
    print(f"Released core {version}.")


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
            if "deploy/chart.yaml" in changed:
                chosen = sorted({*chosen, *filter(compose_project, everything)})
            edge = any(path.startswith(("edge/", "host/")) for path in changed)

    compose, pages, down = [], [], []
    for slug in chosen:
        registered = entry(slug)
        kind = registered["deploy"]["kind"] if registered else None
        if kind == "compose" and compose_project(slug):
            (compose if spec(slug)["enabled"] else down).append(slug)
        elif kind == "pages" and (DEPLOY / slug).is_dir() and values(slug).get("enabled", True):
            pages.append(slug)
    return {"edge": edge, "compose": compose, "down": down, "wake": edge or bool(compose or down), "pages": pages}


def show_plan():
    for key, value in plan().items():
        print(f"{key}={json.dumps(value)}")


def deploy_compose(slug):
    project = spec(slug)
    with tempfile.TemporaryDirectory() as workdir:
        compose, route = render(slug, project, chart_at(project["core"], pathlib.Path(workdir)), pathlib.Path(workdir) / "out")
        print(agent.send({
            "name": slug,
            "action": "up",
            "files": {"compose.yaml": compose.read_text(encoding="utf-8"), "sleep": project.get("sleep", {}).get("after")},
            "route": route.read_text(encoding="utf-8") if route.exists() else None,
            "require": required(project),
        }))
    host = entry(slug)["deploy"]["host"]
    cloudflare.point_at_vps(host, project.get("proxied", False))
    print(f"Deployed {slug} at https://{host}.")


def take_down(slug):
    print(agent.send({"name": slug, "action": "down"}))


def deploy_edge():
    cloudflare.point_at_vps(DEPLOY_HOST)
    print(agent.send({
        "name": "edge",
        "action": "up",
        "files": {name: (ROOT / "edge" / name).read_text(encoding="utf-8") for name in ("compose.yaml", "traefik.yml")},
        "route": (ROOT / "edge" / "agent.yml").read_text(encoding="utf-8"),
        "require": ["CF_DNS_API_TOKEN"],
    }))


def deploy_wake():
    jobs = []
    for slug in filter(compose_project, projects()):
        project = spec(slug)
        sleep = project.get("sleep", {})
        if project["enabled"] and sleep.get("wake"):
            url = (
                f"http://sablier:10000/api/strategies/blocking?group={slug}"
                f"&session_duration={sleep.get('wakeFor', '15m')}&timeout=5m"
            )
            jobs.append(
                f'[job-run "wake-{slug}"]\n'
                f"schedule = {sleep['wake']}\n"
                f"image = {CURL}\n"
                "network = edge\n"
                f"command = -fsS -o /dev/null --max-time 360 {url}\n"
                "delete = true\n"
            )
    if not jobs:
        print(agent.send({"name": "wake", "action": "down"}))
        return
    print(agent.send({
        "name": "wake",
        "action": "up",
        "files": {"compose.yaml": (ROOT / "edge" / "wake.yaml").read_text(encoding="utf-8"), "ofelia.ini": "\n".join(jobs)},
        "restart": True,
    }))


def apply():
    steps = plan()
    failed = []
    for name, step in (
        [("edge", deploy_edge)] * steps["edge"]
        + [(slug, lambda slug=slug: deploy_compose(slug)) for slug in steps["compose"]]
        + [(slug, lambda slug=slug: take_down(slug)) for slug in steps["down"]]
        + [("wake", deploy_wake)] * steps["wake"]
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
    tag = values(slug).get("tag") or tags(slug).get("site")
    if not tag:
        sys.exit(f"error: {slug} has no recorded build; push to its main branch or pin tag in deploy/{slug}/values.yaml")
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
    rows = []
    version = core_version()
    base = os.environ.get("BASE_REF")
    if base and released(version) and subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--quiet", f"origin/{base}...HEAD", "--", "charts/core"]
    ).returncode:
        problems.append(f"charts/core changed but core {version} is already released; bump charts/core/Chart.yaml")

    with tempfile.TemporaryDirectory() as workdir:
        workdir = pathlib.Path(workdir)
        for slug in projects():
            registered = entry(slug)
            kind = registered["deploy"]["kind"] if registered else None
            if kind == "pages":
                problems.extend(f"deploy/{slug}: unknown field {key}" for key in set(values(slug)) - {"enabled", "tag"})
                continue
            if kind != "compose" or not compose_project(slug):
                problems.append(f"deploy/{slug}: needs core.yaml and deploy.kind compose in projects/{slug}.yml, or deploy.kind pages")
                continue
            try:
                project = spec(slug)
            except (ValueError, OSError) as error:
                problems.append(f"deploy/{slug}: {error}")
                continue

            charts = []
            if released(project["core"]):
                charts.append(chart_at(project["core"], workdir))
            if project["core"] == version:
                charts.append(CHART)
            if not charts:
                problems.append(f"deploy/{slug}: core {project['core']} is not released")
                continue

            rendered = None
            for index, chart in enumerate(charts):
                try:
                    compose, _ = render(slug, project, chart, workdir / f"{slug}-{index}")
                    run("docker", "compose", "-f", str(compose), "config", "--quiet", "--no-interpolate", capture=True)
                    rendered = compose.read_text(encoding="utf-8")
                except subprocess.CalledProcessError as error:
                    problems.append(f"deploy/{slug}: {(error.stderr or error.stdout).strip()}")
            if rendered is None:
                continue

            problems.extend(
                f"deploy/{slug}: ${{{name}}} has no default; add {name} to a service's secrets"
                for name in sorted(set(REFERENCE.findall(rendered)) - set(required(project)))
            )

            limits = reserves = 0
            for service in yaml.safe_load(rendered)["services"].values():
                resources = service["deploy"]["resources"]
                limits += mebibytes(resources["limits"]["memory"])
                reserves += mebibytes(resources.get("reservations", {}).get("memory", "0M"))
            rows.append((slug, limits, reserves, project["enabled"]))

    host = load(DEPLOY / "host.yaml")
    budget = mebibytes(host["memory"]) - mebibytes(host["system"])
    reserved = sum(row[2] for row in rows)
    print(f"{'project':<24}{'limit':>8}{'reserve':>9}  enabled")
    for slug, limit, reserve, on in rows:
        print(f"{slug:<24}{limit:>7}M{reserve:>8}M  {'yes' if on else 'no'}")
    print(f"{'total':<24}{sum(row[1] for row in rows):>7}M{reserved:>8}M  budget {budget}M")
    if reserved > budget:
        problems.append(f"reserved memory {reserved}M exceeds the {budget}M budget in deploy/host.yaml")

    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    return 1 if problems else 0


COMMANDS = {
    "release-core": release_core,
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
