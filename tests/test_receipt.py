"""Tests for receipt.py (design/LAB1.md 7.1).

Uses a temporary git repository served over file:// as the student's public repo, sample issue
bodies in the exact shape GitHub renders issue forms, and a fake deadlines.json.
"""

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import receipt
from receipt import FIELD_LABELS, ReceiptError

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
RECEIPT_PY = REPO_ROOT / "receipt.py"

DEADLINES = {
    "timezone": "Europe/Warsaw",
    "labs": {
        "1": {"attempt1_due": "2026-09-20T23:59:59", "corrections_due": "2026-09-26T08:00:00"},
        "2": {"attempt1_due": "2026-09-27T23:59:59", "corrections_due": "2026-10-09T23:59:59"},
    },
}
ROSTER = {"_comment": "ignored", "octocat": "octocat/svcdesk", "Other-Student": "other-student/svcdesk"}
ON_TIME = "2026-09-19T14:00:00Z"
LATE = "2026-09-20T22:30:00Z"  # 00:30 Monday in Warsaw (CEST = UTC+2)


def form_body(**fields):
    """Render an issue body exactly as GitHub renders the issue form (label headings, _No response_)."""
    parts = []
    for label, key in FIELD_LABELS.items():
        value = fields.get(key)
        parts.append(f"### {label}\n\n{value if value not in (None, '') else receipt.NO_RESPONSE}")
    return "\n\n".join(parts) + "\n"


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="session")
def student_repo(tmp_path_factory):
    """A bare repo at <base>/octocat/svcdesk.git with:
    A: specs/spec.md (600 bytes) + src/README.md          <- lightweight tag `specs-only`
    B: adds src/app.py                                    <- annotated tag lab1/v1; main
    C: on branch `feature`, not on main
    """
    base = tmp_path_factory.mktemp("remote")
    work = base / "work"
    work.mkdir()
    git("init", "-q", "-b", "main", cwd=work)
    git("config", "user.email", "s@example.com", cwd=work)
    git("config", "user.name", "Student", cwd=work)
    (work / "specs").mkdir()
    (work / "src").mkdir()
    (work / "specs" / "spec.md").write_text("# Spec\n" + "x" * 600)
    (work / "src" / "README.md").write_text("code goes here\n")
    git("add", ".", cwd=work)
    git("commit", "-q", "-m", "specs", cwd=work)
    a = git("rev-parse", "HEAD", cwd=work)
    git("tag", "specs-only", cwd=work)
    (work / "src" / "app.py").write_text("print('hi')\n")
    git("add", ".", cwd=work)
    git("commit", "-q", "-m", "implementation", cwd=work)
    b = git("rev-parse", "HEAD", cwd=work)
    git("tag", "-a", "lab1/v1", "-m", "lab 1 attempt 1", cwd=work)
    git("checkout", "-q", "-b", "feature", cwd=work)
    (work / "notes.md").write_text("not on main\n")
    git("add", ".", cwd=work)
    git("commit", "-q", "-m", "feature", cwd=work)
    c = git("rev-parse", "HEAD", cwd=work)
    git("checkout", "-q", "main", cwd=work)
    bare = base / "octocat" / "svcdesk.git"
    bare.parent.mkdir()
    git("clone", "-q", "--bare", str(work), str(bare), cwd=base)
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=bare)
    return {"base": f"file://{base}/", "url": f"file://{bare}", "work": work, "A": a, "B": b, "C": c}


# ---------------------------------------------------------------------------- parser

def test_parse_submission_body():
    body = form_body(lab="1", kind="submission", repository="octocat/svcdesk", tag="lab1/v1")
    assert receipt.parse_issue_body(body) == {
        "lab": "1", "kind": "submission", "repository": "octocat/svcdesk", "tag": "lab1/v1",
        "commit": "", "text": "",
    }


