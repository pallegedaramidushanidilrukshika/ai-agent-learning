import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from workflow_control import (
    CONTROL_DIRECTORY_ENV,
    WORKFLOW_ID_ENV,
    WORKFLOW_ID_PATTERN,
    WORKFLOW_RUN_DIRECTORY_ENV,
    _atomic_json,
    _read_json,
    validate_fix_review_decision,
    validate_manual_submission,
    validate_recording_submission,
    validate_review_decision,
)
from browser_recording import RecordingError, parse_recording
from workflow_validation import project_process_environment
from evidence_files import EvidenceError, public_summary, store_attachments, validate_attachments


PROJECT_DIR = Path(__file__).resolve().parent
ORCHESTRATOR = PROJECT_DIR / "workflow_orchestrator.py"
MAX_REQUEST_BYTES = 32_768
# Only the start and evidence-upload routes carry attached files (text,
# at most 1 MB in total; JSON escaping can roughly double that).
EVIDENCE_REQUEST_BYTES = 3 * 1024 * 1024
# A recording is at most 64 KB of text; JSON escaping can roughly double it.
RECORDING_REQUEST_BYTES = 160 * 1024
MAX_TITLE_LENGTH = 180
MAX_DESCRIPTION_LENGTH = 8_000
MAX_RETRIES = 2
# Browser replay evidence the console may show or download. Only these exact
# names in the run folder are served; nothing else in the run is readable.
REPLAY_EVIDENCE_PATTERN = re.compile(
    r"browser_replay_(?:before_fix|after_fix_attempt_[1-9][0-9]?)_(?:failure\.png|trace\.zip)"
)
REPLAY_EVIDENCE_TYPES = {".png": "image/png", ".zip": "application/zip"}
MAX_REPLAY_EVIDENCE_BYTES = 50 * 1024 * 1024
TERMINAL_STATUSES = {"PASS", "FAIL", "FAILED", "STOPPED", "BLOCKED_FINAL", "UNCONFIRMED", "NEEDS_REVIEW"}
# Written once the orchestrator process has exited; no further actions apply.
TERMINAL_STATE_FIELDS = {
    "terminal": True,
    "pause_kind": None,
    "allowed_actions": [],
}
ALLOWED_ORIGINS = {
    "http://127.0.0.1:5173",
    "http://localhost:5173",
}
AWS_CREDENTIAL_NAMES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
)


class RequestError(ValueError):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


