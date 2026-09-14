#!/usr/bin/env python3
"""roster_import.py - build roster.json from the Moodle form export (CSV).

  python3 roster_import.py export.csv --login-column "GitHub login" --repository-column "Repository URL"
  python3 roster_import.py export.csv ... --out roster.json          # write it (default: print to stdout)
  python3 roster_import.py export.csv ... --merge                    # keep entries already in --out

The repository column may hold `owner/name`, `https://github.com/owner/name`, `.../owner/name.git` or
`git@github.com:owner/name.git`; the login column a GitHub login (surrounding spaces and an `@` are
stripped). Every row is validated (login pattern, owner/name pattern, no duplicate login, no two logins on
one repository); all problems are listed and nothing is written when there is one (exit 2). Keys starting
with `_` in an existing roster are kept.

Live fix during a session: edit roster.json, commit and push to the submissions repository; the next
issue picks it up (the workflow checks the file out fresh every run). A refused receipt never costs an
attempt, so a student whose login was missing simply opens the issue again after the push.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}/[A-Za-z0-9._-]{1,100}$")
URL_RE = re.compile(r"^(?:https?://(?:www\.)?github\.com/|git@github\.com:)([^/\s]+/[^/\s]+?)(?:\.git)?/?$", re.I)


def normalise_login(value: str) -> str:
    return value.strip().lstrip("@").strip()


def normalise_repository(value: str) -> str:
    text = value.strip()
    match = URL_RE.match(text)
    if match:
        text = match.group(1)
    if text.lower().endswith(".git"):
        text = text[:-4]
    return text.strip("/")


def build_roster(rows: list[dict], login_column: str, repository_column: str) -> tuple[dict[str, str], list[str]]:
    roster: dict[str, str] = {}
    problems: list[str] = []
    seen_repos: dict[str, str] = {}
    for number, row in enumerate(rows, start=2):  # row 1 is the header
        if login_column not in row or repository_column not in row:
            problems.append(f"row {number}: missing column {login_column!r} or {repository_column!r}")
            continue
        login = normalise_login(row[login_column] or "")
        repository = normalise_repository(row[repository_column] or "")
        if not login and not repository:
            continue  # an empty line
        if not LOGIN_RE.match(login):
            problems.append(f"row {number}: {login!r} is not a GitHub login")
            continue
        if not REPOSITORY_RE.match(repository) or repository.split("/", 1)[1] in (".", ".."):
            problems.append(f"row {number}: {row[repository_column]!r} is not owner/name or a github.com URL")
            continue
        key = login.lower()
        if key in roster:
            problems.append(f"row {number}: login {login} appears twice ({roster[key]} and {repository})")
            continue
        if repository.lower() in seen_repos:
            problems.append(f"row {number}: repository {repository} is already registered for {seen_repos[repository.lower()]}")
            continue
        roster[key] = repository
        seen_repos[repository.lower()] = login
    return roster, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="roster_import.py", description="Moodle CSV -> roster.json")
    parser.add_argument("csv", help="the Moodle form export")
    parser.add_argument("--login-column", default="github_login")
    parser.add_argument("--repository-column", default="repository")
    parser.add_argument("--out", help="write here instead of stdout")
    parser.add_argument("--merge", action="store_true", help="keep entries already present in --out")
    args = parser.parse_args(argv)

    with open(args.csv, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    roster, problems = build_roster(rows, args.login_column, args.repository_column)
    if problems:
        print("roster not written:\n" + "\n".join(f"- {p}" for p in problems), file=sys.stderr)
        return 2
    existing: dict = {}
    if args.out and Path(args.out).is_file():
        existing = json.loads(Path(args.out).read_text(encoding="utf-8"))
    merged = {k: v for k, v in existing.items() if str(k).startswith("_")}
    if args.merge:
        merged.update({k: v for k, v in existing.items() if not str(k).startswith("_")})
    merged.update(roster)
    text = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"{len(roster)} students written to {args.out} ({len(merged) - len([k for k in merged if str(k).startswith('_')])} entries in total)")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