def test_parse_prediction_body_keeps_inner_headings_and_crlf():
    text = "I predict that\n\n### Plan\n\n- the SLA clock is the hard part\n"
    body = form_body(lab="2", kind="prediction", repository="octocat/svcdesk", commit="a" * 40, text=text)
    fields = receipt.parse_issue_body(body.replace("\n", "\r\n"))  # GitHub sends CRLF
    assert fields["text"] == "I predict that\n\n### Plan\n\n- the SLA clock is the hard part"
    assert fields["commit"] == "a" * 40


def test_parse_ignores_unknown_headings_and_garbage():
    assert receipt.parse_issue_body("hello\n\n### Something\n\nvalue\n") == {}
    assert receipt.parse_issue_body("") == {}
    assert receipt.parse_issue_body(None) == {}


def test_form_labels_match_the_issue_template():
    yaml = pytest.importorskip("yaml")
    form = yaml.safe_load((REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "submission.yml").read_text())
    elements = [e for e in form["body"] if e["type"] != "markdown"]
    assert {e["attributes"]["label"]: e["id"] for e in elements} == FIELD_LABELS
    lab = next(e for e in elements if e["id"] == "lab")
    assert lab["attributes"]["options"] == [str(n) for n in range(1, 9)]
    kind = next(e for e in elements if e["id"] == "kind")
    assert kind["attributes"]["options"] == list(receipt.KINDS)
    for required in ("lab", "kind", "repository"):
        assert next(e for e in elements if e["id"] == required)["validations"]["required"] is True


# ---------------------------------------------------------------------------- validation

def test_validate_submission():
    sub = receipt.validate_fields({"lab": "1", "kind": "submission", "repository": "octocat/svcdesk", "tag": "lab1/v1"})
    assert (sub.lab, sub.kind, sub.tag, sub.commit, sub.text) == (1, "submission", "lab1/v1", None, None)


def test_validate_submission_normalizes_optional_commit():
    sub = receipt.validate_fields({"lab": "1", "kind": "submission", "repository": "o/n", "tag": "v1", "commit": "A" * 40})
    assert sub.commit == "a" * 40


@pytest.mark.parametrize("bad", [
    {"lab": "9", "kind": "submission", "repository": "o/n", "tag": "v1"},
    {"lab": "one", "kind": "submission", "repository": "o/n", "tag": "v1"},
    {"lab": "1", "kind": "release", "repository": "o/n", "tag": "v1"},
    {"lab": "1", "kind": "submission", "repository": "just-a-name", "tag": "v1"},
    {"lab": "1", "kind": "submission", "repository": "o/n.git", "tag": "v1"},
    {"lab": "1", "kind": "submission", "repository": "-o/n", "tag": "v1"},
    {"lab": "1", "kind": "submission", "repository": "o/n", "tag": ""},
    {"lab": "1", "kind": "submission", "repository": "o/n", "tag": "a..b"},
    {"lab": "1", "kind": "submission", "repository": "o/n", "tag": "lab1/"},
    {"lab": "1", "kind": "submission", "repository": "o/n", "tag": ".hidden"},
    {"lab": "1", "kind": "submission", "repository": "o/n", "tag": "v1.lock"},
    {"lab": "1", "kind": "submission", "repository": "o/n", "tag": "v 1"},
    {"lab": "1", "kind": "submission", "repository": "o/n", "tag": "v1", "commit": "abc"},
    {"lab": "1", "kind": "specs", "repository": "o/n", "commit": ""},
    {"lab": "1", "kind": "specs", "repository": "o/n", "commit": "g" * 40},
    {"lab": "1", "kind": "prediction", "repository": "o/n", "commit": "a" * 40, "text": "  "},
    {},
])
def test_validate_refuses(bad):
    with pytest.raises(ReceiptError):
        receipt.validate_fields(bad)


def test_validate_specs_and_prediction_ignore_irrelevant_fields():
    specs = receipt.validate_fields({"lab": "1", "kind": "specs", "repository": "o/n", "commit": "b" * 40, "tag": "ignored"})
    assert specs.tag is None and specs.commit == "b" * 40
    pred = receipt.validate_fields({"lab": "1", "kind": "prediction", "repository": "o/n", "commit": "b" * 40, "text": "It will work."})
    assert pred.text == "It will work."


