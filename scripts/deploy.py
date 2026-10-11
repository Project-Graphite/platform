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

import cloudflare

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
CHART = ROOT / "charts" / "core"
REMOTE = "graphite"
BASE = "/srv/graphite"
CURL = "curlimages/curl:8.22.0"
CHANGE = re.compile(r"^(?:deploy|tags|projects)/([a-z0-9-]+)(?:/|[.]json$|[.]yml$)")
REFERENCE = re.compile(r"(?<![$])[$][{]([A-Z][A-Z0-9_]*)[}]")
MEMORY = re.compile(r"^([0-9]+)([MG])$")


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None


def entry(slug):
    return load(ROOT / "projects" / f"{slug}.yml")


def values(slug):
    return load(DEPLOY / slug / "values.yaml")


def tags(slug):
    path = ROOT / "tags" / f"{slug}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def projects():
    return sorted(path.parent.name for path in DEPLOY.glob("*/values.yaml"))


def enabled(spec):
    return bool(spec) and spec.get("enabled", True)


def mebibytes(memory):
    amount, unit = MEMORY.match(memory).groups()
    return int(amount) * (1024 if unit == "G" else 1)


def run(*command, capture=False, stdin=None):
    return subprocess.run(command, check=True, text=True, capture_output=capture, input=stdin).stdout


def git(*args):
    return run("git", "-C", str(ROOT), *args, capture=True).strip()


def ssh(command, capture=False, stdin=None):
    return run("ssh", REMOTE, command, capture=capture, stdin=stdin)


def upload(path, target):
    ssh(f"cat > {target}", stdin=path.read_text(encoding="utf-8"))


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


def render(slug, chart, out):
    registered = entry(slug)
    name = registered["name"].replace(",", r"\,")
    shutil.rmtree(out, ignore_errors=True)
    run(
        "helm", "template", slug, str(chart),
        "--values", str(DEPLOY / slug / "values.yaml"),
        "--set-string", f"slug={slug}",
        "--set-string", f"host={registered['deploy'].get('host', '')}",
        "--set-string", f"name={name}",
        "--set-json", f"tags={json.dumps(tags(slug))}",
        "--output-dir", str(out),
        capture=True,
    )
    templates = out / "core" / "templates"
    return templates / "compose.yaml", templates / "route.yaml"


def prepare():
    ssh(f"mkdir -p {BASE}/bin")
    upload(ROOT / "host" / "graphite-up", f"{BASE}/bin/graphite-up")
    ssh(f"chmod 755 {BASE}/bin/graphite-up")


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
            edge = any(path.startswith(("edge/", "host/")) for path in changed)

    compose, pages, down = [], [], []
    for slug in chosen:
        registered, spec = entry(slug), values(slug)
        kind = registered["deploy"]["kind"] if registered else None
        if kind == "compose":
            (compose if enabled(spec) else down).append(slug)
        elif kind == "pages" and enabled(spec):
            pages.append(slug)

    print(f"compose={json.dumps(compose)}")
    print(f"pages={json.dumps(pages)}")
    print(f"down={json.dumps(down)}")
    print(f"edge={str(edge).lower()}")
    print(f"wake={str(edge or bool(compose or down)).lower()}")


def deploy_compose(slug):
    spec = values(slug)
    remote = f"{BASE}/{slug}"
    with tempfile.TemporaryDirectory() as workdir:
        workdir = pathlib.Path(workdir)
        compose, route = render(slug, chart_at(spec["core"], workdir), workdir / "out")
        prepare()
        ssh(f"mkdir -p {remote} && touch {remote}/secrets.env && chmod 600 {remote}/secrets.env")
        present = set(ssh(f"cut -d= -f1 {remote}/secrets.env", capture=True).split())
        missing = sorted(set(spec.get("secrets", [])) - present)
        if missing:
            sys.exit(f"error: {slug} is waiting on secrets: {', '.join(missing)}")
        upload(compose, f"{remote}/compose.yaml")
        after = spec.get("sleep", {}).get("after")
        ssh(f"printf %s {after} > {remote}/sleep" if after else f"rm -f {remote}/sleep")
        ssh(f"{BASE}/bin/graphite-up {slug}")
        if route.exists():
            upload(route, f"{BASE}/edge/routes/{slug}.yml")
        else:
            ssh(f"rm -f {BASE}/edge/routes/{slug}.yml")
    host = entry(slug)["deploy"]["host"]
    cloudflare.point(host, cloudflare.tunnel())
    print(f"Deployed {slug} at https://{host}.")


