import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from datetime import datetime

from workflow_artifacts import ARTIFACT_DIR_ENV, resolve_artifact_path
from workflow_control import (
    CONTROL_DIRECTORY_ENV,
    WORKFLOW_ID_ENV,
    WORKFLOW_RUN_DIRECTORY_ENV,
    WorkflowStopRequested,
    check_stop_requested,
    control_enabled,
    control_directory,
    publish_workflow_state,
    record_stage,
    recording_feedback,
    wait_for_blocker_action,
    wait_for_fix_review,
    wait_for_manual_reproduction,
    wait_for_reproduction_review,
)
from workflow_validation import (
    BUILD_COMMAND_TIMEOUT_SECONDS,
    run_bounded,
    build_result_has_success_evidence,
    diagnosis_validation_errors,
    project_process_environment,
    runtime_result_has_success_evidence,
    test_result_has_success_evidence,
)
from local_websocket_demo import run_level3_websocket_demo
import browser_recording
import reproduction_tests

# Agent output contains symbols such as the Jest marker; a cp1252 console or
# log pipe cannot encode them, and print() would crash the whole workflow.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# ============================================================
# BASE CONFIGURATION
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_RETRIES = 2
# Rounds of "AI writes a reproduction test, human reviews it".
MAX_REPRODUCTION_TEST_ROUNDS = 3
AI_REPRODUCTION_SOURCE = "ai_reproduction_test"
# A Playwright codegen recording the user uploaded, replayed as steps.
BROWSER_RECORDING_SOURCE = "browser_recording"
# Sources whose reproduction is an executable check that must pass after the fix.
CONFIRMED_SOURCES = ("automatic", AI_REPRODUCTION_SOURCE, BROWSER_RECORDING_SOURCE)


# ============================================================
# WORKFLOW ARTIFACT FILES
# ============================================================

BUG_REPORT_FILE = os.path.join(
    BASE_DIR,
    "bug_report.json"
)

REPOSITORY_CONTEXT_FILE = resolve_artifact_path("repository_context.json", BASE_DIR)
REPRODUCTION_RESULT_FILE = resolve_artifact_path("reproduction_result.json", BASE_DIR)
MANUAL_REPRODUCTION_FILE = resolve_artifact_path("manual_reproduction.json", BASE_DIR)
REPRODUCTION_TEST_FILE = resolve_artifact_path("reproduction_test.json", BASE_DIR)
CONFIRMED_REPRODUCTION_TEST_FILE = resolve_artifact_path(
    "confirmed_reproduction_test.json", BASE_DIR
)
REPRODUCTION_TEST_FEEDBACK_FILE = resolve_artifact_path(
    "reproduction_test_feedback.json", BASE_DIR
)
CODE_FIX_ORIGINALS_FILE = resolve_artifact_path("code_fix_originals.json", BASE_DIR)
FIX_BASELINE_FILE = resolve_artifact_path("fix_baseline.json", BASE_DIR)
FIX_REVIEW_FILE = resolve_artifact_path("fix_review.json", BASE_DIR)

DIAGNOSIS_FILE = resolve_artifact_path("diagnosis.json", BASE_DIR)

CODE_FIX_RESULT_FILE = resolve_artifact_path("code_fix_result.json", BASE_DIR)

TEST_GENERATION_RESULT_FILE = resolve_artifact_path(
    "test_generation_result.json",
    BASE_DIR,
)

TEST_RESULT_FILE = resolve_artifact_path("test_result.json", BASE_DIR)

BUILD_RESULT_FILE = resolve_artifact_path("build_result.json", BASE_DIR)

VALIDATION_RESULT_FILE = resolve_artifact_path("validation_result.json", BASE_DIR)

VERIFICATION_REPORT_FILE = resolve_artifact_path("verification_report.json", BASE_DIR)

WORKFLOW_REPORT_FILE = resolve_artifact_path("workflow_report.json", BASE_DIR)

ACTIVE_BUG_REPORT_FILE = BUG_REPORT_FILE
ACTIVE_RUN_ARTIFACT_DIR = None
CHILD_ENV_OVERRIDES = {}
# Progress of the current run, so a Stop Safely report keeps completed stages.
ACTIVE_WORKFLOW_PROGRESS = None


# ============================================================
# STALE ARTIFACTS
# ============================================================

STALE_ARTIFACTS = (
    REPOSITORY_CONTEXT_FILE,
    REPRODUCTION_RESULT_FILE,
    DIAGNOSIS_FILE,
    TEST_RESULT_FILE,
    VERIFICATION_REPORT_FILE,
    WORKFLOW_REPORT_FILE,
    BUILD_RESULT_FILE,
    VALIDATION_RESULT_FILE,
    CODE_FIX_RESULT_FILE,
)


# ============================================================
# AGENTS
# ============================================================

AGENTS = {
    "repository_analysis": "repository_agent.py",
    "bug_reproduction": "bug_reproduction_agent.py",
    "bug_diagnosis": "bug_diagnosis_agent.py",
    "code_fix": "code_fix_agent.py",
    "test_generation": "test_generation_agent.py",
    "test_execution": "test_result_checker.py",
    "verification": "verification_agent.py",
    "reproduction_test": "reproduction_test_agent.py",
    "browser_replay": "browser_replay_agent.py",
}


# ============================================================
# DISPLAY HELPERS
# ============================================================

def print_header(title):
    print(f"\n{'=' * 75}\n{title}\n{'=' * 75}")


def print_stage(number, name):
    if control_enabled():
        if check_stop_requested(name):
            raise WorkflowStopRequested(name)
        publish_workflow_state("RUNNING", name)
        record_stage(number, name, "RUNNING")
    print(f"\n{'-' * 75}\nSTAGE {number}: {name}\n{'-' * 75}")


# ============================================================
# JSON HELPERS
# ============================================================

def load_json(path):
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            indent=4,
            ensure_ascii=False
        )


# ============================================================
# CLEAN OLD WORKFLOW ARTIFACTS
# ============================================================

def remove_stale_artifacts():
    existing_paths = [path for path in STALE_ARTIFACTS if os.path.isfile(path)]
    archive_directory = os.path.join(
        BASE_DIR,
        "workflow_artifact_archive",
        datetime.now().strftime("%Y%m%d_%H%M%S_%f"),
    )

    if existing_paths:
        for path in existing_paths:
            relative_path = os.path.relpath(path, BASE_DIR)
            archive_path = os.path.join(archive_directory, relative_path)
            os.makedirs(os.path.dirname(archive_path), exist_ok=True)
            shutil.copy2(path, archive_path)

    for path in STALE_ARTIFACTS:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as error:
            print(
                f"Unable to remove stale artifact {path}: {error}"
            )


# ============================================================
# RUN AGENT
# ============================================================

def create_run_artifact_dir(base_dir=BASE_DIR):
    runs_dir = os.path.join(base_dir, "workflow_runs")
    os.makedirs(runs_dir, exist_ok=True)

    run_id = (
        datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        + "_"
        + uuid.uuid4().hex
    )
    run_dir = os.path.join(runs_dir, run_id)
    os.mkdir(run_dir)
    return run_dir


def configure_run_artifact_paths(run_dir=None):
    global ACTIVE_RUN_ARTIFACT_DIR
    global REPOSITORY_CONTEXT_FILE
    global REPRODUCTION_RESULT_FILE
    global MANUAL_REPRODUCTION_FILE
    global REPRODUCTION_TEST_FILE
    global CONFIRMED_REPRODUCTION_TEST_FILE
    global REPRODUCTION_TEST_FEEDBACK_FILE
    global CODE_FIX_ORIGINALS_FILE
    global FIX_BASELINE_FILE
    global FIX_REVIEW_FILE
    global DIAGNOSIS_FILE
    global CODE_FIX_RESULT_FILE
    global TEST_GENERATION_RESULT_FILE
    global TEST_RESULT_FILE
    global BUILD_RESULT_FILE
    global VALIDATION_RESULT_FILE
    global VERIFICATION_REPORT_FILE
    global WORKFLOW_REPORT_FILE

    ACTIVE_RUN_ARTIFACT_DIR = run_dir
    REPOSITORY_CONTEXT_FILE = resolve_artifact_path(
        "repository_context.json", BASE_DIR, run_dir
    )
    REPRODUCTION_RESULT_FILE = resolve_artifact_path(
        "reproduction_result.json", BASE_DIR, run_dir
    )
    MANUAL_REPRODUCTION_FILE = resolve_artifact_path(
        "manual_reproduction.json", BASE_DIR, run_dir
    )
    REPRODUCTION_TEST_FILE = resolve_artifact_path(
        "reproduction_test.json", BASE_DIR, run_dir
    )
    CONFIRMED_REPRODUCTION_TEST_FILE = resolve_artifact_path(
        "confirmed_reproduction_test.json", BASE_DIR, run_dir
    )
    REPRODUCTION_TEST_FEEDBACK_FILE = resolve_artifact_path(
        "reproduction_test_feedback.json", BASE_DIR, run_dir
    )
    CODE_FIX_ORIGINALS_FILE = resolve_artifact_path(
        "code_fix_originals.json", BASE_DIR, run_dir
    )
    FIX_BASELINE_FILE = resolve_artifact_path("fix_baseline.json", BASE_DIR, run_dir)
    FIX_REVIEW_FILE = resolve_artifact_path("fix_review.json", BASE_DIR, run_dir)
    DIAGNOSIS_FILE = resolve_artifact_path(
        "diagnosis.json",
        BASE_DIR,
        run_dir,
    )
    CODE_FIX_RESULT_FILE = resolve_artifact_path(
        "code_fix_result.json",
        BASE_DIR,
        run_dir,
    )
    TEST_GENERATION_RESULT_FILE = resolve_artifact_path(
        "test_generation_result.json", BASE_DIR, run_dir
    )
    TEST_RESULT_FILE = resolve_artifact_path("test_result.json", BASE_DIR, run_dir)
    BUILD_RESULT_FILE = resolve_artifact_path("build_result.json", BASE_DIR, run_dir)
    VALIDATION_RESULT_FILE = resolve_artifact_path(
        "validation_result.json", BASE_DIR, run_dir
    )
    VERIFICATION_REPORT_FILE = resolve_artifact_path(
        "verification_report.json", BASE_DIR, run_dir
    )
    WORKFLOW_REPORT_FILE = resolve_artifact_path(
        "workflow_report.json", BASE_DIR, run_dir
    )


