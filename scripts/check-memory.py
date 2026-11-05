import re
import sys

import yaml

import deploy

UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def mebibytes(value):
    amount, unit = re.fullmatch(r"([0-9]+)([bkmg]?)b?", str(value).lower()).groups()
    return int(amount) * UNITS[unit] // 1024**2


def main():
    rows = []
    problems = []
    for slug in filter(deploy.compose_project, deploy.projects()):
        try:
            config = deploy.render(slug)[0]
        except ValueError as error:
            problems.append(str(error))
            continue
        resources = {name: service.get("deploy", {}).get("resources", {}) for name, service in config["services"].items()}
        problems.extend(f"deploy/{slug}: {name} has no memory limit" for name, entry in resources.items() if not entry.get("limits", {}).get("memory"))
        rows.append((
            slug,
            sum(mebibytes(entry.get("limits", {}).get("memory", 0)) for entry in resources.values()),
            sum(mebibytes(entry.get("reservations", {}).get("memory", 0)) for entry in resources.values()),
        ))

    host = yaml.safe_load(deploy.read(deploy.DEPLOY / "host.yaml"))
    budget = mebibytes(host["memory"]) - mebibytes(host["system"])
    reserved = sum(row[2] for row in rows)
    print(f"{'project':<24}{'limit':>8}{'reserve':>9}")
    for slug, limit, reserve in rows:
        print(f"{slug:<24}{limit:>7}M{reserve:>8}M")
    print(f"{'total':<24}{sum(row[1] for row in rows):>7}M{reserved:>8}M  budget {budget}M")
    if reserved > budget:
        problems.append(f"reserved memory {reserved}M exceeds the {budget}M budget in deploy/host.yaml")
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
