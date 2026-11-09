"""AI reproduction test inside the workflow: review loop, re-run after fix,
and the confirmed test reaching diagnosis.

Mocked unit tests: agents, the model and the test runner are simulated.
"""

import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

import reproduction_tests
import workflow_orchestrator
from test_workflow_artifacts import run_mocked_retry_workflow
from workflow_artifacts import ARTIFACT_DIR_ENV


TEST_PATH = "backend/test/ai-reproduction.test.js"


@pytest.fixture
def loop(tmp_path, monkeypatch):
    """Orchestrator configured for one run, with control and agents mocked."""
    repository = tmp_path / "repo"
    repository.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    workflow_orchestrator.configure_run_artifact_paths(os.fspath(run_dir))
    context = {
        "repository_path": os.fspath(repository),
        "execution_strategy": {
            "technology": "React/Vite + AWS Serverless",
            "test_framework": "Jest",
            "backend_path": "backend",
            "test_command": ["node", "backend\\node_modules\\jest\\bin\\jest.js", "--runInBand"],
        },
    }
    state = {"agent_calls": [], "classifications": [], "decisions": [], "reviews": []}

    def fake_run_agent(script_name, arguments=None):
        state["agent_calls"].append(list(arguments or []))
        round_number = len(state["agent_calls"])
        classification = (
            state["classifications"][round_number - 1]
            if round_number <= len(state["classifications"])
            else reproduction_tests.FAILS_ON_CURRENT_CODE
        )
        if classification == "UNSUPPORTED":
            workflow_orchestrator.save_json(workflow_orchestrator.REPRODUCTION_TEST_FILE, {"status": "UNSUPPORTED", "reason": "not supported"})
            return {"success": True, "return_code": 0}
        content = f"test('round {round_number}', () => {{ expect(1).toBe(2); }});\n"
        test_file = repository / TEST_PATH
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text(content, encoding="utf-8")
        workflow_orchestrator.save_json(workflow_orchestrator.REPRODUCTION_TEST_FILE, {
            "status": "GENERATED",
            "round": round_number,
            "framework": "jest",
            "test_file": TEST_PATH,
            "content": content,
            "content_sha256": reproduction_tests.content_sha256(content),
            "classification": classification,
            "run": {"return_code": 1, "tests": 1, "failed": 1, "output_tail": "expected 2 received 1"},
        })
        return {"success": True, "return_code": 0}

    def fake_review(artifact, round_number, max_rounds, **_kwargs):
        state["reviews"].append((round_number, artifact["classification"]))
        return state["decisions"].pop(0) if state["decisions"] else None

    monkeypatch.setattr(workflow_orchestrator, "run_agent", fake_run_agent)
    monkeypatch.setattr(workflow_orchestrator, "control_enabled", lambda: True)
    monkeypatch.setattr(workflow_orchestrator, "check_stop_requested", lambda _stage: False)
    monkeypatch.setattr(workflow_orchestrator, "publish_workflow_state", lambda *a, **k: None)
    monkeypatch.setattr(workflow_orchestrator, "record_stage", lambda *a, **k: None)
    monkeypatch.setattr(workflow_orchestrator, "wait_for_reproduction_review", fake_review)
    return SimpleNamespace(repository=repository, run_dir=run_dir, context=context, state=state)


def test_revise_then_confirm_saves_the_confirmed_test(loop):
    loop.state["decisions"] = [
        {"decision": "revise", "feedback": "Send both messages on the same connection"},
        {"decision": "confirm", "feedback": ""},
    ]

    outcome = workflow_orchestrator.collect_ai_reproduction_test(loop.context)

    assert outcome["status"] == "CONFIRMED"
    assert len(loop.state["agent_calls"]) == 2
    second_call = loop.state["agent_calls"][1]
    assert second_call[:2] == ["--round", "2"]
    feedback = json.loads(Path(second_call[second_call.index("--feedback") + 1]).read_text(encoding="utf-8"))
    assert feedback["feedback"] == "Send both messages on the same connection"
    assert "round 1" in feedback["previous_content"]
    confirmed = json.loads((loop.run_dir / "confirmed_reproduction_test.json").read_text(encoding="utf-8"))
    assert confirmed["confirmed_by"] == "user"
    assert confirmed["round"] == 2
    assert confirmed["before_fix"]["classification"] == reproduction_tests.FAILS_ON_CURRENT_CODE
    assert (loop.repository / TEST_PATH).read_text(encoding="utf-8") == confirmed["content"]


