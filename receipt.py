#!/usr/bin/env python3
"""Acceptance receipts for the ITSM 2026 course (design/LAB1.md 7.1, agenda 7.4 rule 1).

The workflow .github/workflows/receipt.yml runs this file on every opened issue. The issue body
arrives through the environment variable ISSUE_BODY (never through ${{ }} inside run:), the login
is the issue author (ISSUE_AUTHOR = github.event.issue.user.login), never a form field.

Pipeline of `receipt.py run` (all steps of LAB1.md 7.1):

  parse the issue-form body -> validate the fields with strict patterns -> roster check
  -> attempt count (prior issues of the same login labelled `receipted`, `lab:<n>`, `kind:<kind>`)
  -> resolve the tag (git ls-remote --tags, peeling ^{}) or verify the commit is on main
     (specs, prediction: the commit is the head of main)
  -> clone at the commit, tree_sha (authoritative) and archive_sha256 (informational)
  -> specs: tree rules; prediction: text_sha256
  -> deadlines.json -> late (after corrections_due) and after_attempt1_due (advisory)
  -> receipt JSON, the comment text, the repository_dispatch payload

Stdlib only; needs `git` on PATH. Exit code is always 0 for `run`; the outcome is reported through
out/status.txt and GITHUB_OUTPUT (status=receipted|refused|error) so the workflow can always post
the comment before deciding whether to fail.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KINDS = ("submission", "specs", "prediction")
MAX_ATTEMPTS = 3
LAB_MIN, LAB_MAX = 1, 8
DEFAULT_GIT_BASE = "https://github.com/"
DEFAULT_TIMEZONE = "Europe/Warsaw"
NO_RESPONSE = "_No response_"
RECEIPT_MARKER = "itsmlab receipt"

# Issue-form heading (the element's `label` in submission.yml) -> field id. tests/test_receipt.py
# cross-checks this table against the form so the two cannot drift apart.
FIELD_LABELS = {
    "Lab": "lab",
    "Kind": "kind",
    "Repository": "repository",
    "Tag": "tag",
    "Commit": "commit",
    "Text": "text",
}

HEADING_RE = re.compile(r"^### (.+?)\s*$")
# GitHub login: 1..39 chars, alphanumerics and single inner hyphens. Repository name: 1..100 chars
# of [A-Za-z0-9._-]; "." and ".." and a ".git" suffix are refused below.
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}/[A-Za-z0-9._-]{1,100}$")
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SPECS_MIN_BYTES = 500
SPECS_IGNORED_NAMES = (".gitkeep", "README.md")
SRC_ALLOWED = ("src/README.md",)


class ReceiptError(Exception):
    """A refusal: the request is invalid or not acceptable. The message is shown to the student."""


@dataclass
class Submission:
    lab: int
    kind: str
    repository: str
    tag: str | None
    commit: str | None
    text: str | None


@dataclass
class Outcome:
    status: str  # receipted | refused | error
    comment: str
    receipt: dict | None = None


# ----------------------------------------------------------------------------------------------
# Parsing and validation
# ----------------------------------------------------------------------------------------------

def parse_issue_body(body: str) -> dict[str, str]:
    """Map the issue-form headings to their values.

    Only the six known headings split the body; any other `### ...` line (for example inside a
    prediction text) belongs to the field before it. `_No response_` becomes an empty string.
    """
    fields: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current is not None:
            text = "\n".join(buffer).strip()
            fields[current] = "" if text == NO_RESPONSE else text

    for line in (body or "").replace("\r\n", "\n").split("\n"):
        match = HEADING_RE.match(line)
        if match and match.group(1) in FIELD_LABELS:
            flush()
            current = FIELD_LABELS[match.group(1)]
            buffer = []
        elif current is not None:
            buffer.append(line)
    flush()
    return fields


def valid_tag(tag: str) -> bool:
    """A conservative subset of git check-ref-format for refs/tags/<tag>."""
    if not TAG_RE.match(tag):
        return False
    if ".." in tag or "//" in tag or "@{" in tag or tag.endswith("/") or tag.endswith("."):
        return False
    for component in tag.split("/"):
        if not component or component.startswith(".") or component.endswith(".lock"):
            return False
    return True


def valid_repository(repository: str) -> bool:
    if not REPOSITORY_RE.match(repository):
        return False
    name = repository.split("/", 1)[1]
    return name not in (".", "..") and not name.lower().endswith(".git")


def validate_fields(fields: dict[str, str]) -> Submission:
    """Apply the strict field rules. Fields irrelevant to the kind are ignored."""
    problems: list[str] = []

    lab_raw = fields.get("lab", "")
    lab = 0
    if not lab_raw.isdigit() or not LAB_MIN <= int(lab_raw) <= LAB_MAX:
        problems.append(f"Lab must be a number from {LAB_MIN} to {LAB_MAX}, got {lab_raw!r}")
    else:
        lab = int(lab_raw)

    kind = fields.get("kind", "")
    if kind not in KINDS:
        problems.append(f"Kind must be one of {', '.join(KINDS)}, got {kind!r}")

    repository = fields.get("repository", "").strip()
    if not valid_repository(repository):
        problems.append(f"Repository must be owner/name of a GitHub repository, got {repository!r}")

    tag_raw = fields.get("tag", "").strip()
    commit_raw = fields.get("commit", "").strip()
    text_raw = fields.get("text", "")
    tag: str | None = None
    commit: str | None = None
    text: str | None = None

    if kind == "submission":
        if not tag_raw:
            problems.append("Tag is required for a submission (the git tag you pushed, e.g. lab1/v1)")
        elif not valid_tag(tag_raw):
            problems.append(f"Tag is not a valid git tag name: {tag_raw!r}")
        else:
            tag = tag_raw
        if commit_raw:
            if not SHA_RE.match(commit_raw):
                problems.append(f"Commit, when given, must be a 40-hex SHA, got {commit_raw!r}")
            else:
                commit = commit_raw.lower()
    elif kind in ("specs", "prediction"):
        if not SHA_RE.match(commit_raw):
            problems.append(f"Commit must be the 40-hex SHA printed by `itsmlab submit`, got {commit_raw!r}")
        else:
            commit = commit_raw.lower()
        if kind == "prediction":
            if not text_raw.strip():
                problems.append("Text is required for a prediction")
            else:
                text = text_raw

    if problems:
        raise ReceiptError("the form is not valid:\n" + "\n".join(f"- {p}" for p in problems))
    return Submission(lab=lab, kind=kind, repository=repository, tag=tag, commit=commit, text=text)


def check_roster(roster: dict[str, str], login: str, repository: str) -> str:
    """Return the roster's canonical repository for the login, or refuse."""
    lookup = {str(k).lower(): str(v) for k, v in roster.items() if not str(k).startswith("_")}
    expected = lookup.get(login.lower())
    if expected is None:
        raise ReceiptError(f"login `{login}` is not in the roster; ask the lecturer to add you")
    if expected.lower() != repository.lower():
        raise ReceiptError(
            f"repository `{repository}` is not the repository registered for `{login}` (`{expected}`)"
        )
    return expected


