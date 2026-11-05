"""Attached recordings and logs: validation, secret redaction, storage, API upload,
and the summaries reaching the AI reproduction-test prompt. Mocked; no model calls."""

import json
import os
import uuid

import pytest

import evidence_files as ev
import reproduction_test_agent as agent
from workflow_ui_api import RequestError, WorkflowManager


HAR = {
    "log": {
        "entries": [
            {
                "request": {
                    "method": "POST",
                    "url": "https://api.example.test/messages?token=abc123secret",
                    "headers": [
                        {"name": "Authorization", "value": "Bearer abcdefghijklmnop"},
                        {"name": "Content-Type", "value": "application/json"},
                    ],
                    "cookies": [{"name": "session", "value": "s3cr3t"}],
                    "postData": {"text": '{"message": "first", "password": "hunter2"}'},
                },
                "response": {
                    "status": 200,
                    "headers": [{"name": "Set-Cookie", "value": "session=s3cr3t"}],
                    "content": {"text": '{"saved": true}'},
                },
                "_webSocketMessages": [
                    {"type": "send", "data": '{"action":"message","requestId":"r1","message":"first message"}'},
                    {"type": "receive", "data": '{"type":"message_ack","status":"saved"}'},
                ],
            }
        ]
    }
}


def test_har_is_redacted_and_summarized():
    record = ev.prepare_attachment("session.har", json.dumps(HAR))

    stored = record["redacted_content"]
    assert record["kind"] == "har"
    assert record["redacted"] is True
    for secret in ("abcdefghijklmnop", "s3cr3t", "hunter2", "abc123secret"):
        assert secret not in stored
        assert secret not in record["summary"]
    assert "POST https://api.example.test/messages" in record["summary"]
    assert "ws send" in record["summary"] and "first message" in record["summary"]
    assert "-> 200" in record["summary"]


@pytest.mark.parametrize(
    "line",
    [
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3OCJ9.c2lnbmF0dXJlc2ln",
        "aws_access_key_id=AKIAABCDEFGHIJKLMNOP",
        "GROQ key gsk_ABCDEFGHIJKLMNOPQRSTUVWX",
        'config {"api_key": "live-key-123456"}',
        "password=supersecret",
    ],
    ids=["jwt-header", "aws-key", "groq-key", "json-api-key", "password"],
)
def test_logs_are_redacted(line):
    record = ev.prepare_attachment("app.log", f"start\n{line}\nERROR something failed\n")
    for secret in ("eyJhbGciOiJIUzI1NiJ9", "AKIAABCDEFGHIJKLMNOP", "gsk_ABCDEFGHIJKLMNOPQRSTUVWX", "live-key-123456", "supersecret"):
        assert secret not in record["redacted_content"]
    assert "ERROR something failed" in record["summary"]


def test_websocket_message_log_summary():
    messages = [{"action": "message", "requestId": f"r{n}", "message": f"m{n}"} for n in range(3)]
    record = ev.prepare_attachment("ws-messages.json", json.dumps(messages))
    assert record["kind"] == "message_log"
    assert "1." in record["summary"] and "m2" in record["summary"]


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("recording.zip", "PK", "unsupported file type"),
        ("trace.exe", "MZ", "unsupported file type"),
        ("notes.log", "", "empty"),
        ("notes.log", "a\x00b", "binary"),
        ("session.har", "{ not json", "not valid JSON"),
        ("session.har", json.dumps({"log": {}}), "log.entries"),
        ("big.log", "x" * (ev.MAX_FILE_BYTES + 1), "larger than"),
        ("", "text", "file name"),
    ],
    ids=["zip", "exe", "empty", "binary", "bad-json", "har-without-entries", "too-large", "no-name"],
)
def test_invalid_attachments_are_rejected(name, content, message):
    with pytest.raises(ev.EvidenceError) as error:
        ev.prepare_attachment(name, content)
    assert message in str(error.value)


def test_path_parts_are_stripped_from_names():
    record = ev.prepare_attachment("..\\..\\secrets\\app.log", "line")
    assert record["stored_name"] == "app.log"


