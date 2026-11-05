"""Browser recording path: control wait, API endpoint and orchestrator handler."""
import json
import uuid

import pytest

import workflow_control
import workflow_orchestrator as orchestrator
from workflow_ui_api import RequestError

from test_workflow_control import configure_control_directory
from test_workflow_ui_api import make_manager, make_payload, set_workflow_state


RECORDING = """import { test, expect } from '@playwright/test';
test('empty task', async ({ page }) => {
  await page.goto('http://localhost:3000/');
  await page.getByRole('textbox').fill('   ');
  await page.getByRole('button', { name: 'Add' }).click();
  await expect(page.locator('#tasks li')).toHaveCount(2);
});
"""


# ---------------------------------------------------------------- control

def test_recording_submission_shape_is_validated():
    ok, error = workflow_control.validate_recording_submission({"filename": "bug.spec.ts", "content": "x"})
    assert error is None and ok == {"filename": "bug.spec.ts", "content": "x"}
    for bad in (
        None,
        {"filename": "../evil.ts", "content": "x"},
        {"filename": "bug.exe", "content": "x"},
        {"filename": "bug.ts", "content": "  "},
        {"filename": "bug.ts", "content": "x" * (64 * 1024 + 1)},
    ):
        assert workflow_control.validate_recording_submission(bad)[0] is None


def test_wait_replays_recordings_until_one_is_confirmed(tmp_path, monkeypatch):
    directory = configure_control_directory(tmp_path, monkeypatch)
    monkeypatch.setattr(workflow_control, "CONTROL_POLL_SECONDS", 0)
    published = []
    original_publish = workflow_control.publish_workflow_state

    def publish(*args, **kwargs):
        original_publish(*args, **kwargs)
        published.append(json.loads((directory / "state.json").read_text(encoding="utf-8")))
        if len(published) in (1, 2):
            workflow_control._atomic_json(
                directory / "recording-submission.json",
                {"filename": f"try{len(published)}.spec.ts", "content": RECORDING},
            )

    monkeypatch.setattr(workflow_control, "publish_workflow_state", publish)
    handled = []

    def handler(submission):
        handled.append(submission["filename"])
        if len(handled) == 1:
            return None, {"kind": "recording_result", "classification": "PASSES_ON_CURRENT_CODE", "summary": "passed"}
        return {"status": "CONFIRMED"}, {"kind": "recording_result"}

    result = workflow_control.wait_for_manual_reproduction(timeout=5, recording_handler=handler)

    assert result == {"kind": "browser_recording", "confirmed": {"status": "CONFIRMED"}}
    assert handled == ["try1.spec.ts", "try2.spec.ts"]
    assert published[0]["allowed_actions"] == ["upload_recording", "stop_safely"]
    assert published[1]["review"]["classification"] == "PASSES_ON_CURRENT_CODE"
    assert published[1]["pause_kind"] == "manual_reproduction"


def test_wait_without_handler_ignores_recordings(tmp_path, monkeypatch):
    directory = configure_control_directory(tmp_path, monkeypatch)
    monkeypatch.setattr(workflow_control, "CONTROL_POLL_SECONDS", 0)
    workflow_control._atomic_json(directory / "recording-submission.json", {"filename": "a.ts", "content": "x"})
    workflow_control._atomic_json(directory / "manual-submission.json", {
        "steps": ["a"], "expected_behavior": "b", "observed_behavior": "c",
    })
    result = workflow_control.wait_for_manual_reproduction(timeout=5)
    assert result["status"] == "MANUAL_SUBMITTED"
    state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    assert state["allowed_actions"] == ["stop_safely"]


def test_stop_during_replay_stops_the_wait(tmp_path, monkeypatch):
    directory = configure_control_directory(tmp_path, monkeypatch)
    monkeypatch.setattr(workflow_control, "CONTROL_POLL_SECONDS", 0)
    workflow_control._atomic_json(directory / "recording-submission.json", {"filename": "a.ts", "content": RECORDING})

    def handler(_submission):
        workflow_control._atomic_json(directory / "stop.request", {"action_id": "x"})
        return None, {"kind": "recording_result"}

    with pytest.raises(workflow_control.WorkflowStopRequested):
        workflow_control.wait_for_manual_reproduction(timeout=5, recording_handler=handler)