# ----------------------------------------------------------------------------------------------
# Git
# ----------------------------------------------------------------------------------------------

def git(*args: str, cwd: str | Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return result.stdout


def git_error_text(exc: subprocess.CalledProcessError) -> str:
    """Last line of git's stderr (or stdout), or the exit code when it printed nothing."""
    text = (exc.stderr or exc.stdout or "").strip()
    return text.splitlines()[-1] if text else f"exit {exc.returncode}"


def resolve_tag(url: str, tag: str) -> str | None:
    """Commit the tag points to on the remote: the peeled `^{}` line for an annotated tag, else the
    plain line for a lightweight tag. None when the tag does not exist."""
    out = git("ls-remote", "--tags", url, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
    plain = peeled = None
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/tags/{tag}^{{}}":
            peeled = sha
        elif ref == f"refs/tags/{tag}":
            plain = sha
    return peeled or plain


def remote_main_head(url: str) -> tuple[str, str] | None:
    """(branch, sha) of `main` on the remote, falling back to the remote's default branch."""
    out = git("ls-remote", "--heads", url, "refs/heads/main")
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == "refs/heads/main":
            return "main", sha
    out = git("ls-remote", "--symref", url, "HEAD")
    branch = sha = None
    for line in out.splitlines():
        if line.startswith("ref: "):
            branch = line.split()[1].removeprefix("refs/heads/")
        elif line.endswith("\tHEAD"):
            sha = line.split("\t")[0]
    if branch and sha:
        return branch, sha
    return None


def clone_at(url: str, commit: str, dest: Path) -> None:
    """Full clone (history is needed for ancestry checks), detached at `commit`."""
    git("clone", "--quiet", "--no-checkout", url, str(dest))
    try:
        git("cat-file", "-e", f"{commit}^{{commit}}", cwd=dest)
    except subprocess.CalledProcessError:
        raise ReceiptError(f"commit {commit} does not exist in {url}") from None
    git("checkout", "--quiet", "--detach", commit, cwd=dest)


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repo, capture_output=True, text=True,
    )
    if result.returncode in (0, 1):
        return result.returncode == 0
    raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)


