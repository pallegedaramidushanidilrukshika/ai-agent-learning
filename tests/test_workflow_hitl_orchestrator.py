"""Human-in-the-loop orchestrator tests.

These are mocked unit tests: the UI control channel, agents, builds and
AI calls are all simulated. They do not prove a real AI-backed fix.
"""

import json
import os

import pytest

import workflow_orchestrator
from test_workflow_artifacts import run_mocked_retry_workflow
from workflow_control import validate_manual_submission


def enable_mocked_ui_control(
    tmp_path,
    monkeypatch,
    blocker_actions=(),
    manual_submission=None,
    stop_before_stage=None,
    fix_review_decision="approve",
):
    """Simulate the UI control channel inside the orchestrator process."""
    run_dir = tmp_path / "workflow-root" / "workflow_runs" / "run-1"
    control = {"blocker_calls": [], "published": [], "manual_waits": 0, "fix_reviews": []}

    def fake_fix_review(fix_review, **_kwargs):
        control["fix_reviews"].append(fix_review)
        if fix_review_decision is None:
            return None
        feedback = "Not the reported bug" if fix_review_decision == "reject" else ""
        return {"decision": fix_review_decision, "feedback": feedback}
    remaining_actions = list(blocker_actions)

    def fake_blocker_action(stage, reason, max_retries, allowed_actions=None, **_kwargs):
        control["blocker_calls"].append({
            "stage": stage,
            "reason": reason,
            "max_retries": max_retries,
            "allowed_actions": list(allowed_actions or []),
        })
        return remaining_actions.pop(0) if remaining_actions else "stop"

    def fake_manual_wait(**_options):
        control["manual_waits"] += 1
        return dict(manual_submission) if manual_submission else None

    def fake_check_stop(stage):
        return bool(stop_before_stage) and str(stage).startswith(stop_before_stage)

    monkeypatch.setenv("WORKFLOW_RUN_DIRECTORY", os.fspath(run_dir))
    monkeypatch.setenv("WORKFLOW_ID", "a" * 32)
    monkeypatch.setattr(workflow_orchestrator, "control_enabled", lambda: True)
    monkeypatch.setattr(
        workflow_orchestrator,
        "control_directory",
        lambda: run_dir / ".control",
    )
    monkeypatch.setattr(workflow_orchestrator, "wait_for_blocker_action", fake_blocker_action)
    monkeypatch.setattr(workflow_orchestrator, "wait_for_manual_reproduction", fake_manual_wait)
    monkeypatch.setattr(workflow_orchestrator, "wait_for_fix_review", fake_fix_review)
    monkeypatch.setattr(workflow_orchestrator, "check_stop_requested", fake_check_stop)
    monkeypatch.setattr(
        workflow_orchestrator,
        "publish_workflow_state",
        lambda status, stage, *args, **kwargs: control["published"].append((status, stage)),
    )
    return control


def missing_runner_context(tmp_path):
    repository = tmp_path / "workflow-root" / "target-project"
    return {
        "repository_path": os.fspath(repository),
        "execution_strategy": {
            "technology": "React/Vite + AWS Serverless",
            "test_framework": "Jest",
            "backend_path": "backend",
            "test_command": [
                "node",
                os.path.join("backend", "node_modules", "jest", "bin", "jest.js"),
                "--runInBand",
            ],
        },
        "architecture": {"components": []},
    }


def read_run_report(run_dir):
    return json.loads((run_dir / "workflow_report.json").read_text(encoding="utf-8"))


def test_ui_stop_at_preflight_blocker_starts_no_later_stage(tmp_path, monkeypatch):
    control = enable_mocked_ui_control(tmp_path, monkeypatch, blocker_actions=["stop"])

    result, run_dir, calls, _contexts = run_mocked_retry_workflow(
        tmp_path,
        monkeypatch,
        context_override=missing_runner_context(tmp_path),
    )

    report = read_run_report(run_dir)
    preflight = next(
        stage for stage in report["stages"]
        if stage["name"] == "Pre-fix Local Dependency Preflight"
    )
    assert result == 1
    assert report["final_status"] == "STOPPED"
    assert preflight["status"] == "STOPPED"
    assert len(control["blocker_calls"]) == 1
    assert control["blocker_calls"][0]["allowed_actions"] == [
        "retry", "continue", "stop_safely",
    ]
    assert calls["code_fix"] == 0
    assert calls["test_generation"] == 0
    assert calls["test_execution"] == 0
    assert calls["verification"] == 0
    assert not any(
        str(stage).startswith(("AI Code Fix", "AI Test Generation", "Test Execution"))
        for _status, stage in control["published"]
    )


