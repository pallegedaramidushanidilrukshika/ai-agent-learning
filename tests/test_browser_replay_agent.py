import os
import subprocess
import tempfile
import unittest
from unittest import mock

import browser_replay_agent as agent


def completed(results, failed_index=None, failure_type=None, total=None):
    return {
        "status": "COMPLETED",
        "steps_total": total if total is not None else len(results),
        "results": results,
        "failed_index": failed_index,
        "failure_type": failure_type,
    }


PASSED = {"status": "passed", "line": 3, "source": "await page.goto('/')"}
ASSERT_FAILED = {
    "status": "failed", "line": 7, "source": "await expect(x).toHaveCount(2);",
    "message": "Assertion failed; actual: 3 element(s)",
}
ACTION_ERROR = {"status": "error", "line": 5, "source": "await page.getByText('Add').click();", "message": "Timeout"}


class ClassifyReplayTests(unittest.TestCase):
    def test_assertion_failure_reproduces_the_bug(self):
        replay = completed([PASSED, ASSERT_FAILED], 1, "assertion", total=3)
        self.assertEqual(agent.classify_replay(replay), agent.FAILS_ON_CURRENT_CODE)
        self.assertIn("Assertion failed at line 7", agent.summarize(replay, agent.FAILS_ON_CURRENT_CODE))

    def test_action_failure_is_an_error_not_a_reproduction(self):
        replay = completed([PASSED, ACTION_ERROR], 1, "action", total=3)
        self.assertEqual(agent.classify_replay(replay), agent.ERROR)
        self.assertIn("Step could not run at line 5", agent.summarize(replay, agent.ERROR))

    def test_full_pass(self):
        self.assertEqual(agent.classify_replay(completed([PASSED, PASSED])), agent.PASSES_ON_CURRENT_CODE)

    def test_incomplete_or_malformed_results_never_pass(self):
        self.assertEqual(agent.classify_replay(completed([PASSED], total=2)), agent.ERROR)
        self.assertEqual(agent.classify_replay(completed([{"status": "skipped"}])), agent.ERROR)
        self.assertEqual(agent.classify_replay(completed("nope")), agent.ERROR)
        self.assertEqual(agent.classify_replay({"status": "ERROR"}), agent.ERROR)
        self.assertEqual(agent.classify_replay(None), agent.ERROR)

    def test_missing_playwright(self):
        replay = {"status": "UNAVAILABLE", "reason": "Playwright is not installed"}
        self.assertEqual(agent.classify_replay(replay), agent.UNAVAILABLE)


class RunReplayTests(unittest.TestCase):
    def test_reads_last_json_line(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, 'noise\n{"status": "COMPLETED"}\n', ""))
        self.assertEqual(agent.run_replay("s.json", "http://localhost:3000", runner=runner), {"status": "COMPLETED"})
        command = runner.call_args.args[0]
        self.assertEqual(command[:2], ["node", agent.REPLAY_SCRIPT])
        self.assertEqual(runner.call_args.kwargs["timeout"], agent.REPLAY_TIMEOUT_SECONDS)

    def test_no_result_and_timeout(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "crash", "trace"))
        self.assertEqual(agent.run_replay("s", "u", runner=runner)["status"], "ERROR")
        runner = mock.Mock(side_effect=subprocess.TimeoutExpired("node", 1))
        self.assertIn("did not finish", agent.run_replay("s", "u", runner=runner)["reason"])


class ReplayRecordingTests(unittest.TestCase):
    def setUp(self):
        self.repository = tempfile.mkdtemp()
        self.context = {
            "repository_path": self.repository,
            "execution_strategy": {"start_command": "node index.js"},
        }
        self.recording = {"recorded_origin": "http://localhost:3999", "steps": [{"action": "goto", "path": "/"}]}

    def test_app_is_started_replayed_and_always_stopped(self):
        process = object()
        start = mock.Mock(return_value=(process, None))
        stop = mock.Mock()
        replay = mock.Mock(side_effect=RuntimeError("boom"))
        with mock.patch.object(agent.runtime, "isolate_local_endpoint", return_value=("http://localhost:3999", {}, None)):
            with self.assertRaises(RuntimeError):
                agent.replay_recording(self.recording, self.context, "steps.json", start=start, stop=stop, replay=replay)
        stop.assert_called_once_with(process)
        self.assertEqual(start.call_args.kwargs["base_url"], "http://localhost:3999")

    def test_successful_reproduction(self):
        start = mock.Mock(return_value=(object(), None))
        replay = mock.Mock(return_value=completed([PASSED, ASSERT_FAILED], 1, "assertion", total=2))
        with mock.patch.object(agent.runtime, "isolate_local_endpoint", return_value=("http://localhost:3999", {}, None)):
            result = agent.replay_recording(self.recording, self.context, "s", start=start, stop=mock.Mock(), replay=replay)
        self.assertEqual(result["classification"], agent.FAILS_ON_CURRENT_CODE)
        self.assertEqual(result["base_url"], "http://localhost:3999")

    def test_busy_port_and_start_failure(self):
        with mock.patch.object(agent.runtime, "isolate_local_endpoint", return_value=(None, {}, "Port busy.")):
            result = agent.replay_recording(self.recording, self.context, "s", start=mock.Mock())
        self.assertEqual(result["classification"], agent.ERROR)
        self.assertIn("Stop the copy of the app", result["summary"])
        start = mock.Mock(return_value=(None, "exit 1"))
        with mock.patch.object(agent.runtime, "isolate_local_endpoint", return_value=("http://localhost:3999", {}, None)):
            result = agent.replay_recording(self.recording, self.context, "s", start=start)
        self.assertEqual(result["summary"], "The app could not be started.")

    def test_missing_repository(self):
        context = dict(self.context, repository_path=os.path.join(self.repository, "missing"))
        result = agent.replay_recording(self.recording, context, "s")
        self.assertEqual(result["classification"], agent.ERROR)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- full-stack apps