def test_roster_check():
    assert receipt.check_roster(ROSTER, "octocat", "octocat/svcdesk") == "octocat/svcdesk"
    assert receipt.check_roster(ROSTER, "OctoCat", "OCTOCAT/SvcDesk") == "octocat/svcdesk"
    assert receipt.check_roster(ROSTER, "other-student", "other-student/svcdesk") == "other-student/svcdesk"
    with pytest.raises(ReceiptError, match="not in the roster"):
        receipt.check_roster(ROSTER, "stranger", "stranger/svcdesk")
    with pytest.raises(ReceiptError, match="not the repository registered"):
        receipt.check_roster(ROSTER, "octocat", "octocat/other")
    with pytest.raises(ReceiptError):
        receipt.check_roster(ROSTER, "_comment", "ignored")


# ---------------------------------------------------------------------------- git

def test_resolve_tag_peels_annotated_tags(student_repo):
    assert receipt.resolve_tag(student_repo["url"], "lab1/v1") == student_repo["B"]
    assert receipt.resolve_tag(student_repo["url"], "specs-only") == student_repo["A"]
    assert receipt.resolve_tag(student_repo["url"], "lab1/v9") is None


def test_remote_main_head(student_repo):
    assert receipt.remote_main_head(student_repo["url"]) == ("main", student_repo["B"])


def test_clone_tree_sha_and_archive(student_repo, tmp_path):
    clone = tmp_path / "clone"
    receipt.clone_at(student_repo["url"], student_repo["A"], clone)
    assert git("rev-parse", "HEAD", cwd=clone) == student_repo["A"]
    assert receipt.tree_sha(clone, student_repo["A"]) == git("rev-parse", f"{student_repo['A']}^{{tree}}", cwd=student_repo["work"])
    expected = hashlib.sha256(
        subprocess.run(["git", "archive", "--format=tar", student_repo["A"]], cwd=student_repo["work"],
                       check=True, capture_output=True).stdout
    ).hexdigest()
    assert receipt.archive_sha256(clone, student_repo["A"]) == expected
    assert receipt.is_ancestor(clone, student_repo["A"], student_repo["B"])
    assert not receipt.is_ancestor(clone, student_repo["B"], student_repo["A"])
    with pytest.raises(ReceiptError, match="does not exist"):
        receipt.clone_at(student_repo["url"], "0" * 40, tmp_path / "clone2")


def test_specs_tree_rules(student_repo, tmp_path):
    clone = tmp_path / "clone"
    receipt.clone_at(student_repo["url"], student_repo["A"], clone)
    assert receipt.specs_tree_problems(receipt.list_tree(clone, student_repo["A"])) == []
    problems = receipt.specs_tree_problems(receipt.list_tree(clone, student_repo["B"]))
    assert len(problems) == 1 and "src/app.py" in problems[0]
    # Only small or ignored files under specs/ do not count.
    entries = [("specs/README.md", 5000, "blob"), ("specs/.gitkeep", 0, "blob"), ("specs/x.md", 499, "blob"),
               ("src/README.md", 10, "blob")]
    problems = receipt.specs_tree_problems(entries)
    assert len(problems) == 1 and "500 bytes" in problems[0]
    assert receipt.specs_tree_problems([("specs/a/b/spec.md", 500, "blob")]) == []


# ---------------------------------------------------------------------------- attempts and deadlines

def prior_issue(number, lab, kind, labels=None, pr=False, body_lab=None, body_kind=None):
    """A prior issue as the API returns it: labelled by the bot at receipt time (`receipted`, `lab:<n>`,
    `kind:<kind>`); the body is whatever the author currently shows (they can edit it)."""
    if labels is None:
        labels = receipt.receipt_labels(lab, kind)
    issue = {"number": number, "labels": [{"name": l} for l in labels],
             "body": form_body(lab=str(body_lab or lab), kind=body_kind or kind, repository="octocat/svcdesk",
                               tag="t", commit="a" * 40, text="x")}
    if pr:
        issue["pull_request"] = {}
    return issue