def test_recording_feedback_is_bounded():
    feedback = workflow_control.recording_feedback({
        "filename": "a.ts",
        "classification": "FAILS_ON_CURRENT_CODE",
        "summary": "s" * 5000,
        "replay": {"results": [{"line": 1, "source": "x" * 900, "status": "failed", "message": "m"}] * 200},
    })
    assert len(feedback["summary"]) == 1000
    assert len(feedback["steps"]) == workflow_control.MAX_RECORDING_STEP_RESULTS
    assert len(feedback["steps"][0]["source"]) == 300


# ---------------------------------------------------------------- API

def recording_request(content=RECORDING, filename="bug.spec.ts"):
    return {"submission_id": str(uuid.uuid4()), "recording": {"filename": filename, "content": content}}


def test_recording_api_checks_state_and_rejects_unsafe_scripts(tmp_path, monkeypatch):
    repository, manager, _scheduled = make_manager(tmp_path, monkeypatch)
    workflow, _created = manager.start(make_payload(repository))
    workflow_id = workflow["workflow_id"]

    with pytest.raises(RequestError) as early:
        manager.submit_recording(workflow_id, recording_request())
    assert early.value.status_code == 409

    # Waiting for manual steps, but recordings were not offered.
    set_workflow_state(manager, workflow_id, status="WAITING_FOR_MANUAL_REPRODUCTION", allowed_actions=["stop_safely"])
    with pytest.raises(RequestError) as not_offered:
        manager.submit_recording(workflow_id, recording_request())
    assert not_offered.value.status_code == 409

    control_path = set_workflow_state(
        manager, workflow_id,
        status="WAITING_FOR_MANUAL_REPRODUCTION",
        pause_kind="manual_reproduction",
        allowed_actions=["upload_recording", "stop_safely"],
    )
    unsafe = RECORDING.replace("await page.getByRole('textbox')", "require('child_process').exec('calc'); await page.getByRole('textbox')")
    with pytest.raises(RequestError) as rejected:
        manager.submit_recording(workflow_id, recording_request(unsafe))
    assert rejected.value.status_code == 400
    assert "Line 4" in str(rejected.value)
    with pytest.raises(RequestError):
        manager.submit_recording(workflow_id, recording_request(filename="bug.exe"))
    assert not (control_path / "recording-submission.json").exists()

    request = recording_request()
    accepted = manager.submit_recording(workflow_id, request)
    duplicate = manager.submit_recording(workflow_id, request)
    assert accepted["status"] == duplicate["status"] == "RECORDING_SUBMITTED"
    assert accepted["allowed_actions"] == ["stop_safely"]
    signal = json.loads((control_path / "recording-submission.json").read_text(encoding="utf-8"))
    assert signal == {"filename": "bug.spec.ts", "content": RECORDING}


def test_recording_result_is_shown_with_the_manual_form(tmp_path, monkeypatch):
    repository, manager, _scheduled = make_manager(tmp_path, monkeypatch)
    workflow, _created = manager.start(make_payload(repository))
    set_workflow_state(
        manager, workflow["workflow_id"],
        status="WAITING_FOR_MANUAL_REPRODUCTION",
        pause_kind="manual_reproduction",
        allowed_actions=["upload_recording", "stop_safely"],
        review={"kind": "recording_result", "filename": "a.ts", "classification": "ERROR",
                "summary": "Step could not run", "steps": [], "secret": "x"},
    )
    review = manager.get(workflow["workflow_id"])["review"]
    assert review == {"kind": "recording_result", "filename": "a.ts", "classification": "ERROR",
                      "summary": "Step could not run", "steps": []}


# ---------------------------------------------------------------- orchestrator

@pytest.fixture
def run_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "ACTIVE_RUN_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setattr(orchestrator, "publish_workflow_state", lambda *a, **k: None)
    return tmp_path


def fake_replay(classification, summary="summary"):
    calls = []

    def run_browser_replay(recording_path, name):
        calls.append((recording_path, name))
        return {"classification": classification, "summary": summary, "replay": {"results": []}}

    return run_browser_replay, calls