def tree_sha(repo: Path, commit: str) -> str:
    return git("rev-parse", f"{commit}^{{tree}}", cwd=repo).strip()


def archive_sha256(repo: Path, commit: str) -> str:
    digest = hashlib.sha256()
    with subprocess.Popen(
        ["git", "archive", "--format=tar", commit], cwd=repo, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as proc:
        assert proc.stdout is not None
        for chunk in iter(lambda: proc.stdout.read(1 << 16), b""):
            digest.update(chunk)
        _, err = proc.communicate()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, proc.args, None, err)
    return digest.hexdigest()


def list_tree(repo: Path, commit: str) -> list[tuple[str, int, str]]:
    """[(path, size, type)] of every entry in the commit's tree."""
    out = git("ls-tree", "-r", "-l", "-z", "--full-tree", commit, cwd=repo)
    entries = []
    for record in out.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        parts = meta.split()
        # <mode> <type> <sha> <size or ->
        size = int(parts[3]) if parts[3].isdigit() else 0
        entries.append((path, size, parts[1]))
    return entries


def specs_tree_problems(entries: list[tuple[str, int, str]]) -> list[str]:
    """Rules for a `specs` receipt (LAB1.md 7.1 step 3 and 4.1 `itsmlab submit --kind specs`)."""
    problems = []
    big_specs = [
        p for p, size, kind in entries
        if kind == "blob" and p.startswith("specs/") and p.rsplit("/", 1)[-1] not in SPECS_IGNORED_NAMES
        and size >= SPECS_MIN_BYTES
    ]
    if not big_specs:
        problems.append(
            f"the tree has no file of at least {SPECS_MIN_BYTES} bytes under specs/ "
            f"(other than {' and '.join(SPECS_IGNORED_NAMES)})"
        )
    src_files = sorted(p for p, _, kind in entries if p.startswith("src/") and p not in SRC_ALLOWED)
    if src_files:
        shown = ", ".join(src_files[:5]) + (" ..." if len(src_files) > 5 else "")
        problems.append(f"the tree already has files under src/ other than src/README.md: {shown}")
    return problems


# ----------------------------------------------------------------------------------------------
# Attempts, deadlines, receipt
# ----------------------------------------------------------------------------------------------

def flatten_issues(data) -> list[dict]:
    """`gh api --paginate --slurp` yields a list of pages; a plain list is accepted too."""
    if isinstance(data, dict):
        return [data]
    issues: list[dict] = []
    for item in data or []:
        if isinstance(item, list):
            issues.extend(i for i in item if isinstance(i, dict))
        elif isinstance(item, dict):
            issues.append(item)
    return issues


def issue_labels(issue: dict) -> set[str]:
    return {str(l.get("name")) if isinstance(l, dict) else str(l) for l in issue.get("labels") or []}


def receipt_labels(lab: int, kind: str) -> list[str]:
    """The labels the workflow adds at receipt time; they are what later attempt counts read."""
    return ["receipted", f"lab:{lab}", f"kind:{kind}"]


def count_prior(prior_issues, current_number: int, lab: int, kind: str) -> int:
    """Prior receipted issues by the same author with the same lab and kind.

    Decided by the labels the bot added when it receipted them (`receipted`, `lab:<n>`, `kind:<kind>`),
    never by the issue body: the author can edit a body at any time (and could turn three receipted lab-1
    submissions into "lab 2" to get a fourth attempt), while labels on a repository they do not own are
    beyond their reach."""
    wanted = set(receipt_labels(lab, kind))
    count = 0
    for issue in flatten_issues(prior_issues):
        if "pull_request" in issue or issue.get("number") == current_number:
            continue
        if wanted <= issue_labels(issue):
            count += 1
    return count