class WorkflowManager:
    def __init__(self, project_dir=PROJECT_DIR, process_factory=None, worker_launcher=None):
        self.project_dir = Path(project_dir).resolve()
        self.runs_dir = self.project_dir / "workflow_runs"
        self.process_factory = process_factory or subprocess.Popen
        self.worker_launcher = worker_launcher or self._launch_thread
        self.lock = threading.RLock()
        self.runs = {}
        self.requests = {}

    def _launch_thread(self, target):
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread

    def _allowed_roots(self):
        values = [self.project_dir]
        configured = os.environ.get("WORKFLOW_ALLOWED_REPOSITORY_ROOTS", "")
        for entry in configured.split(os.pathsep):
            if entry.strip():
                values.append(Path(entry.strip()).expanduser().resolve())
        return tuple(dict.fromkeys(values))

    def _validate_repository_path(self, value):
        if not isinstance(value, str) or not value.strip() or len(value) > 4096:
            raise RequestError(400, "Repository path is required and must be at most 4,096 characters.")
        repository = Path(value).expanduser()
        if not repository.is_absolute():
            raise RequestError(400, "Repository path must be absolute.")
        try:
            repository = repository.resolve(strict=True)
        except (OSError, RuntimeError):
            raise RequestError(400, "Repository path does not exist or cannot be resolved.") from None
        if not repository.is_dir():
            raise RequestError(400, "Repository path must be an existing directory.")
        if not any(repository == root or root in repository.parents for root in self._allowed_roots()):
            raise RequestError(403, "Repository path is outside the configured allowed roots.")
        return repository

    @staticmethod
    def _validate_request_id(value):
        if not isinstance(value, str) or len(value) != 36:
            raise RequestError(400, "request_id must be a UUID.")
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError):
            raise RequestError(400, "request_id must be a UUID.") from None
        if str(parsed) != value.lower():
            raise RequestError(400, "request_id must be a canonical UUID.")
        return str(parsed)

    def start(self, payload):
        if not isinstance(payload, dict):
            raise RequestError(400, "Request body must be a JSON object.")
        request_id = self._validate_request_id(payload.get("request_id"))
        with self.lock:
            existing_id = self.requests.get(request_id)
            if existing_id:
                return self.get(existing_id), False

        repository = self._validate_repository_path(payload.get("repository_path"))
        title = payload.get("bug_title")
        description = payload.get("bug_description")
        if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_LENGTH:
            raise RequestError(400, "Bug title is required and must be at most 180 characters.")
        if not isinstance(description, str) or not description.strip() or len(description) > MAX_DESCRIPTION_LENGTH:
            raise RequestError(400, "Bug description is required and must be at most 8,000 characters.")
        if payload.get("allow_local_commands") is not True:
            raise RequestError(403, "Explicit approval to run repository tests/builds is required.")
        if payload.get("allow_ai") is not True:
            raise RequestError(403, "Explicit approval for AI API calls is required by the current workflow.")

        manual_submission = None
        if payload.get("manual_reproduction") is not None:
            manual_submission, errors = validate_manual_submission(
                payload["manual_reproduction"]
            )
            if errors:
                raise RequestError(400, "; ".join(errors))

        attachments = payload.get("attachments")
        if attachments is not None:
            try:
                validate_attachments(attachments)
            except EvidenceError as error:
                raise RequestError(400, str(error)) from None

        workflow_id = uuid.uuid4().hex
        run_directory = self.runs_dir / workflow_id
        control_directory = run_directory / ".control"
        control_directory.mkdir(parents=True, exist_ok=False)
        bug_report_path = control_directory / "bug-report.json"
        bug_report = {
            "bug_title": title.strip(),
            "bug_description": description.strip(),
            "reproduction": {
                "steps": manual_submission["steps"] if manual_submission else [],
                "expected_behavior": manual_submission["expected_behavior"] if manual_submission else "",
                "observed_behavior": manual_submission["observed_behavior"] if manual_submission else "",
                "supporting_evidence": manual_submission["supporting_evidence"] if manual_submission else [],
            },
            "ui_submission": True,
        }
        _atomic_json(bug_report_path, bug_report)
        _atomic_json(control_directory / "state.json", {
            "workflow_id": workflow_id,
            "request_id": request_id,
            "status": "QUEUED",
            "stage": "Queued",
            "blocker_reason": None,
            "pause_kind": None,
            "retry_count": 0,
            "created_at": time.time(),
            "updated_at": time.time(),
        })
        if manual_submission:
            _atomic_json(control_directory / "manual-submission.json", manual_submission)
        if attachments:
            # Stored before the workflow starts, so the first AI reproduction
            # test already sees them.
            store_attachments(str(run_directory), attachments)

        record = {
            "workflow_id": workflow_id,
            "request_id": request_id,
            "repository_path": str(repository),
            "run_directory": run_directory,
            "control_directory": control_directory,
            "bug_report_path": bug_report_path,
            "allow_ai": True,
            "allow_local_commands": True,
            "process": None,
        }
        with self.lock:
            raced_id = self.requests.get(request_id)
            if raced_id:
                return self.get(raced_id), False
            self.runs[workflow_id] = record
            self.requests[request_id] = workflow_id
        self.worker_launcher(lambda: self._run(record))
        return self.get(workflow_id), True

    def _run(self, record):
        environment = project_process_environment()
        for name in AWS_CREDENTIAL_NAMES:
            environment.pop(name, None)
        environment.update({
            "WORKFLOW_AI_APPROVED": "true",
            "WORKFLOW_LOCAL_COMMANDS_APPROVED": "true",
            WORKFLOW_ID_ENV: record["workflow_id"],
            CONTROL_DIRECTORY_ENV: str(record["control_directory"]),
            WORKFLOW_RUN_DIRECTORY_ENV: str(record["run_directory"]),
            "BUG_REPORT_FILE": str(record["bug_report_path"]),
            "LOCAL_REPOSITORY_PATH": record["repository_path"],
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_EC2_METADATA_DISABLED": "true",
        })
        command = [
            sys.executable,
            str(ORCHESTRATOR),
            "--local-repository",
            record["repository_path"],
            str(record["bug_report_path"]),
        ]
        try:
            log_path = record["run_directory"] / "workflow.log"
            with log_path.open("x", encoding="utf-8") as log_file:
                process = self.process_factory(
                    command,
                    cwd=str(self.project_dir),
                    env=environment,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    shell=False,
                )
                record["process"] = process
                return_code = process.wait()
            report_path = record["run_directory"] / "workflow_report.json"
            report = _read_json(report_path)
            with self.lock:
                self._finish(record, report, report_path, return_code)
        except Exception as error:
            with self.lock:
                state_path = record["control_directory"] / "state.json"
                state = _read_json(state_path) or {}
                state.update({
                    "status": "FAILED",
                    "blocker_reason": f"Workflow process failed ({type(error).__name__}).",
                    **TERMINAL_STATE_FIELDS,
                    "updated_at": time.time(),
                })
                _atomic_json(state_path, state)

    def _finish(self, record, report, report_path, return_code):
        state_path = record["control_directory"] / "state.json"
        state = _read_json(state_path) or {}
        if report:
            # The orchestrator's report is authoritative, but PASS also
            # requires a clean process exit.
            final_status = report.get("final_status")
            if not isinstance(final_status, str) or not final_status:
                final_status = "FAIL"
            if final_status == "PASS" and return_code != 0:
                final_status = "FAIL"
            state.update({
                "status": final_status,
                "stage": "Workflow complete",
                "blocker_reason": (
                    "Report said PASS but the workflow process exited with an error."
                    if report.get("final_status") == "PASS" and final_status != "PASS"
                    else None
                ),
                "return_code": return_code,
                **TERMINAL_STATE_FIELDS,
                "updated_at": time.time(),
            })
        elif state.get("status") in {"STOPPING", "STOPPED"}:
            state.update({
                "status": "STOPPED",
                "stage": state.get("stage", "Stopped safely"),
                "return_code": return_code,
                **TERMINAL_STATE_FIELDS,
                "updated_at": time.time(),
            })
            _atomic_json(report_path, {
                "workflow": "AI Agentic Software Debugging Workflow",
                "target_repository": record["repository_path"],
                "workflow_id": record["workflow_id"],
                "stages": [],
                "retry_history": [],
                "final_status": "STOPPED",
                "deployment_status": "NOT_DEPLOYED",
                "deployment_readiness": "NOT_ASSESSED",
                "stop_reason": "Stopped safely by user; completed artifacts are preserved.",
            })
        else:
            state.update({
                "status": "FAIL",
                "blocker_reason": "Workflow exited without a final report.",
                "return_code": return_code,
                **TERMINAL_STATE_FIELDS,
                "updated_at": time.time(),
            })
        _atomic_json(state_path, state)

    def get(self, workflow_id):
        if not isinstance(workflow_id, str) or not WORKFLOW_ID_PATTERN.fullmatch(workflow_id):
            raise RequestError(400, "Invalid workflow ID.")
        record = self.runs.get(workflow_id)
        if record is None:
            path = self.runs_dir / workflow_id / ".control" / "state.json"
            state = _read_json(path)
            if state is None or state.get("workflow_id") != workflow_id:
                raise RequestError(404, "Workflow was not found.")
            report_path = path.parents[1] / "workflow_report.json"
            report = _read_json(report_path)
            return self._public_status(
                workflow_id, state, report, path.parents[1], live=False
            )
        state = _read_json(record["control_directory"] / "state.json") or {}
        report = _read_json(record["run_directory"] / "workflow_report.json")
        return self._public_status(
            workflow_id, state, report, record["run_directory"], live=True
        )

    @staticmethod
    def _public_status(workflow_id, state, report, run_directory, live):
        # Live progress comes from the control state; once the run has
        # finished, the final report is the authoritative stage record.
        stages = (
            report.get("stages", [])
            if isinstance(report, dict)
            else state.get("stages", [])
        )
        if not isinstance(stages, list):
            stages = []
        safe_stages = [
            {"stage": item.get("stage"), "name": item.get("name"), "status": item.get("status")}
            for item in stages
            if isinstance(item, dict)
        ]
        final_report = None
        if isinstance(report, dict):
            diagnosis = report.get("diagnosis")
            final_report = {
                "final_status": report.get("final_status"),
                "technology": report.get("detected_technology"),
                "reproduction_source": report.get("reproduction_source"),
                "root_cause": diagnosis.get("root_cause") if isinstance(diagnosis, dict) else None,
                "stages": safe_stages,
                "retry_history": report.get("retry_history", []),
                "deployment_status": report.get("deployment_status", "NOT_DEPLOYED"),
                "deployment_readiness": report.get("deployment_readiness", "NOT_ASSESSED"),
                "reproduction_confirmed": report.get("reproduction_confirmed"),
                "ai_limit": str(report["ai_limit"])[:500] if report.get("ai_limit") else None,
                "fix_review": (
                    {key: report["fix_review"].get(key) for key in ("decision", "feedback", "reviewed_by")}
                    if isinstance(report.get("fix_review"), dict)
                    else None
                ),
            }
        status = state.get("status", "UNKNOWN")
        # For a live run, only the API marks completion (after the process
        # exits); the orchestrator can publish STOPPED before its report is
        # written. Runs from an earlier API session fall back to the status.
        terminal = state.get("terminal") is True or (
            not live and status in TERMINAL_STATUSES
        )
        blocker_reason = state.get("blocker_reason")
        if not live and not terminal:
            # The API that ran this workflow has restarted, so nothing can
            # finish or control it any more. Show that instead of an
            # endless "running" state.
            status = "INTERRUPTED"
            terminal = True
            blocker_reason = (
                "This run was interrupted: the workflow API stopped while it was running. "
                "Completed stages and artifacts are kept in the run folder."
            )
        retry_count = state.get("retry_count", 0)
        allowed_actions = state.get("allowed_actions", [])
        if terminal or not isinstance(allowed_actions, list):
            allowed_actions = []
        if type(retry_count) is not int or retry_count >= MAX_RETRIES:
            # The retry budget is shared across blockers; never offer a
            # Retry the API would reject.
            allowed_actions = [action for action in allowed_actions if action != "retry"]
        return {
            "workflow_id": workflow_id,
            "status": status,
            "terminal": terminal,
            "stage": state.get("stage", "Unknown"),
            "blocker_reason": blocker_reason,
            "pause_kind": None if terminal else state.get("pause_kind"),
            "allowed_actions": allowed_actions,
            "retry_count": retry_count,
            "max_retries": MAX_RETRIES,
            "stages": safe_stages,
            "report": final_report,
            "review": WorkflowManager._public_review(state, terminal),
            "evidence": public_summary(str(run_directory)),
            "replay_evidence": WorkflowManager._replay_evidence(run_directory),
            "artifact_directory": str(run_directory),
        }

    @staticmethod
    def _replay_evidence(run_directory):
        """Failure screenshots and traces saved by browser replays, oldest first."""
        items = []
        try:
            entries = list(Path(run_directory).iterdir())
        except OSError:
            return items
        for path in entries:
            if not REPLAY_EVIDENCE_PATTERN.fullmatch(path.name) or path.is_symlink():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.is_file():
                items.append({
                    "name": path.name,
                    "kind": "screenshot" if path.suffix == ".png" else "trace",
                    "bytes": stat.st_size,
                    "modified": stat.st_mtime,
                })
        items.sort(key=lambda item: (item["modified"], item["name"]))
        return [{key: item[key] for key in ("name", "kind", "bytes")} for item in items]

    def _run_directory(self, workflow_id):
        if not isinstance(workflow_id, str) or not WORKFLOW_ID_PATTERN.fullmatch(workflow_id):
            raise RequestError(400, "Invalid workflow ID.")
        record = self.runs.get(workflow_id)
        if record is not None:
            return Path(record["run_directory"])
        state = _read_json(self.runs_dir / workflow_id / ".control" / "state.json")
        if state is None or state.get("workflow_id") != workflow_id:
            raise RequestError(404, "Workflow was not found.")
        return self.runs_dir / workflow_id

    def replay_evidence_file(self, workflow_id, name):
        """Bytes and content type of one replay screenshot or trace."""
        if not isinstance(name, str) or not REPLAY_EVIDENCE_PATTERN.fullmatch(name):
            raise RequestError(404, "File was not found.")
        run_directory = self._run_directory(workflow_id)
        path = run_directory / name
        try:
            if path.is_symlink() or not path.is_file():
                raise RequestError(404, "File was not found.")
            if path.resolve().parent != run_directory.resolve():
                raise RequestError(404, "File was not found.")
            if path.stat().st_size > MAX_REPLAY_EVIDENCE_BYTES:
                raise RequestError(413, "File is too large to download from the console.")
            content = path.read_bytes()
        except OSError:
            raise RequestError(404, "File was not found.") from None
        return content, REPLAY_EVIDENCE_TYPES[path.suffix]

    @staticmethod
    def _public_review(state, terminal):
        """The AI reproduction test or the fix awaiting review, if any."""
        review = state.get("review")
        if terminal or not isinstance(review, dict):
            return None
        if state.get("pause_kind") == "fix_review":
            return {
                key: review.get(key)
                for key in ("kind", "reason", "files", "diff", "diff_truncated", "checks", "scope_findings")
            }
        if state.get("pause_kind") == "manual_reproduction":
            if review.get("kind") != "recording_result":
                return None
            return {
                key: review.get(key)
                for key in ("kind", "filename", "classification", "summary", "steps")
            }
        if state.get("pause_kind") != "reproduction_test_review":
            return None
        return {
            key: review.get(key)
            for key in (
                "round", "max_rounds", "framework", "test_file",
                "classification", "content", "output_tail", "tests", "failed",
            )
        }

    def submit_manual_reproduction(self, workflow_id, payload):
        # One lock serializes state checks and signal writes so concurrent
        # requests cannot both pass the same state check.
        with self.lock:
            return self._submit_manual_reproduction(workflow_id, payload)

    def _submit_manual_reproduction(self, workflow_id, payload):
        status = self.get(workflow_id)
        if not isinstance(payload, dict):
            raise RequestError(400, "Request body must be a JSON object.")
        submission_id = payload.get("submission_id")
        self._validate_request_id(submission_id)
        state_path = self.runs_dir / workflow_id / ".control" / "state.json"
        current_state = _read_json(state_path) or {}
        if current_state.get("last_submission_id") == submission_id:
            return self.get(workflow_id)
        if status["status"] != "WAITING_FOR_MANUAL_REPRODUCTION":
            raise RequestError(409, "Manual reproduction can only be submitted while the workflow is waiting for it.")
        submission, errors = validate_manual_submission(payload.get("manual_reproduction"))
        if errors:
            raise RequestError(400, "; ".join(errors))
        control_path = self.runs_dir / workflow_id / ".control"
        if (control_path / "manual-submission.json").exists():
            raise RequestError(409, "A manual reproduction submission is already pending.")
        state = _read_json(state_path) or {}
        state.update({
            "status": "SUBMISSION_RECEIVED",
            "last_submission_id": submission_id,
            "updated_at": time.time(),
        })
        self._update_state_then_signal(
            state_path, state, control_path / "manual-submission.json", submission
        )
        return self.get(workflow_id)

    @staticmethod
    def _update_state_then_signal(state_path, state, signal_path, signal_value):
        # State goes first: once the signal exists the orchestrator may consume
        # it and publish its next state at any moment, which a later API state
        # write would otherwise overwrite with stale data.
        previous = _read_json(state_path)
        _atomic_json(state_path, state)
        try:
            _atomic_json(signal_path, signal_value)
        except OSError:
            if previous is not None:
                _atomic_json(state_path, previous)
            raise RequestError(503, "The workflow control signal could not be written.") from None

    def submit_fix_review(self, workflow_id, payload):
        with self.lock:
            current = self.get(workflow_id)
            if not isinstance(payload, dict):
                raise RequestError(400, "Request body must be a JSON object.")
            review_id = self._validate_request_id(payload.get("review_id"))
            control_path = self.runs_dir / workflow_id / ".control"
            state_path = control_path / "state.json"
            state = _read_json(state_path) or {}
            if state.get("last_fix_review_id") == review_id:
                return self.get(workflow_id)
            if current["terminal"] or current["status"] != "WAITING_FOR_FIX_REVIEW":
                raise RequestError(409, "A fix review can only be submitted while a fix is waiting for review.")
            decision, error = validate_fix_review_decision(
                {"decision": payload.get("decision"), "feedback": payload.get("feedback", "")}
            )
            if error:
                raise RequestError(400, error)
            if (control_path / "fix-review.json").exists():
                raise RequestError(409, "A fix review is already pending.")
            state.update({
                "status": "REVIEW_SUBMITTED",
                "last_fix_review_id": review_id,
                "updated_at": time.time(),
            })
            self._update_state_then_signal(
                state_path, state, control_path / "fix-review.json", dict(decision, review_id=review_id),
            )
            return self.get(workflow_id)

    def upload_evidence(self, workflow_id, payload):
        with self.lock:
            current = self.get(workflow_id)
            if not isinstance(payload, dict):
                raise RequestError(400, "Request body must be a JSON object.")
            upload_id = self._validate_request_id(payload.get("upload_id"))
            state_path = self.runs_dir / workflow_id / ".control" / "state.json"
            state = _read_json(state_path) or {}
            if upload_id in (state.get("evidence_upload_ids") or []):
                return self.get(workflow_id)
            if current["terminal"]:
                raise RequestError(409, "Evidence can only be attached while the workflow is running.")
            try:
                store_attachments(str(self.runs_dir / workflow_id), payload.get("attachments"))
            except EvidenceError as error:
                raise RequestError(400, str(error)) from None
            state = _read_json(state_path) or {}
            state["evidence_upload_ids"] = (state.get("evidence_upload_ids") or [])[-20:] + [upload_id]
            state["updated_at"] = time.time()
            _atomic_json(state_path, state)
            return self.get(workflow_id)

    def submit_reproduction_review(self, workflow_id, payload):
        with self.lock:
            return self._submit_reproduction_review(workflow_id, payload)

    def _submit_reproduction_review(self, workflow_id, payload):
        current = self.get(workflow_id)
        if not isinstance(payload, dict):
            raise RequestError(400, "Request body must be a JSON object.")
        review_id = self._validate_request_id(payload.get("review_id"))
        control_path = self.runs_dir / workflow_id / ".control"
        state_path = control_path / "state.json"
        state = _read_json(state_path) or {}
        if state.get("last_review_id") == review_id:
            return self.get(workflow_id)
        if current["terminal"] or current["status"] != "WAITING_FOR_TEST_REVIEW":
            raise RequestError(409, "A review can only be submitted while a reproduction test is waiting for review.")
        decision, error = validate_review_decision(
            {"decision": payload.get("decision"), "feedback": payload.get("feedback", "")},
            current.get("allowed_actions") or [],
        )
        if error:
            status = 409 if payload.get("decision") in ("confirm", "revise", "manual") and "not allowed" in error else 400
            raise RequestError(status, error)
        if (control_path / "test-review.json").exists():
            raise RequestError(409, "A review is already pending.")
        state.update({
            "status": "REVIEW_SUBMITTED",
            "last_review_id": review_id,
            "updated_at": time.time(),
        })
        self._update_state_then_signal(
            state_path,
            state,
            control_path / "test-review.json",
            dict(decision, review_id=review_id),
        )
        return self.get(workflow_id)

    def submit_recording(self, workflow_id, payload):
        with self.lock:
            current = self.get(workflow_id)
            if not isinstance(payload, dict):
                raise RequestError(400, "Request body must be a JSON object.")
            submission_id = self._validate_request_id(payload.get("submission_id"))
            control_path = self.runs_dir / workflow_id / ".control"
            state_path = control_path / "state.json"
            state = _read_json(state_path) or {}
            if state.get("last_recording_id") == submission_id:
                return self.get(workflow_id)
            if (
                current["terminal"]
                or current["status"] != "WAITING_FOR_MANUAL_REPRODUCTION"
                or "upload_recording" not in (current.get("allowed_actions") or [])
            ):
                raise RequestError(409, "A recording can only be uploaded while the workflow is waiting for reproduction details.")
            submission, error = validate_recording_submission(payload.get("recording"))
            if error:
                raise RequestError(400, error)
            # Reject unsafe or unsupported scripts here, so the user sees the
            # problem at once; the orchestrator validates again before replay.
            try:
                parse_recording(submission["content"], submission["filename"])
            except RecordingError as rejected:
                raise RequestError(400, "Recording rejected: " + "; ".join(rejected.errors)) from None
            if (control_path / "recording-submission.json").exists():
                raise RequestError(409, "A recording is already being replayed.")
            state.update({
                "status": "RECORDING_SUBMITTED",
                "pause_kind": None,
                "blocker_reason": None,
                "last_recording_id": submission_id,
                "allowed_actions": ["stop_safely"],
                "updated_at": time.time(),
            })
            self._update_state_then_signal(
                state_path, state, control_path / "recording-submission.json", submission,
            )
            return self.get(workflow_id)

    def action(self, workflow_id, payload):
        with self.lock:
            return self._action(workflow_id, payload)

    def _action(self, workflow_id, payload):
        current = self.get(workflow_id)
        if not isinstance(payload, dict):
            raise RequestError(400, "Request body must be a JSON object.")
        action_id = self._validate_request_id(payload.get("action_id"))
        action = payload.get("action")
        control_path = self.runs_dir / workflow_id / ".control"
        state_path = control_path / "state.json"
        state = _read_json(state_path) or {}
        if state.get("last_action_id") == action_id:
            return self.get(workflow_id)
        if action == "stop_safely":
            if current["terminal"] or current["status"] in TERMINAL_STATUSES:
                raise RequestError(409, "Workflow is already terminal.")
            state.update({
                "status": "STOPPING",
                "last_action_id": action_id,
                "updated_at": time.time(),
            })
            self._update_state_then_signal(
                state_path, state, control_path / "stop.request", {"action_id": action_id}
            )
            return self.get(workflow_id)

        if action not in {"retry", "continue"}:
            raise RequestError(400, "Action must be retry, continue, or stop_safely.")
        if current["terminal"] or current["status"] != "BLOCKED":
            raise RequestError(409, "Retry or Continue is only valid while the workflow is blocked.")
        allowed_actions = current.get("allowed_actions")
        if isinstance(allowed_actions, list) and action not in allowed_actions:
            raise RequestError(409, "This action is not allowed for the current blocker.")
        retry_count = state.get("retry_count", 0)
        if action == "retry":
            if type(retry_count) is not int or retry_count >= MAX_RETRIES:
                raise RequestError(409, "The blocker retry limit has been reached.")
            retry_count += 1
        action_value = {"action": action, "retry_number": retry_count, "action_id": action_id}
        if (control_path / "action.json").exists():
            raise RequestError(409, "Another blocker action is already pending.")
        state.update({
            "status": "ACTION_SUBMITTED",
            "retry_count": retry_count,
            "last_action_id": action_id,
            "updated_at": time.time(),
        })
        self._update_state_then_signal(
            state_path, state, control_path / "action.json", action_value
        )
        return self.get(workflow_id)


