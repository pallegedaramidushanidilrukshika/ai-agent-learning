import json
import os
import subprocess
import sys
import threading
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from workflow_control import _atomic_json
from workflow_ui_api import RequestError, WorkflowManager, make_handler


def make_payload(repository, **overrides):
    payload = {
        "request_id": str(uuid.uuid4()),
        "repository_path": os.fspath(repository),
        "bug_title": "Messages overwrite",
        "bug_description": "Multiple messages from one connection are not retained.",
        "allow_local_commands": True,
        "allow_ai": True,
    }
    payload.update(overrides)
    return payload


def make_manager(tmp_path, monkeypatch, process_factory=None):
    repository = tmp_path / "target"
    repository.mkdir()
    scheduled = []
    manager = WorkflowManager(
        project_dir=tmp_path,
        process_factory=process_factory,
        worker_launcher=scheduled.append,
    )
    monkeypatch.setenv("WORKFLOW_ALLOWED_REPOSITORY_ROOTS", os.fspath(tmp_path))
    return repository, manager, scheduled


def set_workflow_state(manager, workflow_id, **changes):
    control_path = manager.runs_dir / workflow_id / ".control"
    state_path = control_path / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(changes)
    _atomic_json(state_path, state)
    return control_path


def test_workflow_start_uses_fixed_command_and_duplicate_request_is_idempotent(
    tmp_path, monkeypatch
):
    repository, manager, scheduled = make_manager(tmp_path, monkeypatch)
    payload = make_payload(repository, command=["format", "arbitrary"])

    first, created = manager.start(payload)
    duplicate, duplicate_created = manager.start(payload)

    assert created is True
    assert duplicate_created is False
    assert first["workflow_id"] == duplicate["workflow_id"]
    assert len(scheduled) == 1
    report = json.loads(
        (manager.runs_dir / first["workflow_id"] / ".control" / "bug-report.json")
        .read_text(encoding="utf-8")
    )
    assert report["bug_title"] == "Messages overwrite"
    assert "command" not in report


@pytest.mark.parametrize(
    "overrides",
    [
        {"repository_path": "relative/path"},
        {"allow_local_commands": False},
        {"allow_ai": False},
        {"bug_title": ""},
        {"bug_description": ""},
    ],
)
def test_workflow_start_rejects_invalid_path_or_missing_explicit_approval(
    tmp_path, monkeypatch, overrides
):
    repository, manager, scheduled = make_manager(tmp_path, monkeypatch)

    with pytest.raises(RequestError):
        manager.start(make_payload(repository, **overrides))

    assert scheduled == []


def test_workflow_start_rejects_paths_outside_allowed_roots(tmp_path, monkeypatch):
    repository, manager, _scheduled = make_manager(tmp_path, monkeypatch)
    outside = tmp_path.parent / f"outside-{uuid.uuid4().hex}"
    outside.mkdir()
    try:
        with pytest.raises(RequestError) as error:
            manager.start(make_payload(outside))
        assert error.value.status_code == 403
    finally:
        outside.rmdir()


def test_manual_reproduction_is_only_accepted_at_wait_state_and_is_idempotent(
    tmp_path, monkeypatch
):
    repository, manager, _scheduled = make_manager(tmp_path, monkeypatch)
    workflow, _created = manager.start(make_payload(repository))
    manual = {
        "steps": ["Connect", "Send two messages"],
        "expected_behavior": "Both messages remain retrievable.",
        "observed_behavior": "Only the latest remains.",
        "supporting_evidence": ["Observed in local test."],
    }
    submission = {"submission_id": str(uuid.uuid4()), "manual_reproduction": manual}
    with pytest.raises(RequestError) as early:
        manager.submit_manual_reproduction(workflow["workflow_id"], submission)
    assert early.value.status_code == 409

    set_workflow_state(
        manager,
        workflow["workflow_id"],
        status="WAITING_FOR_MANUAL_REPRODUCTION",
    )
    accepted = manager.submit_manual_reproduction(workflow["workflow_id"], submission)
    duplicate = manager.submit_manual_reproduction(workflow["workflow_id"], submission)

    assert accepted["status"] == "SUBMISSION_RECEIVED"
    assert duplicate["status"] == "SUBMISSION_RECEIVED"
    artifact = json.loads(
        (manager.runs_dir / workflow["workflow_id"] / ".control" / "manual-submission.json")
        .read_text(encoding="utf-8")
    )
    assert artifact["status"] == "MANUAL_SUBMITTED"
    assert artifact["source"] == "user"
    assert artifact["steps"] == ["Connect", "Send two messages"]