def test_ui_preflight_retries_are_bounded_and_drop_retry_when_exhausted(
    tmp_path, monkeypatch
):
    control = enable_mocked_ui_control(
        tmp_path,
        monkeypatch,
        blocker_actions=["retry", "retry", "stop"],
    )

    result, run_dir, calls, _contexts = run_mocked_retry_workflow(
        tmp_path,
        monkeypatch,
        context_override=missing_runner_context(tmp_path),
    )

    assert result == 1
    assert read_run_report(run_dir)["final_status"] == "STOPPED"
    offered = [call["allowed_actions"] for call in control["blocker_calls"]]
    assert offered == [
        ["retry", "continue", "stop_safely"],
        ["retry", "continue", "stop_safely"],
        ["continue", "stop_safely"],
    ]
    assert calls["code_fix"] == 0


def test_ui_stop_before_code_fix_keeps_completed_stages_and_artifacts(
    tmp_path, monkeypatch
):
    enable_mocked_ui_control(tmp_path, monkeypatch, stop_before_stage="AI Code Fix")

    result, run_dir, calls, _contexts = run_mocked_retry_workflow(tmp_path, monkeypatch)

    report = read_run_report(run_dir)
    statuses = [(stage["stage"], stage["status"]) for stage in report["stages"]]
    assert result == 1
    assert report["final_status"] == "STOPPED"
    assert report["stopped_before_stage"] == "AI Code Fix (Attempt 1)"
    assert report["deployment_status"] == "NOT_DEPLOYED"
    assert statuses[:3] == [(1, "SUCCESS"), (2, "SUCCESS"), (3, "SUCCESS")]
    assert statuses[-1] == (None, "STOPPED")
    assert report["diagnosis"]["root_cause"] == "Mocked root cause."
    assert (run_dir / "diagnosis.json").is_file()
    assert (run_dir / "reproduction_result.json").is_file()
    assert calls["code_fix"] == 0
    assert calls["test_generation"] == 0


def test_ui_manual_evidence_reaches_initial_and_retry_diagnosis(tmp_path, monkeypatch):
    submission, errors = validate_manual_submission({
        "steps": ["Connect one client", "Send two messages"],
        "expected_behavior": "Both messages are stored.",
        "observed_behavior": "Only the second message remains.",
        "supporting_evidence": ["DynamoDB scan shows one item."],
    })
    assert errors == []
    control = enable_mocked_ui_control(
        tmp_path,
        monkeypatch,
        blocker_actions=["retry"],
        manual_submission=submission,
    )

    result, run_dir, calls, retry_contexts = run_mocked_retry_workflow(
        tmp_path,
        monkeypatch,
        failure_stage="verification",
        failed_attempts={1},
        reproduction_status="NOT_REPRODUCED",
    )

    report = read_run_report(run_dir)
    manual_path = run_dir / "manual_reproduction.json"
    manual = json.loads(manual_path.read_text(encoding="utf-8"))
    assert result == 0
    assert report["final_status"] == "PASS"
    assert report["reproduction_source"] == "manual"
    assert report["reproduction_evidence"]["manual_supporting_evidence"] == [
        "DynamoDB scan shows one item."
    ]
    assert control["manual_waits"] == 1
    assert manual["status"] == "MANUAL_SUBMITTED"
    assert manual["source"] == "user"
    assert manual["automatic_reproduction_status"] == "NOT_REPRODUCED"
    assert calls["diagnosis_arguments"][0] == ["--manual-reproduction", str(manual_path)]
    assert calls["diagnosis_arguments"][1][1:] == ["--manual-reproduction", str(manual_path)]
    assert retry_contexts[0]["manual_reproduction"]["steps"] == [
        "Connect one client",
        "Send two messages",
    ]
    assert [call["stage"] for call in control["blocker_calls"]] == ["Fix Verification"]
    # The bug was only user-reported, so the fix needed approval before PASS.
    assert len(control["fix_reviews"]) == 1
    assert report["fix_review"]["decision"] == "approve"
    assert report["reproduction_confirmed"] is False