def test_count_prior_filters_lab_kind_labels_prs_and_self():
    pages = [[prior_issue(1, 1, "submission"), prior_issue(2, 1, "submission", labels=())],
             [prior_issue(3, 1, "specs"), prior_issue(4, 2, "submission"), prior_issue(5, 1, "submission", pr=True),
              prior_issue(6, 1, "submission")]]
    assert receipt.count_prior(pages, current_number=6, lab=1, kind="submission") == 1
    assert receipt.count_prior(pages, current_number=99, lab=1, kind="submission") == 2
    assert receipt.count_prior(pages, current_number=99, lab=1, kind="specs") == 1
    assert receipt.count_prior([], 99, 1, "submission") == 0
    # A flat list (one page) is accepted as well.
    assert receipt.count_prior(pages[0] + pages[1], 99, 1, "submission") == 2


def test_count_prior_reads_labels_never_the_editable_body():
    """Three receipted lab-1 submissions whose bodies were edited to say `Lab: 2` / `Kind: specs` still count
    as lab-1 submissions: the labels were set by the bot and the author cannot change them."""
    edited = [prior_issue(n, 1, "submission", body_lab=2, body_kind="specs") for n in (1, 2, 3)]
    assert receipt.count_prior(edited, 99, 1, "submission") == 3
    # and an issue whose body claims lab 1 but that was receipted as lab 2 does not count
    assert receipt.count_prior([prior_issue(7, 2, "submission", body_lab=1)], 99, 1, "submission") == 0
    # only `receipted` (an older label scheme) is not enough
    assert receipt.count_prior([prior_issue(8, 1, "submission", labels=("receipted",))], 99, 1, "submission") == 0
    assert receipt.receipt_labels(1, "submission") == ["receipted", "lab:1", "kind:submission"]


def test_published_deadlines_fall_on_the_published_weekdays():
    """deadlines.json (LAB1.md 10.6): attempt 1 is due Sunday 23:59:59 after the session (advisory); corrections
    close at the start of the next session, Saturday 08:00:00, Lab 8 on Sat 30 Jan 2027 08:00:00."""
    data = json.loads((REPO_ROOT / "deadlines.json").read_text())
    assert data["timezone"] == "Europe/Warsaw" and sorted(data["labs"]) == [str(n) for n in range(1, 9)]
    for lab, entry in data["labs"].items():
        first = receipt.parse_instant(entry["attempt1_due"])
        corrections = receipt.parse_instant(entry["corrections_due"])
        assert first.weekday() == 6 and (first.hour, first.minute, first.second) == (23, 59, 59), lab
        assert corrections.weekday() == 5 and (corrections.hour, corrections.minute, corrections.second) == (8, 0, 0), lab
        assert corrections > first, lab
    assert data["labs"]["1"]["corrections_due"] == "2026-09-26T08:00:00"
    assert data["labs"]["8"]["corrections_due"] == "2027-01-30T08:00:00"