import contextlib
import json as _json
import os as _os

import pytest


def mern_project(root, with_vite=True, env_example="ATLAS_URI=mongodb://localhost:27017/\nPORT=5050\n"):
    client = root / "mern" / "client"
    server = root / "mern" / "server"
    (client / "node_modules" / "vite" / "bin").mkdir(parents=True)
    server.mkdir(parents=True)
    (client / "package.json").write_text(_json.dumps({"scripts": {"dev": "vite" if with_vite else "react-scripts start"}}), encoding="utf-8")
    (client / "node_modules" / "vite" / "bin" / "vite.js").write_text("", encoding="utf-8")
    if env_example is not None:
        (server / "config.env.example").write_text(env_example, encoding="utf-8")
    (server / "config.env").write_text("ATLAS_URI=mongodb+srv://real-user:real-secret@cluster/\n", encoding="utf-8")
    return {
        "repository_path": str(root),
        "execution_strategy": {"database": "MongoDB", "backend_base_url": "http://localhost:5999"},
        "architecture": {"components": [
            {"type": "frontend", "path": "mern/client", "start_command": "npm run dev"},
            {"type": "backend", "path": "mern/server", "start_command": "npm start"},
            {"type": "database", "technology": "MongoDB", "path": None, "start_command": None},
        ]},
    }


def full_stack_fakes(order, replay_result):
    def start(repository, command, base_url=None, readiness_path="/", working_directory=None,
              environment=None, explicit_environment=None):
        is_vite = isinstance(command, list) and "vite" in " ".join(command)
        name = "frontend" if is_vite else "backend"
        order.append(("start", name, base_url, dict(explicit_environment or {}), dict(environment or {})))
        return name, None

    def stop(process):
        order.append(("stop", process))

    def replay(steps_path, base_url, screenshot_path=None):
        order.append(("replay", base_url))
        return replay_result

    @contextlib.contextmanager
    def database():
        order.append(("database", "up"))
        try:
            yield "mongodb://127.0.0.1:1234/"
        finally:
            order.append(("database", "down"))

    return start, stop, replay, database


def test_full_stack_replay_starts_database_backend_frontend_and_stops_them(tmp_path, monkeypatch):
    context = mern_project(tmp_path)
    monkeypatch.setattr(agent, "port_in_use", lambda url: False)
    order = []
    start, stop, replay, database = full_stack_fakes(order, completed([PASSED, ASSERT_FAILED], 1, "assertion"))

    result = agent.replay_recording({"recorded_origin": "http://localhost:5173"}, context, "s",
                                    start=start, stop=stop, replay=replay, database=database)

    assert result["classification"] == agent.FAILS_ON_CURRENT_CODE
    assert result["isolated_database"] is True
    steps = [entry[:2] for entry in order]
    assert steps == [("database", "up"), ("start", "backend"), ("start", "frontend"),
                     ("replay", result["base_url"]), ("stop", "frontend"), ("stop", "backend"), ("database", "down")]
    backend_start = order[1]
    assert backend_start[2] == "http://localhost:5999"
    assert backend_start[3] == {"ATLAS_URI": "mongodb://127.0.0.1:1234/"}
    assert result["base_url"].startswith("http://127.0.0.1:")


def test_full_stack_replay_refuses_to_use_an_unisolated_database(tmp_path, monkeypatch):
    context = mern_project(tmp_path, env_example=None)
    monkeypatch.setattr(agent, "port_in_use", lambda url: False)
    order = []
    start, stop, replay, database = full_stack_fakes(order, completed([PASSED]))

    result = agent.replay_recording({}, context, "s", start=start, stop=stop, replay=replay, database=database)

    assert result["classification"] == agent.ERROR
    assert "Refused" in result["summary"]
    assert order == []