@pytest.mark.parametrize(
    ("decision", "final_status", "review_status"),
    [("reject", "FAIL", "FAILED"), (None, "UNCONFIRMED", "SKIPPED")],
)
def test_unconfirmed_fix_is_not_pass_without_approval(tmp_path, monkeypatch, decision, final_status, review_status):
    submission, _errors = validate_manual_submission({
        "steps": ["Connect", "Send two messages"],
        "expected_behavior": "Both stored.",
        "observed_behavior": "One stored.",
        "supporting_evidence": [],
    })
    control = enable_mocked_ui_control(
        tmp_path, monkeypatch, manual_submission=submission, fix_review_decision=decision,
    )

    result, run_dir, _calls, _contexts = run_mocked_retry_workflow(
        tmp_path, monkeypatch, reproduction_status="NOT_REPRODUCED",
    )

    report = read_run_report(run_dir)
    review_stage = next(stage for stage in report["stages"] if stage["name"] == "Human Fix Review")
    assert result == 1
    assert report["final_status"] == final_status
    assert review_stage["status"] == review_status
    assert len(control["fix_reviews"]) == 1
    assert (run_dir / "fix_review.json").is_file()


def test_confirmed_bug_does_not_ask_for_fix_review(tmp_path, monkeypatch):
    control = enable_mocked_ui_control(tmp_path, monkeypatch)

    result, run_dir, _calls, _contexts = run_mocked_retry_workflow(tmp_path, monkeypatch)

    report = read_run_report(run_dir)
    assert result == 0
    assert report["final_status"] == "PASS"
    assert report["reproduction_confirmed"] is True
    assert control["fix_reviews"] == []
    assert "fix_review" not in report


def test_ui_manual_wait_timeout_stops_before_diagnosis(tmp_path, monkeypatch):
    enable_mocked_ui_control(tmp_path, monkeypatch, manual_submission=None)

    result, run_dir, calls, _contexts = run_mocked_retry_workflow(
        tmp_path,
        monkeypatch,
        reproduction_status="NOT_REPRODUCED",
    )

    report = read_run_report(run_dir)
    assert result == 1
    assert report["final_status"] == "FAIL"
    assert calls["bug_diagnosis"] == 0
    assert calls["code_fix"] == 0
    assert not (run_dir / "manual_reproduction.json").exists()


def test_ui_retry_exhaustion_fails_without_extra_attempts(tmp_path, monkeypatch):
    control = enable_mocked_ui_control(
        tmp_path,
        monkeypatch,
        blocker_actions=["retry", "retry", "retry"],
    )

    result, run_dir, calls, _contexts = run_mocked_retry_workflow(
        tmp_path,
        monkeypatch,
        failure_stage="verification",
        failed_attempts={1, 2, 3, 4},
    )

    report = read_run_report(run_dir)
    assert result == 1
    assert report["final_status"] == "FAIL"
    assert [item["overall"] for item in report["retry_history"]] == ["FAIL", "FAIL", "FAIL"]
    assert calls["code_fix"] == 3
    assert len(control["blocker_calls"]) == 2


def test_ui_stop_at_verification_blocker_prevents_retry(tmp_path, monkeypatch):
    control = enable_mocked_ui_control(tmp_path, monkeypatch, blocker_actions=["stop"])

    result, run_dir, calls, _contexts = run_mocked_retry_workflow(
        tmp_path,
        monkeypatch,
        failure_stage="verification",
        failed_attempts={1, 2, 3},
    )

    report = read_run_report(run_dir)
    assert result == 1
    assert report["final_status"] == "STOPPED"
    assert calls["code_fix"] == 1
    assert calls["bug_diagnosis"] == 1
    assert control["blocker_calls"][0]["allowed_actions"] == ["retry", "stop_safely"]