def test_deadlines_are_local_time_in_warsaw():
    assert receipt.deadline_for(DEADLINES, 1, "attempt1_due") == datetime(2026, 9, 20, 21, 59, 59, tzinfo=timezone.utc)
    assert receipt.deadline_for(DEADLINES, 1, "corrections_due") == datetime(2026, 9, 26, 6, 0, 0, tzinfo=timezone.utc)
    # late means after corrections_due, whatever the attempt number; the Sunday deadline is advisory
    assert receipt.is_late(DEADLINES, 1, receipt.parse_instant(ON_TIME)) is False
    assert receipt.is_late(DEADLINES, 1, receipt.parse_instant("2026-09-20T21:59:59Z")) is False
    assert receipt.is_late(DEADLINES, 1, receipt.parse_instant(LATE)) is False
    assert receipt.is_after_attempt1_due(DEADLINES, 1, receipt.parse_instant(LATE)) is True
    assert receipt.is_after_attempt1_due(DEADLINES, 1, receipt.parse_instant(ON_TIME)) is False
    assert receipt.is_late(DEADLINES, 1, receipt.parse_instant("2026-09-26T06:00:00Z")) is False  # equality is not late
    assert receipt.is_late(DEADLINES, 1, receipt.parse_instant("2026-09-26T06:00:01Z")) is True
    with pytest.raises(ReceiptError, match="no entry for lab 3"):
        receipt.deadline_for(DEADLINES, 3, "attempt1_due")
    explicit = {"labs": {"1": {"attempt1_due": "2026-09-20T23:59:59+02:00", "corrections_due": "2026-09-26T08:00:00+02:00"}}}
    assert receipt.deadline_for(explicit, 1, "attempt1_due") == receipt.deadline_for(DEADLINES, 1, "attempt1_due")


# ---------------------------------------------------------------------------- receipt format

def test_comment_format_roundtrip():
    sub = receipt.Submission(1, "submission", "octocat/svcdesk", "lab1/v1", None, None)
    r = receipt.build_receipt(sub, login="octocat", repository="octocat/svcdesk", commit="b" * 40, tree="c" * 40,
                              archive="d" * 64, attempt=1, late=False, received_at=ON_TIME, issue=42, run_id=123)
    assert list(r) == ["lab", "kind", "login", "repository", "tag", "commit", "tree_sha", "archive_sha256",
                       "attempt", "late", "after_attempt1_due", "received_at", "issue", "run_id", "specs_issues"]
    assert r["specs_issues"] == []  # submission receipts always carry the list (empty: no specs receipt filed)
    comment = receipt.format_comment(r)
    first, second, rest = comment.split("\n", 2)
    assert first == "itsmlab receipt" and rest == ""
    assert json.loads(second) == r
    assert receipt.parse_receipt_comment(comment) == r
    assert receipt.parse_receipt_comment("something else") is None
    assert receipt.dispatch_payload(r) == {"event_type": "receipt", "client_payload": {"receipt": r}}


# ---------------------------------------------------------------------------- end to end

def run_process(student_repo, body, *, login="octocat", prior=(), created_at=ON_TIME, number=42, grants=None):
    return receipt.process(body=body, login=login, issue_number=number, created_at=created_at, run_id=123,
                           roster=ROSTER, deadlines=DEADLINES, prior_issues=list(prior), grants=grants,
                           git_base=student_repo["base"])


def test_submission_happy_path(student_repo):
    out = run_process(student_repo, form_body(lab="1", kind="submission", repository="octocat/svcdesk", tag="lab1/v1"))
    assert out.status == "receipted", out.comment
    r = out.receipt
    assert r["lab"] == 1 and r["kind"] == "submission" and r["login"] == "octocat"
    assert r["tag"] == "lab1/v1" and r["commit"] == student_repo["B"]
    assert r["tree_sha"] == git("rev-parse", f"{student_repo['B']}^{{tree}}", cwd=student_repo["work"])
    assert len(r["archive_sha256"]) == 64
    assert r["attempt"] == 1 and r["late"] is False and r["received_at"] == ON_TIME
    assert r["issue"] == 42 and r["run_id"] == 123 and "text_sha256" not in r
    assert out.comment == receipt.format_comment(r)


def test_submission_attempt_counting_and_cap(student_repo):
    body = form_body(lab="1", kind="submission", repository="octocat/svcdesk", tag="lab1/v1")
    prior = [prior_issue(n, 1, "submission") for n in (1, 2)]
    out = run_process(student_repo, body, prior=prior)
    assert out.status == "receipted" and out.receipt["attempt"] == 3
    prior.append(prior_issue(3, 1, "submission"))
    out = run_process(student_repo, body, prior=prior)
    assert out.status == "refused" and "attempt 4 is refused" in out.comment and out.receipt is None
    assert "3 receipted submissions" in out.comment and "granted" not in out.comment
    # Specs receipts are not capped and do not count as submission attempts.
    prior_specs = [prior_issue(n, 1, "specs") for n in (1, 2, 3)]
    out = run_process(student_repo, body, prior=prior_specs)
    assert out.status == "receipted" and out.receipt["attempt"] == 1