def test_a_test_that_does_not_fail_cannot_be_confirmed(loop):
    loop.state["classifications"] = [reproduction_tests.PASSES_ON_CURRENT_CODE]
    loop.state["decisions"] = [{"decision": "confirm", "feedback": ""}]

    outcome = workflow_orchestrator.collect_ai_reproduction_test(loop.context)

    assert outcome["status"] == "UNAVAILABLE"
    assert not (loop.run_dir / "confirmed_reproduction_test.json").exists()
    assert not (loop.repository / TEST_PATH).exists()


def test_manual_choice_removes_the_unconfirmed_test(loop):
    loop.state["decisions"] = [{"decision": "manual", "feedback": ""}]

    outcome = workflow_orchestrator.collect_ai_reproduction_test(loop.context)

    assert outcome["status"] == "MANUAL_REQUESTED"
    assert not (loop.repository / TEST_PATH).exists()


def test_rounds_are_bounded(loop):
    loop.state["decisions"] = [{"decision": "revise", "feedback": f"try {n}"} for n in range(5)]

    outcome = workflow_orchestrator.collect_ai_reproduction_test(loop.context)

    assert outcome["status"] == "EXHAUSTED"
    assert len(loop.state["agent_calls"]) == workflow_orchestrator.MAX_REPRODUCTION_TEST_ROUNDS
    assert not (loop.repository / TEST_PATH).exists()


def test_unsupported_project_skips_review(loop):
    loop.state["classifications"] = ["UNSUPPORTED"]

    outcome = workflow_orchestrator.collect_ai_reproduction_test(loop.context)

    assert outcome["status"] == "UNSUPPORTED"
    assert loop.state["reviews"] == []


def test_review_timeout_stops_reproduction(loop):
    outcome = workflow_orchestrator.collect_ai_reproduction_test(loop.context)

    assert outcome["status"] == "TIMED_OUT"
    assert not (loop.repository / TEST_PATH).exists()


def test_not_offered_without_console_or_terminal(loop, monkeypatch):
    monkeypatch.setattr(workflow_orchestrator, "control_enabled", lambda: False)
    monkeypatch.setattr(workflow_orchestrator.sys, "stdin", SimpleNamespace(isatty=lambda: False))

    assert workflow_orchestrator.collect_ai_reproduction_test(loop.context)["status"] == "NOT_OFFERED"
    assert loop.state["agent_calls"] == []


def test_rerun_restores_a_modified_test_before_running(loop, monkeypatch):
    loop.state["decisions"] = [{"decision": "confirm", "feedback": ""}]
    workflow_orchestrator.collect_ai_reproduction_test(loop.context)
    confirmed = json.loads((loop.run_dir / "confirmed_reproduction_test.json").read_text(encoding="utf-8"))
    (loop.repository / TEST_PATH).write_text("test('weakened', () => {});\n", encoding="utf-8")
    seen = {}

    def fake_run(repository, runner):
        seen["content"] = (Path(repository) / runner["test_path"]).read_text(encoding="utf-8")
        return {"return_code": 0, "tests": 1, "passed": 1, "failed": 0, "suite_error": False, "timed_out": False}

    monkeypatch.setattr(reproduction_tests, "run_single_test", fake_run)
    rerun = workflow_orchestrator.run_confirmed_reproduction_test(loop.context)

    assert rerun["status"] == "PASSED"
    assert rerun["test_restored_before_run"] is True
    assert seen["content"] == confirmed["content"]


# ------------------------------------------------------------
# Full workflow with a confirmed test (collection step stubbed)
# ------------------------------------------------------------