def parse_workflow_arguments(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("repository_url", nargs="?")
    parser.add_argument("bug_report", nargs="?")
    parser.add_argument("--local-repository")
    parser.add_argument("--validation-only", action="store_true")
    parser.add_argument("--local-websocket-demo", action="store_true")
    parser.add_argument("--ai-diagnosis", action="store_true")
    arguments = parser.parse_args(argv)

    if arguments.local_repository and arguments.repository_url:
        if arguments.bug_report:
            parser.error(
                "Use either positional URL and bug-report arguments, or "
                "--local-repository with at most one positional bug-report."
            )
        arguments.bug_report = arguments.repository_url
        arguments.repository_url = None

    if arguments.validation_only and not arguments.local_repository:
        parser.error("--validation-only requires --local-repository PATH.")

    if arguments.local_websocket_demo and not arguments.local_repository:
        parser.error("--local-websocket-demo requires --local-repository PATH.")

    if arguments.local_websocket_demo and arguments.validation_only:
        parser.error("Choose either --validation-only or --local-websocket-demo.")

    if arguments.ai_diagnosis and not arguments.local_websocket_demo:
        parser.error("--ai-diagnosis requires --local-websocket-demo.")

    if arguments.local_websocket_demo and arguments.bug_report:
        parser.error(
            "--local-websocket-demo creates its own run-scoped deterministic bug report."
        )

    if arguments.local_repository:
        arguments.local_repository = os.path.abspath(
            os.path.expanduser(arguments.local_repository)
        )
        if not os.path.isdir(arguments.local_repository):
            parser.error(
                "--local-repository must identify an existing directory."
            )

    return arguments


# Groq free-tier limits seen while this run's agents called the AI.
AI_LIMIT_EVENTS = []
PENDING_AI_LIMIT = None
_AI_LIMIT_SIGNS = (
    "rate_limit_exceeded", "error code: 429", "error code: 413",
    "request too large", "tokens per day", "tokens per minute",
)
_AI_CONNECTION_PATTERN = re.compile(r"^\s*Connection error\.\s*$", re.MULTILINE)
_AI_WAIT_PATTERN = re.compile(
    r"try again in\s+(?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)s)?(?:([\d.]+)ms)?",
    re.IGNORECASE,
)


def _describe_wait(seconds):
    if seconds is None:
        return "a few minutes"
    if seconds < 60:
        return "1 minute"
    minutes = int(-(-seconds // 60))
    if minutes < 60:
        return f"{minutes} minutes"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def ai_limit_from_output(text):
    """A plain message when an agent failed on a Groq limit, else None.

    Only the limit kind and wait time are taken from the provider text;
    the message itself is fixed, so no request content or key is shown.
    """
    lower = (text or "").lower()
    if not any(sign in lower for sign in _AI_LIMIT_SIGNS):
        # The Groq SDK prints "Connection error." when it cannot reach the API.
        # LangSmith upload warnings ("Connection error caused ...") do not count.
        if "apiconnectionerror" in lower or _AI_CONNECTION_PATTERN.search(text or ""):
            return {
                "kind": "connection",
                "wait_seconds": None,
                "message": (
                    "The workflow could not reach the AI service (Groq). Check the "
                    "internet connection, then press Retry or start the run again."
                ),
            }
        return None
    wait_seconds = None
    match = _AI_WAIT_PATTERN.search(text)
    if match and any(match.groups()):
        hours, minutes, secs, millis = match.groups()
        wait_seconds = (
            int(hours or 0) * 3600 + int(minutes or 0) * 60
            + float(secs or 0) + float(millis or 0) / 1000
        )
    if "error code: 413" in lower or "request too large" in lower:
        return {
            "kind": "request_too_large",
            "wait_seconds": None,
            "message": (
                "The request sent to the AI (Groq) was larger than the free tier "
                "allows (HTTP 413). Waiting will not help: the prompt or the files "
                "sent with it must be smaller."
            ),
        }
    if "tokens per day" in lower or "(tpd)" in lower:
        return {
            "kind": "daily",
            "wait_seconds": wait_seconds,
            "message": (
                "The AI (Groq) free-tier daily token limit is used up. Wait about "
                f"{_describe_wait(wait_seconds)}, then press Retry, or stop safely "
                "and start the run again later."
            ),
        }
    return {
        "kind": "per_minute",
        "wait_seconds": wait_seconds,
        "message": (
            "The AI (Groq) per-minute token limit was hit several times in a row. "
            f"Wait about {_describe_wait(wait_seconds)}, then press Retry."
        ),
    }


def note_ai_limit(result):
    """Remember an AI limit from a failed agent so the console can explain it."""
    global PENDING_AI_LIMIT
    if result.get("success"):
        return
    limit = ai_limit_from_output("\n".join((result.get("output") or "", result.get("error") or "")))
    if limit is None:
        return
    result["ai_limit"] = limit
    AI_LIMIT_EVENTS.append(limit)
    PENDING_AI_LIMIT = limit


def blocker_reason_with_ai_limit(reason):
    """Put a pending AI limit message in front of a blocker reason, once."""
    global PENDING_AI_LIMIT
    limit, PENDING_AI_LIMIT = PENDING_AI_LIMIT, None
    return f"{limit['message']} ({reason})" if limit else reason


def run_agent(script_name, arguments=None):
    command = [
        sys.executable,
        os.path.join(BASE_DIR, script_name)
    ]

    if arguments:
        command.extend(arguments)

    print(
        f"\nRunning: {' '.join(command)}"
    )

    child_environment = local_workflow_environment()
    child_environment.update(CHILD_ENV_OVERRIDES)
    if ACTIVE_RUN_ARTIFACT_DIR:
        child_environment[ARTIFACT_DIR_ENV] = ACTIVE_RUN_ARTIFACT_DIR

    try:
        result = subprocess.run(
            command,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="backslashreplace",
            env=child_environment,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as error:
        return {
            "success": False,
            "return_code": -1,
            "output": "",
            "error": str(error),
        }

    stdout = result.stdout or ""
    stderr = result.stderr or ""

    if stdout:
        print(stdout)

    if stderr:
        print(stderr)

    agent_result = {
        "success": result.returncode == 0,
        "return_code": result.returncode,
        "output": stdout,
        "error": stderr,
    }
    note_ai_limit(agent_result)
    return agent_result


def diagnosis_failure_reason(result, fallback):
    if not isinstance(result, dict):
        return fallback

    error = result.get("error")
    if isinstance(error, str) and error.strip():
        return f"{fallback} Details: {error.strip()[-2000:]}"
    return fallback


def local_workflow_environment():
    environment = os.environ.copy()
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_ROLE_ARN",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    ):
        environment.pop(name, None)
    environment["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
    environment["AWS_CONFIG_FILE"] = os.devnull
    environment["AWS_EC2_METADATA_DISABLED"] = "true"
    return environment


def project_uses_jest(repository_path, strategy):
    """The detected framework is a guess (MERN reports "Cypress / Jest").
    Jest is only required when the test command runs it or the backend's
    package.json declares it; otherwise tests use Node's built-in runner."""
    test_command = strategy.get("test_command")
    if isinstance(test_command, list) and any("jest" in str(part).lower() for part in test_command):
        return True
    package_path = os.path.join(repository_path, strategy.get("backend_path") or ".", "package.json")
    try:
        with open(package_path, "r", encoding="utf-8") as handle:
            package = json.load(handle)
    except (OSError, ValueError):
        return False
    return isinstance(package, dict) and any(
        isinstance(package.get(section), dict) and "jest" in package[section]
        for section in ("dependencies", "devDependencies")
    )


def local_execution_blockers(context):
    repository_path = get_repository_path(context)
    if not isinstance(repository_path, str) or not repository_path:
        return ["Cannot preflight dependencies without a repository path."]

    strategy = get_strategy(context)
    blockers = []
    test_framework = str(strategy.get("test_framework") or "").lower()
    if "jest" in test_framework and project_uses_jest(repository_path, strategy):
        backend_path = strategy.get("backend_path") or "."
        test_command = strategy.get("test_command")
        runner_relative = (
            test_command[1]
            if isinstance(test_command, list)
            and len(test_command) > 1
            and isinstance(test_command[1], str)
            else os.path.join("node_modules", "jest", "bin", "jest.js")
        )
        runner_path = os.path.abspath(os.path.join(repository_path, runner_relative))
        backend_runner = os.path.abspath(
            os.path.join(
                repository_path,
                backend_path,
                "node_modules",
                "jest",
                "bin",
                "jest.js",
            )
        )
        if not os.path.isfile(runner_path) and not os.path.isfile(backend_runner):
            blockers.append(
                f"Configured Jest runner is missing under '{backend_path}'; "
                "package installation is disabled."
            )

    architecture = context.get("architecture", {})
    components = architecture.get("components", []) if isinstance(architecture, dict) else []
    for component in components:
        if not isinstance(component, dict) or not component.get("build_command"):
            continue
        if str(component.get("package_manager") or "").lower() not in {
            "npm",
            "yarn",
            "pnpm",
        }:
            continue
        component_path = component.get("path") or "."
        working_directory = os.path.abspath(
            os.path.join(repository_path, component_path)
        )
        if (
            os.path.isfile(os.path.join(working_directory, "package.json"))
            and not os.path.isdir(os.path.join(working_directory, "node_modules"))
        ):
            blockers.append(
                f"Build dependencies are missing for component "
                f"'{component.get('name', component_path)}'; package installation is disabled."
            )

    return blockers


def run_code_fix_stage():
    for path in (CODE_FIX_RESULT_FILE, CODE_FIX_ORIGINALS_FILE):
        if os.path.exists(path):
            os.remove(path)

    result = run_agent(
        AGENTS["code_fix"],
        [DIAGNOSIS_FILE],
    )
    record_fix_baseline()
    return result, load_json(CODE_FIX_RESULT_FILE)


def record_fix_baseline():
    """Keep each file's content from before the FIRST fix attempt touched it,
    so a reviewer sees the full change against the original code."""
    originals = load_json(CODE_FIX_ORIGINALS_FILE)
    if not isinstance(originals, dict) or not originals:
        return
    baseline = load_json(FIX_BASELINE_FILE)
    baseline = baseline if isinstance(baseline, dict) else {}
    for relative_path, original in originals.items():
        if isinstance(relative_path, str) and relative_path not in baseline:
            baseline[relative_path] = original if isinstance(original, str) else None
    save_json(FIX_BASELINE_FILE, baseline)


MAX_REVIEW_DIFF_CHARS = 40000


def build_fix_diff(context):
    """Unified diff of every fixed file: original (before any attempt) vs now."""
    import difflib

    baseline = load_json(FIX_BASELINE_FILE)
    files, chunks, total, truncated = [], [], 0, False
    for relative_path, original in sorted((baseline or {}).items()):
        path = _repository_file(context, relative_path)
        if path is None:
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                current = handle.read()
        except OSError:
            current = None
        if current == original:
            continue
        diff_lines = list(difflib.unified_diff(
            (original or "").splitlines(keepends=True),
            (current or "").splitlines(keepends=True),
            fromfile=f"a/{relative_path}" if original is not None else "/dev/null",
            tofile=f"b/{relative_path}" if current is not None else "/dev/null",
        ))
        text = "".join(line if line.endswith("\n") else line + "\n" for line in diff_lines)
        # Separate changed places, counted without context lines so that
        # nearby edits are not merged into one hunk.
        regions = sum(
            1 for line in difflib.unified_diff(
                (original or "").splitlines(), (current or "").splitlines(), n=0, lineterm="",
            )
            if line.startswith("@@")
        )
        files.append({
            "file": relative_path,
            "added": sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")),
            "removed": sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---")),
            "regions": regions,
        })
        if total + len(text) > MAX_REVIEW_DIFF_CHARS:
            truncated = True
            text = text[: max(0, MAX_REVIEW_DIFF_CHARS - total)]
        total += len(text)
        chunks.append(text)
    return {"files": files, "diff": "".join(chunks), "diff_truncated": truncated}


FIX_SCOPE_MAX_REGIONS = 3
FIX_SCOPE_MAX_CHANGED_LINES = 60


def _repository_relative(path):
    return str(path).replace("\\", "/").removeprefix("./").strip("/")


def fix_scope_findings(fix_diff, affected_files):
    """Reasons a fix may change more than the reported bug needs (empty when it looks focused)."""
    if isinstance(affected_files, str):
        affected_files = [affected_files]
    expected = {_repository_relative(name) for name in affected_files or [] if isinstance(name, str)}
    files = fix_diff.get("files") or []
    findings = []

    outside = [entry["file"] for entry in files if expected and _repository_relative(entry["file"]) not in expected]
    if outside:
        findings.append(
            f"The fix changed files the diagnosis did not name: {', '.join(outside)}."
        )
    regions = sum(int(entry.get("regions") or 0) for entry in files)
    if regions > FIX_SCOPE_MAX_REGIONS:
        findings.append(
            f"The fix changed {regions} separate places in the code (more than "
            f"{FIX_SCOPE_MAX_REGIONS}), so it may also change code unrelated to the bug."
        )
    changed_lines = sum(int(entry.get("added") or 0) + int(entry.get("removed") or 0) for entry in files)
    if changed_lines > FIX_SCOPE_MAX_CHANGED_LINES:
        findings.append(
            f"The fix changed {changed_lines} lines (more than {FIX_SCOPE_MAX_CHANGED_LINES})."
        )
    return findings


def prompt_fix_review(fix_review):
    """Terminal version of the fix review."""
    try:
        interactive = sys.stdin.isatty()
    except (AttributeError, OSError):
        interactive = False
    if not interactive:
        return None
    print_header(fix_review.get("title") or "FIX REVIEW")
    print(fix_review["reason"])
    for finding in fix_review.get("scope_findings") or []:
        print(f"- {finding}")
    print(fix_review["diff"] or "(No file changes were recorded.)")
    try:
        answer = input("Approve this fix? [a]pprove / [r]eject: ").strip().lower()
        if answer in ("a", "approve"):
            return {"decision": "approve", "feedback": ""}
        if answer in ("r", "reject"):
            reason = input("Why is the fix rejected? ").strip()
            return {"decision": "reject", "feedback": reason or "Rejected in the terminal."}
    except (EOFError, KeyboardInterrupt):
        pass
    return None


def review_unconfirmed_fix(context, checks, scope_findings=None, fix_diff=None):
    """Ask a human to approve a fix that checks alone cannot prove.

    Used when the bug was never confirmed by an automated test, or when a fix
    for a confirmed bug looks wider than the bug needs (scope_findings).
    """
    fix_review = dict(fix_diff) if fix_diff is not None else build_fix_diff(context)
    if scope_findings:
        fix_review.update({
            "title": "FIX REVIEW: THE FIX MAY CHANGE MORE THAN THE BUG NEEDS",
            "reason": (
                "The bug was confirmed and all checks passed, but the fix looks wider "
                "than the reported bug needs. Check that every change is needed."
            ),
            "message": (
                "Checks passed, but the fix may change more than the bug needs. "
                "Review the changes, then approve or reject."
            ),
            "scope_findings": list(scope_findings),
        })
    else:
        fix_review.update({
            "title": "FIX REVIEW: THE BUG WAS NOT CONFIRMED BY A TEST",
            "reason": (
                "The bug was reproduced only from user-reported steps, not by an "
                "automated test, so passing tests do not prove the reported bug is fixed."
            ),
        })
    fix_review["checks"] = checks
    if control_enabled():
        decision = wait_for_fix_review(fix_review)
    else:
        decision = prompt_fix_review(fix_review)
    fix_review["decision"] = decision["decision"] if decision else None
    fix_review["feedback"] = decision.get("feedback") if decision else None
    fix_review["reviewed_by"] = "user" if decision else None
    save_json(FIX_REVIEW_FILE, fix_review)
    return fix_review


# ============================================================
# EXECUTION STRATEGY
# ============================================================

def get_strategy(context):
    if not isinstance(context, dict):
        return {}

    nested = context.get("execution_strategy")

    if isinstance(nested, dict):
        return nested

    keys = (
        "technology",
        "package_manager",
        "install_command",
        "start_command",
        "test_command",
        "build_command",
        "validation_method",
        "test_framework",
    )

    return {
        key: context.get(key)
        for key in keys
        if key in context
    }


# ============================================================
# DYNAMIC REPOSITORY PATH
# ============================================================

def get_repository_path(context):
    if not isinstance(context, dict):
        return None

    path = context.get("repository_path")

    if isinstance(path, str) and path.strip():
        return path

    path = context.get("repository")

    if isinstance(path, str) and path.strip():
        return path

    nested = context.get("execution_strategy")
    if isinstance(nested, dict):
        path = nested.get("repository_path")
        if isinstance(path, str) and path.strip():
            return path

    return None


# ============================================================
# VALIDATE REPOSITORY CONTEXT
# ============================================================

def validate_context(context):
    if not isinstance(context, dict):
        return (
            False,
            "repository_context.json is missing or invalid."
        )

    repository_path = get_repository_path(context)
    strategy = get_strategy(context)

    if not repository_path:
        return (
            False,
            "repository_context.json does not contain repository_path."
        )

    if not os.path.isdir(repository_path):
        return (
            False,
            "repository_path is not an existing directory."
        )

    if not strategy.get("technology"):
        return (
            False,
            "repository_context.json does not contain technology."
        )

    if not strategy.get("test_command"):
        return (
            False,
            "repository_context.json does not contain test_command."
        )

    return True, None


# ============================================================
# VALIDATE REPRODUCTION
# ============================================================

def validate_reproduction():
    result = load_json(REPRODUCTION_RESULT_FILE)

    return (
        isinstance(result, dict)
        and result.get("status") == "REPRODUCED"
    )


def validate_manual_reproduction():
    manual = load_json(MANUAL_REPRODUCTION_FILE)
    if not isinstance(manual, dict):
        return False

    steps = manual.get("steps")
    supporting_evidence = manual.get("supporting_evidence", [])
    return (
        manual.get("status") == "MANUAL_SUBMITTED"
        and manual.get("source") == "user"
        and isinstance(steps, list)
        and bool(steps)
        and all(isinstance(step, str) and step.strip() for step in steps)
        and isinstance(manual.get("expected_behavior"), str)
        and bool(manual["expected_behavior"].strip())
        and isinstance(manual.get("observed_behavior"), str)
        and bool(manual["observed_behavior"].strip())
        and isinstance(supporting_evidence, list)
        and all(isinstance(item, str) for item in supporting_evidence)
    )


def collect_manual_reproduction(reproduction):
    if control_enabled():
        submission = wait_for_manual_reproduction(
            recording_handler=handle_recording_submission
        )
        if submission is None:
            return {
                "status": "TIMED_OUT",
                "reason": "Manual reproduction was not submitted before the wait timed out.",
            }
        if submission.get("kind") == "browser_recording":
            return {
                "status": "RECORDING_CONFIRMED",
                "confirmed": submission["confirmed"],
                "reason": (
                    "The user's Playwright recording was replayed on the current code "
                    "and a recorded assertion failed, which shows the bug."
                ),
            }
        submission["automatic_reproduction_status"] = (
            reproduction.get("status") if isinstance(reproduction, dict) else None
        )
        evidence_index = load_json(
            os.path.join(ACTIVE_RUN_ARTIFACT_DIR or BASE_DIR, "evidence_index.json")
        )
        if isinstance(evidence_index, dict) and isinstance(evidence_index.get("files"), list):
            # Redacted summaries of attached recordings/logs, for diagnosis.
            submission["attached_evidence"] = [
                {
                    "name": item.get("name"),
                    "kind": item.get("kind"),
                    "summary": str(item.get("summary", ""))[:1500],
                }
                for item in evidence_index["files"][:5]
                if isinstance(item, dict)
            ]
        artifact_path = resolve_artifact_path(
            "manual_reproduction.json", BASE_DIR, ACTIVE_RUN_ARTIFACT_DIR
        )
        try:
            with open(artifact_path, "x", encoding="utf-8") as file:
                json.dump(submission, file, indent=4, ensure_ascii=False)
        except FileExistsError:
            return {
                "status": "ARTIFACT_CONFLICT",
                "artifact_path": artifact_path,
                "reason": "Manual reproduction artifact already exists; it was not overwritten.",
            }
        except OSError as error:
            return {
                "status": "SAVE_FAILED",
                "reason": f"Manual reproduction could not be saved: {error}",
            }
        return {
            "status": "CAPTURED",
            "artifact_path": artifact_path,
            "reason": "Manual reproduction evidence was saved for initial and retry diagnosis.",
        }

    try:
        interactive = sys.stdin.isatty()
    except (AttributeError, OSError):
        interactive = False

    if not interactive:
        return {
            "status": "NOT_OFFERED",
            "reason": "Manual reproduction requires an interactive terminal.",
        }

    def cancelled():
        return {
            "status": "CANCELLED",
            "reason": "The user cancelled manual reproduction.",
        }

    try:
        response = input(
            "Automatic reproduction failed. Submit manual reproduction details? [y/N]: "
        ).strip().lower()
        if response not in ("y", "yes"):
            return cancelled()

        steps = []
        while True:
            step = input(
                f"Reproduction step {len(steps) + 1} "
                "(blank to finish, 'cancel' to stop): "
            ).strip()
            if step.lower() == "cancel":
                return cancelled()
            if not step:
                break
            steps.append(step)

        expected_behavior = ""
        while not expected_behavior:
            expected_behavior = input(
                "Expected behavior (required; what should happen? "
                "Enter 'unknown' if unclear, or 'cancel' to stop): "
            ).strip()
            if expected_behavior.lower() == "cancel":
                return cancelled()
            if not expected_behavior:
                print("Please enter expected behavior, or type 'cancel'.")

        observed_behavior = ""
        while not observed_behavior:
            observed_behavior = input(
                "Observed behavior (required; what actually happened? "
                "Enter 'not verified' if you have not run the steps, "
                "or 'cancel' to stop): "
            ).strip()
            if observed_behavior.lower() == "cancel":
                return cancelled()
            if not observed_behavior:
                print("Please enter the observed result, or type 'cancel'.")

        supporting_evidence = []
        while True:
            evidence_line = input(
                "Supporting evidence, such as errors or logs "
                "(optional; blank to finish, 'cancel' to stop): "
            ).rstrip()
            if evidence_line.strip().lower() == "cancel":
                return cancelled()
            if not evidence_line.strip():
                break
            supporting_evidence.append(evidence_line)
    except (EOFError, KeyboardInterrupt):
        return cancelled()

    missing_fields = []
    if not steps:
        missing_fields.append("reproduction_steps")
    if not expected_behavior:
        missing_fields.append("expected_behavior")
    if not observed_behavior:
        missing_fields.append("observed_behavior")
    if missing_fields:
        return {
            "status": "INSUFFICIENT",
            "missing_fields": missing_fields,
            "reason": "Required manual reproduction information is missing.",
        }

    artifact_path = resolve_artifact_path(
        "manual_reproduction.json", BASE_DIR, ACTIVE_RUN_ARTIFACT_DIR
    )
    submission = {
        "status": "MANUAL_SUBMITTED",
        "source": "user",
        "automatic_reproduction_status": (
            reproduction.get("status")
            if isinstance(reproduction, dict)
            else None
        ),
        "steps": steps,
        "expected_behavior": expected_behavior,
        "observed_behavior": observed_behavior,
        "supporting_evidence": supporting_evidence,
    }

    try:
        with open(artifact_path, "x", encoding="utf-8") as file:
            json.dump(submission, file, indent=4, ensure_ascii=False)
    except FileExistsError:
        return {
            "status": "ARTIFACT_CONFLICT",
            "artifact_path": artifact_path,
            "reason": "Manual reproduction artifact already exists; it was not overwritten.",
        }
    except OSError as error:
        return {
            "status": "SAVE_FAILED",
            "reason": f"Manual reproduction could not be saved: {error}",
        }

    return {
        "status": "CAPTURED",
        "artifact_path": artifact_path,
        "reason": (
            "Manual reproduction details were captured as user-reported "
            "information; they are not automatically verified. The workflow "
            "can continue to diagnosis using the submitted steps and outcomes."
        ),
    }


# ============================================================
# AI REPRODUCTION TEST (human-confirmed)
# ============================================================

def _repository_file(context, relative_path):
    repository = get_repository_path(context)
    if not repository or not isinstance(relative_path, str):
        return None
    root = os.path.realpath(repository)
    full = os.path.realpath(os.path.join(root, relative_path))
    try:
        return full if os.path.commonpath([root, full]) == root else None
    except ValueError:
        return None


def remove_unconfirmed_reproduction_test(context, artifact):
    """An unconfirmed AI test must not stay in the repository."""
    if not isinstance(artifact, dict):
        return
    path = _repository_file(context, artifact.get("test_file"))
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                current = handle.read()
        except OSError:
            return
        if reproduction_tests.content_sha256(current) == artifact.get("content_sha256"):
            os.remove(path)


def prompt_reproduction_review(artifact):
    """Terminal version of the review step."""
    try:
        interactive = sys.stdin.isatty()
    except (AttributeError, OSError):
        interactive = False
    if not interactive:
        return None
    run = artifact.get("run") if isinstance(artifact.get("run"), dict) else {}
    classification = artifact.get("classification")
    print_header("AI REPRODUCTION TEST FOR REVIEW")
    print(f"Test file: {artifact.get('test_file')}")
    print(f"Result on current code: {classification}")
    print("\n".join(str(artifact.get("content", "")).splitlines()[:80]))
    print("\nRunner output (tail):\n" + str(run.get("output_tail", ""))[-1500:])
    can_confirm = classification == reproduction_tests.FAILS_ON_CURRENT_CODE
    choices = "[c]onfirm / [r]evise / [m]anual" if can_confirm else "[r]evise / [m]anual"
    try:
        answer = input(f"Does this test reproduce the bug? {choices}: ").strip().lower()
        if answer in ("c", "confirm") and can_confirm:
            return {"decision": "confirm", "feedback": ""}
        if answer in ("r", "revise"):
            feedback = input("What should the test do differently? ").strip()
            if feedback:
                return {"decision": "revise", "feedback": feedback[:4000]}
    except (EOFError, KeyboardInterrupt):
        pass
    return {"decision": "manual", "feedback": ""}


def collect_ai_reproduction_test(context):
    """AI writes a reproduction test; a human confirms it before it is trusted.

    Returns a dict whose status is CONFIRMED, MANUAL_REQUESTED, UNSUPPORTED,
    UNAVAILABLE, EXHAUSTED, TIMED_OUT or NOT_OFFERED.
    """
    try:
        interactive = sys.stdin.isatty()
    except (AttributeError, OSError):
        interactive = False
    if not control_enabled() and not interactive:
        return {"status": "NOT_OFFERED", "reason": "Reviewing an AI reproduction test needs the console or a terminal."}

    rounds = []
    feedback_arguments = []
    for round_number in range(1, MAX_REPRODUCTION_TEST_ROUNDS + 1):
        stage_name = f"AI Reproduction Test (Round {round_number})"
        print_stage(2, stage_name)
        if os.path.exists(REPRODUCTION_TEST_FILE):
            os.remove(REPRODUCTION_TEST_FILE)
        # Recordings and logs the user attached (re-read every round, so
        # files added during review are used by the revision).
        evidence_index = os.path.join(ACTIVE_RUN_ARTIFACT_DIR or BASE_DIR, "evidence_index.json")
        evidence_arguments = ["--evidence", evidence_index] if os.path.isfile(evidence_index) else []
        result = run_agent(
            AGENTS["reproduction_test"],
            ["--round", str(round_number)] + feedback_arguments + evidence_arguments,
        )
        artifact = load_json(REPRODUCTION_TEST_FILE)
        if isinstance(artifact, dict) and artifact.get("status") == "UNSUPPORTED":
            record_stage(2, stage_name, "SKIPPED")
            return {"status": "UNSUPPORTED", "reason": artifact.get("reason"), "rounds": rounds}
        if not result["success"] or not isinstance(artifact, dict) or artifact.get("status") != "GENERATED":
            reason = (artifact or {}).get("reason") if isinstance(artifact, dict) else None
            rounds.append({"round": round_number, "status": "NOT_GENERATED", "reason": reason})
            record_stage(2, stage_name, "FAILED")
            return {"status": "UNAVAILABLE", "reason": reason or "The AI could not produce a reproduction test.", "rounds": rounds}

        record = {
            "round": round_number,
            "test_file": artifact.get("test_file"),
            "classification": artifact.get("classification"),
        }
        if control_enabled():
            decision = wait_for_reproduction_review(artifact, round_number, MAX_REPRODUCTION_TEST_ROUNDS)
        else:
            decision = prompt_reproduction_review(artifact)
        if decision is None:
            remove_unconfirmed_reproduction_test(context, artifact)
            rounds.append(dict(record, decision="timed_out"))
            record_stage(2, stage_name, "TIMED_OUT")
            return {"status": "TIMED_OUT", "reason": "The reproduction test was not reviewed in time.", "rounds": rounds}

        record.update(decision=decision["decision"], feedback=decision.get("feedback") or None)
        rounds.append(record)

        if decision["decision"] == "confirm":
            if artifact.get("classification") != reproduction_tests.FAILS_ON_CURRENT_CODE:
                record["decision"] = "confirm_rejected"
                record_stage(2, stage_name, "FAILED")
                remove_unconfirmed_reproduction_test(context, artifact)
                return {"status": "UNAVAILABLE", "reason": "Only a test that fails on the current code can be confirmed.", "rounds": rounds}
            confirmed = {
                "status": "CONFIRMED",
                "confirmed_by": "user",
                "round": round_number,
                "framework": artifact.get("framework"),
                "test_file": artifact.get("test_file"),
                "content": artifact.get("content"),
                "content_sha256": artifact.get("content_sha256"),
                "before_fix": {
                    "classification": artifact.get("classification"),
                    "run": artifact.get("run"),
                },
            }
            save_json(CONFIRMED_REPRODUCTION_TEST_FILE, confirmed)
            record_stage(2, stage_name, "CONFIRMED")
            return {"status": "CONFIRMED", "confirmed": confirmed, "rounds": rounds}

        remove_unconfirmed_reproduction_test(context, artifact)
        if decision["decision"] == "manual":
            record_stage(2, stage_name, "MANUAL_REQUESTED")
            return {"status": "MANUAL_REQUESTED", "rounds": rounds}
        record_stage(2, stage_name, "REVISED")

        save_json(REPRODUCTION_TEST_FEEDBACK_FILE, {
            "previous_content": artifact.get("content"),
            "previous_classification": artifact.get("classification"),
            "previous_output": (artifact.get("run") or {}).get("output_tail"),
            "feedback": decision.get("feedback"),
        })
        feedback_arguments = ["--feedback", REPRODUCTION_TEST_FEEDBACK_FILE]

    return {"status": "EXHAUSTED", "reason": "No reproduction test was confirmed.", "rounds": rounds}


def run_confirmed_reproduction_test(context):
    """After the fix, the unchanged confirmed test must pass."""
    confirmed = load_json(CONFIRMED_REPRODUCTION_TEST_FILE)
    runner = reproduction_tests.select_runner(context)
    if not isinstance(confirmed, dict) or runner is None:
        return {"status": "FAILED", "reason": "The confirmed reproduction test is unavailable."}
    path = _repository_file(context, confirmed.get("test_file"))
    if path is None or confirmed.get("test_file") != runner["test_path"]:
        return {"status": "FAILED", "reason": "The confirmed reproduction test path is invalid."}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            current = handle.read()
    except OSError:
        current = None
    # The fix must not change the test; restore the confirmed version if it did.
    restored = reproduction_tests.content_sha256(current) != confirmed.get("content_sha256")
    if restored:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(confirmed.get("content") or "")
    run = reproduction_tests.run_single_test(get_repository_path(context), runner)
    passed = reproduction_tests.passes_after_fix(run)
    return {
        "status": "PASSED" if passed else "FAILED",
        "test_file": confirmed.get("test_file"),
        "test_restored_before_run": restored,
        "classification": reproduction_tests.classify_run(run),
        "run": run,
    }


def run_artifact_path(filename):
    return resolve_artifact_path(filename, BASE_DIR, ACTIVE_RUN_ARTIFACT_DIR)


def confirmed_reproduction_file(source):
    if source == BROWSER_RECORDING_SOURCE:
        return run_artifact_path("confirmed_browser_recording.json")
    return CONFIRMED_REPRODUCTION_TEST_FILE


def run_browser_replay(recording_path, name):
    """Start the app, replay the recording's steps, stop the app."""
    output_path = run_artifact_path(f"{name}.json")
    screenshot_path = run_artifact_path(f"{name}_failure.png")
    trace_path = run_artifact_path(f"{name}_trace.zip")
    # Remove files from an earlier replay with the same name, so the console
    # never shows evidence from a different recording.
    for path in (output_path, screenshot_path, trace_path):
        if os.path.exists(path):
            os.remove(path)
    run_agent(
        AGENTS["browser_replay"],
        [
            "--recording", recording_path,
            "--output", output_path,
            "--screenshot", screenshot_path,
            "--trace", trace_path,
        ],
    )
    result = load_json(output_path)
    if not isinstance(result, dict) or not isinstance(result.get("classification"), str):
        return {"classification": "ERROR", "summary": "The browser replay did not produce a result."}
    return result


def handle_recording_submission(submission):
    """Validate and replay an uploaded Playwright recording on the current code.

    Returns (confirmed, feedback). It is confirmed only when a recorded
    assertion fails, which shows the bug; the user wrote that assertion.
    """
    filename = submission["filename"]
    try:
        parsed = browser_recording.parse_recording(submission["content"], filename)
    except browser_recording.RecordingError as error:
        return None, {
            "kind": "recording_result",
            "filename": filename,
            "classification": "REJECTED",
            "summary": "The recording was rejected: " + "; ".join(error.errors),
            "steps": [],
        }

    candidate_path = run_artifact_path("browser_recording.json")
    save_json(candidate_path, dict(parsed, filename=filename))
    publish_workflow_state(
        "REPLAYING_RECORDING",
        "Bug Reproduction",
        f"Starting the app and replaying {filename} on the current code.",
        None,
        ["stop_safely"],
    )
    result = run_browser_replay(candidate_path, "browser_replay_before_fix")
    result["filename"] = filename
    feedback = recording_feedback(result)
    if result["classification"] != reproduction_tests.FAILS_ON_CURRENT_CODE:
        if result["classification"] == reproduction_tests.PASSES_ON_CURRENT_CODE:
            feedback["summary"] = (
                "Every recorded step and assertion passed on the current code, so the "
                "recording does not show the bug. Record an assertion for the correct "
                "behaviour that the bug breaks."
            )
        return None, feedback

    content = submission["content"]
    confirmed = {
        "status": "CONFIRMED",
        "confirmed_by": "user",
        "source": BROWSER_RECORDING_SOURCE,
        "framework": "playwright-recording",
        "test_file": filename,
        "content": content,
        "content_sha256": reproduction_tests.content_sha256(content),
        "language": parsed["language"],
        "recorded_origin": parsed["recorded_origin"],
        "steps": parsed["steps"],
        "before_fix": {
            "classification": result["classification"],
            "summary": result.get("summary"),
            "run": {"output_tail": result.get("summary"), "steps": feedback["steps"]},
        },
    }
    save_json(run_artifact_path("confirmed_browser_recording.json"), confirmed)
    return confirmed, feedback


def record_browser_replay_as_runtime_validation(rerun, attempt):
    """The replay started the fixed app and drove it over HTTP in a browser,
    so a passing replay is the HTTP runtime validation that fix verification
    requires. A failed replay must not leave an older passing result behind."""
    if rerun.get("status") != "PASSED":
        if os.path.exists(VALIDATION_RESULT_FILE):
            os.remove(VALIDATION_RESULT_FILE)
        return
    save_json(VALIDATION_RESULT_FILE, {
        "status": "PASSED",
        "return_code": 0,
        "source": BROWSER_RECORDING_SOURCE,
        "message": (
            "The confirmed browser recording was replayed on the freshly started, "
            "fixed app; every recorded step and assertion passed."
        ),
        "checks": [{
            "name": "Browser recording replay",
            "kind": "runtime",
            "status": "PASSED",
            "return_code": 0,
            "command": ["node", "browser_replay/replay.mjs", str(rerun.get("test_file"))],
            "replay_result_ref": f"browser_replay_after_fix_attempt_{attempt}.json",
        }],
    })


def run_confirmed_browser_recording(attempt):
    """After the fix, every recorded step and assertion must pass."""
    path = run_artifact_path("confirmed_browser_recording.json")
    confirmed = load_json(path)
    if not isinstance(confirmed, dict) or confirmed.get("status") != "CONFIRMED":
        return {"status": "FAILED", "reason": "The confirmed browser recording is unavailable."}
    result = run_browser_replay(path, f"browser_replay_after_fix_attempt_{attempt}")
    passed = result["classification"] == reproduction_tests.PASSES_ON_CURRENT_CODE
    return {
        "status": "PASSED" if passed else "FAILED",
        "test_file": confirmed.get("test_file"),
        "classification": result["classification"],
        "summary": result.get("summary"),
        "replay": result.get("replay"),
    }



# ============================================================
# VALIDATE DIAGNOSIS
# ============================================================

def validate_diagnosis():
    diagnosis = load_json(DIAGNOSIS_FILE)
    manual_reproduction = load_json(MANUAL_REPRODUCTION_FILE)
    return not diagnosis_validation_errors(
        diagnosis,
        manual_reproduction if isinstance(manual_reproduction, dict) else None,
    )


# ============================================================
# VALIDATE TEST RESULT
# ============================================================

def validate_test_result():
    result = load_json(TEST_RESULT_FILE)
    return test_result_has_success_evidence(result)


def command_parts(command):
    if isinstance(command, list):
        parts = [str(part) for part in command]
    elif isinstance(command, str):
        parts = shlex.split(command, posix=False)
    else:
        return []

    if os.name == "nt" and parts:
        shims = {"npm": "npm.cmd", "npx": "npx.cmd", "yarn": "yarn.cmd", "pnpm": "pnpm.cmd"}
        parts[0] = shims.get(parts[0].lower(), parts[0])

    return parts


def run_component_builds(context):
    repository_path = get_repository_path(context)
    architecture = context.get("architecture", {})
    components = architecture.get("components", []) if isinstance(architecture, dict) else []
    build_components = [
        component
        for component in components
        if isinstance(component, dict) and component.get("build_command")
    ]

    if not build_components:
        strategy = get_strategy(context)
        if strategy.get("build_command"):
            build_components = [{
                "name": "application",
                "type": "application",
                "path": ".",
                "package_manager": strategy.get("package_manager"),
                "build_command": strategy["build_command"],
            }]

    results = []

    for component in build_components:

        component_path = component.get("path") or "."
        working_directory = os.path.abspath(
            os.path.join(repository_path, component_path)
        )
        if not os.path.isdir(working_directory):
            results.append({
                "component": component.get("name"),
                "status": "FAILED",
                "return_code": 1,
                "error": f"Build directory does not exist: {working_directory}",
            })
            continue

        package_manager = str(component.get("package_manager") or "").lower()
        lockfiles = {
            "npm": "package-lock.json",
            "yarn": "yarn.lock",
            "pnpm": "pnpm-lock.yaml",
        }
        install_required = False
        if package_manager in lockfiles:
            lockfile_path = os.path.join(working_directory, lockfiles[package_manager])
            if os.path.isfile(lockfile_path):
                install_required = True
            elif os.path.isfile(os.path.join(working_directory, "package.json")):
                install_required = True

        if install_required:
            dependency_directory = os.path.join(working_directory, "node_modules")
            if not os.path.isdir(dependency_directory):
                results.append({
                    "component": component.get("name"),
                    "path": component_path,
                    "status": "BLOCKED",
                    "return_code": None,
                    "reason": (
                        "Local dependencies are missing; automatic package "
                        "installation is disabled."
                    ),
                })
                continue

        try:
            build_parts = command_parts(component["build_command"])
            build_result = run_bounded(
                build_parts,
                timeout=BUILD_COMMAND_TIMEOUT_SECONDS,
                cwd=working_directory,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                env=project_process_environment(),
            )
            results.append({
                "component": component.get("name"),
                "path": component_path,
                "command": " ".join(build_parts),
                "status": "PASSED" if build_result.returncode == 0 else "FAILED",
                "return_code": build_result.returncode,
                "stdout": build_result.stdout or "",
                "stderr": build_result.stderr or "",
            })
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            results.append({
                "component": component.get("name"),
                "path": component_path,
                "status": "FAILED",
                "return_code": 1,
                "error": str(error),
            })

    if not results:
        aggregate = {
            "status": "SKIPPED",
            "return_code": 0,
            "message": "No component build command was detected.",
            "components": [],
        }
    else:
        successful = all(item.get("status") == "PASSED" for item in results)
        blocked = any(item.get("status") == "BLOCKED" for item in results)
        aggregate = {
            "status": "BLOCKED" if blocked else "PASSED" if successful else "FAILED",
            "return_code": 0 if successful else 1,
            "components": results,
        }

    save_json(BUILD_RESULT_FILE, aggregate)
    return aggregate


def run_validation_only(context, include_backend_tests=True):
    repository_path = os.path.realpath(get_repository_path(context) or "")
    architecture = context.get("architecture", {})
    components = architecture.get("components", []) if isinstance(architecture, dict) else []
    frontend = next(
        (component for component in components
         if isinstance(component, dict) and component.get("type") == "frontend"),
        None,
    )
    backend = next(
        (component for component in components
         if isinstance(component, dict) and component.get("type") == "backend"),
        None,
    )
    command_specs = []
    setup_errors = []

    if frontend is None:
        setup_errors.append("No frontend component was detected.")
    else:
        if frontend.get("build_command") == "npm run build":
            command_specs.append(("frontend_build", frontend, ["npm", "run", "build"], "build"))
        else:
            setup_errors.append("Frontend npm run build command was not detected.")

        if frontend.get("test_command") == "npm run lint":
            command_specs.append(("frontend_lint", frontend, ["npm", "run", "lint"], "test"))
        else:
            setup_errors.append("Frontend npm run lint command was not detected.")

    if include_backend_tests and backend is None:
        setup_errors.append("No backend component was detected.")
    elif (
        include_backend_tests
        and str(backend.get("test_framework") or "").lower() == "jest"
    ):
        jest_command = backend.get("test_command")
        component_path = backend.get("path") or "."
        backend_directory = os.path.realpath(
            os.path.join(repository_path, component_path)
        )
        expected_runner = os.path.join("node_modules", "jest", "bin", "jest.js")
        runner_path = os.path.join(backend_directory, expected_runner)
        if (
            isinstance(jest_command, list)
            and len(jest_command) == 3
            and jest_command[0] == "node"
            and os.path.normcase(os.path.normpath(str(jest_command[1])))
            == os.path.normcase(os.path.normpath(expected_runner))
            and jest_command[2] == "--runInBand"
            and os.path.isfile(runner_path)
        ):
            command_specs.append((
                "backend_jest",
                backend,
                ["node", expected_runner, "--runInBand"],
                "test",
            ))
        else:
            setup_errors.append("Installed local Jest runner was not detected.")
    elif include_backend_tests:
        setup_errors.append("Backend Jest test framework was not detected.")

    check_results = []
    for check_name, component, command, kind in command_specs:
        component_path = component.get("path") or "."
        working_directory = os.path.realpath(
            os.path.join(repository_path, component_path)
        )
        try:
            within_repository = os.path.normcase(
                os.path.commonpath([repository_path, working_directory])
            ) == os.path.normcase(repository_path)
        except ValueError:
            within_repository = False
        if not within_repository:
            check_results.append({
                "name": check_name,
                "status": "FAILED",
                "return_code": 1,
                "error": "Component working directory is outside the local repository.",
            })
            continue

        command_args = command_parts(command)
        try:
            process = run_bounded(
                command_args,
                timeout=BUILD_COMMAND_TIMEOUT_SECONDS,
                cwd=working_directory,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                env=project_process_environment(),
            )
            check_results.append({
                "name": check_name,
                "kind": kind,
                "component": component.get("name"),
                "path": component_path,
                "command": command_args,
                "status": "PASSED" if process.returncode == 0 else "FAILED",
                "return_code": process.returncode,
                "stdout": process.stdout or "",
                "stderr": process.stderr or "",
            })
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            check_results.append({
                "name": check_name,
                "kind": kind,
                "component": component.get("name"),
                "path": component_path,
                "command": command_args,
                "status": "FAILED",
                "return_code": 1,
                "error": str(error),
            })

    for error in setup_errors:
        check_results.append({
            "name": "validation_configuration",
            "status": "FAILED",
            "return_code": 1,
            "error": error,
        })

    build_checks = [item for item in check_results if item.get("kind") == "build"]
    test_checks = [item for item in check_results if item.get("kind") == "test"]
    checks_passed = (
        not setup_errors
        and len(build_checks) == 1
        and len(test_checks) == (2 if include_backend_tests else 1)
        and all(item.get("status") == "PASSED" for item in check_results)
    )
    return_code = 0 if checks_passed else 1
    result_status = "PASSED" if checks_passed else "FAILED"

    build_result = {
        "status": "PASSED" if build_checks and all(item["status"] == "PASSED" for item in build_checks) else "FAILED",
        "return_code": 0 if build_checks and all(item["status"] == "PASSED" for item in build_checks) else 1,
        "components": build_checks,
    }
    test_result = {
        "status": "PASSED" if test_checks and all(item["status"] == "PASSED" for item in test_checks) else "FAILED",
        "return_code": 0 if test_checks and all(item["status"] == "PASSED" for item in test_checks) else 1,
        "checks": test_checks,
    }
    validation_result = {
        "mode": "validation-only",
        "status": result_status,
        "return_code": return_code,
        "application_started": False,
        "dependency_installation": False,
        "aws_accessed": False,
        "checks": check_results,
    }

    save_json(BUILD_RESULT_FILE, build_result)
    save_json(TEST_RESULT_FILE, test_result)
    save_json(VALIDATION_RESULT_FILE, validation_result)
    save_json(VERIFICATION_REPORT_FILE, {
        "status": "NOT_APPLICABLE",
        "reason": ["Validation-only mode does not reproduce a bug or verify a fix."],
        "validation_result": validation_result,
    })

    return validation_result


def run_post_fix_runtime_validation(initial_reproduction):
    active_bug = load_json(ACTIVE_BUG_REPORT_FILE) or {}
    reproduction = active_bug.get("reproduction", {})
    if not isinstance(reproduction, dict) or not reproduction.get("action"):
        return {
            "status": "SKIPPED",
            "return_code": 0,
            "message": "No configured generic HTTP reproduction; existing project verification applies.",
        }

    result = run_agent(
        AGENTS["bug_reproduction"],
        [ACTIVE_BUG_REPORT_FILE],
    )
    post_fix_result = load_json(REPRODUCTION_RESULT_FILE)
    save_json(REPRODUCTION_RESULT_FILE, initial_reproduction)

    reproduction_bugs = (
        post_fix_result.get("bugs", [])
        if isinstance(post_fix_result, dict)
        else []
    )
    bug_result = reproduction_bugs[0] if reproduction_bugs else {}
    evidence = bug_result.get("evidence", [])
    evidence_text = " ".join(str(item) for item in evidence)
    status_match = re.search(
        r"Configured action request returned HTTP (\d+)",
        evidence_text,
    )
    action_status = int(status_match.group(1)) if status_match else None
    bug_no_longer_reproduces = (
        isinstance(post_fix_result, dict)
        and post_fix_result.get("status") == "NOT_REPRODUCED"
        and bug_result.get("bug")
        and action_status is not None
        and 400 <= action_status < 500
        and any(
            "No newly persisted record containing the invalid request fields" in str(item)
            for item in evidence
        )
    )
    validation = {
        "status": (
            "PASSED"
            if result["success"] and bug_no_longer_reproduces
            else "FAILED"
        ),
        "return_code": (
            0
            if result["success"] and bug_no_longer_reproduces
            else 1
        ),
        "expected_post_fix_status": "NOT_REPRODUCED",
        "observed_action_http_status": action_status,
        "observed_post_fix_result": post_fix_result,
        "message": (
            "The invalid request was rejected and no new blank record was observed."
            if bug_no_longer_reproduces
            else "Runtime validation did not prove the invalid request was rejected without insertion."
        ),
    }
    save_json(VALIDATION_RESULT_FILE, validation)
    return validation


# ============================================================
# VALIDATE VERIFICATION
# ============================================================

def validate_verification():
    report = load_json(VERIFICATION_REPORT_FILE)

    if not isinstance(report, dict):
        return False

    status = report.get(
        "status",
        report.get("overall_status")
    )

    if status not in (
        "PASS",
        "PASSED",
        "SUCCESS"
    ) or report.get("return_code") != 0:
        return False

    current_test_result = load_json(TEST_RESULT_FILE)
    if (
        not test_result_has_success_evidence(report.get("test_result"))
        or report.get("test_result") != current_test_result
    ):
        return False

    context = load_json(REPOSITORY_CONTEXT_FILE) or {}
    strategy = get_strategy(context)
    architecture = context.get("architecture", {})
    components = architecture.get("components", []) if isinstance(architecture, dict) else []
    build_components = [
        component
        for component in components
        if isinstance(component, dict) and component.get("build_command")
    ]
    expected_builds = len(build_components) if build_components else int(
        bool(strategy.get("build_command"))
    )
    if expected_builds:
        current_build = load_json(BUILD_RESULT_FILE)
        if (
            report.get("build_result") != current_build
            or not build_result_has_success_evidence(current_build, expected_builds)
        ):
            return False

    current_validation = load_json(VALIDATION_RESULT_FILE)
    if report.get("validation_result") != current_validation:
        return False
    validation_method = strategy.get("validation_method")
    if current_validation is None:
        if "http" in str(validation_method or "").lower():
            return False
    elif not runtime_result_has_success_evidence(current_validation):
        return False

    return True


# ============================================================
# STAGE RESULT
# ============================================================

def add_stage_result(
    stage_results,
    stage,
    name,
    status,
    details=None
):
    item = {
        "stage": stage,
        "name": name,
        "status": status,
        "timestamp": datetime.now().isoformat(),
    }

    # Only the first failed stage after the limit was hit gets the message.
    if status == "FAILED" and PENDING_AI_LIMIT is not None and not PENDING_AI_LIMIT.get("stage_noted"):
        PENDING_AI_LIMIT["stage_noted"] = True
        details = dict(details or {})
        details["ai_limit"] = PENDING_AI_LIMIT["message"]

    if details is not None:
        item["details"] = details

    stage_results.append(item)
    if control_enabled() and stage is not None:
        record_stage(stage, name, status)


# ============================================================
# ARCHIVE DIAGNOSIS
# ============================================================

def archive_diagnosis(
    attempt,
    prefix="diagnosis_attempt"
):
    if not os.path.exists(DIAGNOSIS_FILE):
        return None

    archive_path = resolve_artifact_path(
        f"{prefix}_{attempt}.json",
        BASE_DIR,
        ACTIVE_RUN_ARTIFACT_DIR,
    )

    shutil.copyfile(
        DIAGNOSIS_FILE,
        archive_path
    )

    return archive_path


# ============================================================
# CREATE RETRY BUG REPORT
# ============================================================

def create_retry_bug_report(
    attempt,
    failure_context
):
    original_bug = load_json(ACTIVE_BUG_REPORT_FILE)

    if not isinstance(original_bug, dict):
        original_bug = {}

    retry_path = resolve_artifact_path(
        f"bug_report_retry_{attempt}.json",
        BASE_DIR,
        ACTIVE_RUN_ARTIFACT_DIR,
    )

    retry_report = {
        **original_bug,
        "retry_attempt": attempt,
        "failure_context": failure_context,
        "instruction": (
            "Re-analyze the current failure using the repository "
            "context, reproduction result, current source, and "
            "failure information."
        ),
    }

    save_json(
        retry_path,
        retry_report
    )

    return retry_path


def load_retry_failure_context():
    failure_context = {
        "previous_reproduction_result": load_json(REPRODUCTION_RESULT_FILE),
        "manual_reproduction": load_json(MANUAL_REPRODUCTION_FILE),
        "previous_test_generation_result": load_json(TEST_GENERATION_RESULT_FILE),
        "previous_code_fix_result": load_json(CODE_FIX_RESULT_FILE),
        "previous_test_result": load_json(TEST_RESULT_FILE),
        "previous_build_result": load_json(BUILD_RESULT_FILE),
        "previous_validation_result": load_json(VALIDATION_RESULT_FILE),
        "previous_verification_result": load_json(VERIFICATION_REPORT_FILE),
        "previous_diagnosis": load_json(DIAGNOSIS_FILE),
    }
    confirmed_test = load_json(CONFIRMED_REPRODUCTION_TEST_FILE)
    if confirmed_test is not None:
        failure_context["confirmed_reproduction_test"] = confirmed_test
    confirmed_recording = load_json(run_artifact_path("confirmed_browser_recording.json"))
    if confirmed_recording is not None:
        failure_context["confirmed_browser_recording"] = confirmed_recording
    return failure_context


# ============================================================
# WORKFLOW REPORT
# ============================================================

def create_workflow_report(
    stage_results,
    retry_history,
    final_status,
    context,
    reproduction_source=None,
):
    strategy = get_strategy(context)

    report = {
        "workflow": (
            "AI Agentic Software Debugging Workflow"
        ),

        "target_repository": get_repository_path(
            context
        ),

        "timestamp": datetime.now().isoformat(),

        "detected_technology": strategy.get(
            "technology"
        ),

        "execution_strategy": strategy,

        "workflow_logic": [
            "GitHub / Repository",
            "Repository Analysis",
            "Technology Detection",
            "Bug Reproduction",
            "Failure Analysis",
            "Root Cause Identification",
            "AI Code Fix",
            "AI Test Generation",
            "Test Execution",
            "Verification",
            "Retry on Failure",
            "Final Workflow Report",
        ],

        "max_retries": MAX_RETRIES,

        "stages": stage_results,

        "retry_history": retry_history,

        "reproduction_result": load_json(
            REPRODUCTION_RESULT_FILE
        ),

        "diagnosis": load_json(
            DIAGNOSIS_FILE
        ),

        "test_result": load_json(
            TEST_RESULT_FILE
        ),

        "verification_report": load_json(
            VERIFICATION_REPORT_FILE
        ),

        "final_status": final_status,
        "deployment_status": "NOT_DEPLOYED",
        "deployment_readiness": "NOT_ASSESSED",
    }

    if reproduction_source:
        manual_reproduction = load_json(MANUAL_REPRODUCTION_FILE)
        report["reproduction_source"] = reproduction_source
        report["reproduction_evidence"] = {
            "automatic_result_ref": os.path.relpath(
                REPRODUCTION_RESULT_FILE,
                ACTIVE_RUN_ARTIFACT_DIR or BASE_DIR,
            ),
            "manual_report_ref": (
                os.path.relpath(
                    MANUAL_REPRODUCTION_FILE,
                    ACTIVE_RUN_ARTIFACT_DIR or BASE_DIR,
                )
                if reproduction_source == "manual"
                else None
            ),
            "manual_supporting_evidence": (
                manual_reproduction.get("supporting_evidence", [])
                if reproduction_source == "manual"
                and isinstance(manual_reproduction, dict)
                else []
            ),
        }
        evidence_index = load_json(
            os.path.join(ACTIVE_RUN_ARTIFACT_DIR or BASE_DIR, "evidence_index.json")
        )
        if isinstance(evidence_index, dict) and isinstance(evidence_index.get("files"), list):
            report["reproduction_evidence"]["attached_files"] = [
                {key: item.get(key) for key in ("name", "stored_as", "kind", "bytes", "sha256", "redacted")}
                for item in evidence_index["files"]
                if isinstance(item, dict)
            ]
        if reproduction_source == AI_REPRODUCTION_SOURCE:
            confirmed = load_json(CONFIRMED_REPRODUCTION_TEST_FILE)
            report["reproduction_evidence"].update({
                "confirmed_test_ref": os.path.relpath(
                    CONFIRMED_REPRODUCTION_TEST_FILE,
                    ACTIVE_RUN_ARTIFACT_DIR or BASE_DIR,
                ),
                "confirmed_test_file": (confirmed or {}).get("test_file"),
                "before_fix": ((confirmed or {}).get("before_fix") or {}).get("classification"),
                "confirmed_by": (confirmed or {}).get("confirmed_by"),
            })
        if reproduction_source == BROWSER_RECORDING_SOURCE:
            recording_path = confirmed_reproduction_file(BROWSER_RECORDING_SOURCE)
            confirmed = load_json(recording_path) or {}
            report["reproduction_evidence"].update({
                "confirmed_recording_ref": os.path.relpath(
                    recording_path,
                    ACTIVE_RUN_ARTIFACT_DIR or BASE_DIR,
                ),
                "recording_file": confirmed.get("test_file"),
                "recording_sha256": confirmed.get("content_sha256"),
                "recording_steps": len(confirmed.get("steps") or []),
                "before_fix": (confirmed.get("before_fix") or {}).get("classification"),
                "confirmed_by": confirmed.get("confirmed_by"),
            })

    save_json(
        WORKFLOW_REPORT_FILE,
        report
    )

    return report


# ============================================================
# MAIN WORKFLOW
# ============================================================

def _run_workflow():

    global ACTIVE_BUG_REPORT_FILE
    global ACTIVE_RUN_ARTIFACT_DIR
    global ACTIVE_WORKFLOW_PROGRESS
    global CHILD_ENV_OVERRIDES

    arguments = parse_workflow_arguments()
    repository_url = arguments.repository_url
    local_repository_path = arguments.local_repository
    selected_bug_report = (
        arguments.bug_report
        if arguments.bug_report and arguments.bug_report.strip()
        else os.getenv("BUG_REPORT_FILE", BUG_REPORT_FILE)
    )

    ACTIVE_BUG_REPORT_FILE = selected_bug_report
    CHILD_ENV_OVERRIDES = {
        "BUG_REPORT_FILE": selected_bug_report,
    }
    if repository_url:
        CHILD_ENV_OVERRIDES["GITHUB_REPOSITORY_URL"] = repository_url
    if local_repository_path:
        CHILD_ENV_OVERRIDES["LOCAL_REPOSITORY_PATH"] = local_repository_path
    if control_enabled():
        requested_run_directory = os.path.realpath(
            os.environ.get(WORKFLOW_RUN_DIRECTORY_ENV, "")
        )
        current_control_directory = control_directory()
        if (
            not requested_run_directory
            or current_control_directory is None
            or os.path.realpath(os.path.dirname(current_control_directory))
            != requested_run_directory
        ):
            raise ValueError("The UI workflow artifact directory is invalid.")
        configure_run_artifact_paths(requested_run_directory)
    else:
        configure_run_artifact_paths(create_run_artifact_dir())

    print_header(
        "AI AGENTIC SOFTWARE DEBUGGING WORKFLOW"
    )

    stage_results = []
    retry_history = []
    ACTIVE_WORKFLOW_PROGRESS = {
        "stage_results": stage_results,
        "retry_history": retry_history,
        "context": {},
        "reproduction_source": None,
    }

    workflow_failed = False
    workflow_blocked = False
    workflow_stopped = False
    final_status = "FAIL"
    tests_passed = False
    builds_passed = False
    runtime_passed = False
    verification_passed = False

    context = {}
    initial_reproduction = None
    reproduction_source = None
    ai_reproduction = {"status": None}
    ai_summary = {}
    tests_passed = False
    builds_passed = False
    runtime_passed = False
    reproduction_test_passed = False


    # ========================================================
    # STAGE 1
    # ========================================================

    print_stage(
        1,
        "Repository Analysis & Technology Detection"
    )

    result = run_agent(
        AGENTS["repository_analysis"]
    )

    context = load_json(
        REPOSITORY_CONTEXT_FILE
    )

    context_valid, context_error = validate_context(
        context
    )
    if isinstance(context, dict):
        ACTIVE_WORKFLOW_PROGRESS["context"] = context

    if result["success"] and context_valid:

        strategy = get_strategy(context)

        add_stage_result(
            stage_results,
            1,
            "Repository Analysis & Technology Detection",
            "SUCCESS",
            {
                "technology": strategy.get(
                    "technology"
                ),

                "execution_strategy": strategy,

                "repository_path": get_repository_path(
                    context
                ),
            },
        )

    else:

        add_stage_result(
            stage_results,
            1,
            "Repository Analysis & Technology Detection",
            "FAILED",
            {
                "reason": (
                    context_error
                    or "Repository analysis failed."
                ),

                "return_code": result[
                    "return_code"
                ],
            },
        )

        workflow_failed = True


    if arguments.validation_only:
        validation_result = run_validation_only(
            context if isinstance(context, dict) else {}
        )
        validation_passed = (
            result["success"]
            and context_valid
            and validation_result["status"] == "PASSED"
        )
        final_status = "PASS" if validation_passed else "FAIL"
        add_stage_result(
            stage_results,
            2,
            "Offline Validation",
            "SUCCESS" if validation_passed else "FAILED",
            validation_result,
        )
        report = create_workflow_report(
            stage_results,
            retry_history,
            final_status,
            context if isinstance(context, dict) else {},
        )
        return 0 if report["final_status"] == "PASS" else 1

    if arguments.local_websocket_demo:
        preflight_result = (
            run_validation_only(context, include_backend_tests=False)
            if result["success"] and context_valid
            else {
                "status": "FAILED",
                "return_code": 1,
                "checks": [],
                "error": context_error or "Repository analysis failed.",
            }
        )
        demo_result = run_level3_websocket_demo(
            context if isinstance(context, dict) else {},
            ACTIVE_RUN_ARTIFACT_DIR,
            preflight_validation=preflight_result,
            diagnosis_mode=(
                "groq_ai" if arguments.ai_diagnosis else "deterministic"
            ),
        )
        retry_history.extend(
            {
                "attempt": attempt.get("attempt"),
                "test_result": (
                    "PASS"
                    if attempt.get("test_result", {}).get("status") == "PASSED"
                    else "FAIL"
                ),
                "verification": "PASS" if attempt.get("verified") else "FAIL",
                "overall": "PASS" if attempt.get("verified") else "FAIL",
                "modified_files": attempt.get("fix_result", {}).get("modified_files", []),
                "semantic_fix_present": attempt.get("semantic_fix_present", False),
            }
            for attempt in demo_result.get("attempts", [])
            if isinstance(attempt, dict)
        )
        for demo_stage in demo_result.get("stages", []):
            add_stage_result(
                stage_results,
                demo_stage["stage"],
                demo_stage["name"],
                demo_stage["status"],
                demo_stage.get("details"),
            )
        final_status = (
            "PASS"
            if (
                result["success"]
                and context_valid
                and preflight_result.get("status") == "PASSED"
                and demo_result.get("final_status") == "PASS"
            )
            else "FAIL"
        )
        demo_result["preflight_validation"] = preflight_result
        demo_result["final_status"] = final_status
        demo_result["return_code"] = 0 if final_status == "PASS" else 1
        demo_result["status"] = "PASSED" if final_status == "PASS" else "FAILED"
        save_json(VALIDATION_RESULT_FILE, demo_result)
        report = create_workflow_report(
            stage_results,
            retry_history,
            final_status,
            context if isinstance(context, dict) else {},
        )
        report.update({
            "workflow": "Level 3 Local WebSocket Bug-Fix Demo",
            "workflow_logic": demo_result.get(
                "workflow_logic",
                report.get("workflow_logic", []),
            ),
            "diagnosis_mode": demo_result.get("diagnosis_mode", "deterministic"),
            "model_call": demo_result.get("model_call"),
            "diagnosis_validation": demo_result.get("diagnosis_validation"),
            "ai_powered_stages": demo_result.get("ai_powered_stages", []),
            "code_fix_mode": demo_result.get("code_fix_mode", "deterministic_demo"),
            "test_generation_mode": demo_result.get(
                "test_generation_mode",
                "deterministic_demo",
            ),
        })
        save_json(WORKFLOW_REPORT_FILE, report)
        print_header("LOCAL WEBSOCKET DEMO RESULT")
        print(f"FINAL STATUS: {final_status}")
        print(f"DIAGNOSIS MODE: {demo_result.get('diagnosis_mode', 'deterministic')}")
        print(
            "MODEL DIAGNOSIS: "
            + (
                "SUCCEEDED"
                if "diagnosis" in demo_result.get("ai_powered_stages", [])
                else "NOT USED OR NOT ACCEPTED"
            )
        )
        for demo_stage in demo_result.get("stages", []):
            print(f"STAGE {demo_stage['stage']}: {demo_stage['status']} - {demo_stage['name']}")

        reproduction_stage = next(
            (
                stage
                for stage in demo_result.get("stages", [])
                if stage.get("stage") == 2
            ),
            {},
        )
        baseline_test_result = reproduction_stage.get("details", {}).get("test_result", {})
        print(
            "BASELINE TESTS: "
            f"{baseline_test_result.get('passed', 0)} passed, "
            f"{baseline_test_result.get('failed', 0)} failed, "
            f"{baseline_test_result.get('tests', 0)} total"
        )
        attempts = demo_result.get("attempts", [])
        if attempts:
            final_attempt = attempts[-1]
            post_fix_result = final_attempt.get("test_result", {})
            print(
                "POST-FIX TESTS: "
                f"{post_fix_result.get('passed', 0)} passed, "
                f"{post_fix_result.get('failed', 0)} failed, "
                f"{post_fix_result.get('tests', 0)} total"
            )
            print(
                "FIXED FILES: "
                + ", ".join(
                    final_attempt.get("fix_result", {}).get("modified_files", [])
                )
            )
        print(f"RUN ARTIFACTS: {ACTIVE_RUN_ARTIFACT_DIR}")
        print(f"\nWorkflow Report:\n{WORKFLOW_REPORT_FILE}")
        return 0 if report["final_status"] == "PASS" else 1


    # ========================================================
    # STAGE 2
    # ========================================================

    if not workflow_failed:

        print_stage(
            2,
            "Bug Reproduction"
        )

        result = run_agent(
            AGENTS["bug_reproduction"]
        )

        reproduction = load_json(
            REPRODUCTION_RESULT_FILE
        )
        initial_reproduction = reproduction

        automatic_reproduction_confirmed = validate_reproduction()
        if result["success"] and automatic_reproduction_confirmed:
            reproduction_source = "automatic"

            add_stage_result(
                stage_results,
                2,
                "Bug Reproduction",
                "SUCCESS",
                reproduction,
            )

        elif automatic_reproduction_confirmed:
            reproduction_source = "unresolved_conflict"
            add_stage_result(
                stage_results,
                2,
                "Bug Reproduction",
                "FAILED",
                {
                    "status": reproduction.get("status"),
                    "return_code": result.get("return_code"),
                    "reason": (
                        "The reproduction agent failed, but its artifact reports "
                        "REPRODUCED. The contradiction must be resolved before "
                        "manual fallback or diagnosis can continue."
                    ),
                },
            )
            workflow_failed = True

        else:
            # First let the AI write a reproduction test that a human
            # confirms; typed steps are the fallback.
            ai_reproduction = collect_ai_reproduction_test(
                context if isinstance(context, dict) else {}
            )
            ai_summary = {
                key: ai_reproduction.get(key)
                for key in ("status", "reason", "rounds")
                if ai_reproduction.get(key) is not None
            }
        if (
            not workflow_failed
            and reproduction_source is None
            and not automatic_reproduction_confirmed
            and ai_reproduction["status"] == "CONFIRMED"
        ):
            reproduction_source = AI_REPRODUCTION_SOURCE
            confirmed = ai_reproduction["confirmed"]
            add_stage_result(
                stage_results,
                2,
                "Bug Reproduction",
                "TEST_CONFIRMED",
                {
                    "status": (
                        reproduction.get("status")
                        if isinstance(reproduction, dict)
                        else None
                    ),
                    "reason": (
                        "A user confirmed that the AI-written test reproduces the bug; "
                        "it fails on the current code."
                    ),
                    "reproduction_test": {
                        "test_file": confirmed.get("test_file"),
                        "framework": confirmed.get("framework"),
                        "round": confirmed.get("round"),
                        "before_fix": confirmed["before_fix"].get("classification"),
                    },
                    "ai_reproduction_test": ai_summary,
                },
            )
        elif (
            not workflow_failed
            and reproduction_source is None
            and not automatic_reproduction_confirmed
            and ai_reproduction["status"] == "TIMED_OUT"
        ):
            add_stage_result(
                stage_results,
                2,
                "Bug Reproduction",
                "FAILED",
                {"reason": ai_reproduction["reason"], "ai_reproduction_test": ai_summary},
            )
            workflow_failed = True
        elif (
            not workflow_failed
            and reproduction_source is None
            and not automatic_reproduction_confirmed
        ):
            manual_fallback = collect_manual_reproduction(reproduction)
            manual_context_valid = (
                manual_fallback["status"] == "CAPTURED"
                and validate_manual_reproduction()
            )
            if manual_fallback["status"] == "RECORDING_CONFIRMED":
                reproduction_source = BROWSER_RECORDING_SOURCE
                confirmed = manual_fallback["confirmed"]
                add_stage_result(
                    stage_results,
                    2,
                    "Bug Reproduction",
                    "RECORDING_CONFIRMED",
                    {
                        "status": (
                            reproduction.get("status")
                            if isinstance(reproduction, dict)
                            else None
                        ),
                        "reason": manual_fallback["reason"],
                        "browser_recording": {
                            "file": confirmed.get("test_file"),
                            "steps": len(confirmed.get("steps") or []),
                            "before_fix": confirmed["before_fix"].get("classification"),
                            "summary": confirmed["before_fix"].get("summary"),
                        },
                        "ai_reproduction_test": ai_summary,
                    },
                )
            elif manual_context_valid:
                reproduction_source = "manual"
                add_stage_result(
                    stage_results,
                    2,
                    "Bug Reproduction",
                    "MANUAL_CAPTURED",
                    {
                        "status": (
                            reproduction.get("status")
                            if isinstance(reproduction, dict)
                            else None
                        ),
                        "reason": manual_fallback["reason"],
                        "manual_fallback": manual_fallback,
                        "ai_reproduction_test": ai_summary,
                    },
                )
            else:
                if manual_fallback["status"] == "CAPTURED":
                    manual_fallback = {
                        "status": "INSUFFICIENT",
                        "reason": "Saved manual reproduction did not pass validation.",
                    }
                failure_reason = manual_fallback.get(
                    "reason",
                    "Bug reproduction did not return REPRODUCED. The workflow will not continue.",
                )
                add_stage_result(
                    stage_results,
                    2,
                    "Bug Reproduction",
                    "FAILED",
                    {
                        "status": (
                            reproduction.get("status")
                            if isinstance(reproduction, dict)
                            else None
                        ),
                        "reason": failure_reason,
                        "manual_fallback": manual_fallback,
                        "ai_reproduction_test": ai_summary,
                    },
                )
                workflow_failed = True


    # ========================================================
    # STAGE 3 - INITIAL DIAGNOSIS
    # ========================================================

    ACTIVE_WORKFLOW_PROGRESS["reproduction_source"] = reproduction_source

    if not workflow_failed:

        print_stage(
            3,
            "AI Failure Analysis & Root Cause Identification"
        )

        if os.path.exists(
            DIAGNOSIS_FILE
        ):
            os.remove(
                DIAGNOSIS_FILE
            )

        diagnosis_arguments = (
            ["--manual-reproduction", MANUAL_REPRODUCTION_FILE]
            if reproduction_source == "manual"
            else ["--reproduction-test", confirmed_reproduction_file(reproduction_source)]
            if reproduction_source in (AI_REPRODUCTION_SOURCE, BROWSER_RECORDING_SOURCE)
            else None
        )
        result = run_agent(
            AGENTS["bug_diagnosis"],
            diagnosis_arguments,
        )

        diagnosis_available = (
            result["success"]
            and validate_diagnosis()
        )

        if diagnosis_available:

            archive_path = archive_diagnosis(
                1
            )

            diagnosis = load_json(
                DIAGNOSIS_FILE
            )

            add_stage_result(
                stage_results,
                3,
                "AI Failure Analysis & Root Cause Identification",
                "SUCCESS",
                {
                    "diagnosis_file": (
                        DIAGNOSIS_FILE
                    ),

                    "archived_diagnosis": (
                        archive_path
                    ),

                    "root_cause": diagnosis.get(
                        "root_cause"
                    ),

                    "affected_files": diagnosis.get(
                        "affected_files"
                    ),
                },
            )

        else:

            add_stage_result(
                stage_results,
                3,
                "AI Failure Analysis & Root Cause Identification",
                "FAILED",
                {
                    "reason": diagnosis_failure_reason(
                        result,
                        "AI diagnosis failed or was invalid.",
                    )
                },
            )

            workflow_failed = True


    # ========================================================
    # DEBUGGING ATTEMPTS
    # ========================================================

    attempt = 0

    if not workflow_failed:
        execution_blockers = local_execution_blockers(context)
        blocker_attempts = 0
        while execution_blockers and control_enabled():
            remaining_retries = MAX_RETRIES - blocker_attempts
            allowed_actions = ["continue", "stop_safely"]
            if remaining_retries > 0:
                allowed_actions.insert(0, "retry")
            action = wait_for_blocker_action(
                "Pre-fix Local Dependency Preflight",
                "; ".join(execution_blockers),
                MAX_RETRIES,
                allowed_actions=allowed_actions,
            )
            if action == "stop":
                workflow_stopped = True
                break
            blocker_attempts += 1
            execution_blockers = local_execution_blockers(context)
        if execution_blockers:
            workflow_blocked = not workflow_stopped
            add_stage_result(
                stage_results,
                4,
                "Pre-fix Local Dependency Preflight",
                "STOPPED" if workflow_stopped else "BLOCKED",
                {"blockers": execution_blockers},
            )
            retry_history.append({
                "attempt": 0,
                "overall": "STOPPED" if workflow_stopped else "BLOCKED",
                "reason": (
                    "Workflow stopped safely before any later stage started."
                    if workflow_stopped
                    else "Required local test/build dependencies remain unavailable."
                ),
            })

    while (
        not workflow_failed
        and not workflow_blocked
        and not workflow_stopped
        and attempt <= MAX_RETRIES
    ):

        attempt += 1

        print_header(
            f"DEBUGGING ATTEMPT {attempt}"
        )


        # ====================================================
        # RETRY DIAGNOSIS
        # ====================================================

        if attempt > 1:

            failure_context = load_retry_failure_context()

            retry_report = create_retry_bug_report(
                attempt,
                failure_context
            )

            if os.path.exists(
                DIAGNOSIS_FILE
            ):
                os.remove(
                    DIAGNOSIS_FILE
                )

            print_stage(
                3,
                f"AI Re-Diagnosis (Attempt {attempt})"
            )

            diagnosis_arguments = [retry_report]
            if reproduction_source == "manual":
                diagnosis_arguments.extend([
                    "--manual-reproduction",
                    MANUAL_REPRODUCTION_FILE,
                ])
            elif reproduction_source in (AI_REPRODUCTION_SOURCE, BROWSER_RECORDING_SOURCE):
                diagnosis_arguments.extend([
                    "--reproduction-test",
                    confirmed_reproduction_file(reproduction_source),
                ])
            result = run_agent(
                AGENTS["bug_diagnosis"],
                diagnosis_arguments,
            )

            if (
                result["success"]
                and validate_diagnosis()
            ):

                archive_path = archive_diagnosis(
                    attempt,
                    "diagnosis_retry"
                )

                add_stage_result(
                    stage_results,
                    3,
                    f"AI Re-Diagnosis (Attempt {attempt})",
                    "SUCCESS",
                    {
                        "archive": archive_path
                    },
                )

            else:

                add_stage_result(
                    stage_results,
                    3,
                    f"AI Re-Diagnosis (Attempt {attempt})",
                    "FAILED",
                    {
                        "reason": diagnosis_failure_reason(
                            result,
                            "Re-diagnosis failed or was invalid.",
                        )
                    },
                )

                retry_history.append(
                    {
                        "attempt": attempt,
                        "overall": "FAIL",
                        "reason": diagnosis_failure_reason(
                            result,
                            "Re-diagnosis failed.",
                        ),
                    }
                )

                workflow_failed = True
                break


        # ====================================================
        # STAGE 4 - CODE FIX
        # ====================================================

        print_stage(
            4,
            f"AI Code Fix (Attempt {attempt})"
        )

        result, code_fix_result = run_code_fix_stage()
        fix_verified = (
            result["success"]
            and isinstance(code_fix_result, dict)
            and code_fix_result.get("status") == "SUCCESS"
            and any(
                isinstance(item, dict)
                and item.get("verified") is True
                for item in code_fix_result.get("modified_files", [])
            )
        )

        if not fix_verified:

            add_stage_result(
                stage_results,
                4,
                f"AI Code Fix (Attempt {attempt})",
                "FAILED",
                {
                    "return_code": (
                        result["return_code"]
                    ),
                    "code_fix_result": code_fix_result,
                },
            )

            retry_history.append(
                {
                    "attempt": attempt,
                    "overall": "FAIL",
                    "reason": "Code fix failed.",
                }
            )

            if attempt > MAX_RETRIES:

                workflow_failed = True
                break

            continue


        add_stage_result(
            stage_results,
            4,
            f"AI Code Fix (Attempt {attempt})",
            "SUCCESS",
            {
                "diagnosis": DIAGNOSIS_FILE
            },
        )


        # ====================================================
        # STAGE 5 - TEST GENERATION
        # ====================================================

        print_stage(
            5,
            f"AI Test Generation (Attempt {attempt})"
        )

        result = run_agent(
            AGENTS["test_generation"],
            [DIAGNOSIS_FILE]
        )

        if not result["success"]:

            add_stage_result(
                stage_results,
                5,
                f"AI Test Generation (Attempt {attempt})",
                "FAILED",
                {
                    "return_code": (
                        result["return_code"]
                    )
                },
            )

            retry_history.append(
                {
                    "attempt": attempt,
                    "overall": "FAIL",
                    "reason": (
                        "Test generation failed."
                    ),
                }
            )

            if attempt > MAX_RETRIES:

                workflow_failed = True
                break

            continue


        add_stage_result(
            stage_results,
            5,
            f"AI Test Generation (Attempt {attempt})",
            "SUCCESS",
            {
                "technology": get_strategy(
                    context
                ).get("technology"),

                "test_command": get_strategy(
                    context
                ).get("test_command"),
            },
        )


        # ====================================================
        # STAGE 6 - TEST EXECUTION
        # ====================================================

        print_stage(
            6,
            f"Test Execution (Attempt {attempt})"
        )

        result = run_agent(
            AGENTS["test_execution"]
        )

        test_result = load_json(
            TEST_RESULT_FILE
        )

        tests_passed = (
            result["success"]
            and validate_test_result()
        )


        # ====================================================
        # COMPONENT BUILD VALIDATION
        # ====================================================

        print_stage(
            6,
            f"Component Build Validation (Attempt {attempt})",
        )
        build_result = run_component_builds(context)
        builds_passed = build_result.get("status") in (
            "PASSED",
            "SKIPPED",
        )
        build_stage_status = (
            "BLOCKED"
            if build_result.get("status") == "BLOCKED"
            else "SUCCESS"
            if builds_passed
            else "FAILED"
        )
        add_stage_result(
            stage_results,
            6,
            f"Component Build Validation (Attempt {attempt})",
            build_stage_status,
            build_result,
        )


        # ====================================================
        # POST-FIX RUNTIME VALIDATION
        # ====================================================

        print_stage(
            6,
            f"Post-Fix Runtime Validation (Attempt {attempt})",
        )
        runtime_validation = run_post_fix_runtime_validation(
            initial_reproduction,
        )
        runtime_passed = runtime_validation.get("status") in ("PASSED", "SKIPPED")
        add_stage_result(
            stage_results,
            6,
            f"Post-Fix Runtime Validation (Attempt {attempt})",
            "SUCCESS" if runtime_passed else "FAILED",
            runtime_validation,
        )

        # The human-confirmed reproduction test failed before the fix;
        # it must now pass, unchanged.
        if reproduction_source == AI_REPRODUCTION_SOURCE:
            print_stage(
                6,
                f"Reproduction Test Re-run (Attempt {attempt})",
            )
            reproduction_rerun = run_confirmed_reproduction_test(
                context if isinstance(context, dict) else {}
            )
            reproduction_test_passed = reproduction_rerun["status"] == "PASSED"
            add_stage_result(
                stage_results,
                6,
                f"Reproduction Test Re-run (Attempt {attempt})",
                "SUCCESS" if reproduction_test_passed else "FAILED",
                reproduction_rerun,
            )
        elif reproduction_source == BROWSER_RECORDING_SOURCE:
            # The user's recording showed the bug before the fix; replayed
            # on the fixed app, every step and assertion must now pass.
            print_stage(
                6,
                f"Browser Recording Replay (Attempt {attempt})",
            )
            reproduction_rerun = run_confirmed_browser_recording(attempt)
            reproduction_test_passed = reproduction_rerun["status"] == "PASSED"
            record_browser_replay_as_runtime_validation(reproduction_rerun, attempt)
            add_stage_result(
                stage_results,
                6,
                f"Browser Recording Replay (Attempt {attempt})",
                "SUCCESS" if reproduction_test_passed else "FAILED",
                reproduction_rerun,
            )
        else:
            reproduction_test_passed = True

        add_stage_result(
            stage_results,
            6,
            f"Test Execution (Attempt {attempt})",
            "SUCCESS" if tests_passed else "FAILED",
            test_result
            or {
                "reason": (
                    "No current test result "
                    "was produced."
                )
            },
        )


        # ====================================================
        # STAGE 7 - VERIFICATION
        # ====================================================

        print_stage(
            7,
            f"Fix Verification (Attempt {attempt})"
        )

        result = run_agent(
            AGENTS["verification"],
            [DIAGNOSIS_FILE]
        )

        verification = load_json(
            VERIFICATION_REPORT_FILE
        )

        verification_passed = (
            result["success"]
            and validate_verification()
        )

        add_stage_result(
            stage_results,
            7,
            f"Fix Verification (Attempt {attempt})",
            "SUCCESS"
            if verification_passed
            else "FAILED",
            verification
            or {
                "reason": (
                    "No current verification "
                    "report was produced."
                )
            },
        )


        # ====================================================
        # ATTEMPT RESULT
        # ====================================================

        attempt_passed = (
            tests_passed
            and builds_passed
            and runtime_passed
            and reproduction_test_passed
            and verification_passed
        )

        retry_history.append(
            {
                "attempt": attempt,

                "test_result": (
                    "PASS"
                    if tests_passed
                    else "FAIL"
                ),

                "verification": (
                    "PASS"
                    if verification_passed
                    else "FAIL"
                ),

                "build": (
                    "PASS"
                    if builds_passed
                    else "FAIL"
                ),

                "runtime_validation": (
                    "PASS"
                    if runtime_passed
                    else "FAIL"
                ),

                "reproduction_test": (
                    ("PASS" if reproduction_test_passed else "FAIL")
                    if reproduction_source in (AI_REPRODUCTION_SOURCE, BROWSER_RECORDING_SOURCE)
                    else "NOT_APPLICABLE"
                ),

                "overall": (
                    "PASS"
                    if attempt_passed
                    else "FAIL"
                ),
            }
        )


        # ====================================================
        # SUCCESS
        # ====================================================

        if attempt_passed:

            final_status = "PASS"
            break

        if control_enabled() and attempt < MAX_RETRIES + 1:
            action = wait_for_blocker_action(
                "Fix Verification",
                blocker_reason_with_ai_limit(
                    "Test, build, runtime, or fix verification did not pass."
                ),
                MAX_RETRIES,
                allowed_actions=["retry", "stop_safely"],
            )
            if action == "stop":
                workflow_stopped = True
                break


        # ====================================================
        # RETRY LIMIT
        # ====================================================

        if attempt >= MAX_RETRIES + 1:

            workflow_failed = True
            break


        print(
            "\nTests or verification failed; "
            "starting a bounded retry."
        )


    # ========================================================
    # STAGE 8 - FINAL VERIFICATION
    # ========================================================

    print_stage(
        8,
        "Final Verification"
    )

    final_status = (
        "STOPPED"
        if workflow_stopped
        else "BLOCKED"
        if workflow_blocked
        else "PASS"
        if (
            not workflow_failed
            and validate_test_result()
            and builds_passed
            and runtime_passed
            and reproduction_test_passed
            and verification_passed
            and validate_verification()
        )
        else "FAIL"
    )

    # A bug that was never confirmed by an automated test (user-reported
    # steps only) is not proven fixed by passing tests: a human must approve.
    # A fix for a confirmed bug that looks wider than the bug needs (files the
    # diagnosis did not name, many separate changes) also needs approval.
    fix_review = None
    reproduction_confirmed = reproduction_source in CONFIRMED_SOURCES
    review_context = context if isinstance(context, dict) else {}
    scope_findings, fix_diff = [], None
    if final_status == "PASS" and reproduction_source != "manual":
        fix_diff = build_fix_diff(review_context)
        diagnosis_for_scope = load_json(DIAGNOSIS_FILE)
        scope_findings = fix_scope_findings(
            fix_diff,
            diagnosis_for_scope.get("affected_files", []) if isinstance(diagnosis_for_scope, dict) else [],
        )
    if final_status == "PASS" and (reproduction_source == "manual" or scope_findings):
        print_stage(8, "Human Fix Review")
        fix_review = review_unconfirmed_fix(
            review_context,
            {
                "tests": "PASS" if tests_passed else "FAIL",
                "build": "PASS" if builds_passed else "FAIL",
                "runtime_validation": "PASS" if runtime_passed else "FAIL",
                "verification": "PASS" if verification_passed else "FAIL",
            },
            scope_findings=scope_findings,
            fix_diff=fix_diff,
        )
        if fix_review["decision"] == "approve":
            review_status, final_status = "SUCCESS", "PASS"
        elif fix_review["decision"] == "reject":
            review_status, final_status = "FAILED", "FAIL"
        else:
            review_status = "SKIPPED"
            final_status = "NEEDS_REVIEW" if scope_findings else "UNCONFIRMED"
        add_stage_result(
            stage_results,
            8,
            "Human Fix Review",
            review_status,
            {
                key: fix_review.get(key)
                for key in ("decision", "feedback", "reviewed_by", "files", "diff_truncated", "scope_findings")
            },
        )


    # ========================================================
    # FINAL RESULT
    # ========================================================

    print_header(
        "FINAL WORKFLOW RESULT"
    )

    print(
        f"\nFINAL STATUS: {final_status}"
    )


    # ========================================================
    # STAGE 9 - WORKFLOW REPORT
    # ========================================================

    print_stage(
        9,
        "Workflow Report"
    )

    report = create_workflow_report(
        stage_results,
        retry_history,
        final_status,
        context if isinstance(context, dict) else {},
        reproduction_source,
    )
    report["reproduction_confirmed"] = reproduction_confirmed
    if AI_LIMIT_EVENTS:
        report["ai_limit"] = AI_LIMIT_EVENTS[-1]["message"]
    if fix_review is not None:
        report["fix_review"] = {
            key: fix_review.get(key)
            for key in ("decision", "feedback", "reviewed_by", "files", "diff_truncated", "checks", "scope_findings")
        }
        report["fix_review"]["artifact_ref"] = os.path.relpath(
            FIX_REVIEW_FILE, ACTIVE_RUN_ARTIFACT_DIR or BASE_DIR
        )
    save_json(WORKFLOW_REPORT_FILE, report)

    print(
        f"\nWorkflow Report:\n"
        f"{WORKFLOW_REPORT_FILE}"
    )

    return (
        0
        if report["final_status"] == "PASS"
        else 1
    )


def get_optional_langfuse_client():
    if os.getenv("WORKFLOW_TRACE_PROVIDER", "").strip().lower() != "langfuse":
        return None

    try:
        from dotenv import load_dotenv

        load_dotenv()
        if not (
            os.getenv("LANGFUSE_PUBLIC_KEY")
            and os.getenv("LANGFUSE_SECRET_KEY")
        ):
            print("Langfuse tracing unavailable; continuing without tracing.")
            return None

        from langfuse import get_client

        return get_client()
    except Exception as error:
        print(
            "Langfuse tracing unavailable; continuing without tracing "
            f"({type(error).__name__})."
        )
        return None


def run_with_optional_langfuse(workflow_runner, client_factory=None):
    client = (client_factory or get_optional_langfuse_client)()
    if client is None:
        return workflow_runner()

    try:
        observation_context = client.start_as_current_observation(
            as_type="agent",
            name="ai-agentic-debugging-workflow",
            input={"workflow": "repository-debugging", "mode": "orchestrated"},
        )
        observation = observation_context.__enter__()
    except Exception as error:
        print(
            "Langfuse tracing could not start; continuing without tracing "
            f"({type(error).__name__})."
        )
        return workflow_runner()

    try:
        return_code = workflow_runner()
    except BaseException as error:
        try:
            observation.update(output={
                "status": "FAILED",
                "error_type": type(error).__name__,
            })
        except Exception:
            pass
        try:
            observation_context.__exit__(type(error), error, error.__traceback__)
        except Exception:
            pass
        try:
            client.flush()
        except Exception:
            pass
        raise

    report = load_json(WORKFLOW_REPORT_FILE)
    stages = report.get("stages", []) if isinstance(report, dict) else []
    retry_history = report.get("retry_history", []) if isinstance(report, dict) else []
    safe_output = {
        "status": "PASS" if return_code == 0 else "FAIL",
        "final_status": report.get("final_status") if isinstance(report, dict) else "FAIL",
        "stages": [
            {
                "stage": stage.get("stage"),
                "name": stage.get("name"),
                "status": stage.get("status"),
            }
            for stage in stages
            if isinstance(stage, dict)
        ],
        "attempts": [
            {
                "attempt": attempt.get("attempt"),
                "overall": attempt.get("overall"),
            }
            for attempt in retry_history
            if isinstance(attempt, dict)
        ],
    }
    try:
        observation.update(output=safe_output)
    except Exception as error:
        print(f"Langfuse trace update failed ({type(error).__name__}).")
    try:
        observation_context.__exit__(None, None, None)
    except Exception as error:
        print(f"Langfuse trace close failed ({type(error).__name__}).")
    try:
        client.flush()
    except Exception as error:
        print(f"Langfuse flush failed ({type(error).__name__}).")
    return return_code


def main():
    try:
        return run_with_optional_langfuse(_run_workflow)
    except WorkflowStopRequested as stop:
        stopped_before = str(stop.args[0]) if stop.args else None
        run_directory_value = os.environ.get(WORKFLOW_RUN_DIRECTORY_ENV)
        if run_directory_value:
            run_directory = os.path.realpath(run_directory_value)
            runs_root = os.path.realpath(os.path.join(BASE_DIR, "workflow_runs"))
            try:
                inside_runs = os.path.commonpath((runs_root, run_directory)) == runs_root
            except ValueError:
                inside_runs = False
            progress = ACTIVE_WORKFLOW_PROGRESS
            run_paths_active = (
                ACTIVE_RUN_ARTIFACT_DIR is not None
                and os.path.realpath(ACTIVE_RUN_ARTIFACT_DIR) == run_directory
            )
            if inside_runs and progress is not None and run_paths_active:
                # Reuse normal report generation so completed stages,
                # retries and artifacts stay in the stopped report.
                add_stage_result(
                    progress["stage_results"],
                    None,
                    "Stopped Safely",
                    "STOPPED",
                    {"stopped_before_stage": stopped_before},
                )
                report = create_workflow_report(
                    progress["stage_results"],
                    progress["retry_history"],
                    "STOPPED",
                    progress["context"],
                    progress["reproduction_source"],
                )
                report.update({
                    "workflow_id": os.environ.get(WORKFLOW_ID_ENV),
                    "stopped_before_stage": stopped_before,
                    "stop_reason": "Stopped safely before the next stage; completed artifacts were preserved.",
                })
                save_json(WORKFLOW_REPORT_FILE, report)
            elif inside_runs:
                save_json(os.path.join(run_directory, "workflow_report.json"), {
                    "workflow": "AI Agentic Software Debugging Workflow",
                    "workflow_id": os.environ.get(WORKFLOW_ID_ENV),
                    "target_repository": os.environ.get("LOCAL_REPOSITORY_PATH"),
                    "final_status": "STOPPED",
                    "deployment_status": "NOT_DEPLOYED",
                    "deployment_readiness": "NOT_ASSESSED",
                    "stop_reason": "Stopped safely before the next stage; completed artifacts were preserved.",
                })
        return 1


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    sys.exit(main())