def test_extra_attempts_lookup():
    """attempt_grants.json: case-insensitive login, `_` keys are comments, anything absent is zero."""
    grants = {"_comment": "x", "labs": {"1": {"_reason": "grader outage", "IwonaSzukala": 2, "adiker": 1},
                                        "2": {"octocat": 5}}}
    assert receipt.extra_attempts(grants, "iwonaszukala", 1) == 2
    assert receipt.extra_attempts(grants, " Adiker ", 1) == 1
    assert receipt.extra_attempts(grants, "octocat", 1) == 0        # granted for lab 2, not lab 1
    assert receipt.extra_attempts(grants, "octocat", 2) == 5
    assert receipt.extra_attempts(grants, "_reason", 1) == 0        # a comment key is never a login
    assert receipt.extra_attempts(grants, "octocat", 3) == 0        # a lab with no grants
    assert receipt.extra_attempts(None, "octocat", 1) == 0 and receipt.extra_attempts({}, "octocat", 1) == 0
    assert receipt.extra_attempts({"labs": {"1": {"octocat": -2}}}, "octocat", 1) == 0  # never lowers the cap
    assert receipt.attempt_limit(grants, "octocat", 2) == receipt.MAX_ATTEMPTS + 5


def test_granted_attempts_raise_the_cap_for_that_student_only(student_repo):
    """A grant of two lets attempts 4 and 5 through and refuses the sixth; another login keeps the cap of
    three. The grant raises the cap and nothing else: the receipt keys do not change."""
    body = form_body(lab="1", kind="submission", repository="octocat/svcdesk", tag="lab1/v1")
    grants = {"labs": {"1": {"OctoCat": 2}}}
    prior = [prior_issue(n, 1, "submission") for n in (1, 2, 3)]
    out = run_process(student_repo, body, prior=prior, grants=grants)
    assert out.status == "receipted" and out.receipt["attempt"] == 4
    assert "attempt_limit" not in out.receipt and "granted" not in out.receipt
    prior.append(prior_issue(4, 1, "submission"))
    out = run_process(student_repo, body, prior=prior, grants=grants)
    assert out.status == "receipted" and out.receipt["attempt"] == 5
    prior.append(prior_issue(5, 1, "submission"))
    out = run_process(student_repo, body, prior=prior, grants=grants)
    assert out.status == "refused" and "5 receipted submissions" in out.comment
    assert "attempt 6 is refused" in out.comment and "2 extra attempt(s)" in out.comment
    # The same history without the grant, and for a login the grant does not name, stops at three.
    assert run_process(student_repo, body, prior=prior[:3]).status == "refused"
    assert run_process(student_repo, body, prior=prior[:3], grants={"labs": {"1": {"someone-else": 2}}}
                       ).status == "refused"


def test_a_grant_does_not_move_a_deadline(student_repo):
    """LAB1.md 10.6: the cap and the deadline are separate. A granted attempt filed after corrections_due
    is still `late`, which the grader records as not counting."""
    body = form_body(lab="1", kind="submission", repository="octocat/svcdesk", tag="lab1/v1")
    prior = [prior_issue(n, 1, "submission") for n in (1, 2, 3)]
    out = run_process(student_repo, body, prior=prior, grants={"labs": {"1": {"octocat": 2}}},
                      created_at="2026-09-26T07:30:00Z")  # after corrections_due (Saturday 08:00 CEST)
    assert out.status == "receipted" and out.receipt["attempt"] == 4 and out.receipt["late"] is True