def test_blocker_actions_enforce_state_retry_limit_and_duplicate_ids(
    tmp_path, monkeypatch
):
    repository, manager, _scheduled = make_manager(tmp_path, monkeypatch)
    workflow, _created = manager.start(make_payload(repository))
    workflow_id = workflow["workflow_id"]
    control_path = set_workflow_state(
        manager,
        workflow_id,
        status="BLOCKED",
        allowed_actions=["retry", "continue", "stop_safely"],
    )
    action_id = str(uuid.uuid4())

    retried = manager.action(workflow_id, {"action_id": action_id, "action": "retry"})
    duplicate = manager.action(workflow_id, {"action_id": action_id, "action": "retry"})
    assert retried["status"] == duplicate["status"] == "ACTION_SUBMITTED"
    assert retried["retry_count"] == duplicate["retry_count"] == 1
    assert json.loads((control_path / "action.json").read_text(encoding="utf-8"))["action"] == "retry"

    with pytest.raises(RequestError) as pending:
        manager.action(workflow_id, {"action_id": str(uuid.uuid4()), "action": "continue"})
    assert pending.value.status_code == 409

    set_workflow_state(
        manager,
        workflow_id,
        status="BLOCKED",
        retry_count=2,
        allowed_actions=["continue", "stop_safely"],
    )
    with pytest.raises(RequestError) as exhausted:
        manager.action(workflow_id, {"action_id": str(uuid.uuid4()), "action": "retry"})
    assert exhausted.value.status_code == 409


def test_stop_safely_signals_running_workflow_and_rejects_terminal_actions(
    tmp_path, monkeypatch
):
    repository, manager, _scheduled = make_manager(tmp_path, monkeypatch)
    workflow, _created = manager.start(make_payload(repository))
    workflow_id = workflow["workflow_id"]
    control_path = manager.runs_dir / workflow_id / ".control"

    stopped = manager.action(
        workflow_id,
        {"action_id": str(uuid.uuid4()), "action": "stop_safely"},
    )
    assert stopped["status"] == "STOPPING"
    assert (control_path / "stop.request").is_file()

    set_workflow_state(manager, workflow_id, status="PASS")
    with pytest.raises(RequestError) as terminal:
        manager.action(
            workflow_id,
            {"action_id": str(uuid.uuid4()), "action": "stop_safely"},
        )
    assert terminal.value.status_code == 409


