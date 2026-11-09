"""Human review of a fix for a bug no automated test confirmed.

Mocked unit tests plus one HTTP test with the real control channel
(the orchestrator process is simulated). No model calls.
"""

import json
import os
import threading
import time
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import workflow_control
import workflow_orchestrator
from workflow_control import _atomic_json
from workflow_ui_api import RequestError, WorkflowManager, make_handler


# ------------------------------------------------------------
# Baseline and diff
# ------------------------------------------------------------

def test_diff_is_against_the_original_before_any_attempt(tmp_path):
    repository = tmp_path / "repo"
    (repository / "src").mkdir(parents=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    workflow_orchestrator.configure_run_artifact_paths(os.fspath(run_dir))
    context = {"repository_path": os.fspath(repository)}
    source = repository / "src" / "app.js"

    # Attempt 1 changes the file; attempt 2 changes it again.
    source.write_text("const a = 2;\n", encoding="utf-8")
    workflow_orchestrator.save_json(workflow_orchestrator.CODE_FIX_ORIGINALS_FILE, {"src/app.js": "const a = 1;\n"})
    workflow_orchestrator.record_fix_baseline()
    source.write_text("const a = 3;\n", encoding="utf-8")
    workflow_orchestrator.save_json(workflow_orchestrator.CODE_FIX_ORIGINALS_FILE, {"src/app.js": "const a = 2;\n", "src/new.js": None})
    workflow_orchestrator.record_fix_baseline()
    (repository / "src" / "new.js").write_text("module.exports = 1;\n", encoding="utf-8")

    result = workflow_orchestrator.build_fix_diff(context)

    assert [item["file"] for item in result["files"]] == ["src/app.js", "src/new.js"]
    assert "-const a = 1;" in result["diff"]
    assert "+const a = 3;" in result["diff"]
    assert "const a = 2;" not in result["diff"]
    assert "--- /dev/null" in result["diff"]
    assert result["files"][0] == {"file": "src/app.js", "added": 1, "removed": 1, "regions": 1}


def test_diff_skips_unchanged_and_outside_files(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "same.txt").write_text("x\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    workflow_orchestrator.configure_run_artifact_paths(os.fspath(run_dir))
    workflow_orchestrator.save_json(workflow_orchestrator.FIX_BASELINE_FILE, {"same.txt": "x\n", "../outside.txt": "y"})

    result = workflow_orchestrator.build_fix_diff({"repository_path": os.fspath(repository)})

    assert result["files"] == []
    assert result["diff"] == ""


# ------------------------------------------------------------
# Control channel
# ------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ({"decision": "approve"}, True),
        ({"decision": "reject", "feedback": "Wrong file changed"}, True),
        ({"decision": "reject", "feedback": " "}, False),
        ({"decision": "merge"}, False),
        ("approve", False),
    ],
)
def test_fix_review_decision_validation(value, ok):
    decision, error = workflow_control.validate_fix_review_decision(value)
    assert (decision is not None) is ok and (error is None) is ok


def test_fix_review_wait_publishes_diff_and_honours_stop(tmp_path, monkeypatch):
    directory = tmp_path / "workflow_runs" / "run" / ".control"
    directory.mkdir(parents=True)
    monkeypatch.setattr(workflow_control, "control_directory", lambda: directory)
    monkeypatch.setenv(workflow_control.WORKFLOW_ID_ENV, "a" * 32)
    _atomic_json(directory / "stop.request", {"action_id": "x"})
    published = []
    original_publish = workflow_control.publish_workflow_state

    def recording_publish(*args, **kwargs):
        original_publish(*args, **kwargs)
        published.append(json.loads((directory / "state.json").read_text(encoding="utf-8")))

    monkeypatch.setattr(workflow_control, "publish_workflow_state", recording_publish)

    with pytest.raises(workflow_control.WorkflowStopRequested):
        workflow_control.wait_for_fix_review({"diff": "+x", "files": [{"file": "a.js"}]}, timeout=5)
    assert published[0]["status"] == "WAITING_FOR_FIX_REVIEW"
    assert published[0]["review"]["kind"] == "fix"
    assert published[0]["review"]["diff"] == "+x"
    assert published[-1]["status"] == "STOPPED"
    assert published[-1]["review"] is None


# ------------------------------------------------------------
# API
# ------------------------------------------------------------

FIX_REVIEW_STATE = {
    "status": "WAITING_FOR_FIX_REVIEW",
    "pause_kind": "fix_review",
    "allowed_actions": ["approve", "reject", "stop_safely"],
    "review": {"kind": "fix", "reason": "unconfirmed", "files": [{"file": "a.js"}], "diff": "+fix", "diff_truncated": False, "checks": {"tests": "PASS"}},
}