def test_failing_assertion_confirms_the_recording(run_directory, monkeypatch):
    replay, calls = fake_replay("FAILS_ON_CURRENT_CODE", "Assertion failed at line 6")
    monkeypatch.setattr(orchestrator, "run_browser_replay", replay)

    confirmed, feedback = orchestrator.handle_recording_submission({"filename": "bug.spec.ts", "content": RECORDING})

    assert confirmed["status"] == "CONFIRMED"
    assert confirmed["source"] == orchestrator.BROWSER_RECORDING_SOURCE
    assert confirmed["test_file"] == "bug.spec.ts"
    assert confirmed["content"] == RECORDING
    assert len(confirmed["steps"]) == 4
    assert confirmed["before_fix"]["classification"] == "FAILS_ON_CURRENT_CODE"
    assert calls[0][1] == "browser_replay_before_fix"
    saved = json.loads((run_directory / "confirmed_browser_recording.json").read_text(encoding="utf-8"))
    assert saved["content_sha256"] == confirmed["content_sha256"]
    assert feedback["classification"] == "FAILS_ON_CURRENT_CODE"


@pytest.mark.parametrize("classification", ["PASSES_ON_CURRENT_CODE", "ERROR", "UNAVAILABLE"])
def test_recordings_that_do_not_show_the_bug_are_not_confirmed(run_directory, monkeypatch, classification):
    replay, _calls = fake_replay(classification, "Playwright is not installed")
    monkeypatch.setattr(orchestrator, "run_browser_replay", replay)

    confirmed, feedback = orchestrator.handle_recording_submission({"filename": "bug.spec.ts", "content": RECORDING})

    assert confirmed is None
    assert feedback["classification"] == classification
    assert not (run_directory / "confirmed_browser_recording.json").exists()
    if classification == "PASSES_ON_CURRENT_CODE":
        assert "does not show the bug" in feedback["summary"]


def test_unsafe_recording_is_rejected_without_replay(run_directory, monkeypatch):
    replay, calls = fake_replay("FAILS_ON_CURRENT_CODE")
    monkeypatch.setattr(orchestrator, "run_browser_replay", replay)

    confirmed, feedback = orchestrator.handle_recording_submission({
        "filename": "bug.spec.ts",
        "content": RECORDING.replace("http://localhost:3000/", "https://example.com/"),
    })

    assert confirmed is None and calls == []
    assert feedback["classification"] == "REJECTED"
    assert "only open the local app" in feedback["summary"]


def test_after_fix_every_step_must_pass(run_directory, monkeypatch):
    replay, _calls = fake_replay("FAILS_ON_CURRENT_CODE")
    monkeypatch.setattr(orchestrator, "run_browser_replay", replay)
    orchestrator.handle_recording_submission({"filename": "bug.spec.ts", "content": RECORDING})

    still_failing = orchestrator.run_confirmed_browser_recording(1)
    assert still_failing["status"] == "FAILED"

    replay, calls = fake_replay("PASSES_ON_CURRENT_CODE")
    monkeypatch.setattr(orchestrator, "run_browser_replay", replay)
    fixed = orchestrator.run_confirmed_browser_recording(2)
    assert fixed["status"] == "PASSED"
    assert calls[0][1] == "browser_replay_after_fix_attempt_2"
    assert calls[0][0].endswith("confirmed_browser_recording.json")


def test_after_fix_without_confirmed_recording_fails(run_directory):
    assert orchestrator.run_confirmed_browser_recording(1)["status"] == "FAILED"


def test_recording_counts_as_a_confirmed_reproduction():
    assert orchestrator.BROWSER_RECORDING_SOURCE in orchestrator.CONFIRMED_SOURCES
    assert "manual" not in orchestrator.CONFIRMED_SOURCES


# ---------------------------------------------------------------- full workflow (mocked agents)

