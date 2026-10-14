import http.client
import json
import os
import pathlib
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEPLOY_HOST = "deploy.project-graphite.com"
LABEL = "alert"
CERT_DAYS = 14
RETRY_SECONDS = 30


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None


def targets():
    for path in sorted((ROOT / "projects").glob("*.yml")):
        deploy = (load(path) or {}).get("deploy") or {}
        folder = ROOT / "deploy" / path.stem
        if deploy.get("kind") in ("compose", "pages") and folder.is_dir() and (load(folder / "values.yaml") or {}).get("enabled", True):
            yield deploy["host"], "/"
            for service in ((load(folder / "core.yaml") or {}).get("services") or {}).values():
                for host in (service or {}).get("hosts", []):
                    yield host, "/"
    yield DEPLOY_HOST, "/healthz"


def check(host, path):
    try:
        connection = http.client.HTTPSConnection(host, timeout=20, context=ssl.create_default_context())
        connection.request("GET", path, headers={"User-Agent": "project-graphite-uptime curl"})
        status = connection.getresponse().status
        certificate = connection.sock.getpeercert()
        connection.close()
    except (OSError, ssl.SSLError, http.client.HTTPException) as error:
        return [f"unreachable: {error}"]
    problems = [f"answered HTTP {status}"] if status >= 500 else []
    days = (ssl.cert_time_to_seconds(certificate["notAfter"]) - time.time()) / 86400
    if days < CERT_DAYS:
        problems.append(f"its certificate expires in {days:.0f} days")
    return problems


def github(method, path, body=None):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}{path}",
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "User-Agent": "project-graphite-uptime",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main():
    results = {}
    for host, path in targets():
        problems = check(host, path)
        if problems:
            time.sleep(RETRY_SECONDS)
            problems = check(host, path)
        results[host] = problems
        print(f"{host}: {'; '.join(problems) or 'up'}")

    try:
        github("POST", "/labels", {"name": LABEL, "color": "b60205", "description": "Raised by Uptime or the console"})
    except urllib.error.HTTPError as error:
        if error.code != 422:
            raise
    open_issues = {
        issue["title"]: issue
        for issue in github("GET", f"/issues?labels={LABEL}&state=open&per_page=100")
        if "pull_request" not in issue
    }
    run = f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    for host, problems in results.items():
        title = f"Down: {host}"
        issue = open_issues.get(title)
        if problems and not issue:
            github("POST", "/issues", {
                "title": title,
                "labels": [LABEL],
                "body": f"`https://{host}` {' and '.join(problems)}, checked twice from GitHub Actions.\n\n{run}",
            })
            print(f"Opened an issue for {host}.")
        elif not problems and issue:
            github("POST", f"/issues/{issue['number']}/comments", {"body": f"Back up.\n\n{run}"})
            github("PATCH", f"/issues/{issue['number']}", {"state": "closed", "state_reason": "completed"})
            print(f"Closed the issue for {host}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