def test_store_limits_and_index(tmp_path):
    records = ev.store_attachments(str(tmp_path), [{"name": "a.log", "content": "one"}, {"name": "b.txt", "content": "two"}])
    assert [item["stored_as"] for item in records] == ["evidence/01-a.log", "evidence/02-b.txt"]
    assert (tmp_path / "evidence" / "01-a.log").read_text(encoding="utf-8") == "one"
    assert len(ev.load_index(str(tmp_path))["files"]) == 2
    with pytest.raises(ev.EvidenceError):
        ev.store_attachments(str(tmp_path), [{"name": f"{n}.log", "content": "x"} for n in range(4)])
    assert len(ev.load_index(str(tmp_path))["files"]) == 2
    assert all("summary" not in item for item in ev.public_summary(str(tmp_path)))


def test_total_size_limit(tmp_path):
    chunk = "x" * (ev.MAX_FILE_BYTES - 10)
    with pytest.raises(ev.EvidenceError) as error:
        ev.store_attachments(str(tmp_path), [{"name": f"{n}.log", "content": chunk} for n in range(5)])
    assert "total" in str(error.value)
    assert not (tmp_path / "evidence").exists()


# ------------------------------------------------------------
# API
# ------------------------------------------------------------

def make_manager(tmp_path, monkeypatch):
    repository = tmp_path / "target"
    repository.mkdir()
    scheduled = []
    monkeypatch.setenv("WORKFLOW_ALLOWED_REPOSITORY_ROOTS", os.fspath(tmp_path))
    manager = WorkflowManager(project_dir=tmp_path, worker_launcher=scheduled.append)
    return repository, manager, scheduled


def start_payload(repository, **extra):
    payload = {
        "request_id": str(uuid.uuid4()),
        "repository_path": os.fspath(repository),
        "bug_title": "Messages overwrite",
        "bug_description": "Second message replaces the first.",
        "allow_local_commands": True,
        "allow_ai": True,
    }
    payload.update(extra)
    return payload


def test_start_stores_attachments_before_the_workflow_runs(tmp_path, monkeypatch):
    repository, manager, scheduled = make_manager(tmp_path, monkeypatch)
    workflow, _ = manager.start(start_payload(repository, attachments=[{"name": "session.har", "content": json.dumps(HAR)}]))

    assert workflow["evidence"][0]["name"] == "session.har"
    assert workflow["evidence"][0]["kind"] == "har"
    stored = (manager.runs_dir / workflow["workflow_id"] / "evidence" / "01-session.har").read_text(encoding="utf-8")
    assert "hunter2" not in stored
    assert len(scheduled) == 1


def test_invalid_start_attachment_creates_nothing(tmp_path, monkeypatch):
    repository, manager, scheduled = make_manager(tmp_path, monkeypatch)
    with pytest.raises(RequestError) as error:
        manager.start(start_payload(repository, attachments=[{"name": "x.exe", "content": "MZ"}]))
    assert error.value.status_code == 400
    assert scheduled == []
    assert not manager.runs_dir.exists() or not any(manager.runs_dir.iterdir())


def test_upload_during_run_is_idempotent_and_refused_after_finish(tmp_path, monkeypatch):
    repository, manager, _ = make_manager(tmp_path, monkeypatch)
    workflow, _ = manager.start(start_payload(repository))
    workflow_id = workflow["workflow_id"]
    upload = {"upload_id": str(uuid.uuid4()), "attachments": [{"name": "app.log", "content": "ERROR boom"}]}

    first = manager.upload_evidence(workflow_id, upload)
    again = manager.upload_evidence(workflow_id, upload)
    assert len(first["evidence"]) == len(again["evidence"]) == 1

    state_path = manager.runs_dir / workflow_id / ".control" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(status="FAIL", terminal=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(RequestError) as error:
        manager.upload_evidence(workflow_id, {"upload_id": str(uuid.uuid4()), "attachments": upload["attachments"]})
    assert error.value.status_code == 409


# ------------------------------------------------------------
# Evidence reaches the reproduction-test prompt
# ------------------------------------------------------------

def test_evidence_summaries_are_in_the_prompt(tmp_path):
    ev.store_attachments(str(tmp_path), [{"name": "session.har", "content": json.dumps(HAR)}])
    index = ev.load_index(str(tmp_path))
    runner = {"framework": "jest", "test_path": "backend/test/ai-reproduction.test.js", "base_command": ["jest"]}

    prompt = agent.build_prompt({"bug_title": "x"}, None, runner, str(tmp_path), {}, None, index)

    assert "ATTACHED EVIDENCE" in prompt
    assert "first message" in prompt
    assert "hunter2" not in prompt
    assert "mocks instead of real network calls" in prompt