def run_workflow_with_confirmed_test(tmp_path, monkeypatch, rerun_results, **workflow_kwargs):
    run_dir = tmp_path / "workflow-root" / "workflow_runs" / "run-1"
    rerun_calls = []

    def fake_collect(context):
        confirmed = {
            "status": "CONFIRMED",
            "confirmed_by": "user",
            "round": 1,
            "framework": "pytest",
            "test_file": "tests/test_ai_reproduction.py",
            "content": "def test_bug():\n    assert False\n",
            "content_sha256": reproduction_tests.content_sha256("def test_bug():\n    assert False\n"),
            "before_fix": {"classification": reproduction_tests.FAILS_ON_CURRENT_CODE, "run": {"output_tail": "assert False"}},
        }
        workflow_orchestrator.save_json(workflow_orchestrator.CONFIRMED_REPRODUCTION_TEST_FILE, confirmed)
        return {"status": "CONFIRMED", "confirmed": confirmed, "rounds": [{"round": 1, "decision": "confirm"}]}

    def fake_rerun(context):
        rerun_calls.append(1)
        passed = rerun_results[min(len(rerun_calls), len(rerun_results)) - 1]
        return {"status": "PASSED" if passed else "FAILED", "test_file": "tests/test_ai_reproduction.py"}

    monkeypatch.setattr(workflow_orchestrator, "collect_ai_reproduction_test", fake_collect)
    monkeypatch.setattr(workflow_orchestrator, "run_confirmed_reproduction_test", fake_rerun)
    result, run_dir, calls, retry_contexts = run_mocked_retry_workflow(
        tmp_path, monkeypatch, reproduction_status="NOT_REPRODUCED", **workflow_kwargs
    )
    report = json.loads((run_dir / "workflow_report.json").read_text(encoding="utf-8"))
    return result, report, calls, rerun_calls, retry_contexts, run_dir


def test_confirmed_test_reaches_diagnosis_and_must_pass_after_fix(tmp_path, monkeypatch):
    result, report, calls, rerun_calls, _contexts, run_dir = run_workflow_with_confirmed_test(
        tmp_path, monkeypatch, rerun_results=[True]
    )

    assert result == 0
    assert report["final_status"] == "PASS"
    assert report["reproduction_source"] == "ai_reproduction_test"
    assert report["reproduction_evidence"]["before_fix"] == reproduction_tests.FAILS_ON_CURRENT_CODE
    assert report["reproduction_evidence"]["confirmed_by"] == "user"
    stage_2 = next(stage for stage in report["stages"] if stage["stage"] == 2)
    assert stage_2["status"] == "TEST_CONFIRMED"
    assert calls["diagnosis_arguments"][0] == [
        "--reproduction-test",
        os.fspath(run_dir / "confirmed_reproduction_test.json"),
    ]
    assert len(rerun_calls) == 1
    assert report["retry_history"][0]["reproduction_test"] == "PASS"


def test_fix_that_does_not_make_the_test_pass_is_not_accepted(tmp_path, monkeypatch):
    result, report, calls, rerun_calls, retry_contexts, run_dir = run_workflow_with_confirmed_test(
        tmp_path, monkeypatch, rerun_results=[False, True]
    )

    assert result == 0
    assert [item["reproduction_test"] for item in report["retry_history"]] == ["FAIL", "PASS"]
    assert [item["overall"] for item in report["retry_history"]] == ["FAIL", "PASS"]
    assert calls["code_fix"] == 2
    assert calls["diagnosis_arguments"][1][1:] == [
        "--reproduction-test",
        os.fspath(run_dir / "confirmed_reproduction_test.json"),
    ]
    assert retry_contexts[0]["confirmed_reproduction_test"]["confirmed_by"] == "user"


def test_workflow_fails_when_the_test_never_passes(tmp_path, monkeypatch):
    result, report, calls, rerun_calls, _contexts, _run_dir = run_workflow_with_confirmed_test(
        tmp_path, monkeypatch, rerun_results=[False]
    )

    assert result == 1
    assert report["final_status"] == "FAIL"
    assert len(rerun_calls) == 3
    assert all(item["reproduction_test"] == "FAIL" for item in report["retry_history"])