def test_submission_receipt_lists_the_authors_specs_issues(student_repo):
    """`specs_issues`: the author's receipted specs issues for the lab, by label; the grader's evaluate job waits
    for exactly these lines in its log (a queued record run) and does not wait when the list is empty."""
    body = form_body(lab="1", kind="submission", repository="octocat/svcdesk", tag="lab1/v1")
    prior = [prior_issue(1, 1, "specs"), prior_issue(2, 1, "submission"), prior_issue(3, 1, "specs"),
             prior_issue(5, 2, "specs"), prior_issue(6, 1, "specs", pr=True),
             prior_issue(7, 1, "specs", labels=("receipted",))]  # lab 2, a PR and an unlabelled one do not count
    out = run_process(student_repo, body, prior=prior)
    assert out.status == "receipted" and out.receipt["specs_issues"] == [1, 3]
    assert receipt.prior_specs_issues(prior, current_number=3, lab=1) == [1]  # the issue itself is excluded
    out = run_process(student_repo, body)
    assert out.status == "receipted" and out.receipt["specs_issues"] == []
    specs = receipt.build_receipt(receipt.Submission(1, "specs", "octocat/svcdesk", None, "a" * 40, None),
                                  login="octocat", repository="octocat/svcdesk", commit="a" * 40, tree="b" * 40,
                                  archive="c" * 64, attempt=1, late=False, received_at=ON_TIME, issue=41, run_id=1,
                                  specs_issues=[1, 3])
    assert "specs_issues" not in specs  # only submission receipts carry the list


def test_submission_after_the_advisory_deadline_is_not_late(student_repo):
    out = run_process(student_repo, form_body(lab="1", kind="submission", repository="octocat/svcdesk", tag="lab1/v1"),
                      created_at=LATE)
    assert out.status == "receipted" and out.receipt["late"] is False and out.receipt["after_attempt1_due"] is True


def test_submission_late(student_repo):
    out = run_process(student_repo, form_body(lab="1", kind="submission", repository="octocat/svcdesk", tag="lab1/v1"),
                      created_at="2026-09-26T07:30:00Z")  # after corrections_due (Saturday 08:00 CEST = 06:00Z)
    assert out.status == "receipted" and out.receipt["late"] is True and out.receipt["after_attempt1_due"] is True


@pytest.mark.parametrize("body_kwargs, login, fragment", [
    (dict(lab="1", kind="submission", repository="octocat/svcdesk", tag="lab1/v9"), "octocat", "does not exist"),
    (dict(lab="1", kind="submission", repository="octocat/svcdesk", tag="lab1/v1", commit="0" * 40), "octocat", "moved"),
    (dict(lab="1", kind="submission", repository="octocat/svcdesk", tag="lab1/v1"), "stranger", "not in the roster"),
    (dict(lab="1", kind="submission", repository="octocat/other", tag="lab1/v1"), "octocat", "not the repository"),
    (dict(lab="3", kind="submission", repository="octocat/svcdesk", tag="lab1/v1"), "octocat", "no entry for lab 3"),
    (dict(lab="1", kind="specs", repository="octocat/svcdesk", commit="0" * 40), "octocat", "does not exist"),
])
def test_refusals(student_repo, body_kwargs, login, fragment):
    out = run_process(student_repo, form_body(**body_kwargs), login=login)
    assert out.status == "refused", out.comment
    assert out.comment.startswith("itsmlab receipt refused\n")
    assert fragment in out.comment


def test_blank_issue_is_refused(student_repo):
    out = run_process(student_repo, "I just want to say hi")
    assert out.status == "refused" and "submission form" in out.comment


def test_specs_receipt(student_repo):
    out = run_process(student_repo, form_body(lab="1", kind="specs", repository="octocat/svcdesk", commit=student_repo["A"]))
    assert out.status == "receipted", out.comment
    assert out.receipt["kind"] == "specs" and out.receipt["tag"] is None and out.receipt["commit"] == student_repo["A"]
    # The implementation commit has src/app.py: not a specs freeze.
    out = run_process(student_repo, form_body(lab="1", kind="specs", repository="octocat/svcdesk", commit=student_repo["B"]))
    assert out.status == "refused" and "src/app.py" in out.comment
    # A commit that exists but is not on main.
    out = run_process(student_repo, form_body(lab="1", kind="specs", repository="octocat/svcdesk", commit=student_repo["C"]))
    assert out.status == "refused" and "not on `main`" in out.comment