def extra_attempts(grants: dict | None, login: str, lab: int) -> int:
    """Extra submission attempts the lecturer granted this login for this lab (attempt_grants.json).

    Logins are matched case-insensitively, as in the roster, and keys starting with `_` are comments.
    A grant raises the cap and nothing else: a receipt after `corrections_due` is still `late`, and the
    grader still records it as not counting."""
    entries = ((grants or {}).get("labs") or {}).get(str(lab)) or {}
    wanted = login.strip().lower()
    for key, value in entries.items():
        if not key.startswith("_") and key.strip().lower() == wanted:
            return max(0, int(value))
    return 0


def attempt_limit(grants: dict | None, login: str, lab: int) -> int:
    return MAX_ATTEMPTS + extra_attempts(grants, login, lab)


def parse_instant(value: str, tzname: str = DEFAULT_TIMEZONE) -> datetime:
    """RFC 3339 / ISO 8601; a naive value is local time in `tzname`."""
    dt = datetime.fromisoformat(value.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tzname))
    return dt


def deadline_for(deadlines: dict, lab: int, key: str) -> datetime:
    """`attempt1_due` or `corrections_due` of a lab as an aware instant."""
    tzname = deadlines.get("timezone", DEFAULT_TIMEZONE)
    entry = (deadlines.get("labs") or {}).get(str(lab))
    if not entry:
        raise ReceiptError(f"deadlines.json has no entry for lab {lab}; ask the lecturer")
    if key not in entry:
        raise ReceiptError(f"deadlines.json lab {lab} has no `{key}`; ask the lecturer")
    return parse_instant(str(entry[key]), tzname)


def is_late(deadlines: dict, lab: int, received_at: datetime) -> bool:
    """design/LAB1.md 10.6: a receipt after `corrections_due` (Saturday 08:00 of the next session) is late,
    whatever its attempt number; the grader marks it counts: false."""
    return received_at > deadline_for(deadlines, lab, "corrections_due")


def is_after_attempt1_due(deadlines: dict, lab: int, received_at: datetime) -> bool:
    """`attempt1_due` (Sunday 23:59:59 after the session) is advisory: recorded, still counts."""
    return received_at > deadline_for(deadlines, lab, "attempt1_due")


def normalized_text_sha256(text: str) -> str:
    normalized = text.replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def prior_specs_issues(prior_issues, current_number: int, lab: int) -> list[int]:
    """Numbers of the author's receipted `specs` issues for the lab, from the labels (like count_prior).

    A submission receipt carries them as `specs_issues`, so the grader's evaluate job knows which specs receipts
    must be in its log before L1-CORE-5 is judged: the `record` run of a specs issue opened seconds before the
    submission may still be queued (grade-lab1.yml, job evaluate). An empty list means the student filed none
    and the grader does not wait."""
    wanted = set(receipt_labels(lab, "specs"))
    numbers = []
    for issue in flatten_issues(prior_issues):
        if "pull_request" in issue or issue.get("number") == current_number:
            continue
        if wanted <= issue_labels(issue) and _is_int(issue.get("number")):
            numbers.append(issue["number"])
    return sorted(set(numbers))


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def build_receipt(sub: Submission, *, login: str, repository: str, commit: str, tree: str,
                  archive: str, attempt: int, late: bool, received_at: str, issue: int,
                  run_id: int, text_sha256: str | None = None, after_attempt1_due: bool = False,
                  specs_issues: list[int] | None = None) -> dict:
    receipt = {
        "lab": sub.lab,
        "kind": sub.kind,
        "login": login,
        "repository": repository,
        "tag": sub.tag,
        "commit": commit,
        "tree_sha": tree,
        "archive_sha256": archive,
    }
    if sub.kind == "prediction":
        receipt["text_sha256"] = text_sha256
    receipt.update({
        "attempt": attempt,
        "late": late,
        "after_attempt1_due": after_attempt1_due,
        "received_at": received_at,
        "issue": issue,
        "run_id": run_id,
    })
    if sub.kind == "submission":
        receipt["specs_issues"] = list(specs_issues or [])
    return receipt


def format_comment(receipt: dict) -> str:
    """The receipt comment of LAB1.md 7.1 step 6: the marker line, then the JSON on one line."""
    return f"{RECEIPT_MARKER}\n{json.dumps(receipt, separators=(', ', ': '))}\n"


def parse_receipt_comment(comment: str) -> dict | None:
    lines = comment.replace("\r\n", "\n").strip().split("\n")
    if len(lines) < 2 or lines[0].strip() != RECEIPT_MARKER:
        return None
    try:
        return json.loads(lines[1])
    except json.JSONDecodeError:
        return None


