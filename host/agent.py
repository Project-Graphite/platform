import hashlib
import hmac
import json
import os
import pathlib
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = pathlib.Path(os.environ.get("GRAPHITE_HOME", "/srv/graphite"))
TOKEN = os.environ["DEPLOY_TOKEN"].encode()
NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
FILES = {"compose.yaml", "traefik.yml", "ofelia.ini", "sleep"}
SKEW = 300


def signed(stamp, signature, body):
    if not stamp.isdigit() or abs(time.time() - int(stamp)) > SKEW:
        return False
    expected = hmac.new(TOKEN, stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(0o600 if path.name == "secrets.env" else 0o644)
    temporary.replace(path)


def run(*command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    output = (result.stdout + result.stderr)[-4000:]
    if result.returncode:
        raise RuntimeError(output.strip() or f"{command[0]} failed")
    return output


def keys(path):
    if not path.exists():
        return set()
    return {line.split("=", 1)[0] for line in path.read_text(encoding="utf-8").splitlines() if "=" in line}


def up(name, request):
    folder = BASE / name
    unknown = sorted(set(request.get("files", {})) - FILES)
    if unknown:
        raise ValueError(f"{', '.join(unknown)} is not a file the agent writes")
    for file, content in request.get("files", {}).items():
        if content is None:
            (folder / file).unlink(missing_ok=True)
        else:
            write(folder / file, content)
    missing = sorted(set(request.get("require", [])) - keys(folder / "secrets.env"))
    if missing:
        raise LookupError(f"{name} is waiting on secrets: {', '.join(missing)}")
    output = run(str(BASE / "bin" / "graphite-up"), name)
    if request.get("restart"):
        output += run("docker", "compose", "--project-directory", str(folder), "-p", name, "restart")
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
    (folder / "sleep").unlink(missing_ok=True)
    if not (folder / "compose.yaml").exists():
        return f"{name} is not deployed."
    return run(
        "docker", "compose", "--project-directory", str(folder), "-p", name,
        "--env-file", str(folder / "secrets.env"), "down", "--remove-orphans",
    )


def secrets(name, request):
    folder = BASE / name
    write(folder / "secrets.env", request["secrets"])
    if not (folder / "compose.yaml").exists():
        return f"Stored the secrets for {name}; it is not deployed yet."
    return run(str(BASE / "bin" / "graphite-up"), name)


class Agent(BaseHTTPRequestHandler):
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
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path != "/deploy" or not signed(
            self.headers.get("X-Graphite-Timestamp", ""), self.headers.get("X-Graphite-Signature", ""), body
        ):
            self.reply(401, {"error": "unsigned or expired request"})
            return
        request = json.loads(body)
        name, action = request.get("name", ""), request.get("action")
        if not NAME.match(name):
            self.reply(400, {"error": f"{name!r} is not a project name"})
            return
        try:
            if action == "up":
                output = up(name, request)
            elif action == "down":
                output = down(name)
            elif action == "secrets":
                output = secrets(name, request)
            else:
                self.reply(400, {"error": f"unknown action {action!r}"})
                return
        except LookupError as error:
            self.reply(409, {"error": str(error)})
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            self.reply(500, {"error": str(error)})
        else:
            self.reply(200, {"output": output})


if __name__ == "__main__":
    HTTPServer((os.environ.get("BIND", "172.17.0.1"), 9000), Agent).serve_forever()
