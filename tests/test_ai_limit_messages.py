"""Groq free-tier limits are explained in plain words, without the provider text."""

import subprocess

import pytest

import workflow_orchestrator as orchestrator
from test_workflow_ui_api import make_manager, make_payload
from workflow_control import _atomic_json

TPD = (
    "groq.RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model "
    "`openai/gpt-oss-20b` in organization `org_secret123` service tier `on_demand` on tokens per day "
    "(TPD): Limit 200000, Used 199500, Requested 1500. Please try again in 7m12.5s. Need more tokens?', "
    "'type': 'tokens', 'code': 'rate_limit_exceeded'}}"
)
TPM = (
    "Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` on "
    "tokens per minute (TPM): Limit 8000, Used 7000, Requested 2000. Please try again in 7.5s.', "
    "'type': 'tokens', 'code': 'rate_limit_exceeded'}}"
)
TOO_LARGE = (
    "Error code: 413 - {'error': {'message': 'Request too large for model `openai/gpt-oss-20b` on "
    "tokens per minute (TPM): Limit 8000, Requested 9500, please reduce your message size and try "
    "again.', 'type': 'tokens', 'code': 'rate_limit_exceeded'}}"
)


@pytest.fixture(autouse=True)
def fresh_limit_state(monkeypatch):
    monkeypatch.setattr(orchestrator, "AI_LIMIT_EVENTS", [])
    monkeypatch.setattr(orchestrator, "PENDING_AI_LIMIT", None)


def test_daily_limit_message_has_wait_time_and_no_provider_text():
    limit = orchestrator.ai_limit_from_output(TPD)
    assert limit["kind"] == "daily"
    assert limit["wait_seconds"] == pytest.approx(432.5)
    assert "daily token limit" in limit["message"]
    assert "8 minutes" in limit["message"]
    assert "org_secret123" not in limit["message"]


def test_per_minute_limit():
    limit = orchestrator.ai_limit_from_output(TPM)
    assert limit["kind"] == "per_minute"
    assert limit["wait_seconds"] == pytest.approx(7.5)
    assert "1 minute" in limit["message"]


def test_request_too_large_says_waiting_does_not_help():
    limit = orchestrator.ai_limit_from_output(TOO_LARGE)
    assert limit["kind"] == "request_too_large"
    assert "Waiting will not help" in limit["message"]


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("tokens per day (TPD) ... Please try again in 1h2m3s.", 3723),
        ("tokens per day (TPD) ... Please try again in 450ms.", 0.45),
        ("tokens per day (TPD) ... Please try again in 3m0.5s.", 180.5),
        ("rate_limit_exceeded, no wait given", None),
    ],
)
def test_wait_times_are_parsed(text, seconds):
    limit = orchestrator.ai_limit_from_output(text)
    if seconds is None:
        assert limit["wait_seconds"] is None
        assert "a few minutes" in limit["message"]
    else:
        assert limit["wait_seconds"] == pytest.approx(seconds)


@pytest.mark.parametrize("text", ["", "AssertionError: expected 2, got 3", "Error code: 500 server error"])
def test_other_failures_are_not_ai_limits(text):
    assert orchestrator.ai_limit_from_output(text) is None


def test_run_agent_notes_a_limit_from_a_failed_agent(monkeypatch):
    monkeypatch.setattr(
        orchestrator.subprocess, "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "Running...\n", TPD),
    )
    result = orchestrator.run_agent("bug_diagnosis_agent.py")
    assert result["success"] is False
    assert result["ai_limit"]["kind"] == "daily"
    assert orchestrator.AI_LIMIT_EVENTS == [result["ai_limit"]]


def test_successful_agent_output_mentioning_limits_is_ignored(monkeypatch):
    monkeypatch.setattr(
        orchestrator.subprocess, "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "retrying after rate_limit_exceeded\n", ""),
    )
    result = orchestrator.run_agent("bug_diagnosis_agent.py")
    assert "ai_limit" not in result
    assert orchestrator.AI_LIMIT_EVENTS == []


def test_limit_is_shown_on_first_failed_stage_and_blocker_once(monkeypatch):
    monkeypatch.setattr(orchestrator, "control_enabled", lambda: False)
    orchestrator.note_ai_limit({"success": False, "output": "", "error": TPM})
    stages = []
    orchestrator.add_stage_result(stages, 5, "AI Test Generation (Attempt 1)", "FAILED", {"return_code": 1})
    orchestrator.add_stage_result(stages, 6, "Test Execution (Attempt 1)", "FAILED")
    assert "per-minute token limit" in stages[0]["details"]["ai_limit"]
    assert stages[0]["details"]["return_code"] == 1
    assert "details" not in stages[1]

    first = orchestrator.blocker_reason_with_ai_limit("Tests did not pass.")
    second = orchestrator.blocker_reason_with_ai_limit("Tests did not pass.")
    assert first.startswith("The AI (Groq) per-minute token limit") and first.endswith("(Tests did not pass.)")
    assert second == "Tests did not pass."


def test_api_report_exposes_the_limit_message(tmp_path, monkeypatch):
    repository, manager, _scheduled = make_manager(tmp_path, monkeypatch)
    workflow, _created = manager.start(make_payload(repository))
    run_dir = manager.runs_dir / workflow["workflow_id"]
    _atomic_json(run_dir / "workflow_report.json", {
        "final_status": "FAIL", "stages": [], "retry_history": [],
        "ai_limit": orchestrator.ai_limit_from_output(TPD)["message"],
    })
    report = manager.get(workflow["workflow_id"])["report"]
    assert "daily token limit" in report["ai_limit"]


def test_connection_error_from_the_groq_sdk_is_explained():
    output = "The model returned a malformed tool call.\n\nTEST GENERATION ERROR\n====\nConnection error.\n"
    limit = orchestrator.ai_limit_from_output(output)
    assert limit["kind"] == "connection"
    assert "internet connection" in limit["message"]
    assert orchestrator.ai_limit_from_output("groq.APIConnectionError: boom")["kind"] == "connection"


def test_langsmith_upload_warning_is_not_a_connection_failure():
    warning = (
        "Failed to send compressed multipart ingest: Connection error caused failure to POST "
        "https://api.smith.langchain.com/runs/multipart in LangSmith API."
    )
    assert orchestrator.ai_limit_from_output(warning) is None
