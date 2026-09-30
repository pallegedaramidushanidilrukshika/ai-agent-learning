import json
import os
import re
import shlex
import subprocess


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONTEXT_PATH = os.path.join(BASE_DIR, "repository_context.json")
OUTPUT_PATH = os.path.join(BASE_DIR, "test_result.json")


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
    combined = output_text or ""

    total = 0
    passed = 0
    failed = 0

    patterns = [
        (r"(?i)(?:ℹ\s*)?tests?\s+(\d+)", "tests"),
        (r"(?i)(?:ℹ\s*)?pass(?:ed)?\s+(\d+)", "pass"),
        (r"(?i)(?:ℹ\s*)?fail(?:ed|ures)?\s+(\d+)", "fail"),
        (r"(?i)(\d+)\s+tests?", "tests"),
        (r"(?i)(\d+)\s+pass(?:ed)?", "pass"),
        (r"(?i)(\d+)\s+fail(?:ed|ures)?", "fail"),
    ]

    for pattern, key in patterns:
        match = re.search(pattern, combined)

        if match:
            value = int(match.group(1))

            if key == "tests":
                total = value
            elif key == "pass":
                passed = value
            elif key == "fail":
                failed = value

    if total == 0 and passed > 0 and failed > 0:
        total = passed + failed

    return total, passed, failed


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
TEST_GENERATION_RESULT_PATH = os.path.join(
    BASE_DIR,
    "test_generation_result.json"
)

generation_result = load_json(TEST_GENERATION_RESULT_PATH)

if (
    generation_result.get("configured_http_reproduction") is True
    and isinstance(generation_result.get("return_code"), int)
):
    generated_status = generation_result.get("status")
    return_code = generation_result["return_code"]
    passed = return_code == 0 and generated_status == "SUCCESS"

    write_result({
        "status": "PASSED" if passed else "FAILED",
        "return_code": return_code,
        "tests": generation_result.get("tests"),
        "passed": generation_result.get("passed"),
        "failed": generation_result.get("failed"),
        "test_file": generation_result.get("test_file"),
        "command": " ".join(
            str(part)
            for part in generation_result.get("execution_command", [])
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

    process = subprocess.run(
        command_parts,
        cwd=working_directory,
        capture_output=True,
        text=True,
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

tests, passed, failed = parse_node_test_summary(combined)

status = "FAILED"

generated_file_exists = (
    isinstance(generated_test_file, str)
    and os.path.isfile(os.path.join(target_repo, generated_test_file))
)

if process.returncode == 0 and (
    generated_file_exists or generation_result.get("status") == "SUCCESS"
):
    status = "PASSED"

if (
    status == "PASSED"
    and isinstance(tests, int)
    and isinstance(passed, int)
    and isinstance(failed, int)
    and (passed != tests or failed != 0)
):
    status = "FAILED"

result = {
    "status": status,
    "return_code": process.returncode,
    "tests": tests,
    "passed": passed,
    "failed": failed,
    "test_file": generated_test_file if isinstance(generated_test_file, str) else None,
    "command": " ".join(command_parts),
    "working_directory": working_directory,
    "stdout": stdout,
    "stderr": stderr,
}

write_result(result)