def make_manager(tmp_path, monkeypatch):
    repository = tmp_path / "target"
    repository.mkdir()
    monkeypatch.setenv("WORKFLOW_ALLOWED_REPOSITORY_ROOTS", os.fspath(tmp_path))
    manager = WorkflowManager(project_dir=tmp_path, worker_launcher=lambda target: None)
    workflow, _ = manager.start({
        "request_id": str(uuid.uuid4()),
        "repository_path": os.fspath(repository),
        "bug_title": "Bug",
        "bug_description": "Details",
        "allow_local_commands": True,
        "allow_ai": True,
    })
    return manager, workflow["workflow_id"]


def set_state(manager, workflow_id, **changes):
    path = manager.runs_dir / workflow_id / ".control" / "state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state.update(changes)
    _atomic_json(path, state)


def test_fix_review_endpoint(tmp_path, monkeypatch):
    manager, workflow_id = make_manager(tmp_path, monkeypatch)
    with pytest.raises(RequestError) as early:
        manager.submit_fix_review(workflow_id, {"review_id": str(uuid.uuid4()), "decision": "approve"})
    assert early.value.status_code == 409

    set_state(manager, workflow_id, **FIX_REVIEW_STATE)
    status = manager.get(workflow_id)
    assert status["review"]["diff"] == "+fix"
    with pytest.raises(RequestError) as no_reason:
        manager.submit_fix_review(workflow_id, {"review_id": str(uuid.uuid4()), "decision": "reject", "feedback": ""})
    assert no_reason.value.status_code == 400

    payload = {"review_id": str(uuid.uuid4()), "decision": "approve"}
    assert manager.submit_fix_review(workflow_id, payload)["status"] == "REVIEW_SUBMITTED"
    assert manager.submit_fix_review(workflow_id, payload)["status"] == "REVIEW_SUBMITTED"
    signal = json.loads((manager.runs_dir / workflow_id / ".control" / "fix-review.json").read_text(encoding="utf-8"))
    assert signal["decision"] == "approve"


def test_unconfirmed_is_a_terminal_status(tmp_path, monkeypatch):
    manager, workflow_id = make_manager(tmp_path, monkeypatch)
    set_state(manager, workflow_id, status="UNCONFIRMED", terminal=True)
    status = manager.get(workflow_id)
    assert status["terminal"] is True
    with pytest.raises(RequestError):
        manager.action(workflow_id, {"action_id": str(uuid.uuid4()), "action": "stop_safely"})


def test_http_fix_review_flow(tmp_path, monkeypatch):
    repository = tmp_path / "target"
    repository.mkdir()
    monkeypatch.setenv("WORKFLOW_ALLOWED_REPOSITORY_ROOTS", os.fspath(tmp_path))
    monkeypatch.setattr(workflow_control, "CONTROL_POLL_SECONDS", 0.02)
    seen = {}

    class ReviewingProcess:
        def __init__(self, _command, **kwargs):
            self.env = kwargs["env"]

        def wait(self):
            control_dir = Path(self.env["WORKFLOW_CONTROL_DIRECTORY"])
            run_dir = Path(self.env["WORKFLOW_RUN_DIRECTORY"])
            monkeypatch.setattr(workflow_control, "control_directory", lambda: control_dir)
            monkeypatch.setenv("WORKFLOW_ID", self.env["WORKFLOW_ID"])
            seen["decision"] = workflow_control.wait_for_fix_review(
                {"diff": "-old\n+new\n", "files": [{"file": "src/app.js", "added": 1, "removed": 1}], "checks": {"tests": "PASS"}},
                timeout=10,
            )
            _atomic_json(run_dir / "workflow_report.json", {
                "final_status": "FAIL", "stages": [], "retry_history": [],
                "reproduction_confirmed": False,
                "fix_review": {"decision": "reject", "feedback": seen["decision"]["feedback"], "reviewed_by": "user"},
            })
            return 1

    manager = WorkflowManager(project_dir=tmp_path, process_factory=ReviewingProcess)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(manager))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def call(method, path, body=None):
        request = Request(
            base + path,
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
        workflow_id = call("POST", "/api/workflows", {
            "request_id": str(uuid.uuid4()), "repository_path": os.fspath(repository),
            "bug_title": "Bug", "bug_description": "Details",
            "allow_local_commands": True, "allow_ai": True,
        })["workflow_id"]
        waiting = wait_for(workflow_id, lambda s: s["status"] == "WAITING_FOR_FIX_REVIEW")
        assert waiting["review"]["kind"] == "fix"
        assert "+new" in waiting["review"]["diff"]
        with pytest.raises(HTTPError) as no_reason:
            call("POST", f"/api/workflows/{workflow_id}/fix-review", {"review_id": str(uuid.uuid4()), "decision": "reject"})
        assert no_reason.value.code == 400
        call("POST", f"/api/workflows/{workflow_id}/fix-review",
             {"review_id": str(uuid.uuid4()), "decision": "reject", "feedback": "Changes the wrong handler"})
        final = wait_for(workflow_id, lambda s: s["terminal"])
        assert final["status"] == "FAIL"
        assert final["report"]["fix_review"]["decision"] == "reject"
        assert final["report"]["reproduction_confirmed"] is False
        assert seen["decision"] == {"decision": "reject", "feedback": "Changes the wrong handler"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