def refusal_comment(reason: str) -> str:
    return (
        f"{RECEIPT_MARKER} refused\n{reason}\n\n"
        "Fix the problem and open a new issue. Refused requests do not count as attempts.\n"
    )


def error_comment(reason: str) -> str:
    return (
        f"{RECEIPT_MARKER} error\nThe receipt workflow failed before it could decide: {reason}\n\n"
        "This is not your fault. The lecturer sees the failed run; retry in an hour or write to them.\n"
    )


# ----------------------------------------------------------------------------------------------
# The full pipeline
# ----------------------------------------------------------------------------------------------

def process(*, body: str, login: str, issue_number: int, created_at: str, run_id: int,
            roster: dict, deadlines: dict, prior_issues, git_base: str = DEFAULT_GIT_BASE,
            work_dir: str | None = None, grants: dict | None = None) -> Outcome:
    try:
        fields = parse_issue_body(body)
        if not fields:
            raise ReceiptError(
                "the issue was not opened with the submission form (no form fields found); "
                "run `itsmlab submit` and open the link it prints"
            )
        sub = validate_fields(fields)
        repository = check_roster(roster, login, sub.repository)
        attempt = count_prior(prior_issues, issue_number, sub.lab, sub.kind) + 1
        granted = extra_attempts(grants, login, sub.lab)
        limit = MAX_ATTEMPTS + granted
        if sub.kind == "submission" and attempt > limit:
            granted_note = (f" (that cap already includes the {granted} extra attempt(s) the lecturer "
                            "granted you for this lab)" if granted else "")
            raise ReceiptError(
                f"you already have {limit} receipted submissions for lab {sub.lab}; "
                f"attempt {attempt} is refused (agenda 7.4 rule 3){granted_note}"
            )
        url = f"{git_base}{repository}.git"

        if sub.kind == "submission":
            try:
                commit = resolve_tag(url, sub.tag or "")
            except subprocess.CalledProcessError as exc:
                raise ReceiptError(
                    f"cannot list tags of {url} (is the repository public?): {git_error_text(exc)}"
                ) from None
            if commit is None:
                raise ReceiptError(f"tag `{sub.tag}` does not exist on {url}; push it first (`git push origin {sub.tag}`)")
            if sub.commit and sub.commit != commit:
                raise ReceiptError(
                    f"tag `{sub.tag}` points to {commit} on GitHub but the form says {sub.commit}: "
                    "the tag moved after `itsmlab submit` ran; run it again"
                )
        else:
            commit = sub.commit or ""

        with tempfile.TemporaryDirectory(prefix="receipt-", dir=work_dir) as tmp:
            clone = Path(tmp) / "clone"
            try:
                clone_at(url, commit, clone)
            except subprocess.CalledProcessError as exc:
                raise ReceiptError(f"cannot clone {url} (is the repository public?): {git_error_text(exc)}") from None

            if sub.kind != "submission":
                head = remote_main_head(url)
                if head is None:
                    raise ReceiptError(f"{url} has no `main` branch and no default branch")
                branch, head_sha = head
                if not is_ancestor(clone, commit, head_sha):
                    raise ReceiptError(f"commit {commit} is not on `{branch}` of {url}; push it first")
                # A specs or prediction receipt (and any later kind that is not a tagged submission) proves an
                # ordering: this came before that work. The grader decides it from the commit alone, so naming
                # an older commit of `main` would back-date the receipt before work already pushed. It must
                # name the head (design/LAB1.md 7.1, LAB2.md 12.18).
                if commit != head_sha:
                    raise ReceiptError(
                        f"commit {commit} is not the head of `{branch}` ({head_sha}): a {sub.kind} receipt must "
                        f"name the current head of `{branch}`, so that everything already pushed comes before "
                        f"it. Run `itsmlab submit {sub.lab} --kind {sub.kind}` again and open the new URL"
                    )

            tree = tree_sha(clone, commit)
            archive = archive_sha256(clone, commit)
            text_sha = None
            if sub.kind == "specs":
                problems = specs_tree_problems(list_tree(clone, commit))
                if problems:
                    raise ReceiptError(
                        "the commit does not qualify as a specs freeze:\n" + "\n".join(f"- {p}" for p in problems)
                    )
            elif sub.kind == "prediction":
                text_sha = normalized_text_sha256(sub.text or "")

        received = parse_instant(created_at)
        late = is_late(deadlines, sub.lab, received)
        after_first = is_after_attempt1_due(deadlines, sub.lab, received)
        receipt = build_receipt(
            sub, login=login, repository=repository, commit=commit, tree=tree, archive=archive,
            attempt=attempt, late=late, received_at=created_at, issue=issue_number, run_id=run_id,
            text_sha256=text_sha, after_attempt1_due=after_first,
            specs_issues=prior_specs_issues(prior_issues, issue_number, sub.lab) if sub.kind == "submission" else None,
        )
        return Outcome("receipted", format_comment(receipt), receipt)
    except ReceiptError as exc:
        return Outcome("refused", refusal_comment(str(exc)))
    except Exception as exc:  # noqa: BLE001 - anything else is an internal error, reported and logged
        print(f"internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return Outcome("error", error_comment(f"{type(exc).__name__}: {exc}"))


def dispatch_payload(receipt: dict) -> dict:
    """Body for POST /repos/<grader>/dispatches: one top-level key in client_payload (GitHub caps at ten)."""
    return {"event_type": "receipt", "client_payload": {"receipt": receipt}}


def write_outputs(outcome: Outcome, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "comment.md").write_text(outcome.comment, encoding="utf-8")
    (out_dir / "status.txt").write_text(outcome.status + "\n", encoding="utf-8")
    if outcome.receipt is not None:
        (out_dir / "receipt.json").write_text(json.dumps(outcome.receipt, indent=2) + "\n", encoding="utf-8")
        (out_dir / "dispatch.json").write_text(
            json.dumps(dispatch_payload(outcome.receipt), indent=2) + "\n", encoding="utf-8"
        )
    gh_output = os.environ.get("GITHUB_OUTPUT")
    lines = [f"status={outcome.status}"]
    if outcome.receipt is not None:
        for key in ("kind", "lab", "attempt", "login", "late"):
            lines.append(f"{key}={json.dumps(outcome.receipt[key]) if key == 'late' else outcome.receipt[key]}")
        lines.append("labels=" + ",".join(receipt_labels(outcome.receipt["lab"], outcome.receipt["kind"])))
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))


