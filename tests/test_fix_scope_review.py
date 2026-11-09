"""Fixes for confirmed bugs that look wider than the bug needs go to a human."""

import json

import pytest

import workflow_orchestrator
from test_workflow_artifacts import run_mocked_retry_workflow
from test_workflow_hitl_orchestrator import enable_mocked_ui_control, read_run_report
from workflow_orchestrator import build_fix_diff, fix_scope_findings


def entry(file, added=2, removed=1, regions=1):
    return {"file": file, "added": added, "removed": removed, "regions": regions}


def test_focused_fix_has_no_findings():
    diff = {"files": [entry("index.js", added=4, removed=2, regions=1)]}
    assert fix_scope_findings(diff, ["index.js"]) == []


def test_file_outside_diagnosis_is_reported():
    diff = {"files": [entry("index.js"), entry("views/index.ejs")]}
    findings = fix_scope_findings(diff, ["index.js"])
    assert len(findings) == 1
    assert "views/index.ejs" in findings[0]


def test_paths_are_compared_without_separators_or_dot_prefix():
    diff = {"files": [entry("mern/server/routes/record.js")]}
    assert fix_scope_findings(diff, ["./mern\\server\\routes\\record.js"]) == []
    assert fix_scope_findings(diff, "mern/server/routes/record.js") == []


def test_no_affected_files_skips_the_file_check():
    assert fix_scope_findings({"files": [entry("a.js")]}, []) == []


def test_many_separate_changes_are_reported():
    # Level 2 rehearsal: the AI also reordered res.status() in every route.
    diff = {"files": [entry("mern/server/routes/record.js", added=13, removed=11, regions=6)]}
    findings = fix_scope_findings(diff, ["mern/server/routes/record.js"])
    assert len(findings) == 1
    assert "6 separate places" in findings[0]


def test_many_changed_lines_are_reported():
    diff = {"files": [entry("index.js", added=50, removed=20, regions=2)]}
    findings = fix_scope_findings(diff, ["index.js"])
    assert len(findings) == 1
    assert "70 lines" in findings[0]


def test_build_fix_diff_counts_separate_regions(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    original = "".join(f"line {number}\n" for number in range(1, 31))
    current = original.replace("line 2\n", "line two\n").replace("line 20\n", "line twenty\n")
    (repo / "app.js").write_text(current, encoding="utf-8")
    baseline = tmp_path / "fix_baseline.json"
    baseline.write_text(json.dumps({"app.js": original}), encoding="utf-8")
    monkeypatch.setattr(workflow_orchestrator, "FIX_BASELINE_FILE", str(baseline))

    diff = build_fix_diff({"repository_path": str(repo)})

    assert diff["files"] == [{"file": "app.js", "added": 2, "removed": 2, "regions": 2}]


WIDE_DIFF = {
    "files": [entry("index.js", added=13, removed=11, regions=6)],
    "diff": "--- a/index.js\n+++ b/index.js\n",
    "diff_truncated": False,
}


@pytest.mark.parametrize(
    ("decision", "final_status", "review_status", "exit_code"),
    [
        ("approve", "PASS", "SUCCESS", 0),
        ("reject", "FAIL", "FAILED", 1),
        (None, "NEEDS_REVIEW", "SKIPPED", 1),
    ],
)
def test_wide_fix_for_confirmed_bug_needs_approval(
    tmp_path, monkeypatch, decision, final_status, review_status, exit_code,
):
    control = enable_mocked_ui_control(tmp_path, monkeypatch, fix_review_decision=decision)
    monkeypatch.setattr(workflow_orchestrator, "build_fix_diff", lambda _context: dict(WIDE_DIFF))

    result, run_dir, _calls, _contexts = run_mocked_retry_workflow(tmp_path, monkeypatch)

    report = read_run_report(run_dir)
    review_stage = next(stage for stage in report["stages"] if stage["name"] == "Human Fix Review")
    assert result == exit_code
    assert report["final_status"] == final_status
    assert report["reproduction_confirmed"] is True
    assert review_stage["status"] == review_status
    assert len(control["fix_reviews"]) == 1
    sent = control["fix_reviews"][0]
    assert any("6 separate places" in finding for finding in sent["scope_findings"])
    assert "wider" in sent["reason"]
    assert report["fix_review"]["scope_findings"] == sent["scope_findings"]


def test_focused_fix_for_confirmed_bug_is_not_reviewed(tmp_path, monkeypatch):
    control = enable_mocked_ui_control(tmp_path, monkeypatch)
    focused = dict(WIDE_DIFF, files=[entry("app.py", added=4, removed=2, regions=1)])
    monkeypatch.setattr(workflow_orchestrator, "build_fix_diff", lambda _context: dict(focused))

    result, run_dir, _calls, _contexts = run_mocked_retry_workflow(tmp_path, monkeypatch)

    report = read_run_report(run_dir)
    assert result == 0
    assert report["final_status"] == "PASS"
    assert control["fix_reviews"] == []
    assert "fix_review" not in report
