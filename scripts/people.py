import json
import sys
import urllib.request

import yaml

import sync
import validate

LABELS = {"generic": "website"}


def get(path):
    with urllib.request.urlopen(sync.request(path)) as response:
        return json.load(response)


def members():
    page = 1
    while True:
        entries = get(f"/orgs/{sync.ORGANIZATION}/members?per_page=100&page={page}")
        yield from entries
        if len(entries) < 100:
            return
        page += 1


def links(user):
    found = [{"label": "github", "url": user["html_url"]}]
    if user.get("blog"):
        blog = user["blog"] if "://" in user["blog"] else f"https://{user['blog']}"
        found.append({"label": "website", "url": blog})
    found.extend(
        {"label": LABELS.get(account["provider"], account["provider"]), "url": account["url"]}
        for account in get(f"/users/{user['login']}/social_accounts")
    )

    unique = []
    for link in found:
        if link["url"].startswith("https://") and link not in unique:
            unique.append(link)
    return unique[:6]


def from_github(person, user):
    profile = {key: value for key, value in person.items() if key != "headline"}
    profile["name"] = user["name"] or user["login"]
    headline = " ".join((user["bio"] or "").split())
    if 4 <= len(headline) <= 100:
        profile["headline"] = headline
    profile["links"] = links(user)
    return profile


def main():
    people = {
        person["github"].lower(): (path, person)
        for path, person in validate.load("people")
        if isinstance(person, dict) and isinstance(person.get("github"), str)
    }
    changed = []

    for member in members():
        login = member["login"]
        path, person = people.get(
            login.lower(),
            (
                sync.ROOT / "people" / f"{login.lower()}.yml",
                {"handle": login.lower(), "github": login, "profile": "github"},
            ),
        )
        if person.get("profile") == "manual":
            continue

        updated = from_github(person, get(f"/users/{login}"))
        if updated != person:
            path.write_text(
                yaml.safe_dump(updated, sort_keys=False, allow_unicode=True, width=100),
                encoding="utf-8",
            )
            changed.append(updated["handle"])

    if changed:
        print(f"Updated {len(changed)} people from GitHub profiles: {', '.join(sorted(changed))}.")
    else:
        print("People already match their GitHub profiles.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
