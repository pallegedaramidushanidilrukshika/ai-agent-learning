import json
import os
from pathlib import Path

import pytest

import workflow_control


def test_atomic_write_retries_windows_sharing_violation(tmp_path, monkeypatch):
    real_replace = os.replace
    calls = {"count": 0}

    def flaky_replace(source, target):
        calls["count"] += 1
        if calls["count"] <= 2:
            raise PermissionError("file is open in another process")
        return real_replace(source, target)

    monkeypatch.setattr(workflow_control.os, "replace", flaky_replace)
    monkeypatch.setattr(workflow_control, "SHARING_RETRY_SECONDS", 0)
    path = tmp_path / "state.json"

    workflow_control._atomic_json(path, {"status": "RUNNING"})

    assert calls["count"] == 3
    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "RUNNING"}
    assert list(tmp_path.glob(".*.tmp")) == []


def test_atomic_write_gives_up_after_bounded_retries(tmp_path, monkeypatch):
    def always_locked(_source, _target):
        raise PermissionError("still locked")

    monkeypatch.setattr(workflow_control.os, "replace", always_locked)
    monkeypatch.setattr(workflow_control, "SHARING_RETRY_SECONDS", 0)

    with pytest.raises(PermissionError):
        workflow_control._atomic_json(tmp_path / "state.json", {"status": "RUNNING"})
    assert list(tmp_path.glob(".*.tmp")) == []


def configure_control_directory(tmp_path, monkeypatch):
    directory = tmp_path / "workflow_runs" / "run" / "human-control"
    directory.mkdir(parents=True)
    monkeypatch.setattr(
        workflow_control,
        "control_directory",
        lambda: directory,
    )
    monkeypatch.setenv(workflow_control.WORKFLOW_ID_ENV, "a" * 32)
    return directory


def test_control_state_is_written_atomically_with_safe_fields(tmp_path, monkeypatch):
    directory = configure_control_directory(tmp_path, monkeypatch)

    workflow_control.publish_workflow_state(
        "BLOCKED",
        "Dependency preflight",
        "Install the missing local test dependency.",
        "blocker",
    )

    state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    assert state["workflow_id"] == "a" * 32
    assert state["status"] == "BLOCKED"
    assert state["stage"] == "Dependency preflight"
    assert state["blocker_reason"] == "Install the missing local test dependency."
    assert list(directory.glob("*.tmp")) == []

    workflow_control.publish_workflow_state("RUNNING", "Next stage")
    resumed_state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    assert resumed_state["allowed_actions"] == []


def test_record_stage_tracks_progress_without_changing_status(tmp_path, monkeypatch):
    directory = configure_control_directory(tmp_path, monkeypatch)
    workflow_control.publish_workflow_state(
        "WAITING_FOR_MANUAL_REPRODUCTION",
        "Bug Reproduction",
        "Provide details.",
        "manual_reproduction",
        ["stop_safely"],
    )

    workflow_control.record_stage(1, "Repository Analysis", "RUNNING")
    workflow_control.record_stage(1, "Repository Analysis", "SUCCESS")
    workflow_control.record_stage(2, "Bug Reproduction", "RUNNING")

    state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "WAITING_FOR_MANUAL_REPRODUCTION"
    assert state["allowed_actions"] == ["stop_safely"]
    assert state["stages"] == [
        {"stage": 1, "name": "Repository Analysis", "status": "SUCCESS"},
        {"stage": 2, "name": "Bug Reproduction", "status": "RUNNING"},
    ]


def test_confirm_is_only_offered_for_a_test_that_fails():
    assert workflow_control.review_allowed_actions("FAILS_ON_CURRENT_CODE") == [
        "confirm", "revise", "manual", "stop_safely",
    ]
    assert "confirm" not in workflow_control.review_allowed_actions("PASSES_ON_CURRENT_CODE")
    assert "confirm" not in workflow_control.review_allowed_actions("ERROR")


@pytest.mark.parametrize(
    ("value", "allowed", "ok"),
    [
        ({"decision": "confirm"}, ["confirm", "revise", "manual"], True),
        ({"decision": "confirm"}, ["revise", "manual"], False),
        ({"decision": "revise", "feedback": "  "}, ["revise"], False),
        ({"decision": "revise", "feedback": "Use one connection"}, ["revise"], True),
        ({"decision": "revise", "feedback": "x" * 4001}, ["revise"], False),
        ({"decision": "delete"}, ["confirm", "revise", "manual"], False),
        ("confirm", ["confirm"], False),
    ],
)
def test_review_decision_validation(value, allowed, ok):
    decision, error = workflow_control.validate_review_decision(value, allowed)
    assert (decision is not None) is ok
    assert (error is None) is ok


