# ITSM 2026 submissions

The public course repository where acceptance receipts are issued (design/LAB1.md 7.1, agenda 7.4 rule 1).
Students never edit it; they open one issue per request through the form, and a bot answers within a
minute with a receipt or a refusal. The lecturer maintains `roster.json`, `deadlines.json` and one secret.

## For students

1. Push your tag (`git push origin lab1/v1`) or, for a specs freeze or a prediction, your `main`.
2. Run `itsmlab submit 1 --kind submission --tag lab1/v1` (or `--kind specs`, `--kind prediction --text
   "..."`) in your repository. It prints a link to the issue form with the fields prefilled. Open it, check
   the fields, submit.
3. Within a minute the bot comments. A receipt looks like

   ```
   itsmlab receipt
   {"lab": 1, "kind": "submission", "login": "octocat", "repository": "octocat/itsm-2026-svcdesk", "tag": "lab1/v1", "commit": "8f4e...", "tree_sha": "1a2b...", "archive_sha256": "5e2d...", "attempt": 1, "late": false, "after_attempt1_due": false, "received_at": "2026-09-19T14:03:27Z", "issue": 42, "run_id": 17654321098, "specs_issues": [41]}
   ```

   and the issue gets the labels `receipted`, `lab:1` and `kind:submission` (attempts are counted from these
   labels, never from the issue text). The grader then grades exactly `commit`/`tree_sha`; moving
   the tag afterwards voids the attempt (and it still counts). `attempt` is your attempt number for that
   lab (three submissions per lab; the first is due on the Sunday after the session, the other two before
   the next session). `after_attempt1_due: true` means the receipt came after the Sunday deadline, which is
   advisory: the attempt still counts. `late: true` means it came after the correction window closed
   (Saturday 08:00, the start of the next session): it is still graded but does not count. `specs_issues`
   (submission receipts only) lists your receipted `specs` issues for the lab; the grader waits for those
   lines in its own log before judging L1-CORE-5, so a specs receipt filed seconds before the submission is
   never missed. The grade itself
   arrives on the same issue about 20 minutes later as a comment `itsmlab grade` with `grade.json` and its
   Sigstore bundle (see the course README, section 6).
4. `itsmlab receipt refused` plus a reason means the request was not accepted: fix the problem and open a
   new issue. Refusals never count as attempts. Typical reasons: the account is not on the roster, the
   repository is not the registered one, the tag does not exist on GitHub, a `specs` commit already has
   files under `src/` (only `src/README.md` is allowed) or no file of at least 500 bytes under `specs/`, a
   fourth submission for the same lab (the message names your own cap: it is higher if the lecturer granted
   you extra attempts after a course-side failure).
5. `itsmlab receipt error`: the bot itself failed (for example GitHub was unreachable). Not your fault;
   retry later or write to the lecturer.

The bot reads only the form fields and the account that opened the issue. The issue title is not read.

## What the workflow does (`.github/workflows/receipt.yml`)

On every opened issue: the body reaches `receipt.py` through the environment variable `ISSUE_BODY` (never
through `${{ }}` inside `run:`), the login is `github.event.issue.user.login`. Then `receipt.py run`:

1. parses the six form headings (`Lab`, `Kind`, `Repository`, `Tag`, `Commit`, `Text`; unknown headings
   inside a prediction text are kept as text) and validates them with strict patterns (lab 1..8, kind
   `submission | specs | prediction`, `owner/name`, a git tag name, a 40-hex SHA, non-empty text);
2. checks the login against `roster.json` and the repository against the roster's entry;
3. counts the author's prior issues labelled `receipted`, `lab:<n>` and `kind:<kind>` (fetched with
   `gh api repos/<repo>/issues?labels=receipted&creator=<login>`; the body is never read for this, because the
   author can edit it); a fourth submission is refused;
4. resolves the tag with `git ls-remote --tags` (the peeled `^{}` line of an annotated tag), or verifies that
   the given commit is on `main` (fallback: the remote's default branch);
5. clones at the commit and records `tree_sha = git rev-parse <commit>^{tree}` and
   `archive_sha256 = sha256(git archive --format=tar <commit>)`;
6. `specs`: at least one file of 500+ bytes under `specs/` (not `.gitkeep`/`README.md`) and nothing under
   `src/` but `src/README.md`; `prediction`: `text_sha256` of the text (CRLF normalised, stripped);
7. reads `deadlines.json` and sets `late` (after `corrections_due`) and `after_attempt1_due` (advisory);
8. writes `out/comment.md`, `out/receipt.json`, `out/dispatch.json` and the outputs `status`, `kind`,
   `lab`, `attempt`, `login`, `late`.
   The receipt JSON carries `after_attempt1_due` right after `late`, and a submission receipt ends with
   `specs_issues`: the numbers of the author's receipted `specs` issues for the lab (from the labels, like the
   attempt count), which the grader's evaluate job waits for in its log (grader README 11.18).

