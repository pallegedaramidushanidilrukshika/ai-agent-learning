"""Browser replay screenshots and traces: saved by the replay, shown by the console."""

import json
import subprocess
import threading
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

import browser_replay_agent as agent
import workflow_orchestrator
from test_workflow_ui_api import make_manager, make_payload
from workflow_ui_api import RequestError, make_handler

PNG = b"\x89PNG\r\n\x1a\nfake"
ZIP = b"PK\x03\x04fake"


def runner_returning(result='{"status": "COMPLETED"}'):
    return mock.Mock(return_value=subprocess.CompletedProcess([], 0, result + "\n", ""))


@pytest.mark.parametrize(
    ("screenshot", "trace", "extra"),
    [
        (None, None, []),
        ("shot.png", None, ["shot.png"]),
        ("shot.png", "trace.zip", ["shot.png", "trace.zip"]),
        (None, "trace.zip", ["", "trace.zip"]),
    ],
)
def test_run_replay_passes_screenshot_and_trace_paths(screenshot, trace, extra):
    runner = runner_returning()
    agent.run_replay("s.json", "http://localhost:3000", screenshot, runner=runner, trace_path=trace)
    assert runner.call_args.args[0] == ["node", agent.REPLAY_SCRIPT, "s.json", "http://localhost:3000", *extra]


def test_cli_trace_option_reaches_the_replay_runner(tmp_path, monkeypatch):
    recording = tmp_path / "recording.json"
    recording.write_text(json.dumps({"steps": [{"action": "goto", "path": "/"}]}), encoding="utf-8")
    context = tmp_path / "repository_context.json"
    context.write_text(json.dumps({"repository_path": str(tmp_path)}), encoding="utf-8")
    monkeypatch.setattr(agent, "resolve_artifact_path", lambda *_args, **_kwargs: str(context))
    seen = {}

    def fake_replay_recording(_recording, _context, _steps, screenshot, replay=None):
        seen["screenshot"], seen["replay"] = screenshot, replay
        return {"classification": agent.ERROR, "summary": "stub"}

    monkeypatch.setattr(agent, "replay_recording", fake_replay_recording)
    agent.main([
        "--recording", str(recording), "--output", str(tmp_path / "out.json"),
        "--screenshot", "shot.png", "--trace", "trace.zip",
    ])
    assert seen["screenshot"] == "shot.png"
    assert seen["replay"].func is agent.run_replay
    assert seen["replay"].keywords == {"trace_path": "trace.zip"}


def test_orchestrator_asks_for_a_trace_and_removes_old_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow_orchestrator, "ACTIVE_RUN_ARTIFACT_DIR", str(tmp_path))
    old_shot = tmp_path / "browser_replay_before_fix_failure.png"
    old_trace = tmp_path / "browser_replay_before_fix_trace.zip"
    old_shot.write_bytes(PNG)
    old_trace.write_bytes(ZIP)
    calls = []

    def fake_run_agent(_agent, arguments):
        calls.append(arguments)
        assert not old_shot.exists() and not old_trace.exists()
        (tmp_path / "browser_replay_before_fix.json").write_text(
            json.dumps({"classification": "FAILS_ON_CURRENT_CODE"}), encoding="utf-8",
        )

    monkeypatch.setattr(workflow_orchestrator, "run_agent", fake_run_agent)
    result = workflow_orchestrator.run_browser_replay("recording.json", "browser_replay_before_fix")

    assert result["classification"] == "FAILS_ON_CURRENT_CODE"
    arguments = calls[0]
    assert arguments[arguments.index("--trace") + 1] == str(old_trace)


def started_run(tmp_path, monkeypatch):
    repository, manager, _scheduled = make_manager(tmp_path, monkeypatch)
    workflow, _created = manager.start(make_payload(repository))
    run_dir = manager.runs_dir / workflow["workflow_id"]
    return manager, workflow["workflow_id"], run_dir


def test_status_lists_only_replay_evidence(tmp_path, monkeypatch):
    manager, workflow_id, run_dir = started_run(tmp_path, monkeypatch)
    (run_dir / "browser_replay_before_fix_failure.png").write_bytes(PNG)
    (run_dir / "browser_replay_after_fix_attempt_1_trace.zip").write_bytes(ZIP)
    (run_dir / "workflow_report.json.png").write_bytes(PNG)
    (run_dir / "other_trace.zip").write_bytes(ZIP)

    names = {item["name"]: item["kind"] for item in manager.get(workflow_id)["replay_evidence"]}

    assert names == {
        "browser_replay_before_fix_failure.png": "screenshot",
        "browser_replay_after_fix_attempt_1_trace.zip": "trace",
    }


def test_evidence_file_is_served_only_for_allowed_names(tmp_path, monkeypatch):
    manager, workflow_id, run_dir = started_run(tmp_path, monkeypatch)
    (run_dir / "browser_replay_before_fix_failure.png").write_bytes(PNG)
    (run_dir / "repository_context.json").write_text("{}", encoding="utf-8")

    assert manager.replay_evidence_file(workflow_id, "browser_replay_before_fix_failure.png") == (PNG, "image/png")
    for name in (
        "repository_context.json",
        "../browser_replay_before_fix_failure.png",
        "browser_replay_before_fix_trace.zip",  # allowed name, but not saved
        "browser_replay_after_fix_attempt_0_trace.zip",
    ):
        with pytest.raises(RequestError) as error:
            manager.replay_evidence_file(workflow_id, name)
        assert error.value.status_code == 404
    with pytest.raises(RequestError) as error:
        manager.replay_evidence_file("not-a-workflow", "browser_replay_before_fix_failure.png")
    assert error.value.status_code == 400


def test_evidence_file_over_the_size_limit_is_refused(tmp_path, monkeypatch):
    manager, workflow_id, run_dir = started_run(tmp_path, monkeypatch)
    (run_dir / "browser_replay_before_fix_trace.zip").write_bytes(ZIP)
    monkeypatch.setattr("workflow_ui_api.MAX_REPLAY_EVIDENCE_BYTES", 3)
    with pytest.raises(RequestError) as error:
        manager.replay_evidence_file(workflow_id, "browser_replay_before_fix_trace.zip")
    assert error.value.status_code == 413


def test_http_route_serves_screenshot_and_trace(tmp_path, monkeypatch):
    manager, workflow_id, run_dir = started_run(tmp_path, monkeypatch)
    (run_dir / "browser_replay_before_fix_failure.png").write_bytes(PNG)
    (run_dir / "browser_replay_before_fix_trace.zip").write_bytes(ZIP)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(manager))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/api/workflows/{workflow_id}/files/"
    try:
        with urlopen(base + "browser_replay_before_fix_failure.png") as response:
            assert response.read() == PNG
            assert response.headers["Content-Type"] == "image/png"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["Content-Disposition"].startswith("inline")
        with urlopen(base + "browser_replay_before_fix_trace.zip") as response:
            assert response.read() == ZIP
            assert response.headers["Content-Type"] == "application/zip"
            assert response.headers["Content-Disposition"].startswith("attachment")
        for name in ("workflow_report.json", "..%2F..%2Fsecret.png"):
            with pytest.raises(HTTPError) as error:
                urlopen(base + name)
            assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