def test_review_wait_publishes_the_test_and_returns_the_decision(tmp_path, monkeypatch):
    directory = configure_control_directory(tmp_path, monkeypatch)
    monkeypatch.setattr(workflow_control, "CONTROL_POLL_SECONDS", 0)
    artifact = {
        "framework": "jest",
        "test_file": "backend/test/ai-reproduction.test.js",
        "classification": "FAILS_ON_CURRENT_CODE",
        "content": "test('x', () => {});",
        "run": {"output_tail": "Expected 2", "tests": 1, "failed": 1},
    }
    published = []
    original_publish = workflow_control.publish_workflow_state

    def publish_then_review(*args, **kwargs):
        original_publish(*args, **kwargs)
        published.append(json.loads((directory / "state.json").read_text(encoding="utf-8")))
        if len(published) == 1:
            workflow_control._atomic_json(directory / "test-review.json", {"decision": "revise", "feedback": ""})
        elif len(published) == 2:
            workflow_control._atomic_json(directory / "test-review.json", {"decision": "confirm"})

    monkeypatch.setattr(workflow_control, "publish_workflow_state", publish_then_review)

    decision = workflow_control.wait_for_reproduction_review(artifact, 1, 3, timeout=5)

    assert decision == {"decision": "confirm", "feedback": ""}
    first = published[0]
    assert first["status"] == "WAITING_FOR_TEST_REVIEW"
    assert first["pause_kind"] == "reproduction_test_review"
    assert first["allowed_actions"][0] == "confirm"
    assert first["review"]["content"] == "test('x', () => {});"
    assert "not accepted" in published[1]["blocker_reason"]


def test_review_wait_honours_stop(tmp_path, monkeypatch):
    directory = configure_control_directory(tmp_path, monkeypatch)
    workflow_control._atomic_json(directory / "stop.request", {"action_id": "x"})

    with pytest.raises(workflow_control.WorkflowStopRequested):
        workflow_control.wait_for_reproduction_review({"classification": "ERROR"}, 1, 3, timeout=5)


def test_stop_signal_is_consumed_once(tmp_path, monkeypatch):
    directory = configure_control_directory(tmp_path, monkeypatch)
    (directory / "stop.request").write_text("{}", encoding="utf-8")

    assert workflow_control.check_stop_requested("Test Execution") is True
    assert workflow_control.stop_requested() is False
    assert json.loads((directory / "state.json").read_text(encoding="utf-8"))["status"] == "STOPPED"


@pytest.mark.parametrize(
    "path_value",
    ["", None],
)
def test_control_directory_is_disabled_without_api_environment(monkeypatch, path_value):
    if path_value is None:
        monkeypatch.delenv(workflow_control.CONTROL_DIRECTORY_ENV, raising=False)
    else:
        monkeypatch.setenv(workflow_control.CONTROL_DIRECTORY_ENV, path_value)

    assert workflow_control.control_directory() is None


def test_control_directory_rejects_paths_outside_run_artifacts(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv(workflow_control.CONTROL_DIRECTORY_ENV, os.fspath(outside))

    assert workflow_control.control_directory() is None


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"steps": [], "expected_behavior": "expected", "observed_behavior": "observed"},
        {"steps": ["step"], "expected_behavior": " ", "observed_behavior": "observed"},
        {"steps": ["step"], "expected_behavior": "expected", "observed_behavior": "observed", "supporting_evidence": "bad"},
        {"steps": ["x" * 4001], "expected_behavior": "expected", "observed_behavior": "observed"},
    ],
)
def test_manual_submission_rejects_missing_or_oversized_fields(payload):
    _submission, errors = workflow_control.validate_manual_submission(payload)

    assert errors


def test_manual_submission_returns_existing_artifact_shape():
    submission, errors = workflow_control.validate_manual_submission({
        "steps": [" Connect to the app ", " Send a message "],
        "expected_behavior": " Each message is retained ",
        "observed_behavior": " Only the last message remains ",
        "supporting_evidence": [" Jest showed one row. "],
    })

    assert errors == []
    assert submission == {
        "status": "MANUAL_SUBMITTED",
        "source": "user",
        "steps": ["Connect to the app", "Send a message"],
        "expected_behavior": "Each message is retained",
        "observed_behavior": "Only the last message remains",
        "supporting_evidence": ["Jest showed one row."],
    }
