import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path


CONTROL_DIRECTORY_ENV = "WORKFLOW_CONTROL_DIRECTORY"
WORKFLOW_ID_ENV = "WORKFLOW_ID"
WORKFLOW_RUN_DIRECTORY_ENV = "WORKFLOW_RUN_DIRECTORY"
CONTROL_POLL_SECONDS = 0.2
CONTROL_WAIT_SECONDS = 1800
WORKFLOW_ID_PATTERN = re.compile(r"^[a-f0-9]{32}(?:-retry-[1-2])?$")
MAX_MANUAL_STEPS = 30
MAX_MANUAL_EVIDENCE_ITEMS = 30
MAX_MANUAL_FIELD_LENGTH = 4000
MAX_EVIDENCE_ITEM_LENGTH = 8000


class WorkflowStopRequested(Exception):
    pass


def control_directory():
    value = os.environ.get(CONTROL_DIRECTORY_ENV)
    if not value:
        return None
    directory = Path(value).resolve()
    runs_root = (Path(__file__).resolve().parent / "workflow_runs").resolve()
    try:
        if os.path.commonpath((os.fspath(runs_root), os.fspath(directory))) != os.fspath(runs_root):
            return None
    except ValueError:
        return None
    return directory


def control_enabled():
    return control_directory() is not None


def validate_manual_submission(value):
    if not isinstance(value, dict):
        return None, ["Manual reproduction payload must be an object."]

    errors = []
    steps = value.get("steps")
    expected = value.get("expected_behavior")
    observed = value.get("observed_behavior")
    evidence = value.get("supporting_evidence", [])

    if (
        not isinstance(steps, list)
        or not steps
        or len(steps) > MAX_MANUAL_STEPS
        or any(
            not isinstance(step, str)
            or not step.strip()
            or len(step) > MAX_MANUAL_FIELD_LENGTH
            for step in steps
        )
    ):
        errors.append("Provide 1 to 30 non-empty reproduction steps (4,000 chars max each).")
    if not isinstance(expected, str) or not expected.strip() or len(expected) > MAX_MANUAL_FIELD_LENGTH:
        errors.append("Expected behavior is required and must be at most 4,000 characters.")
    if not isinstance(observed, str) or not observed.strip() or len(observed) > MAX_MANUAL_FIELD_LENGTH:
        errors.append("Observed behavior is required and must be at most 4,000 characters.")
    if (
        not isinstance(evidence, list)
        or len(evidence) > MAX_MANUAL_EVIDENCE_ITEMS
        or any(
            not isinstance(item, str)
            or len(item) > MAX_EVIDENCE_ITEM_LENGTH
            for item in evidence
        )
    ):
        errors.append("Evidence must be a list of at most 30 text items (8,000 chars max each).")

    if errors:
        return None, errors
    return {
        "status": "MANUAL_SUBMITTED",
        "source": "user",
        "steps": [step.strip() for step in steps],
        "expected_behavior": expected.strip(),
        "observed_behavior": observed.strip(),
        "supporting_evidence": [item.strip() for item in evidence if item.strip()],
    }, []