def test_prediction_receipt(student_repo):
    text = "The SLA clock will take longest.\r\n\r\n### because\r\n\r\nDST."
    out = run_process(student_repo, form_body(lab="1", kind="prediction", repository="octocat/svcdesk",
                                              commit=student_repo["B"], text=text))
    assert out.status == "receipted", out.comment
    r = out.receipt
    assert r["kind"] == "prediction" and r["commit"] == student_repo["B"]
    assert r["text_sha256"] == hashlib.sha256(text.replace("\r\n", "\n").strip().encode()).hexdigest()
    assert list(r)[:9] == ["lab", "kind", "login", "repository", "tag", "commit", "tree_sha", "archive_sha256", "text_sha256"]


def test_cli_run_writes_outputs(student_repo, tmp_path):
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps(ROSTER))
    deadlines = tmp_path / "deadlines.json"
    deadlines.write_text(json.dumps(DEADLINES))
    prior = tmp_path / "prior.json"
    prior.write_text(json.dumps([[prior_issue(7, 1, "submission")]]))
    gh_output = tmp_path / "gh_output"
    env = dict(os.environ, ISSUE_BODY=form_body(lab="1", kind="submission", repository="octocat/svcdesk", tag="lab1/v1"),
               ISSUE_AUTHOR="octocat", ISSUE_NUMBER="42", ISSUE_CREATED_AT=ON_TIME, RUN_ID="123",
               GITHUB_OUTPUT=str(gh_output))
    out_dir = tmp_path / "out"
    result = subprocess.run(
        [sys.executable, str(RECEIPT_PY), "run", "--roster", str(roster), "--deadlines", str(deadlines),
         "--prior", str(prior), "--out", str(out_dir), "--git-base", student_repo["base"]],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (out_dir / "status.txt").read_text() == "receipted\n"
    written = json.loads((out_dir / "receipt.json").read_text())
    assert written["attempt"] == 2 and written["commit"] == student_repo["B"]
    dispatch = json.loads((out_dir / "dispatch.json").read_text())
    assert dispatch == {"event_type": "receipt", "client_payload": {"receipt": written}}
    assert (out_dir / "comment.md").read_text() == receipt.format_comment(written)
    outputs = dict(line.split("=", 1) for line in gh_output.read_text().splitlines())
    assert outputs == {"status": "receipted", "kind": "submission", "lab": "1", "attempt": "2", "login": "octocat",
                       "late": "false", "labels": "receipted,lab:1,kind:submission"}


def test_cli_run_refusal_exits_zero_with_status(student_repo, tmp_path):
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps(ROSTER))
    deadlines = tmp_path / "deadlines.json"
    deadlines.write_text(json.dumps(DEADLINES))
    env = dict(os.environ, ISSUE_BODY="not a form", ISSUE_AUTHOR="octocat", ISSUE_NUMBER="43",
               ISSUE_CREATED_AT=ON_TIME, RUN_ID="124")
    env.pop("GITHUB_OUTPUT", None)
    out_dir = tmp_path / "out"
    result = subprocess.run(
        [sys.executable, str(RECEIPT_PY), "run", "--roster", str(roster), "--deadlines", str(deadlines),
         "--out", str(out_dir)], env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "status=refused"
    assert (out_dir / "status.txt").read_text() == "refused\n"
    assert not (out_dir / "receipt.json").exists()


def test_cli_parse():
    body = form_body(lab="1", kind="specs", repository="octocat/svcdesk", commit="a" * 40)
    result = subprocess.run([sys.executable, str(RECEIPT_PY), "parse"], input=body, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["commit"] == "a" * 40