The workflow posts the comment, adds the labels `receipted`, `lab:<n>`, `kind:<kind>` and sends `repository_dispatch` with
`event_type: receipt` and `client_payload: {"receipt": {...}}` to the private grader repo using the secret
`GRADER_DISPATCH_TOKEN`. A refused or errored request gets its comment and the run is marked failed so the
lecturer sees it in the Actions list.

## For the lecturer: setup

1. Create the public repository (default name `swasik/itsm-2026-submissions`; the student template's
   `itsmlab.yaml` must name it) and push this directory as its root.
2. `roster.json`: `"<github login>": "<owner>/<name>"` per student; keys starting with `_` are comments.
   Build it from the Moodle export with `python3 roster_import.py export.csv --login-column "<login column>"
   --repository-column "<repository column>" --out roster.json` (validates every row, refuses duplicates and
   writes nothing on an error; `--merge` keeps existing entries). Live fix: push a corrected `roster.json`; the
   next issue picks it up and a refused issue never costs an attempt.
3. `deadlines.json`: per lab `attempt1_due` (Sunday 23:59:59 after the session, advisory) and
   `corrections_due` (Saturday 08:00:00 of the next session, the instant after which a receipt is `late`,
   unless that lab carries an `_extended` note in `deadlines.json` - Lab 1 was extended on 2026-09-25 to
   Sunday 27 September 23:59:59, so it can be corrected while Lab 2 runs) as
   naive local times in `timezone` (Europe/Warsaw) or with an explicit offset. A lab without an entry refuses
   every receipt for that lab with a clear message, so fill all eight before term.
4. `attempt_grants.json` (optional): extra submission attempts, `"labs": {"<n>": {"<login>": <extra>}}`,
   logins matched case-insensitively and `_` keys ignored, with the reason in the lab's `_reason`. Use it
   when a course-side failure cost students attempts: it raises the cap of three for those students only.
   It moves no deadline, so a receipt after `corrections_due` is still `late` and still does not count. A
   missing file simply means no grants.
5. Secret `GRADER_DISPATCH_TOKEN`: a fine-grained PAT restricted to the private grader repo with
   "Contents: Read and write" (what `repository_dispatch` needs). Optional variable `GRADER_REPO` when the
   grader repo is not `swasik/itsm-2026-grader`.
6. `config.yml` disables blank issues and links a Discussions page for questions; edit or remove that link.
7. The labels `receipted`, `lab:<n>`, `kind:<kind>` are created automatically on first use; never edit them
   by hand on a student's issue, the attempt count depends on them. Runs are serialised per author
   (`concurrency: receipt-<login>` with `queue: max`: pending runs wait, they are never cancelled), so two
   issues opened within seconds get consecutive attempt numbers and both get their comment.

Re-dispatching a receipt by hand (when the dispatch step failed after the comment was posted; needs the
same token in `GH_TOKEN`; not run here because the repositories do not exist yet):

```
printf '%s' '{"event_type": "receipt", "client_payload": {"receipt": <the JSON line of the comment>}}' > dispatch.json
gh api --method POST repos/swasik/itsm-2026-grader/dispatches --input dispatch.json
```

## Testing the parser locally

```
cd itsm/2026/grader/submissions-repo
uv sync
uv run pytest -q
```

Expected: `53 passed`. The tests use a temporary git repository served over `file://` with an annotated
tag, a lightweight tag and a branch not on `main`, sample issue bodies in the exact shape GitHub renders
issue forms, and a fake `deadlines.json`; one test cross-checks the form's labels against the parser's
table so the two cannot drift apart. From the grader root, `uv run pytest -q` runs the same tests together
with the grader's.

Parsing a body by hand:

```
printf '### Lab\n\n1\n\n### Kind\n\nsubmission\n\n### Repository\n\noctocat/itsm-2026-svcdesk\n\n### Tag\n\nlab1/v1\n\n### Commit\n\n_No response_\n\n### Text\n\n_No response_\n' | uv run python receipt.py parse
```

prints

```
{
  "lab": "1",
  "kind": "submission",
  "repository": "octocat/itsm-2026-svcdesk",
  "tag": "lab1/v1",
  "commit": "",
  "text": ""
}
```

The full pipeline runs with `receipt.py run --roster roster.json --deadlines deadlines.json --prior
prior.json --out out` and the environment variables `ISSUE_BODY`, `ISSUE_AUTHOR`, `ISSUE_NUMBER`,
`ISSUE_CREATED_AT`, `RUN_ID`; `--git-base file:///some/dir/` points it at local bare repositories
(`<dir>/<owner>/<name>.git`), which is how `tests/test_receipt.py::test_cli_run_writes_outputs` drives it.

## Open questions

See `grader/README.md`, section "Open questions", items 1 to 6 (attempt counting endpoint, `attempt` on
non-submission receipts, the late first attempt, the prediction hash, the `main` fallback, the raw comment
format).