def test_api_launches_only_the_existing_orchestrator_with_sanitized_environment(
    tmp_path, monkeypatch
):
    repository, _manager, scheduled = make_manager(tmp_path, monkeypatch)
    captured = {}

    class FakeProcess:
        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)
            run_directory = Path(kwargs["env"]["WORKFLOW_RUN_DIRECTORY"])
            (run_directory / "workflow_report.json").write_text(
                json.dumps({
                    "final_status": "FAIL",
                    "stages": [],
                    "retry_history": [],
                    "deployment_status": "NOT_DEPLOYED",
                    "deployment_readiness": "NOT_ASSESSED",
                }),
                encoding="utf-8",
            )

        def wait(self):
            return 1

    manager = WorkflowManager(
        project_dir=tmp_path,
        process_factory=FakeProcess,
        worker_launcher=scheduled.append,
    )
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "not-passed")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-passed")

    workflow, _created = manager.start(make_payload(repository))
    scheduled[0]()

    assert captured["command"][0] == sys.executable
    assert Path(captured["command"][1]).name == "workflow_orchestrator.py"
    assert captured["command"][2:4] == ["--local-repository", os.fspath(repository)]
    assert captured["shell"] is False
    assert captured["env"]["WORKFLOW_AI_APPROVED"] == "true"
    assert captured["env"]["WORKFLOW_LOCAL_COMMANDS_APPROVED"] == "true"
    assert "AWS_ACCESS_KEY_ID" not in captured["env"]
    assert "AWS_SECRET_ACCESS_KEY" not in captured["env"]
    assert manager.get(workflow["workflow_id"])["status"] == "FAIL"