# ------------------------------------------------------------
# Diagnosis agent receives the confirmed test
# ------------------------------------------------------------

def test_diagnosis_prompt_uses_the_confirmed_test_as_evidence(tmp_path, monkeypatch):
    class StopAfterPrompt(Exception):
        pass

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "repository_context.json").write_text(json.dumps({"repository_path": os.fspath(tmp_path), "file_contents": {}}), encoding="utf-8")
    (run_dir / "reproduction_result.json").write_text(json.dumps({"status": "EXECUTION_ERROR"}), encoding="utf-8")
    confirmed_path = run_dir / "confirmed_reproduction_test.json"
    confirmed_path.write_text(json.dumps({
        "status": "CONFIRMED",
        "test_file": TEST_PATH,
        "framework": "jest",
        "content": "test('keeps both messages', () => { expect(rows).toEqual(['first', 'second']); });",
        "before_fix": {"classification": "FAILS_ON_CURRENT_CODE", "run": {"output_tail": "Expected: [first, second] Received: [second]"}},
    }), encoding="utf-8")
    bug_path = tmp_path / "bug.json"
    bug_path.write_text(json.dumps({"bug_title": "Messages overwrite"}), encoding="utf-8")

    captured = {}
    client = Mock()

    def capture(**kwargs):
        captured.update(kwargs)
        raise StopAfterPrompt

    client.chat.completions.create.side_effect = capture
    groq_module = ModuleType("groq")
    groq_module.Groq = Mock(return_value=client)
    groq_module.APIStatusError = type("APIStatusError", (Exception,), {})
    dotenv_module = ModuleType("dotenv")
    dotenv_module.load_dotenv = Mock()
    langchain_groq_module = ModuleType("langchain_groq")
    langchain_groq_module.ChatGroq = Mock()
    monkeypatch.setitem(sys.modules, "groq", groq_module)
    monkeypatch.setitem(sys.modules, "dotenv", dotenv_module)
    monkeypatch.setitem(sys.modules, "langchain_groq", langchain_groq_module)
    monkeypatch.setenv("GROQ_API_KEY", "mock-key")
    monkeypatch.setenv(ARTIFACT_DIR_ENV, os.fspath(run_dir))
    monkeypatch.setattr(sys, "argv", ["bug_diagnosis_agent.py", os.fspath(bug_path), "--reproduction-test", os.fspath(confirmed_path)])
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    path = Path(__file__).resolve().parents[1] / "bug_diagnosis_agent.py"
    spec = importlib.util.spec_from_file_location("diagnosis_with_reproduction_test", path)
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(SystemExit):
        spec.loader.exec_module(module)

    prompt = captured["messages"][0]["content"]
    assert "USER-CONFIRMED TEST" in prompt
    assert "human-confirmed-reproduction-test" in prompt
    assert "keeps both messages" in prompt
    assert "Received: [second]" in prompt
    assert "without changing the test" in prompt


@pytest.mark.parametrize(
    ("classifications", "decisions", "expected"),
    [
        (["UNSUPPORTED"], [], ["RUNNING", "SKIPPED"]),
        ([reproduction_tests.FAILS_ON_CURRENT_CODE], [{"decision": "manual", "feedback": ""}], ["RUNNING", "MANUAL_REQUESTED"]),
        (
            [reproduction_tests.FAILS_ON_CURRENT_CODE] * 2,
            [{"decision": "revise", "feedback": "x"}, {"decision": "confirm", "feedback": ""}],
            ["RUNNING", "REVISED", "RUNNING", "CONFIRMED"],
        ),
        ([reproduction_tests.FAILS_ON_CURRENT_CODE], [], ["RUNNING", "TIMED_OUT"]),
    ],
)
def test_each_round_is_marked_finished_in_the_console(loop, monkeypatch, classifications, decisions, expected):
    recorded = []
    monkeypatch.setattr(workflow_orchestrator, "record_stage", lambda number, name, status: recorded.append(status))
    loop.state["classifications"] = classifications
    loop.state["decisions"] = decisions

    workflow_orchestrator.collect_ai_reproduction_test(loop.context)

    assert recorded == expected
