import json
import subprocess
import sys
from pathlib import Path

import roster_import

ROSTER_IMPORT_PY = Path(__file__).resolve().parents[1] / "roster_import.py"


def test_normalisation():
    assert roster_import.normalise_repository("https://github.com/Octo-Cat/itsm-svcdesk.git") == "Octo-Cat/itsm-svcdesk"
    assert roster_import.normalise_repository("git@github.com:o/n.git") == "o/n"
    assert roster_import.normalise_repository(" o/n/ ") == "o/n"
    assert roster_import.normalise_login(" @octocat ") == "octocat"


def test_build_roster_validates_everything():
    rows = [
        {"github_login": "octocat", "repository": "https://github.com/octocat/itsm-2026-svcdesk"},
        {"github_login": "@Jan-Kowalski", "repository": "Jan-Kowalski/svcdesk"},
        {"github_login": "", "repository": ""},
    ]
    roster, problems = roster_import.build_roster(rows, "github_login", "repository")
    assert problems == []
    assert roster == {"octocat": "octocat/itsm-2026-svcdesk", "jan-kowalski": "Jan-Kowalski/svcdesk"}
    bad = rows + [
        {"github_login": "bad login", "repository": "x/y"},
        {"github_login": "octocat", "repository": "octocat/other"},
        {"github_login": "someone", "repository": "OCTOCAT/itsm-2026-svcdesk"},
        {"github_login": "nobody", "repository": "just-a-name"},
        {"login": "wrong-columns"},
    ]
    roster, problems = roster_import.build_roster(bad, "github_login", "repository")
    assert len(problems) == 5
    assert any("not a GitHub login" in p for p in problems) and any("appears twice" in p for p in problems)
    assert any("already registered" in p for p in problems) and any("owner/name" in p for p in problems)
    assert any("missing column" in p for p in problems)


def test_cli_writes_and_merges(tmp_path: Path):
    csv_file = tmp_path / "export.csv"
    csv_file.write_text("﻿Name,GitHub login,Repository URL\nAda,ada,https://github.com/ada/svcdesk\n")
    out = tmp_path / "roster.json"
    out.write_text(json.dumps({"_comment": "kept", "old-student": "old-student/svcdesk"}))
    cmd = [sys.executable, str(ROSTER_IMPORT_PY), str(csv_file), "--login-column", "GitHub login",
           "--repository-column", "Repository URL", "--out", str(out)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(out.read_text()) == {"_comment": "kept", "ada": "ada/svcdesk"}
    out.write_text(json.dumps({"_comment": "kept", "old-student": "old-student/svcdesk"}))
    result = subprocess.run(cmd + ["--merge"], capture_output=True, text=True)
    assert result.returncode == 0 and json.loads(out.read_text()) == {"_comment": "kept", "old-student": "old-student/svcdesk", "ada": "ada/svcdesk"}
    csv_file.write_text("GitHub login,Repository URL\nada,not a repo\n")
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 2 and "roster not written" in result.stderr
    assert "ada" in json.loads(out.read_text())  # untouched on error
