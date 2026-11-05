import hashlib
import hmac
import json
import os
import pathlib
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = pathlib.Path(os.environ.get("GRAPHITE_HOME", "/srv/graphite"))
TOKEN = os.environ["DEPLOY_TOKEN"].encode()
ENV = pathlib.Path("/etc/graphite")
NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
FILES = re.compile(r"^(compose\.yaml|traefik\.yml|values|files/[A-Za-z0-9._-]+)$")
HOST_ACCESS = {"console", "edge", "backup"}
HOST_SETTINGS = (
    "privileged", "pid", "ipc", "uts", "userns_mode", "network_mode", "cgroup", "cgroup_parent",
    "cap_add", "devices", "device_cgroup_rules", "volumes_from", "sysctls", "ports", "build", "env_file",
)
RULE = re.compile(r"^Host\(`[a-z0-9.-]+`\)(?: \|\| Host\(`[a-z0-9.-]+`\))*$")
HOSTS = re.compile(r"Host\(`([^`]+)`\)")
SKEW = 300
MAX_BODY = 4 << 20
CLEAN = {"PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"), "HOME": os.environ.get("HOME", "/root")}
LOCK = threading.Lock()
SEEN_LOCK = threading.Lock()
SEEN = {}


class Refused(Exception):
    pass