def load_json(path: str, default=None):
    """`default` also covers a missing file: an optional input (--prior, --grants) that was never created
    must not fail every receipt, while --roster and --deadlines pass no default and still raise."""
    if path is None:
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        if default is None:
            raise
        return default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="receipt.py", description="acceptance receipts (LAB1.md 7.1)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_parse = sub.add_parser("parse", help="parse an issue body from stdin and print the fields as JSON")

    p_run = sub.add_parser("run", help="run the full pipeline for the issue described by the environment")
    p_run.add_argument("--roster", required=True, help="roster.json (login -> owner/name)")
    p_run.add_argument("--deadlines", required=True, help="deadlines.json")
    p_run.add_argument("--prior", help="JSON from `gh api --paginate --slurp .../issues?labels=receipted&creator=<login>`")
    p_run.add_argument("--grants", help="attempt_grants.json (extra submission attempts per lab and login)")
    p_run.add_argument("--out", default="out", help="directory for comment.md, status.txt, receipt.json, dispatch.json")
    p_run.add_argument("--git-base", default=DEFAULT_GIT_BASE, help="prefix for clone URLs (default https://github.com/)")
    p_run.add_argument("--work", default=None, help="directory for the temporary clone (default: system temp)")

    args = parser.parse_args(argv)
    if args.command == "parse":
        print(json.dumps(parse_issue_body(sys.stdin.read()), indent=2))
        return 0

    env = os.environ
    missing = [k for k in ("ISSUE_BODY", "ISSUE_AUTHOR", "ISSUE_NUMBER", "ISSUE_CREATED_AT", "RUN_ID") if k not in env]
    if missing:
        print(f"error: missing environment variables: {', '.join(missing)}", file=sys.stderr)
        return 2
    outcome = process(
        body=env["ISSUE_BODY"], login=env["ISSUE_AUTHOR"], issue_number=int(env["ISSUE_NUMBER"]),
        created_at=env["ISSUE_CREATED_AT"], run_id=int(env["RUN_ID"]),
        roster=load_json(args.roster), deadlines=load_json(args.deadlines),
        prior_issues=load_json(args.prior, default=[]), grants=load_json(args.grants, default={}),
        git_base=args.git_base, work_dir=args.work,
    )
    write_outputs(outcome, Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
