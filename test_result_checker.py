import json
import os
import re
import shlex
import subprocess

from workflow_artifacts import resolve_artifact_path
from workflow_validation import (
    counts_are_consistent,
    parse_test_summary,
    run_bounded,
    project_process_environment,
)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONTEXT_PATH = resolve_artifact_path("repository_context.json", BASE_DIR)
OUTPUT_PATH = resolve_artifact_path("test_result.json", BASE_DIR)
TEST_GENERATION_RESULT_PATH = resolve_artifact_path(
    "test_generation_result.json",
    BASE_DIR,
)


def load_json(path):
    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_result(result):
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=False)

    print(json.dumps(result, indent=2, ensure_ascii=False))


def failed_result(message, command=None):
    return {
        "status": "FAILED",
        "return_code": 1,
        "tests": 0,
        "passed": 0,
        "failed": 0,
        "test_file": None,
        "command": command,
        "stdout": "",
        "stderr": message,
    }


def parse_node_test_summary(output_text):
    return parse_test_summary(output_text)


def main():
    context = load_json(CONTEXT_PATH)

    if not context:
        write_result(
            failed_result(
                "repository_context.json is missing or invalid."
            )
        )
        raise SystemExit(0)


    target_repo = context.get("repository_path")

    if not isinstance(target_repo, str) or not target_repo.strip():
        write_result(
            failed_result(
                "repository_path is missing from repository_context.json."
            )
        )
        raise SystemExit(0)


    target_repo = os.path.abspath(target_repo)

    if not os.path.isdir(target_repo):
        write_result(
            failed_result(
                f"repository_path does not exist: {target_repo}"
            )
        )
        raise SystemExit(0)


    execution_strategy = context.get("execution_strategy")

    if isinstance(execution_strategy, dict):
        test_command = execution_strategy.get("test_command")
    else:
        test_command = context.get("test_command")


    # ---------------------------------------------------------
    # Use generated test file when available
    # ---------------------------------------------------------
    generation_result = load_json(TEST_GENERATION_RESULT_PATH)

    # Tests that need the app the workflow started (configured HTTP checks,
    # or AI tests for an HTTP app) were already run with the app running;
    # running them again here, without it, would only fail.
    if (
        (
            generation_result.get("configured_http_reproduction") is True
            or generation_result.get("workflow_started_app") is True
        )
        and isinstance(generation_result.get("return_code"), int)
    ):
        generated_status = generation_result.get("status")
        return_code = generation_result["return_code"]
        tests = generation_result.get("tests")
        passed_count = generation_result.get("passed")
        failed_count = generation_result.get("failed")
        skipped_count = generation_result.get("skipped")
        passed = (
            return_code == 0
            and generated_status == "SUCCESS"
            and counts_are_consistent(
                tests,
                passed_count,
                failed_count,
                skipped_count,
            )
            and passed_count > 0
            and failed_count == 0
        )

        write_result({
            "status": "PASSED" if passed else "FAILED",
            "return_code": return_code,
            "tests": tests,
            "passed": passed_count,
            "failed": failed_count,
            "skipped": skipped_count,
            "test_file": generation_result.get("test_file"),
            "command": (
                lambda value: value if isinstance(value, str) else " ".join(str(part) for part in value)
            )(
                generation_result.get("execution_command")
                or generation_result.get("test_command")
                or []
            ),
            "stdout": generation_result.get("stdout", ""),
            "stderr": generation_result.get("stderr", ""),
            "message": generation_result.get("message"),
        })
        raise SystemExit(0 if passed else 1)

    generated_test_file = generation_result.get("test_file")
    execution_command = generation_result.get("execution_command")
    execution_working_directory = generation_result.get(
        "working_directory",
        ".",
    )

    if execution_command:
        test_command = execution_command

    if (
        not execution_command
        and
        isinstance(generated_test_file, str)
        and generated_test_file.strip()
    ):
        test_file_path = os.path.join(
            target_repo,
            generated_test_file
        )

        if os.path.isfile(test_file_path):
            test_command = f"node --test {generated_test_file}"

    if not test_command:
        write_result(
            failed_result(
                "No test command was detected in repository_context.json."
            )
        )
        raise SystemExit(0)


    if isinstance(test_command, list):
        command_parts = [str(part) for part in test_command]
    else:
        try:
            command_parts = shlex.split(str(test_command), posix=False)
        except ValueError as error:
            write_result(
                failed_result(
                    f"Invalid test command: {error}",
                    str(test_command),
                )
            )
            raise SystemExit(0)


    if not command_parts:
        write_result(
            failed_result(
                "The configured test command is empty.",
                str(test_command),
            )
        )
        raise SystemExit(0)


    try:
        working_directory = os.path.abspath(
            os.path.join(target_repo, execution_working_directory)
        )
        repository_prefix = target_repo + os.sep

        if (
            working_directory != target_repo
            and not working_directory.startswith(repository_prefix)
        ):
            write_result(
                failed_result(
                    "Test working directory is outside the target repository.",
                    " ".join(command_parts),
                )
            )
            raise SystemExit(0)

        process = run_bounded(
            command_parts,
            cwd=working_directory,
            env=project_process_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        write_result(
            failed_result(
                f"Test command could not be executed: {error}",
                " ".join(command_parts),
            )
        )
        raise SystemExit(0)


    stdout = process.stdout or ""
    stderr = process.stderr or ""
    combined = (stdout + "\n" + stderr).strip()

    tests, passed, failed, skipped = parse_test_summary(combined)

    status = "FAILED"

    generated_file_exists = (
        isinstance(generated_test_file, str)
        and os.path.isfile(os.path.join(target_repo, generated_test_file))
    )

    if process.returncode == 0 and (
        generated_file_exists or generation_result.get("status") == "SUCCESS"
    ) and isinstance(tests, int) and tests > 0 and isinstance(passed, int) and passed > 0 and failed == 0 and passed + skipped == tests:
        status = "PASSED"

    result = {
        "status": status,
        "return_code": process.returncode,
        "tests": tests,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "test_file": generated_test_file if isinstance(generated_test_file, str) else None,
        "command": " ".join(command_parts),
        "working_directory": working_directory,
        "stdout": stdout,
        "stderr": stderr,
    }

    write_result(result)

if __name__ == "__main__":
    main()
