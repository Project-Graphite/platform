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
CONSOLE_TOKEN = os.environ.get("CONSOLE_TOKEN", "").encode()
ENV = pathlib.Path("/etc/graphite")
NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
FILES = re.compile(r"^(compose\.yaml|traefik\.yml|values|files/[A-Za-z0-9._-]+)$")
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


def up(name, request):
    folder = BASE / name
    unknown = sorted(file for file in request.get("files", {}) if not FILES.match(file))
    if unknown:
        raise ValueError(f"{', '.join(unknown)} is not a file the agent writes")
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


def reapply(name):
    if not (BASE / name / "compose.yaml").exists():
        raise LookupError(f"{name} is not deployed")
    return run(str(BASE / "bin" / "graphite-up"), name)


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
        if self.path == "/reapply":
            allowed = CONSOLE_TOKEN and hmac.compare_digest(self.headers.get("Authorization", "").encode(), b"Bearer " + CONSOLE_TOKEN)
        else:
            allowed = self.path == "/deploy" and signed(
                self.headers.get("X-Graphite-Timestamp", ""), self.headers.get("X-Graphite-Signature", ""), body
            )
        if not allowed:
            self.reply(401, {"error": "unsigned or expired request"})
            return
        request = json.loads(body)
        name, action = request.get("name", ""), "reapply" if self.path == "/reapply" else request.get("action")
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
            elif action == "reapply":
                output = reapply(name)
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