def _json_object_without_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON fields are not allowed.")
        result[key] = value
    return result


def make_handler(manager):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WorkflowControl/1.0"

        def log_message(self, _format, *_args):
            return

        def _send(self, status, body):
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            if self.close_connection:
                self.send_header("Connection", "close")
            origin = self.headers.get("Origin")
            if origin in ALLOWED_ORIGINS:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(encoded)

        def _send_file(self, content, content_type, name):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            disposition = "inline" if content_type == "image/png" else "attachment"
            self.send_header("Content-Disposition", f'{disposition}; filename="{name}"')
            self.end_headers()
            self.wfile.write(content)

        def _body(self, limit=MAX_REQUEST_BYTES):
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                raise RequestError(400, "Invalid Content-Length.") from None
            if length < 0 or length > limit:
                remaining = min(max(length, 0), limit + 1)
                while remaining:
                    chunk = self.rfile.read(min(8192, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                self.close_connection = True
                raise RequestError(413, f"Request body must be between 0 and {limit // 1024} KB.")
            try:
                return json.loads(
                    self.rfile.read(length).decode("utf-8"),
                    object_pairs_hook=_json_object_without_duplicate_keys,
                )
            except (UnicodeError, json.JSONDecodeError, ValueError) as error:
                raise RequestError(400, str(error) or "Invalid JSON request.") from None

        def do_OPTIONS(self):
            origin = self.headers.get("Origin")
            if origin not in ALLOWED_ORIGINS:
                self._send(403, {"error": "Origin is not allowed."})
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Vary", "Origin")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/api/health":
                self._send(200, {"status": "running", "service": "workflow-control"})
                return
            prefix = "/api/workflows/"
            parts = path[len(prefix):].split("/") if path.startswith(prefix) else []
            if len(parts) == 3 and parts[1] == "files":
                try:
                    content, content_type = manager.replay_evidence_file(parts[0], parts[2])
                except RequestError as error:
                    self._send(error.status_code, {"error": str(error)})
                    return
                self._send_file(content, content_type, parts[2])
                return
            if path.startswith(prefix) and path.count("/") == 3:
                try:
                    self._send(200, manager.get(path[len(prefix):]))
                except RequestError as error:
                    self._send(error.status_code, {"error": str(error)})
                return
            self._send(404, {"error": "Route not found."})

        def do_POST(self):
            path = urlsplit(self.path).path
            try:
                carries_files = path == "/api/workflows" or (
                    path.startswith("/api/workflows/") and path.endswith("/evidence")
                )
                carries_recording = path.startswith("/api/workflows/") and path.endswith("/recording")
                payload = self._body(
                    EVIDENCE_REQUEST_BYTES if carries_files
                    else RECORDING_REQUEST_BYTES if carries_recording
                    else MAX_REQUEST_BYTES
                )
                origin = self.headers.get("Origin")
                if origin is not None and origin not in ALLOWED_ORIGINS:
                    raise RequestError(403, "Origin is not allowed.")
                if path == "/api/workflows":
                    result, created = manager.start(payload)
                    self._send(202 if created else 200, result)
                    return
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[0:2] == ["api", "workflows"]:
                    workflow_id, operation = parts[2], parts[3]
                    if operation == "manual-reproduction":
                        self._send(202, manager.submit_manual_reproduction(workflow_id, payload))
                        return
                    if operation == "actions":
                        self._send(202, manager.action(workflow_id, payload))
                        return
                    if operation == "reproduction-review":
                        self._send(202, manager.submit_reproduction_review(workflow_id, payload))
                        return
                    if operation == "evidence":
                        self._send(202, manager.upload_evidence(workflow_id, payload))
                        return
                    if operation == "fix-review":
                        self._send(202, manager.submit_fix_review(workflow_id, payload))
                        return
                    if operation == "recording":
                        self._send(202, manager.submit_recording(workflow_id, payload))
                        return
                self._send(404, {"error": "Route not found."})
            except RequestError as error:
                self._send(error.status_code, {"error": str(error)})

    return Handler


def main():
    port_value = os.environ.get("WORKFLOW_UI_API_PORT", "8765")
    try:
        port = int(port_value)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SystemExit("WORKFLOW_UI_API_PORT must be 1..65535.") from None
    manager = WorkflowManager()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(manager))
    server.daemon_threads = True
    print(f"Workflow UI API listening on http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping workflow UI API; active workflows are not force-terminated.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()