def test_full_stack_replay_stops_backend_when_frontend_fails(tmp_path, monkeypatch):
    context = mern_project(tmp_path)
    monkeypatch.setattr(agent, "port_in_use", lambda url: False)
    order = []
    start, stop, replay, database = full_stack_fakes(order, completed([PASSED]))

    def failing_start(repository, command, **kwargs):
        if isinstance(command, list):
            order.append(("start", "frontend-failed"))
            return None, "vite crashed"
        return start(repository, command, **kwargs)

    result = agent.replay_recording({}, context, "s", start=failing_start, stop=stop, replay=replay, database=database)

    assert result["summary"] == "The frontend could not be started."
    assert ("stop", "backend") in [entry[:2] for entry in order]
    assert order[-1] == ("database", "down")
    assert not any(entry[0] == "replay" for entry in order)


def test_busy_backend_port_and_non_vite_frontend_are_reported(tmp_path, monkeypatch):
    context = mern_project(tmp_path)
    monkeypatch.setattr(agent, "port_in_use", lambda url: True)
    result = agent.replay_recording({}, context, "s", start=mock.Mock())
    assert "already in use" in result["summary"]

    other = mern_project(tmp_path / "cra", with_vite=False)
    monkeypatch.setattr(agent, "port_in_use", lambda url: False)
    result = agent.replay_recording({}, other, "s", start=mock.Mock())
    assert "Only Vite frontends" in result["summary"]


def test_uri_variables_come_only_from_example_files(tmp_path):
    context = mern_project(tmp_path)
    assert agent.mongodb_uri_variables(_os.path.join(context["repository_path"], "mern", "server")) == ["ATLAS_URI"]


@pytest.mark.skipif(agent.find_mongod() is None, reason="mongod is not installed")
def test_isolated_mongodb_runs_on_a_temporary_folder_and_is_removed():
    with agent.isolated_mongodb() as uri:
        port = int(uri.rsplit(":", 1)[1].strip("/"))
        assert agent.port_in_use(f"http://127.0.0.1:{port}")
    deadline = __import__("time").monotonic() + 10
    while agent.port_in_use(f"http://127.0.0.1:{port}") and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.2)
    assert not agent.port_in_use(f"http://127.0.0.1:{port}")


def test_start_application_passes_explicit_variables_the_allowlist_would_drop(tmp_path, monkeypatch):
    from workflow_validation import project_process_environment

    # The trap that sent the first live replay to the real database:
    assert "ATLAS_URI" not in project_process_environment(overrides={"ATLAS_URI": "x"})

    captured = {}

    class FakeProcess:
        def poll(self):
            return None

    def fake_popen(parts, cwd=None, env=None, **kwargs):
        captured["env"] = env
        return FakeProcess()

    monkeypatch.setattr(agent.runtime.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(agent.runtime, "http_request", lambda *a, **k: {"success": True, "status_code": 200})
    monkeypatch.setattr(agent.runtime.time, "sleep", lambda *_: None)

    process, error = agent.runtime.start_application(
        str(tmp_path), ["node", "server.js"], base_url="http://localhost:5999",
        environment={"PORT": "5999"}, explicit_environment={"ATLAS_URI": "mongodb://127.0.0.1:1234/"},
    )

    assert error is None and process is not None
    assert captured["env"]["ATLAS_URI"] == "mongodb://127.0.0.1:1234/"
    assert captured["env"]["PORT"] == "5999"


def test_record_mode_serves_the_isolated_app_until_the_user_is_done(tmp_path, monkeypatch, capsys):
    context = mern_project(tmp_path)
    monkeypatch.setattr(agent, "port_in_use", lambda url: False)
    order = []
    start, stop, _replay, database = full_stack_fakes(order, None)

    def wait(_prompt):
        order.append(("user", "recorded"))

    code = agent.serve_for_recording(str(tmp_path), wait=wait, start=start, stop=stop, database=database, context=context)

    output = capsys.readouterr().out
    assert code == 0
    assert "npx playwright codegen http://127.0.0.1:" in output
    assert "temporary (deleted when you stop)" in output
    steps = [entry[:2] for entry in order]
    assert steps.index(("user", "recorded")) < steps.index(("stop", "frontend"))
    assert steps[-1] == ("database", "down")


def test_record_mode_refuses_single_apps(tmp_path, capsys):
    context = {"repository_path": str(tmp_path), "execution_strategy": {"start_command": "node index.js"}}
    assert agent.serve_for_recording(str(tmp_path), context=context) == 1
    assert "Start it the usual way" in capsys.readouterr().out