def run_workflow_with_recording(tmp_path, monkeypatch, replay_results):
    from test_workflow_artifacts import run_mocked_retry_workflow

    replay_calls = []

    def fake_ai(_context):
        return {"status": "UNAVAILABLE", "reason": "No AI test.", "rounds": []}

    def fake_collect_manual(_reproduction):
        confirmed = {
            "status": "CONFIRMED",
            "confirmed_by": "user",
            "source": orchestrator.BROWSER_RECORDING_SOURCE,
            "framework": "playwright-recording",
            "test_file": "bug.spec.ts",
            "content": RECORDING,
            "content_sha256": "0" * 64,
            "steps": [{"action": "goto", "path": "/"}],
            "before_fix": {"classification": "FAILS_ON_CURRENT_CODE", "summary": "Assertion failed at line 6"},
        }
        orchestrator.save_json(orchestrator.confirmed_reproduction_file(orchestrator.BROWSER_RECORDING_SOURCE), confirmed)
        return {"status": "RECORDING_CONFIRMED", "confirmed": confirmed, "reason": "Recording showed the bug."}

    def fake_replay(attempt):
        replay_calls.append(attempt)
        passed = replay_results[min(len(replay_calls), len(replay_results)) - 1]
        return {"status": "PASSED" if passed else "FAILED", "test_file": "bug.spec.ts"}

    monkeypatch.setattr(orchestrator, "collect_ai_reproduction_test", fake_ai)
    monkeypatch.setattr(orchestrator, "collect_manual_reproduction", fake_collect_manual)
    monkeypatch.setattr(orchestrator, "run_confirmed_browser_recording", fake_replay)
    result, run_dir, calls, retry_contexts = run_mocked_retry_workflow(
        tmp_path, monkeypatch, reproduction_status="NOT_REPRODUCED"
    )
    report = json.loads((run_dir / "workflow_report.json").read_text(encoding="utf-8"))
    return result, report, calls, replay_calls, retry_contexts, run_dir


def test_recording_reaches_diagnosis_and_must_pass_after_fix(tmp_path, monkeypatch):
    result, report, calls, replay_calls, _contexts, run_dir = run_workflow_with_recording(tmp_path, monkeypatch, [True])

    assert result == 0
    assert report["final_status"] == "PASS"
    assert report["reproduction_source"] == "browser_recording"
    assert report["reproduction_confirmed"] is True
    assert "fix_review" not in report
    evidence = report["reproduction_evidence"]
    assert evidence["recording_file"] == "bug.spec.ts"
    assert evidence["before_fix"] == "FAILS_ON_CURRENT_CODE"
    assert evidence["confirmed_by"] == "user"
    stage_2 = next(stage for stage in report["stages"] if stage["stage"] == 2)
    assert stage_2["status"] == "RECORDING_CONFIRMED"
    assert calls["diagnosis_arguments"][0] == [
        "--reproduction-test",
        str(run_dir / "confirmed_browser_recording.json"),
    ]
    assert replay_calls == [1]
    assert report["retry_history"][0]["reproduction_test"] == "PASS"


def test_fix_must_make_the_recording_pass(tmp_path, monkeypatch):
    result, report, calls, replay_calls, retry_contexts, _run_dir = run_workflow_with_recording(
        tmp_path, monkeypatch, [False, True]
    )

    assert result == 0
    assert [item["reproduction_test"] for item in report["retry_history"]] == ["FAIL", "PASS"]
    assert calls["code_fix"] == 2
    assert retry_contexts[0]["confirmed_browser_recording"]["test_file"] == "bug.spec.ts"


def test_workflow_fails_when_the_recording_never_passes(tmp_path, monkeypatch):
    result, report, _calls, replay_calls, _contexts, _run_dir = run_workflow_with_recording(tmp_path, monkeypatch, [False])

    assert result == 1
    assert report["final_status"] == "FAIL"
    assert len(replay_calls) == 3


def test_passing_replay_is_saved_as_http_runtime_validation(tmp_path, monkeypatch):
    from workflow_validation import runtime_result_has_success_evidence

    path = tmp_path / "validation_result.json"
    monkeypatch.setattr(orchestrator, "VALIDATION_RESULT_FILE", str(path))

    orchestrator.record_browser_replay_as_runtime_validation({"status": "PASSED", "test_file": "bug.spec.ts"}, 1)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["source"] == "browser_recording"
    assert runtime_result_has_success_evidence(saved) is True

    # A later failed replay removes the old passing evidence.
    orchestrator.record_browser_replay_as_runtime_validation({"status": "FAILED", "test_file": "bug.spec.ts"}, 2)
    assert not path.exists()
