"""Bounded retries when the model returns unusable diagnosis output.

Mocked unit tests: the Groq client is replaced, so no paid call is made.
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

from workflow_artifacts import ARTIFACT_DIR_ENV


VALID_DIAGNOSIS = {
    "bug_title": "Retry demo",
    "project_type": "Python",
    "failure_analysis": "Mocked failure evidence.",
    "root_cause": "Mocked root cause.",
    "affected_files": ["app.py"],
    "evidence": ["Mocked deterministic evidence."],
    "proposed_fix": "Apply a mocked fix.",
    "verification_plan": ["Run mocked tests."],
}


def model_response(content):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, reasoning=None),
                finish_reason="stop",
            )
        ]
    )


def run_diagnosis_agent(monkeypatch, tmp_path, contents):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "repository_context.json").write_text(
        json.dumps({"repository_path": os.fspath(tmp_path), "file_contents": {}}),
        encoding="utf-8",
    )
    (run_dir / "reproduction_result.json").write_text(
        json.dumps({"status": "REPRODUCED", "evidence": ["Mocked failure."]}),
        encoding="utf-8",
    )
    bug_report_path = tmp_path / "bug-report.json"
    bug_report_path.write_text(json.dumps({"bug_title": "Retry demo"}), encoding="utf-8")

    dotenv_module = ModuleType("dotenv")
    dotenv_module.load_dotenv = Mock()
    langchain_groq_module = ModuleType("langchain_groq")
    langchain_groq_module.ChatGroq = Mock()
    groq_module = ModuleType("groq")
    groq_module.APIStatusError = type("APIStatusError", (Exception,), {})
    groq_client = Mock()
    groq_client.chat.completions.create.side_effect = [
        model_response(content) for content in contents
    ]
    groq_module.Groq = Mock(return_value=groq_client)
    monkeypatch.setitem(sys.modules, "dotenv", dotenv_module)
    monkeypatch.setitem(sys.modules, "langchain_groq", langchain_groq_module)
    monkeypatch.setitem(sys.modules, "groq", groq_module)
    monkeypatch.setenv("GROQ_API_KEY", "mock-retry-test-key")
    monkeypatch.setenv(ARTIFACT_DIR_ENV, os.fspath(run_dir))
    monkeypatch.setattr(sys, "argv", ["bug_diagnosis_agent.py", os.fspath(bug_report_path)])
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    agent_path = Path(__file__).resolve().parents[1] / "bug_diagnosis_agent.py"
    spec = importlib.util.spec_from_file_location("bug_diagnosis_retry_test", agent_path)
    module = importlib.util.module_from_spec(spec)
    exit_code = 0
    try:
        spec.loader.exec_module(module)
    except SystemExit as stop:
        exit_code = stop.code
    return exit_code, groq_client, run_dir


def test_incomplete_diagnosis_is_retried_once_with_the_rejection_reason(
    tmp_path, monkeypatch
):
    incomplete = dict(VALID_DIAGNOSIS, proposed_fix="", verification_plan=[])

    exit_code, client, run_dir = run_diagnosis_agent(
        monkeypatch, tmp_path, [json.dumps(incomplete), json.dumps(VALID_DIAGNOSIS)]
    )

    assert exit_code in (0, None)
    assert client.chat.completions.create.call_count == 2
    retry_prompt = client.chat.completions.create.call_args_list[1].kwargs["messages"][0]["content"]
    assert "YOUR PREVIOUS RESPONSE WAS REJECTED" in retry_prompt
    assert "proposed_fix" in retry_prompt
    saved = json.loads((run_dir / "diagnosis.json").read_text(encoding="utf-8"))
    assert saved["root_cause"] == "Mocked root cause."
    model_call = json.loads((run_dir / "diagnosis_model_call.json").read_text(encoding="utf-8"))
    assert model_call["response_attempts"] == 2


@pytest.mark.parametrize("bad_first", ["", "{ not json"])
def test_empty_or_invalid_json_is_retried_once(tmp_path, monkeypatch, bad_first):
    exit_code, client, run_dir = run_diagnosis_agent(
        monkeypatch, tmp_path, [bad_first, json.dumps(VALID_DIAGNOSIS)]
    )

    assert exit_code in (0, None)
    assert client.chat.completions.create.call_count == 2
    assert (run_dir / "diagnosis.json").is_file()


def test_diagnosis_fails_after_two_unusable_responses(tmp_path, monkeypatch):
    incomplete = dict(VALID_DIAGNOSIS, proposed_fix="")

    exit_code, client, run_dir = run_diagnosis_agent(
        monkeypatch, tmp_path, [json.dumps(incomplete), json.dumps(incomplete)]
    )

    assert exit_code == 1
    assert client.chat.completions.create.call_count == 2
    assert not (run_dir / "diagnosis.json").exists()
