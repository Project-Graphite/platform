import contextlib
import datetime
import hashlib
import http.client
import io
import json
import math
import os
import pathlib
import shutil
import socket
import sqlite3
import sys
import tarfile
import tempfile
import time
import traceback
import urllib.parse

ROOT = pathlib.Path(os.environ.get("BACKUP_ROOT", "/backups"))
VOLUMES = pathlib.Path(os.environ.get("VOLUMES_ROOT", "/volumes"))
REQUESTS = ROOT / ".requests"
WORK = ROOT / ".work"
SCHEDULE = {"enabled": True, "everyHours": 24, "hour": 3, "keepDays": 14}
STAMP = "%Y%m%dT%H%M%SZ"
SQLITE = b"SQLite format 3\x00"
SIDECARS = ("-journal", "-wal", "-shm")
DUMP = "/tmp/graphite-backup.dump"


def log(message):
    print(f"{datetime.datetime.now(datetime.UTC):%Y-%m-%dT%H:%M:%SZ} {message}", flush=True)


class Docker(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=1800)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


def docker(method, path, body=None, stream=False, headers=None):
    connection = Docker()
    if isinstance(body, (dict, list)):
        body, headers = json.dumps(body), {"Content-Type": "application/json"}
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    if response.status >= 400:
        raise RuntimeError(f"Docker answered {response.status} to {method} {path.split('?')[0]}: {response.read().decode(errors='replace')[:300]}")
    if stream:
        return response
    data = response.read()
    connection.close()
    return json.loads(data) if data else None


def filtered(path, **filters):
    return f"{path}?all=1&filters={urllib.parse.quote(json.dumps(filters))}"


def execute(container, command):
    created = docker("POST", f"/containers/{container}/exec", {"Cmd": command, "AttachStdout": True, "AttachStderr": True})
    response = docker("POST", f"/exec/{created['Id']}/start", {"Detach": False, "Tty": False}, stream=True)
    output = {1: bytearray(), 2: bytearray()}
    while header := response.read(8):
        output.setdefault(header[0], bytearray()).extend(response.read(int.from_bytes(header[4:], "big")))
    code = docker("GET", f"/exec/{created['Id']}/json")["ExitCode"]
    if code:
        raise RuntimeError(f"{command[0]} exited with {code}: {output[2].decode(errors='replace')[-500:].strip()}")
    return output[1].decode()


def digest(stream):
    hasher = hashlib.sha256()
    while chunk := stream.read(1 << 20):
        hasher.update(chunk)
    return hasher.hexdigest()


def file_digest(path):
    with open(path, "rb") as stream:
        return digest(stream)


def is_sqlite(path):
    with open(path, "rb") as stream:
        return stream.read(len(SQLITE)) == SQLITE


def copy_sqlite(source, target):
    target.unlink(missing_ok=True)
    with contextlib.closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as reader:
        with contextlib.closing(sqlite3.connect(target)) as writer:
            reader.backup(writer)


def integrity(path):
    with contextlib.closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as database:
        result = database.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"integrity_check answered {result}")


def sidecar(path):
    for suffix in SIDECARS:
        if path.name.endswith(suffix):
            base = path.with_name(path.name[: -len(suffix)])
            return base.is_file() and is_sqlite(base)
    return False


def pack_volume(source, target, work):
    databases = []
    with tarfile.open(target, "w:gz") as tar:
        for path in sorted(source.rglob("*")):
            name = path.relative_to(source).as_posix()
            if path.is_file() and not path.is_symlink() and not sidecar(path) and is_sqlite(path):
                copy = work / "copy.db"
                copy_sqlite(path, copy)
                info = tar.gettarinfo(path, name)
                info.size = copy.stat().st_size
                with open(copy, "rb") as stream:
                    tar.addfile(info, stream)
                databases.append(name)
            elif not (path.is_file() and sidecar(path)):
                tar.add(path, name, recursive=False)
    return databases


def check_volume(packed, item, work):
    with tarfile.open(fileobj=packed, mode="r|gz") as inner:
        for entry in inner:
            if not entry.isfile():
                continue
            with inner.extractfile(entry) as data:
                if entry.name not in item["sqlite"]:
                    digest(data)
                    continue
                copy = work / "check.db"
                with open(copy, "wb") as out:
                    shutil.copyfileobj(data, out)
                integrity(copy)