def test_http_api_health_origin_and_body_size_guards(tmp_path, monkeypatch):
    repository, manager, _scheduled = make_manager(tmp_path, monkeypatch)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(manager))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base_url}/api/health", timeout=2) as response:
            assert json.load(response)["status"] == "running"

        denied = Request(
            f"{base_url}/api/workflows",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "Origin": "https://not-allowed.example",
            },
            method="POST",
        )
        with pytest.raises(HTTPError) as origin_error:
            urlopen(denied, timeout=2)
        assert origin_error.value.code == 403

        # Ordinary routes keep the 32 KB limit.
        oversized = Request(
            f"{base_url}/api/workflows/{'a' * 32}/actions",
            data=b" " * 32_769,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as size_error:
            urlopen(oversized, timeout=2)
        assert size_error.value.code == 413

        # Routes that carry attached files allow up to 3 MB, and no more.
        oversized_files = Request(
            f"{base_url}/api/workflows",
            data=b" " * (3 * 1024 * 1024 + 1),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as file_size_error:
            urlopen(oversized_files, timeout=5)
        assert file_size_error.value.code == 413
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ------------------------------------------------------------
# Completion status, terminal states and concurrency
# ------------------------------------------------------------

def run_with_fake_process(tmp_path, monkeypatch, report, return_code, state_changes=None):
    repository = tmp_path / "target"
    repository.mkdir()
    monkeypatch.setenv("WORKFLOW_ALLOWED_REPOSITORY_ROOTS", os.fspath(tmp_path))
    scheduled = []

    class FakeProcess:
        def __init__(self, _command, **kwargs):
            self.run_directory = Path(kwargs["env"]["WORKFLOW_RUN_DIRECTORY"])

        def wait(self):
            if state_changes:
                state_path = self.run_directory / ".control" / "state.json"
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state.update(state_changes)
                _atomic_json(state_path, state)
            if report is not None:
                _atomic_json(self.run_directory / "workflow_report.json", report)
            return return_code

    manager = WorkflowManager(
        project_dir=tmp_path,
        process_factory=FakeProcess,
        worker_launcher=scheduled.append,
    )
    workflow, _created = manager.start(make_payload(repository))
    scheduled[0]()
    return manager, workflow["workflow_id"]


def report_with_status(final_status):
    return {
        "final_status": final_status,
        "stages": [{"stage": 1, "name": "Repository Analysis", "status": "SUCCESS"}],
        "retry_history": [],
        "deployment_status": "NOT_DEPLOYED",
        "deployment_readiness": "NOT_ASSESSED",
    }


def test_pass_report_with_failed_process_exit_is_not_pass(tmp_path, monkeypatch):
    manager, workflow_id = run_with_fake_process(
        tmp_path, monkeypatch, report_with_status("PASS"), return_code=1
    )

    status = manager.get(workflow_id)
    assert status["status"] == "FAIL"
    assert status["terminal"] is True
    assert status["allowed_actions"] == []
    assert "exited with an error" in status["blocker_reason"]


def test_pass_requires_clean_exit_and_report(tmp_path, monkeypatch):
    manager, workflow_id = run_with_fake_process(
        tmp_path, monkeypatch, report_with_status("PASS"), return_code=0
    )
    assert manager.get(workflow_id)["status"] == "PASS"


def test_missing_report_is_fail_even_with_clean_exit(tmp_path, monkeypatch):
    manager, workflow_id = run_with_fake_process(tmp_path, monkeypatch, None, return_code=0)

    status = manager.get(workflow_id)
    assert status["status"] == "FAIL"
    assert status["report"] is None
    assert status["terminal"] is True


def test_final_blocked_report_is_terminal_and_rejects_actions(tmp_path, monkeypatch):
    manager, workflow_id = run_with_fake_process(
        tmp_path,
        monkeypatch,
        report_with_status("BLOCKED"),
        return_code=1,
        state_changes={
            "status": "BLOCKED",
            "allowed_actions": ["retry", "continue", "stop_safely"],
            "pause_kind": "blocker",
        },
    )

    status = manager.get(workflow_id)
    assert status["status"] == "BLOCKED"
    assert status["terminal"] is True
    assert status["allowed_actions"] == []
    assert status["pause_kind"] is None
    for action in ("retry", "continue", "stop_safely"):
        with pytest.raises(RequestError) as error:
            manager.action(workflow_id, {"action_id": str(uuid.uuid4()), "action": action})
        assert error.value.status_code == 409


def test_stop_without_report_writes_stopped_report(tmp_path, monkeypatch):
    manager, workflow_id = run_with_fake_process(
        tmp_path, monkeypatch, None, return_code=1, state_changes={"status": "STOPPING"}
    )

    status = manager.get(workflow_id)
    assert status["status"] == "STOPPED"
    assert status["terminal"] is True
    assert status["report"]["final_status"] == "STOPPED"
    assert status["report"]["deployment_status"] == "NOT_DEPLOYED"


def test_orchestrator_report_wins_over_late_stop_request(tmp_path, monkeypatch):
    manager, workflow_id = run_with_fake_process(
        tmp_path,
        monkeypatch,
        report_with_status("FAIL"),
        return_code=1,
        state_changes={"status": "STOPPING"},
    )
    assert manager.get(workflow_id)["status"] == "FAIL"


def test_live_run_is_not_terminal_until_process_exits(tmp_path, monkeypatch):
    # The orchestrator publishes STOPPED before writing its final report;
    # the UI must keep polling until the API records process exit.
    repository, manager, _scheduled = make_manager(tmp_path, monkeypatch)
    workflow, _created = manager.start(make_payload(repository))
    set_workflow_state(manager, workflow["workflow_id"], status="STOPPED")

    live = manager.get(workflow["workflow_id"])
    assert live["status"] == "STOPPED"
    assert live["terminal"] is False

    manager.runs.clear()  # simulate a restarted API reading the run from disk
    assert manager.get(workflow["workflow_id"])["terminal"] is True


def test_live_stages_are_shown_until_the_report_exists(tmp_path, monkeypatch):
    repository, manager, _scheduled = make_manager(tmp_path, monkeypatch)
    workflow, _created = manager.start(make_payload(repository))
    workflow_id = workflow["workflow_id"]
    live_stages = [
        {"stage": 1, "name": "Repository Analysis", "status": "SUCCESS"},
        {"stage": 2, "name": "Bug Reproduction", "status": "RUNNING"},
    ]
    set_workflow_state(manager, workflow_id, status="RUNNING", stages=live_stages)

    assert manager.get(workflow_id)["stages"] == live_stages

    _atomic_json(
        manager.runs_dir / workflow_id / "workflow_report.json",
        report_with_status("FAIL"),
    )
    assert manager.get(workflow_id)["stages"] == [
        {"stage": 1, "name": "Repository Analysis", "status": "SUCCESS"}
    ]


REVIEW_STATE = {
    "status": "WAITING_FOR_TEST_REVIEW",
    "pause_kind": "reproduction_test_review",
    "allowed_actions": ["confirm", "revise", "manual", "stop_safely"],
    "review": {
        "round": 1,
        "max_rounds": 3,
        "framework": "jest",
        "test_file": "backend/test/ai-reproduction.test.js",
        "classification": "FAILS_ON_CURRENT_CODE",
        "content": "test('keeps both', () => {});",
        "output_tail": "Expected 2 received 1",
        "tests": 1,
        "failed": 1,
        "internal_note": "not public",
    },
}


def review_payload(decision, feedback=""):
    return {"review_id": str(uuid.uuid4()), "decision": decision, "feedback": feedback}


def test_review_is_public_only_while_waiting(tmp_path, monkeypatch):
    repository, manager, _scheduled = make_manager(tmp_path, monkeypatch)
    workflow, _created = manager.start(make_payload(repository))
    workflow_id = workflow["workflow_id"]
    assert manager.get(workflow_id)["review"] is None

    set_workflow_state(manager, workflow_id, **REVIEW_STATE)
    review = manager.get(workflow_id)["review"]
    assert review["content"] == "test('keeps both', () => {});"
    assert review["classification"] == "FAILS_ON_CURRENT_CODE"
    assert "internal_note" not in review


def test_review_submission_writes_signal_and_is_idempotent(tmp_path, monkeypatch):
    repository, manager, _scheduled = make_manager(tmp_path, monkeypatch)
    workflow, _created = manager.start(make_payload(repository))
    workflow_id = workflow["workflow_id"]
    control_path = set_workflow_state(manager, workflow_id, **REVIEW_STATE)
    payload = review_payload("revise", "Send both messages on one connection")

    first = manager.submit_reproduction_review(workflow_id, payload)
    again = manager.submit_reproduction_review(workflow_id, payload)

    assert first["status"] == again["status"] == "REVIEW_SUBMITTED"
    signal = json.loads((control_path / "test-review.json").read_text(encoding="utf-8"))
    assert signal["decision"] == "revise"
    assert signal["feedback"] == "Send both messages on one connection"


@pytest.mark.parametrize(
    ("state_changes", "payload", "status_code"),
    [
        ({}, review_payload("confirm"), 409),
        ({"allowed_actions": ["revise", "manual", "stop_safely"]}, review_payload("confirm"), 409),
        ({}, review_payload("revise", ""), 400),
        ({}, review_payload("delete"), 400),
        ({}, {"decision": "confirm"}, 400),
    ],
)
def test_invalid_reviews_are_rejected(tmp_path, monkeypatch, state_changes, payload, status_code):
    repository, manager, _scheduled = make_manager(tmp_path, monkeypatch)
    workflow, _created = manager.start(make_payload(repository))
    workflow_id = workflow["workflow_id"]
    if state_changes or payload.get("decision") != "confirm" or "review_id" not in payload:
        set_workflow_state(manager, workflow_id, **dict(REVIEW_STATE, **state_changes))
    with pytest.raises(RequestError) as error:
        manager.submit_reproduction_review(workflow_id, payload)
    assert error.value.status_code == status_code
    assert not (manager.runs_dir / workflow_id / ".control" / "test-review.json").exists()


def test_exhausted_retry_budget_is_not_offered(tmp_path, monkeypatch):
    repository, manager, _scheduled = make_manager(tmp_path, monkeypatch)
    workflow, _created = manager.start(make_payload(repository))
    set_workflow_state(
        manager,
        workflow["workflow_id"],
        status="BLOCKED",
        retry_count=2,
        allowed_actions=["retry", "stop_safely"],
    )

    status = manager.get(workflow["workflow_id"])
    assert status["allowed_actions"] == ["stop_safely"]


def test_concurrent_distinct_actions_accept_only_one(tmp_path, monkeypatch):
    repository, manager, _scheduled = make_manager(tmp_path, monkeypatch)
    workflow, _created = manager.start(make_payload(repository))
    workflow_id = workflow["workflow_id"]
    set_workflow_state(
        manager,
        workflow_id,
        status="BLOCKED",
        allowed_actions=["retry", "continue", "stop_safely"],
    )
    barrier = threading.Barrier(6)
    outcomes = []

    def submit(action):
        barrier.wait()
        try:
            manager.action(workflow_id, {"action_id": str(uuid.uuid4()), "action": action})
            outcomes.append(("ok", action))
        except RequestError as error:
            outcomes.append((error.status_code, action))

    threads = [
        threading.Thread(target=submit, args=(action,))
        for action in ["retry", "continue"] * 3
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    accepted = [item for item in outcomes if item[0] == "ok"]
    assert len(outcomes) == 6
    assert len(accepted) == 1
    assert all(item[0] == 409 for item in outcomes if item[0] != "ok")
    assert manager.get(workflow_id)["retry_count"] in (0, 1)


def test_second_manual_submission_is_rejected_while_one_is_pending(tmp_path, monkeypatch):
    repository, manager, _scheduled = make_manager(tmp_path, monkeypatch)
    workflow, _created = manager.start(make_payload(repository))
    workflow_id = workflow["workflow_id"]
    manual = {
        "steps": ["Connect"],
        "expected_behavior": "Kept.",
        "observed_behavior": "Lost.",
        "supporting_evidence": [],
    }
    set_workflow_state(manager, workflow_id, status="WAITING_FOR_MANUAL_REPRODUCTION")
    manager.submit_manual_reproduction(
        workflow_id, {"submission_id": str(uuid.uuid4()), "manual_reproduction": manual}
    )
    set_workflow_state(manager, workflow_id, status="WAITING_FOR_MANUAL_REPRODUCTION")

    with pytest.raises(RequestError) as error:
        manager.submit_manual_reproduction(
            workflow_id, {"submission_id": str(uuid.uuid4()), "manual_reproduction": manual}
        )
    assert error.value.status_code == 409


@pytest.mark.parametrize("workflow_id", ["../etc", "A" * 32, "x", "a" * 31, "a" * 32 + "/x"])
def test_invalid_workflow_ids_are_rejected(tmp_path, monkeypatch, workflow_id):
    _repository, manager, _scheduled = make_manager(tmp_path, monkeypatch)
    with pytest.raises(RequestError) as error:
        manager.get(workflow_id)
    assert error.value.status_code == 400


def test_manual_evidence_limits_are_enforced_at_start(tmp_path, monkeypatch):
    repository, manager, scheduled = make_manager(tmp_path, monkeypatch)
    oversized = {
        "steps": ["Connect"],
        "expected_behavior": "Kept.",
        "observed_behavior": "Lost.",
        "supporting_evidence": ["x" * 8001],
    }
    with pytest.raises(RequestError) as error:
        manager.start(make_payload(repository, manual_reproduction=oversized))
    assert error.value.status_code == 400
    assert scheduled == []


# ------------------------------------------------------------
# HTTP API + real file control channel (orchestrator process faked)
# ------------------------------------------------------------

def test_http_ui_flow_drives_real_control_channel(tmp_path, monkeypatch):
    import time

    import workflow_control

    repository = tmp_path / "target"
    repository.mkdir()
    monkeypatch.setenv("WORKFLOW_ALLOWED_REPOSITORY_ROOTS", os.fspath(tmp_path))
    monkeypatch.setattr(workflow_control, "CONTROL_POLL_SECONDS", 0.02)
    seen = {}

    class ControlledProcess:
        """Stands in for workflow_orchestrator.py, using the real control functions."""

        def __init__(self, _command, **kwargs):
            self.env = kwargs["env"]

        def wait(self):
            control_dir = Path(self.env["WORKFLOW_CONTROL_DIRECTORY"])
            run_dir = Path(self.env["WORKFLOW_RUN_DIRECTORY"])
            monkeypatch.setattr(workflow_control, "control_directory", lambda: control_dir)
            monkeypatch.setenv("WORKFLOW_ID", self.env["WORKFLOW_ID"])
            seen["manual"] = workflow_control.wait_for_manual_reproduction(timeout=10)
            _atomic_json(run_dir / "manual_reproduction.json", seen["manual"])
            seen["first"] = workflow_control.wait_for_blocker_action(
                "Fix Verification", "Verification failed.", 2,
                allowed_actions=["retry", "stop_safely"], timeout=10,
            )
            seen["second"] = workflow_control.wait_for_blocker_action(
                "Fix Verification", "Verification failed again.", 2,
                allowed_actions=["retry", "stop_safely"], timeout=10,
            )
            _atomic_json(run_dir / "workflow_report.json", report_with_status("STOPPED"))
            return 1

    manager = WorkflowManager(project_dir=tmp_path, process_factory=ControlledProcess)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(manager))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    def call(method, path, body=None):
        request = Request(
            f"{base_url}{path}",
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1:5173"},
            method=method,
        )
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def wait_for(workflow_id, predicate):
        deadline = time.monotonic() + 10
        status = None
        while time.monotonic() < deadline:
            status = call("GET", f"/api/workflows/{workflow_id}")
            if predicate(status):
                return status
            time.sleep(0.02)
        raise AssertionError(f"Timed out; last status {status}")

    try:
        started = call("POST", "/api/workflows", make_payload(repository))
        workflow_id = started["workflow_id"]

        waiting = wait_for(
            workflow_id, lambda s: s["status"] == "WAITING_FOR_MANUAL_REPRODUCTION"
        )
        assert waiting["pause_kind"] == "manual_reproduction"
        call("POST", f"/api/workflows/{workflow_id}/manual-reproduction", {
            "submission_id": str(uuid.uuid4()),
            "manual_reproduction": {
                "steps": ["Connect", "Send two messages"],
                "expected_behavior": "Both kept.",
                "observed_behavior": "One kept.",
                "supporting_evidence": ["scan output"],
            },
        })

        blocked = wait_for(workflow_id, lambda s: s["status"] == "BLOCKED")
        assert blocked["allowed_actions"] == ["retry", "stop_safely"]
        retried = call("POST", f"/api/workflows/{workflow_id}/actions", {
            "action_id": str(uuid.uuid4()), "action": "retry",
        })
        assert retried["retry_count"] == 1

        wait_for(
            workflow_id,
            lambda s: s["status"] == "BLOCKED"
            and s["blocker_reason"] == "Verification failed again.",
        )
        call("POST", f"/api/workflows/{workflow_id}/actions", {
            "action_id": str(uuid.uuid4()), "action": "stop_safely",
        })

        final = wait_for(workflow_id, lambda s: s["terminal"])
        assert final["status"] == "STOPPED"
        assert final["report"]["final_status"] == "STOPPED"
        assert seen["manual"]["source"] == "user"
        assert seen["manual"]["steps"] == ["Connect", "Send two messages"]
        assert seen["first"] == "retry"
        assert seen["second"] == "stop"
        artifact = json.loads(
            (manager.runs_dir / workflow_id / "manual_reproduction.json")
            .read_text(encoding="utf-8")
        )
        assert artifact["status"] == "MANUAL_SUBMITTED"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_reproduction_test_review_flow(tmp_path, monkeypatch):
    import time

    import workflow_control

    repository = tmp_path / "target"
    repository.mkdir()
    monkeypatch.setenv("WORKFLOW_ALLOWED_REPOSITORY_ROOTS", os.fspath(tmp_path))
    monkeypatch.setattr(workflow_control, "CONTROL_POLL_SECONDS", 0.02)
    decisions = []

    def artifact(round_number, classification):
        return {
            "framework": "jest",
            "test_file": "backend/test/ai-reproduction.test.js",
            "classification": classification,
            "content": f"test('round {round_number}', () => {{}});",
            "run": {"output_tail": f"round {round_number} output", "tests": 1, "failed": 1},
        }

    class ReviewingProcess:
        """Stands in for the orchestrator's review loop using the real control functions."""

        def __init__(self, _command, **kwargs):
            self.env = kwargs["env"]

        def wait(self):
            control_dir = Path(self.env["WORKFLOW_CONTROL_DIRECTORY"])
            run_dir = Path(self.env["WORKFLOW_RUN_DIRECTORY"])
            monkeypatch.setattr(workflow_control, "control_directory", lambda: control_dir)
            monkeypatch.setenv("WORKFLOW_ID", self.env["WORKFLOW_ID"])
            decisions.append(workflow_control.wait_for_reproduction_review(
                artifact(1, "PASSES_ON_CURRENT_CODE"), 1, 3, timeout=10
            ))
            decisions.append(workflow_control.wait_for_reproduction_review(
                artifact(2, "FAILS_ON_CURRENT_CODE"), 2, 3, timeout=10
            ))
            _atomic_json(run_dir / "workflow_report.json", report_with_status("FAIL"))
            return 1

    manager = WorkflowManager(project_dir=tmp_path, process_factory=ReviewingProcess)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(manager))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    def call(method, path, body=None):
        request = Request(
            f"{base_url}{path}",
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1:5173"},
            method=method,
        )
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def wait_for(workflow_id, predicate):
        deadline = time.monotonic() + 10
        status = None
        while time.monotonic() < deadline:
            status = call("GET", f"/api/workflows/{workflow_id}")
            if predicate(status):
                return status
            time.sleep(0.02)
        raise AssertionError(f"Timed out; last status {status}")

    try:
        workflow_id = call("POST", "/api/workflows", make_payload(repository))["workflow_id"]

        first = wait_for(workflow_id, lambda s: (s.get("review") or {}).get("round") == 1)
        assert first["status"] == "WAITING_FOR_TEST_REVIEW"
        assert "confirm" not in first["allowed_actions"]
        assert first["review"]["content"] == "test('round 1', () => {});"
        with pytest.raises(HTTPError) as not_allowed:
            call("POST", f"/api/workflows/{workflow_id}/reproduction-review", review_payload("confirm"))
        assert not_allowed.value.code == 409
        call("POST", f"/api/workflows/{workflow_id}/reproduction-review",
             review_payload("revise", "Send both messages on the same connection"))

        second = wait_for(workflow_id, lambda s: (s.get("review") or {}).get("round") == 2)
        assert second["allowed_actions"][0] == "confirm"
        call("POST", f"/api/workflows/{workflow_id}/reproduction-review", review_payload("confirm"))

        final = wait_for(workflow_id, lambda s: s["terminal"])
        assert final["review"] is None
        assert decisions == [
            {"decision": "revise", "feedback": "Send both messages on the same connection"},
            {"decision": "confirm", "feedback": ""},
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_run_left_running_by_a_restarted_api_is_shown_as_interrupted(tmp_path, monkeypatch):
    repository, manager, _scheduled = make_manager(tmp_path, monkeypatch)
    workflow, _created = manager.start(make_payload(repository))
    set_workflow_state(
        manager, workflow["workflow_id"],
        status="STOPPING", pause_kind="manual_reproduction", allowed_actions=["stop_safely"],
    )
    assert manager.get(workflow["workflow_id"])["terminal"] is False

    manager.runs.clear()  # the API restarted; nothing is attached to the run
    status = manager.get(workflow["workflow_id"])

    assert status["status"] == "INTERRUPTED"
    assert status["terminal"] is True
    assert status["allowed_actions"] == []
    assert status["pause_kind"] is None
    assert "interrupted" in status["blocker_reason"]
