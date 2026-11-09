"""Run and judge a single AI-written reproduction test.

A reproduction test is accepted as evidence only when a human confirms it and
it fails on the current code because of the bug (assertion failure, not a
crash). After the fix, the same unchanged test must pass.

Supported runners: Jest and pytest. Other technologies fall back to the
manual reproduction form.
"""

import hashlib
import os
import shlex
import subprocess

from workflow_validation import parse_test_summary, project_process_environment


FAILS_ON_CURRENT_CODE = "FAILS_ON_CURRENT_CODE"
PASSES_ON_CURRENT_CODE = "PASSES_ON_CURRENT_CODE"
RUN_ERROR = "ERROR"
OUTPUT_TAIL_CHARS = 4000
RUN_TIMEOUT_SECONDS = 300

_JEST_SUITE_ERRORS = (
    "test suite failed to run",
    "cannot find module",
    "syntaxerror",
    "your test suite must contain at least one test",
    "no tests found",
)
_PYTEST_SUITE_ERRORS = (
    "error collecting",
    "errors during collection",
    "importerror",
    "modulenotfounderror",
    "syntaxerror",
    "no tests ran",
)


# A crash inside the test (missing file, undefined name, ...) is not evidence
# of the bug; only an assertion mismatch is.
_JEST_RUNTIME_ERRORS = (
    "enoent", "referenceerror", "typeerror", "syntaxerror", "rangeerror",
    "cannot find module", "is not a function", "is not defined",
    "cannot read properties of", "cannot access",
)
_PYTEST_RUNTIME_ERRORS = (
    "filenotfounderror", "modulenotfounderror", "importerror", "nameerror",
    "attributeerror", "typeerror", "keyerror", "indexerror", "syntaxerror",
)


def failure_kind(framework, output):
    """'assertion' when the failure is an expectation mismatch, else 'runtime_error'."""
    lowered = (output or "").lower()
    if framework == "jest":
        # Only inspect the failure report, not console output printed before it.
        marker = lowered.find("● ")  # "●" starts each failed-test block
        report = lowered[marker:] if marker >= 0 else lowered
        if any(term in report for term in _JEST_RUNTIME_ERRORS):
            return "runtime_error"
        return "assertion" if "expect(" in report else "runtime_error"
    error_lines = [line for line in lowered.splitlines() if line.startswith("e ")]
    if any(term in line for line in error_lines for term in _PYTEST_RUNTIME_ERRORS):
        return "runtime_error"
    return "assertion" if ("assertionerror" in lowered or any(line.lstrip("e ").startswith("assert") for line in error_lines)) else "runtime_error"


def content_sha256(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _command_parts(command):
    if isinstance(command, list):
        return [str(part) for part in command]
    if isinstance(command, str) and command.strip():
        try:
            return shlex.split(command, posix=False)
        except ValueError:
            return None
    return None


def select_runner(repository_context):
    """Return {framework, base_command, test_path} or None when unsupported."""
    if not isinstance(repository_context, dict):
        return None
    strategy = repository_context.get("execution_strategy")
    if not isinstance(strategy, dict):
        return None
    command = _command_parts(strategy.get("test_command"))
    if not command:
        return None
    command_text = " ".join(command).lower()
    framework_text = str(strategy.get("test_framework", "")).lower()
    technology_text = str(strategy.get("technology", "")).lower()

    if "jest" in command_text or "jest" in framework_text:
        if "jest" not in command_text:
            return None
        backend_path = strategy.get("backend_path")
        directory = (
            f"{str(backend_path).strip('/').replace(os.sep, '/')}/test"
            if isinstance(backend_path, str) and backend_path.strip()
            else "test"
        )
        return {
            "framework": "jest",
            "base_command": [
                part for part in command if part not in ("--watch", "--watchAll")
            ],
            "test_path": f"{directory}/ai-reproduction.test.js",
        }

    if "pytest" in command_text or (
        "python" in technology_text and "pytest" in framework_text
    ):
        if "pytest" not in command_text:
            return None
        return {
            "framework": "pytest",
            "base_command": command,
            "test_path": "tests/test_ai_reproduction.py",
        }
    return None


def single_test_command(runner):
    if runner["framework"] == "jest":
        return runner["base_command"] + ["--runTestsByPath", runner["test_path"]]
    return runner["base_command"] + [runner["test_path"], "-q"]


def run_single_test(repository, runner, timeout=RUN_TIMEOUT_SECONDS, runner_function=None):
    """Run only the reproduction test file and return structured evidence."""
    command = single_test_command(runner)
    try:
        process = (runner_function or subprocess.run)(
            command,
            cwd=repository,
            env=project_process_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "command": command,
            "return_code": None,
            "timed_out": True,
            "tests": None,
            "passed": None,
            "failed": None,
            "skipped": None,
            "suite_error": True,
            "output_tail": f"The test did not finish within {timeout} seconds.",
        }
    except OSError as error:
        return {
            "command": command,
            "return_code": None,
            "timed_out": False,
            "tests": None,
            "passed": None,
            "failed": None,
            "skipped": None,
            "suite_error": True,
            "output_tail": f"The test runner could not start: {error}",
        }

    output = (process.stdout or "") + "\n" + (process.stderr or "")
    tests, passed, failed, skipped = parse_test_summary(output)
    markers = _JEST_SUITE_ERRORS if runner["framework"] == "jest" else _PYTEST_SUITE_ERRORS
    lowered = output.lower()
    suite_error = any(marker in lowered for marker in markers)
    if runner["framework"] == "pytest" and process.returncode not in (0, 1):
        suite_error = True
    kind = failure_kind(runner["framework"], output) if process.returncode not in (0, None) else None
    return {
        "failure_kind": kind,
        "command": command,
        "return_code": process.returncode,
        "timed_out": False,
        "tests": tests,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "suite_error": suite_error,
        "output_tail": output.strip()[-OUTPUT_TAIL_CHARS:],
    }


def classify_run(run):
    """FAILS_ON_CURRENT_CODE only for real test failures, never for crashes."""
    if not isinstance(run, dict) or run.get("suite_error") or run.get("timed_out"):
        return RUN_ERROR
    tests, failed = run.get("tests"), run.get("failed")
    if type(tests) is not int or tests <= 0 or type(failed) is not int:
        return RUN_ERROR
    if run.get("return_code") == 0 and failed == 0:
        return PASSES_ON_CURRENT_CODE
    if run.get("return_code") not in (0, None) and failed > 0:
        # Older records have no failure_kind; treat them as before.
        if run.get("failure_kind", "assertion") != "assertion":
            return RUN_ERROR
        return FAILS_ON_CURRENT_CODE
    return RUN_ERROR


def passes_after_fix(run):
    return classify_run(run) == PASSES_ON_CURRENT_CODE