def signed(stamp, signature, body):
    if not stamp.isdigit() or abs(time.time() - int(stamp)) > SKEW:
        return False
    expected = hmac.new(TOKEN, stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False
    now = time.time()
    with SEEN_LOCK:
        for seen, until in list(SEEN.items()):
            if until < now:
                del SEEN[seen]
        if signature in SEEN:
            return False
        SEEN[signature] = now + 2 * SKEW
    return True


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(0o644)
    temporary.replace(path)


def run(*command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=600, env=CLEAN)
    output = (result.stdout + result.stderr)[-4000:]
    if result.returncode:
        raise RuntimeError(output.strip() or f"{command[0]} failed")
    return output


def env_file(name):
    path = ENV / f"{name}.env"
    return path if path.exists() else BASE / name / "secrets.env"


def keys(path):
    if not path.exists():
        return set()
    return {line.split("=", 1)[0] for line in path.read_text(encoding="utf-8").splitlines() if "=" in line}


def compose_options(name):
    values = BASE / name / "values"
    return [*(["--env-file", str(values)] if values.exists() else []), "--env-file", str(env_file(name))]


def inside(path):
    return isinstance(path, str) and path.startswith("./") and ".." not in pathlib.PurePosixPath(path).parts


def compose_problems(name, text):
    try:
        compose = json.loads(text)
    except ValueError:
        return ["compose.yaml is not the JSON deploy.py renders"]
    problems = []
    for service_name, service in (compose.get("services") or {}).items():
        where = f"services.{service_name}"
        problems.extend(f"{where}.{key}" for key in HOST_SETTINGS if service.get(key))
        if any(option != "no-new-privileges:true" for option in service.get("security_opt") or []):
            problems.append(f"{where}.security_opt")
        for volume in service.get("volumes") or []:
            if volume.get("type") not in ("volume", "bind", "tmpfs") or (volume.get("type") == "bind" and not inside(volume.get("source"))):
                problems.append(f"{where}.volumes {volume.get('source')}")
    for key in ("configs", "secrets"):
        for entry_name, entry in (compose.get(key) or {}).items():
            if entry.get("external") or entry.get("environment") or ("file" in entry and not inside(entry["file"])):
                problems.append(f"{key}.{entry_name}")
    for volume_name, volume in (compose.get("volumes") or {}).items():
        if volume.get("external") or volume.get("driver_opts") or volume.get("driver") not in (None, "local") or not str(volume.get("name", f"{name}_")).startswith(f"{name}_"):
            problems.append(f"volumes.{volume_name}")
    for network_name, network in (compose.get("networks") or {}).items():
        if network.get("driver_opts") or (network.get("external") and network.get("name", network_name) != "edge"):
            problems.append(f"networks.{network_name}")
    return problems


def route_problems(name, text):
    try:
        http = json.loads(text)["http"]
    except (ValueError, KeyError, TypeError):
        return ["the route is not the JSON deploy.py renders"]
    problems = [f"route {key}" for key in http if key not in ("routers", "services")]
    for router_name, router in http.get("routers", {}).items():
        if set(router) - {"rule", "entryPoints", "service", "tls"} or not RULE.match(router.get("rule", "")):
            problems.append(f"router {router_name}")
    for service_name, service in http.get("services", {}).items():
        for server in service.get("loadBalancer", {}).get("servers", []):
            backend = re.match(r"^http://([a-z0-9-]+):[0-9]+$", server.get("url", ""))
            if not backend or not (backend[1] == name or backend[1].startswith(f"{name}-")):
                problems.append(f"service {service_name} sends traffic to {server.get('url')}")
    claimed = set(HOSTS.findall(text))
    for other in (BASE / "edge" / "routes").glob("*.yml"):
        taken = claimed & set(HOSTS.findall(other.read_text(encoding="utf-8")))
        if other.stem != name and taken:
            problems.append(f"{', '.join(sorted(taken))} already belongs to {other.stem}")
    return problems


def up(name, request):
    folder = BASE / name
    unknown = sorted(file for file in request.get("files", {}) if not FILES.match(file))
    if unknown:
        raise ValueError(f"{', '.join(unknown)} is not a file the agent writes")
    if name not in HOST_ACCESS:
        problems = compose_problems(name, request.get("files", {}).get("compose.yaml") or "")
        if request.get("route") is not None:
            problems += route_problems(name, request["route"])
        if problems:
            raise Refused(f"{name} may not use: {'; '.join(problems)}")
    missing = sorted(set(request.get("require", [])) - keys(env_file(name)))
    if missing:
        raise LookupError(f"{name} is waiting on secrets: {', '.join(missing)}")
    for file, content in request.get("files", {}).items():
        if content is None:
            (folder / file).unlink(missing_ok=True)
        else:
            write(folder / file, content)
    output = run(str(BASE / "bin" / "graphite-up"), name)
    if "route" in request:
        route = BASE / "edge" / "routes" / f"{name}.yml"
        if request["route"] is None:
            route.unlink(missing_ok=True)
        else:
            write(route, request["route"])
    return output


def down(name):
    folder = BASE / name
    (BASE / "edge" / "routes" / f"{name}.yml").unlink(missing_ok=True)
    if not (folder / "compose.yaml").exists():
        return f"{name} is not deployed."
    return run("docker", "compose", "--project-directory", str(folder), "-p", name, *compose_options(name), "down", "--remove-orphans")


class Agent(BaseHTTPRequestHandler):
    timeout = 30

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply(200 if self.path == "/healthz" else 404, {"ok": self.path == "/healthz"})

    def do_POST(self):
        length = self.headers.get("Content-Length", "")
        if self.path != "/deploy" or not length.isdigit() or int(length) > MAX_BODY:
            self.reply(404 if self.path != "/deploy" else 413, {"error": "not found" if self.path != "/deploy" else "the request is too large"})
            return
        body = self.rfile.read(int(length))
        if not signed(self.headers.get("X-Graphite-Timestamp", ""), self.headers.get("X-Graphite-Signature", ""), body):
            self.reply(401, {"error": "unsigned, expired or repeated request"})
            return
        request = json.loads(body)
        name, action = request.get("name", ""), request.get("action")
        if not NAME.match(name):
            self.reply(400, {"error": f"{name!r} is not a project name"})
            return
        try:
            with LOCK:
                if action == "up":
                    output = up(name, request)
                elif action == "down":
                    output = down(name)
                else:
                    self.reply(400, {"error": f"unknown action {action!r}"})
                    return
        except Refused as error:
            self.reply(403, {"error": str(error)})
        except LookupError as error:
            self.reply(409, {"error": str(error)})
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            self.reply(500, {"error": str(error)})
        else:
            self.reply(200, {"output": output})


if __name__ == "__main__":
    ThreadingHTTPServer((os.environ.get("BIND", "172.17.0.1"), 9000), Agent).serve_forever()