def take_down(slug):
    remote = f"{BASE}/{slug}"
    ssh(
        f"rm -f {BASE}/edge/routes/{slug}.yml {remote}/sleep; "
        f"if [ -f {remote}/compose.yaml ]; then "
        f"cd {remote} && docker compose -p {slug} --env-file secrets.env down --remove-orphans; fi"
    )
    print(f"{slug} is not deployed.")


def deploy_edge():
    cloudflare.configure_tunnel(os.environ["DEPLOY_SSH_HOST"])
    prepare()
    ssh(f"mkdir -p {BASE}/edge/routes")
    for name in ("compose.yaml", "traefik.yml"):
        upload(ROOT / "edge" / name, f"{BASE}/edge/{name}")
    ssh(f"{BASE}/bin/graphite-up edge")
    print("The edge is up.")


def deploy_wake():
    jobs = []
    for slug in projects():
        spec = values(slug)
        sleep = spec.get("sleep", {}) if spec else {}
        if enabled(spec) and sleep.get("wake"):
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
    remote = f"{BASE}/wake"
    if not jobs:
        ssh(f"if [ -f {remote}/compose.yaml ]; then cd {remote} && docker compose -p wake down; fi")
        print("No project wakes on a schedule.")
        return
    prepare()
    ssh(f"mkdir -p {remote}")
    ssh(f"cat > {remote}/ofelia.ini", stdin="\n".join(jobs))
    upload(ROOT / "edge" / "wake.yaml", f"{remote}/compose.yaml")
    ssh(f"{BASE}/bin/graphite-up wake && cd {remote} && docker compose -p wake restart")
    print(f"Scheduled {len(jobs)} wake jobs.")


def pages_info(slug):
    registered, spec = entry(slug), values(slug)
    tag = spec.get("tag") or tags(slug).get("site")
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
            registered, spec = entry(slug), values(slug)
            kind = registered["deploy"]["kind"] if registered else None
            if kind == "pages":
                problems.extend(f"deploy/{slug}: unknown field {key}" for key in set(spec) - {"enabled", "tag"})
                continue
            if kind != "compose":
                problems.append(f"deploy/{slug}: projects/{slug}.yml must declare deploy.kind compose or pages")
                continue

            charts = []
            if released(spec.get("core", "")):
                charts.append(chart_at(spec["core"], workdir))
            if spec.get("core") == version:
                charts.append(CHART)
            if not charts:
                problems.append(f"deploy/{slug}: core {spec.get('core')} is not released")
                continue

            for index, chart in enumerate(charts):
                try:
                    compose, _ = render(slug, chart, workdir / f"{slug}-{index}")
                    run("docker", "compose", "-f", str(compose), "config", "--quiet", "--no-interpolate", capture=True)
                except subprocess.CalledProcessError as error:
                    problems.append(f"deploy/{slug}: {(error.stderr or error.stdout).strip()}")

            declared = set(spec.get("secrets", []))
            for name, service in spec.get("services", {}).items():
                for key, value in service.get("env", {}).items():
                    problems.extend(
                        f"deploy/{slug}: services.{name}.env.{key} uses ${{{reference}}} without a default; add {reference} to secrets"
                        for reference in REFERENCE.findall(str(value))
                        if reference not in declared
                    )

            services = spec.get("services", {}).values()
            limit = sum(mebibytes(service["resources"]["memory"]) for service in services)
            reserve = sum(mebibytes(service["resources"].get("reserve", "0M")) for service in services)
            rows.append((slug, limit, reserve, enabled(spec)))

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
    "plan": plan,
    "check": check,
    "compose": deploy_compose,
    "down": take_down,
    "edge": deploy_edge,
    "wake": deploy_wake,
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