def verify(archive, project):
    errors = []
    with tempfile.TemporaryDirectory(dir=WORK) as work, tarfile.open(archive) as tar:
        manifest = json.load(tar.extractfile("manifest.json"))
        if manifest.get("project") != project:
            return [f"the archive is for {manifest.get('project')}, not {project}"]
        for item in manifest["items"]:
            try:
                with tar.extractfile(item["path"]) as stream:
                    if digest(stream) != item["sha256"]:
                        errors.append(f"{item['path']} does not match its checksum")
                        continue
                if item["kind"] == "volume":
                    with tar.extractfile(item["path"]) as packed:
                        check_volume(packed, item, pathlib.Path(work))
            except (KeyError, OSError, tarfile.TarError, sqlite3.Error, RuntimeError) as error:
                errors.append(f"{item['path']}: {error}")
    return errors


def dump_postgres(container, target):
    if container["State"] != "running":
        raise RuntimeError("its container is not running")
    script = f'set -e; pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f {DUMP}; pg_restore --list {DUMP} > /dev/null; sha256sum {DUMP}'
    try:
        expected = execute(container["Id"], ["sh", "-c", script]).split()[0]
        with tarfile.open(fileobj=docker("GET", f"/containers/{container['Id']}/archive?path={DUMP}", stream=True), mode="r|") as stream:
            for member in stream:
                with stream.extractfile(member) as source, open(target, "wb") as out:
                    shutil.copyfileobj(source, out)
    finally:
        execute(container["Id"], ["rm", "-f", DUMP])
    if file_digest(target) != expected:
        raise RuntimeError("the dump changed on its way out of the container")


def targets():
    found = {}
    for container in docker("GET", filtered("/containers/json", label=["graphite.backup.postgres=true"])):
        project = container["Labels"].get("com.docker.compose.project", "other")
        found.setdefault(project, {"postgres": [], "volumes": []})["postgres"].append(container)
    for container in docker("GET", filtered("/containers/json", label=["graphite.backup.volumes"])):
        project = container["Labels"].get("com.docker.compose.project", "other")
        volumes = found.setdefault(project, {"postgres": [], "volumes": []})["volumes"]
        for name in filter(None, (part.strip() for part in container["Labels"]["graphite.backup.volumes"].split(","))):
            if all(volume["Name"] != f"{project}_{name}" for volume in volumes):
                volumes.append({"Name": f"{project}_{name}", "Labels": {"com.docker.compose.volume": name}})
    return found


def write_summary(folder, stamp, summary):
    temporary = folder / f"{stamp}.json.partial"
    temporary.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    temporary.replace(folder / f"{stamp}.json")


def summarise(archive, project, manifest, errors):
    errors = errors + verify(archive, project)
    return {**manifest, "archive": archive.name, "bytes": archive.stat().st_size, "sha256": file_digest(archive), "ok": not errors, "errors": errors}


def backup(project, found, now):
    stamp = now.strftime(STAMP)
    folder = ROOT / project
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / f"{stamp}.tar"
    manifest = {"project": project, "stamp": stamp, "started": now.isoformat(), "items": []}
    errors = []
    with tempfile.TemporaryDirectory(dir=WORK) as work, tarfile.open(f"{archive}.partial", "w") as tar:
        work = pathlib.Path(work)

        def add(path, name, **item):
            tar.add(path, name)
            manifest["items"].append({"path": name, "bytes": path.stat().st_size, "sha256": file_digest(path), **item})

        for container in found["postgres"]:
            service = container["Labels"].get("com.docker.compose.service", container["Id"][:12])
            try:
                dump_postgres(container, work / f"{service}.dump")
                add(work / f"{service}.dump", f"postgres/{service}.dump", kind="postgres", service=service)
            except (RuntimeError, OSError, tarfile.TarError) as error:
                errors.append(f"postgres {service}: {error}")
        for volume in found["volumes"]:
            name = (volume.get("Labels") or {}).get("com.docker.compose.volume", volume["Name"])
            try:
                databases = pack_volume(VOLUMES / volume["Name"] / "_data", work / f"{name}.tar.gz", work)
                add(work / f"{name}.tar.gz", f"volumes/{name}.tar.gz", kind="volume", volume=volume["Name"], sqlite=databases)
            except (OSError, sqlite3.Error, tarfile.TarError) as error:
                errors.append(f"volume {name}: {error}")
        manifest["finished"] = datetime.datetime.now(datetime.UTC).isoformat()
        data = json.dumps(manifest, indent=2).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size, info.mtime = len(data), int(time.time())
        tar.addfile(info, io.BytesIO(data))
    pathlib.Path(f"{archive}.partial").replace(archive)
    summary = summarise(archive, project, manifest, errors)
    write_summary(folder, stamp, summary)
    log(f"{project}: {archive.name}, {summary['bytes']} bytes, {'verified' if summary['ok'] else 'FAILED: ' + '; '.join(summary['errors'])}")
    return summary