# On Windows, replacing or reading a file fails with PermissionError while
# another process (for example the UI API polling state.json) briefly has it
# open. Retry for a short, bounded time instead of crashing the workflow.
SHARING_RETRY_ATTEMPTS = 50
SHARING_RETRY_SECONDS = 0.02


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary_path.open("x", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        for attempt in range(SHARING_RETRY_ATTEMPTS):
            try:
                os.replace(temporary_path, path)
                break
            except PermissionError:
                if attempt == SHARING_RETRY_ATTEMPTS - 1:
                    raise
                time.sleep(SHARING_RETRY_SECONDS)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _read_json(path):
    for attempt in range(SHARING_RETRY_ATTEMPTS):
        try:
            with path.open("r", encoding="utf-8") as source:
                value = json.load(source)
            return value if isinstance(value, dict) else None
        except PermissionError:
            if attempt == SHARING_RETRY_ATTEMPTS - 1:
                return None
            time.sleep(SHARING_RETRY_SECONDS)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
    return None


def publish_workflow_state(
    status,
    stage,
    blocker_reason=None,
    pause_kind=None,
    allowed_actions=None,
    stage_number=None,
    stage_status=None,
    review=None,
):
    directory = control_directory()
    if directory is None:
        return
    workflow_id = os.environ.get(WORKFLOW_ID_ENV, "")
    previous = _read_json(directory / "state.json") or {}
    stages = list(previous.get("stages", []))
    if isinstance(stage_status, str):
        stage_record = {
            "stage": stage_number,
            "name": str(stage)[:160],
            "status": stage_status,
        }
        matching_index = next(
            (
                index
                for index, item in enumerate(stages)
                if isinstance(item, dict)
                and item.get("stage") == stage_number
                and item.get("name") == stage_record["name"]
            ),
            None,
        )
        if matching_index is None:
            stages.append(stage_record)
        else:
            stages[matching_index] = stage_record
    state = {
        **previous,
        "workflow_id": workflow_id,
        "status": status,
        "stage": str(stage)[:160],
        "blocker_reason": str(blocker_reason)[:2000] if blocker_reason else None,
        "pause_kind": pause_kind,
        "allowed_actions": (
            allowed_actions
            if allowed_actions is not None
            else []
        ),
        "stages": stages,
        # Only present while a reproduction test is waiting for review.
        "review": review if isinstance(review, dict) else None,
        "updated_at": time.time(),
    }
    _atomic_json(directory / "state.json", state)


def record_stage(stage_number, name, stage_status):
    """Record one stage's progress for the console without changing the
    overall workflow status (which only publish_workflow_state sets)."""
    directory = control_directory()
    if directory is None or not isinstance(stage_status, str):
        return
    previous = _read_json(directory / "state.json") or {}
    stages = [item for item in previous.get("stages", []) if isinstance(item, dict)]
    stage_record = {
        "stage": stage_number,
        "name": str(name)[:160],
        "status": stage_status[:40],
    }
    for index, item in enumerate(stages):
        if item.get("stage") == stage_number and item.get("name") == stage_record["name"]:
            stages[index] = stage_record
            break
    else:
        stages.append(stage_record)
    _atomic_json(directory / "state.json", {
        **previous,
        "stages": stages[-60:],
        "updated_at": time.time(),
    })


def _consume_signal(name):
    directory = control_directory()
    if directory is None:
        return None
    path = directory / name
    value = _read_json(path)
    if value is None:
        return None
    try:
        path.unlink()
    except FileNotFoundError:
        return None
    return value


def stop_requested():
    return _consume_signal("stop.request") is not None


def check_stop_requested(stage):
    if not control_enabled():
        return False
    if stop_requested():
        publish_workflow_state("STOPPED", stage)
        return True
    return False


MAX_RECORDING_FILENAME_LENGTH = 100
RECORDING_FILENAME_PATTERN = re.compile(r"^[\w.-]{1,100}\.(?:js|ts|mjs|cjs|py)$")
MAX_RECORDING_STEP_RESULTS = 80


def validate_recording_submission(value):
    """Return (submission, error) for an uploaded Playwright codegen recording.

    Only shape and size are checked here; browser_recording.parse_recording
    decides whether the script is a safe step list.
    """
    from browser_recording import MAX_SCRIPT_BYTES

    if not isinstance(value, dict):
        return None, "Recording must be an object."
    filename = value.get("filename")
    content = value.get("content")
    if not isinstance(filename, str) or not RECORDING_FILENAME_PATTERN.fullmatch(filename):
        return None, "Recording file name must end in .js, .ts, .mjs, .cjs or .py (letters, digits, . _ - only)."
    if not isinstance(content, str) or not content.strip():
        return None, "Recording content is empty."
    if len(content.encode("utf-8")) > MAX_SCRIPT_BYTES:
        return None, f"Recording is larger than {MAX_SCRIPT_BYTES // 1024} KB."
    return {"filename": filename, "content": content}, None


def recording_feedback(result):
    """The part of a replay result the console shows next to the manual form."""
    replay = result.get("replay") if isinstance(result.get("replay"), dict) else {}
    steps = replay.get("results") if isinstance(replay.get("results"), list) else []
    return {
        "kind": "recording_result",
        "filename": result.get("filename"),
        "classification": result.get("classification"),
        "summary": str(result.get("summary", ""))[:1000],
        "steps": [
            {
                "line": item.get("line"),
                "source": str(item.get("source", ""))[:300],
                "status": item.get("status"),
                "message": str(item.get("message") or "")[:300] or None,
            }
            for item in steps[:MAX_RECORDING_STEP_RESULTS]
            if isinstance(item, dict)
        ],
    }


def wait_for_manual_reproduction(timeout=CONTROL_WAIT_SECONDS, recording_handler=None):
    """Wait for typed steps or, when recording_handler is given, a browser recording.

    recording_handler(submission) replays the recording and returns
    (confirmed, feedback): confirmed is returned as {"kind": "browser_recording",
    "confirmed": ...}; otherwise feedback is shown and the wait goes on.
    """
    directory = control_directory()
    if directory is None:
        return None
    submission_path = directory / "manual-submission.json"
    deadline = time.monotonic() + timeout
    actions = ["upload_recording", "stop_safely"] if recording_handler else ["stop_safely"]
    reason = (
        "Automatic reproduction did not confirm the bug; provide reproduction details"
        + (" or upload a Playwright recording." if recording_handler else ".")
    )
    publish_workflow_state(
        "WAITING_FOR_MANUAL_REPRODUCTION",
        "Bug Reproduction",
        reason,
        "manual_reproduction",
        actions,
    )
    while time.monotonic() < deadline:
        if stop_requested():
            publish_workflow_state("STOPPED", "Bug Reproduction")
            raise WorkflowStopRequested()
        recording = _consume_signal("recording-submission.json") if recording_handler else None
        if recording is not None:
            submission, error = validate_recording_submission(recording)
            feedback = {"kind": "recording_result", "summary": error, "classification": "REJECTED"}
            if submission is not None:
                confirmed, feedback = recording_handler(submission)
                if confirmed is not None:
                    return {"kind": "browser_recording", "confirmed": confirmed}
            if stop_requested():
                publish_workflow_state("STOPPED", "Bug Reproduction")
                raise WorkflowStopRequested()
            publish_workflow_state(
                "WAITING_FOR_MANUAL_REPRODUCTION",
                "Bug Reproduction",
                "The recording did not confirm the bug. Upload another recording or describe the bug manually.",
                "manual_reproduction",
                actions,
                review=feedback,
            )
            continue
        submission = _read_json(submission_path)
        if submission is not None:
            try:
                submission_path.unlink()
            except FileNotFoundError:
                continue
            validated, _errors = validate_manual_submission(submission)
            if validated is not None:
                return validated
            publish_workflow_state(
                "WAITING_FOR_MANUAL_REPRODUCTION",
                "Bug Reproduction",
                "The submitted reproduction evidence was invalid.",
                "manual_reproduction",
                ["stop_safely"],
            )
        time.sleep(CONTROL_POLL_SECONDS)
    return None


REVIEW_DECISIONS = ("confirm", "revise", "manual")
MAX_REVIEW_FEEDBACK_LENGTH = 4000
MAX_REVIEW_CONTENT_LENGTH = 12000
MAX_REVIEW_OUTPUT_LENGTH = 4000
REVIEW_GUIDANCE = {
    "FAILS_ON_CURRENT_CODE": (
        "The test fails on the current code. Check that it fails because of the "
        "reported bug, then confirm it, or ask for a revision."
    ),
    "PASSES_ON_CURRENT_CODE": (
        "The test passes on the current code, so it does not reproduce the bug. "
        "Describe what it misses, or describe the bug manually."
    ),
    "ERROR": (
        "The test could not run correctly (for example a syntax or import error). "
        "Ask for a revision, or describe the bug manually."
    ),
}


def review_allowed_actions(classification):
    actions = ["revise", "manual", "stop_safely"]
    if classification == "FAILS_ON_CURRENT_CODE":
        actions.insert(0, "confirm")
    return actions


def validate_review_decision(value, allowed_actions):
    """Return (decision, error). Confirm is only valid for a test that fails."""
    if not isinstance(value, dict):
        return None, "Review decision must be an object."
    decision = value.get("decision")
    if decision not in REVIEW_DECISIONS or decision not in allowed_actions:
        return None, "This review decision is not allowed for the current test."
    feedback = value.get("feedback", "")
    if feedback is None:
        feedback = ""
    if not isinstance(feedback, str) or len(feedback) > MAX_REVIEW_FEEDBACK_LENGTH:
        return None, "Feedback must be text of at most 4,000 characters."
    if decision == "revise" and not feedback.strip():
        return None, "Describe what the test should do differently."
    return {"decision": decision, "feedback": feedback.strip()}, None


def wait_for_reproduction_review(artifact, round_number, max_rounds, timeout=CONTROL_WAIT_SECONDS):
    """Pause until the user confirms, revises or rejects the AI reproduction test."""
    directory = control_directory()
    if directory is None:
        return None
    classification = artifact.get("classification")
    run = artifact.get("run") if isinstance(artifact.get("run"), dict) else {}
    allowed_actions = review_allowed_actions(classification)
    stage = f"AI Reproduction Test (Round {round_number})"
    review = {
        "round": round_number,
        "max_rounds": max_rounds,
        "framework": artifact.get("framework"),
        "test_file": artifact.get("test_file"),
        "classification": classification,
        "content": str(artifact.get("content", ""))[:MAX_REVIEW_CONTENT_LENGTH],
        "output_tail": str(run.get("output_tail", ""))[-MAX_REVIEW_OUTPUT_LENGTH:],
        "tests": run.get("tests"),
        "failed": run.get("failed"),
    }
    publish_workflow_state(
        "WAITING_FOR_TEST_REVIEW",
        stage,
        REVIEW_GUIDANCE.get(classification, REVIEW_GUIDANCE["ERROR"]),
        "reproduction_test_review",
        allowed_actions,
        review=review,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop_requested():
            publish_workflow_state("STOPPED", stage)
            raise WorkflowStopRequested(stage)
        signal = _consume_signal("test-review.json")
        if signal is not None:
            decision, error = validate_review_decision(signal, allowed_actions)
            if decision is not None:
                return decision
            publish_workflow_state(
                "WAITING_FOR_TEST_REVIEW",
                stage,
                f"The review was not accepted: {error}",
                "reproduction_test_review",
                allowed_actions,
                review=review,
            )
        time.sleep(CONTROL_POLL_SECONDS)
    return None


FIX_REVIEW_ACTIONS = ["approve", "reject", "stop_safely"]
MAX_FIX_DIFF_LENGTH = 40000


def validate_fix_review_decision(value):
    if not isinstance(value, dict):
        return None, "Fix review decision must be an object."
    decision = value.get("decision")
    if decision not in ("approve", "reject"):
        return None, "Decision must be approve or reject."
    feedback = value.get("feedback", "") or ""
    if not isinstance(feedback, str) or len(feedback) > MAX_REVIEW_FEEDBACK_LENGTH:
        return None, "Feedback must be text of at most 4,000 characters."
    if decision == "reject" and not feedback.strip():
        return None, "Explain why the fix is rejected."
    return {"decision": decision, "feedback": feedback.strip()}, None


def wait_for_fix_review(fix_review, timeout=CONTROL_WAIT_SECONDS):
    """Pause until a human approves or rejects a fix for an unconfirmed bug."""
    directory = control_directory()
    if directory is None:
        return None
    stage = "Human Fix Review"
    review = {
        "kind": "fix",
        "reason": fix_review.get("reason"),
        "files": fix_review.get("files", [])[:30],
        "diff": str(fix_review.get("diff", ""))[:MAX_FIX_DIFF_LENGTH],
        "diff_truncated": bool(fix_review.get("diff_truncated")),
        "checks": fix_review.get("checks", {}),
        "scope_findings": [str(item)[:500] for item in (fix_review.get("scope_findings") or [])[:10]],
    }
    message = fix_review.get("message") or (
        "The bug was never confirmed by an automated test, so the fix needs your "
        "approval. Review the changes and test results, then approve or reject."
    )
    publish_workflow_state(
        "WAITING_FOR_FIX_REVIEW", stage, message, "fix_review", FIX_REVIEW_ACTIONS, review=review,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop_requested():
            publish_workflow_state("STOPPED", stage)
            raise WorkflowStopRequested(stage)
        signal = _consume_signal("fix-review.json")
        if signal is not None:
            decision, error = validate_fix_review_decision(signal)
            if decision is not None:
                return decision
            publish_workflow_state(
                "WAITING_FOR_FIX_REVIEW", stage, f"The review was not accepted: {error}",
                "fix_review", FIX_REVIEW_ACTIONS, review=review,
            )
        time.sleep(CONTROL_POLL_SECONDS)
    return None


def wait_for_blocker_action(
    stage,
    blocker_reason,
    max_retries,
    allowed_actions=None,
    timeout=CONTROL_WAIT_SECONDS,
):
    directory = control_directory()
    if directory is None:
        return "stop"
    allowed_actions = allowed_actions or ["retry", "continue", "stop_safely"]
    deadline = time.monotonic() + timeout
    publish_workflow_state(
        "BLOCKED",
        stage,
        blocker_reason,
        "blocker",
        allowed_actions,
    )
    while time.monotonic() < deadline:
        if stop_requested():
            publish_workflow_state("STOPPED", stage)
            return "stop"
        action = _consume_signal("action.json")
        if action and action.get("action") in {"continue", "retry"}:
            if action["action"] == "retry":
                retry_number = action.get("retry_number")
                if type(retry_number) is not int or not 1 <= retry_number <= max_retries:
                    publish_workflow_state(
                        "BLOCKED",
                        stage,
                        "The blocker retry limit has been reached.",
                        "blocker",
                        [action for action in allowed_actions if action != "retry"],
                    )
                    continue
            return action["action"]
        time.sleep(CONTROL_POLL_SECONDS)
    publish_workflow_state(
        "BLOCKED",
        stage,
        "Human action timed out; no later stage was started.",
        "blocker",
        allowed_actions,
    )
    return "stop"
