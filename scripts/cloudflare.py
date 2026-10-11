import json
import os
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.cloudflare.com/client/v4"
ROUTABLE = {"A", "AAAA", "CNAME"}


def call(method, path, body=None):
    request = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)["result"]
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise RuntimeError(f"Cloudflare answered {error.code} for {method} {path}: {error.read().decode()}") from error


def point(name, target):
    zone = os.environ["CLOUDFLARE_ZONE_ID"]
    records = [
        record
        for record in call("GET", f"/zones/{zone}/dns_records?name={urllib.parse.quote(name)}")
        if record["type"] in ROUTABLE
    ]
    if [(record["type"], record["content"], record["proxied"]) for record in records] == [("CNAME", target, True)]:
        return
    for record in records:
        call("DELETE", f"/zones/{zone}/dns_records/{record['id']}")
    call("POST", f"/zones/{zone}/dns_records", {"type": "CNAME", "name": name, "content": target, "proxied": True})
    print(f"Pointed {name} at {target}.")


def tunnel():
    return f"{os.environ['TUNNEL_ID']}.cfargotunnel.com"


def configure_tunnel(ssh_host):
    account = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    call(
        "PUT",
        f"/accounts/{account}/cfd_tunnel/{os.environ['TUNNEL_ID']}/configurations",
        {
            "config": {
                "ingress": [
                    {"hostname": ssh_host, "service": "ssh://localhost:22"},
                    {"service": "http://localhost:8080"},
                ]
            }
        },
    )
    point(ssh_host, tunnel())


def pages_site(project, host):
    projects = f"/accounts/{os.environ['CLOUDFLARE_ACCOUNT_ID']}/pages/projects"
    existing = call("GET", f"{projects}/{project}")
    if existing is None:
        call("POST", projects, {"name": project, "production_branch": "main"})
        print(f"Created the Pages project {project}.")
    elif existing.get("source"):
        raise RuntimeError(
            f"The Pages project {project} is connected to Git. Delete it in Cloudflare so the "
            "platform can create a direct-upload project with the same name."
        )
    if host not in {domain["name"] for domain in call("GET", f"{projects}/{project}/domains")}:
        call("POST", f"{projects}/{project}/domains", {"name": host})
        print(f"Attached {host} to {project}.")
    point(host, f"{project}.pages.dev")