def schedule():
    try:
        saved = json.loads((ROOT / ".schedule.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        saved = {}
    return {**SCHEDULE, **saved}


def next_run(plan, after):
    period = plan["everyHours"] * 3600
    anchor = plan["hour"] * 3600
    return datetime.datetime.fromtimestamp(anchor + math.ceil((after.timestamp() - anchor) / period) * period, datetime.UTC)


def prune(now):
    cutoff = now - datetime.timedelta(days=schedule()["keepDays"])
    for path in ROOT.glob("*/*.tar"):
        try:
            stamp = datetime.datetime.strptime(path.stem, STAMP).replace(tzinfo=datetime.UTC)
        except ValueError:
            continue
        if stamp < cutoff:
            path.unlink()
            path.with_suffix(".json").unlink(missing_ok=True)
            log(f"removed {path.parent.name}/{path.name}")
    for path in REQUESTS.glob("*.result"):
        if path.stat().st_mtime < cutoff.timestamp():
            path.unlink()


def run(only=None):
    now = datetime.datetime.now(datetime.UTC)
    found = targets()
    if only is not None and only not in found:
        raise RuntimeError(f"{only} has no container labelled graphite.backup.postgres and no volume labelled graphite.backup")
    summaries = []
    for project, items in sorted(found.items()):
        if only is not None and project != only:
            continue
        try:
            summaries.append(backup(project, items, now))
        except (OSError, RuntimeError, tarfile.TarError, json.JSONDecodeError) as error:
            (ROOT / project).mkdir(parents=True, exist_ok=True)
            write_summary(ROOT / project, now.strftime(STAMP), {"project": project, "stamp": now.strftime(STAMP), "started": now.isoformat(), "items": [], "ok": False, "errors": [str(error)]})
            log(f"{project}: FAILED: {error}")
            summaries.append({"project": project, "ok": False, "errors": [str(error)]})
    prune(now)
    return summaries


def project_containers(project):
    return docker("GET", filtered("/containers/json", label=[f"com.docker.compose.project={project}"]))


def put_file(container, source, size, name, work):
    bundle = work / "upload.tar"
    with tarfile.open(bundle, "w") as tar:
        info = tarfile.TarInfo(name)
        info.size, info.mtime = size, int(time.time())
        tar.addfile(info, source)
    with open(bundle, "rb") as body:
        docker("PUT", f"/containers/{container}/archive?path=/tmp", body=body, headers={"Content-Type": "application/x-tar", "Content-Length": str(bundle.stat().st_size)})


def wait_healthy(ids, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        states = [docker("GET", f"/containers/{container}/json")["State"] for container in ids]
        if all(state["Running"] and state.get("Health", {}).get("Status", "healthy") == "healthy" for state in states):
            return
        time.sleep(5)
    raise RuntimeError("the project was not healthy five minutes after the restore")


def restore(project, name):
    archive = ROOT / project / name
    errors = verify(archive, project)
    if errors:
        raise RuntimeError("the archive failed verification: " + "; ".join(errors))
    containers = project_containers(project)
    databases = {c["Labels"].get("com.docker.compose.service"): c for c in containers if c["Labels"].get("graphite.backup.postgres") == "true"}
    running = [c["Id"] for c in containers if c["State"] == "running" and c["Id"] not in {d["Id"] for d in databases.values()}]
    with tempfile.TemporaryDirectory(dir=WORK) as work, tarfile.open(archive) as tar:
        work = pathlib.Path(work)
        manifest = json.load(tar.extractfile("manifest.json"))
        dumps = [item for item in manifest["items"] if item["kind"] == "postgres"]
        for item in dumps:
            database = databases.get(item["service"])
            if not database or database["State"] != "running":
                raise RuntimeError(f"{project} has no running {item['service']} to restore into")
            with tar.extractfile(item["path"]) as source:
                put_file(database["Id"], source, item["bytes"], "graphite-restore.dump", work)
            execute(database["Id"], ["pg_restore", "--list", "/tmp/graphite-restore.dump"])
        for container in running:
            docker("POST", f"/containers/{container}/stop?t=30")
        try:
            for item in dumps:
                execute(databases[item["service"]]["Id"], [
                    "sh", "-c",
                    'pg_restore --clean --if-exists --no-owner --single-transaction -U "$POSTGRES_USER" -d "$POSTGRES_DB" /tmp/graphite-restore.dump',
                ])
            for item in (entry for entry in manifest["items"] if entry["kind"] == "volume"):
                target = VOLUMES / item["volume"] / "_data"
                for child in target.iterdir():
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                with tar.extractfile(item["path"]) as packed, tarfile.open(fileobj=packed, mode="r|gz") as inner:
                    inner.extractall(target, filter="tar")
        finally:
            for database in databases.values():
                execute(database["Id"], ["rm", "-f", "/tmp/graphite-restore.dump"])
            for container in running:
                docker("POST", f"/containers/{container}/start")
    wait_healthy([*running, *(d["Id"] for d in databases.values())])
    return f"Restored {project} from {name}: {len(dumps)} databases and {len(manifest['items']) - len(dumps)} volumes; it is healthy."


def handle(request):
    working = request.with_suffix(".running")
    request.replace(working)
    result = {"at": datetime.datetime.now(datetime.UTC).isoformat()}
    try:
        order = json.loads(working.read_text(encoding="utf-8"))
        result.update(order)
        log(f"{order['action']} {order.get('project')} {order.get('archive', '')} for {order.get('by')}")
        if order["action"] == "run":
            summaries = run(order["project"])
            result.update(ok=all(entry["ok"] for entry in summaries), output="; ".join(f"{entry['project']}: {'verified' if entry['ok'] else '; '.join(entry['errors'])}" for entry in summaries))
        elif order["action"] == "verify":
            archive = ROOT / order["project"] / order["archive"]
            with tarfile.open(archive) as tar:
                manifest = json.load(tar.extractfile("manifest.json"))
            summary = summarise(archive, order["project"], {**manifest, "stamp": archive.stem, "imported": True}, [])
            write_summary(archive.parent, archive.stem, summary)
            result.update(ok=summary["ok"], output="verified" if summary["ok"] else "; ".join(summary["errors"]))
        elif order["action"] == "restore":
            result.update(ok=True, output=restore(order["project"], order["archive"]))
        else:
            result.update(ok=False, output=f"unknown action {order['action']}")
    except Exception as error:
        traceback.print_exc()
        result.update(ok=False, output=str(error))
    working.with_suffix(".result").write_text(json.dumps(result), encoding="utf-8")
    working.unlink()
    log(f"{result.get('action')} {result.get('project')}: {'done' if result['ok'] else 'FAILED: ' + result['output']}")


def due(plan, last, now):
    return plan["enabled"] and next_run(plan, last + datetime.timedelta(milliseconds=1)) <= now


def serve():
    for folder in (REQUESTS, WORK):
        folder.mkdir(parents=True, exist_ok=True)
    for interrupted in REQUESTS.glob("*.running"):
        try:
            order = json.loads(interrupted.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            order = {}
        interrupted.with_suffix(".result").write_text(json.dumps({**order, "ok": False, "output": "interrupted by a restart"}), encoding="utf-8")
        interrupted.unlink()
    last = datetime.datetime.now(datetime.UTC)
    plan = schedule()
    log(f"waiting for requests; the next scheduled run is at {next_run(plan, last):%Y-%m-%d %H:%M}Z" if plan["enabled"] else "waiting for requests; scheduled runs are off")
    while True:
        for request in sorted(REQUESTS.glob("*.request")):
            handle(request)
        now = datetime.datetime.now(datetime.UTC)
        if due(schedule(), last, now):
            run()
        last = now
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(serve())
