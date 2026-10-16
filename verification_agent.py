import json
import os
import sys

from workflow_artifacts import resolve_artifact_path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(
        encoding="utf-8",
        errors="replace",
    )


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONTEXT_PATH = resolve_artifact_path("repository_context.json", BASE_DIR)
TEST_RESULT_PATH = resolve_artifact_path("test_result.json", BASE_DIR)
BUILD_RESULT_PATH = resolve_artifact_path("build_result.json", BASE_DIR)
VALIDATION_RESULT_PATH = resolve_artifact_path("validation_result.json", BASE_DIR)
DIAGNOSIS_PATH = resolve_artifact_path("diagnosis.json", BASE_DIR)
REPORT_PATH = resolve_artifact_path("verification_report.json", BASE_DIR)


def load_json(path):
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return None


def save_report(report):
    with open(REPORT_PATH, "w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)

    print(json.dumps(report, indent=2, ensure_ascii=False))


def failed_report(reason, context=None, test_result=None, diagnosis=None):
    report = {
        "status": "FAIL",
        "reason": [reason],
        "technology": (context or {}).get("technology"),
        "validation_method": (context or {}).get("validation_method"),
        "test_command": (context or {}).get("test_command"),
        "build_command": (context or {}).get("build_command"),
        "changed_files": [],
        "test_result": test_result or {},
        "build_result": {},
        "validation_result": {},
        "diagnosis": diagnosis or {}
    }
    save_report(report)
    return report


repository_context = load_json(CONTEXT_PATH)
if not isinstance(repository_context, dict):
    failed_report("repository_context.json is missing or invalid.")
    raise SystemExit(0)

test_result = load_json(TEST_RESULT_PATH)
if not isinstance(test_result, dict):
    failed_report(
        "test_result.json is missing or invalid.",
        context=repository_context
    )
    raise SystemExit(0)

diagnosis = load_json(DIAGNOSIS_PATH)
if not isinstance(diagnosis, dict):
    diagnosis = {}

strategy = repository_context.get("execution_strategy", {})

if not isinstance(strategy, dict):
    strategy = {}

technology = strategy.get("technology")
test_command = strategy.get("test_command")
build_command = strategy.get("build_command")
validation_method = strategy.get("validation_method")
test_framework = strategy.get("test_framework")
architecture = repository_context.get("architecture", {})
components = architecture.get("components", []) if isinstance(architecture, dict) else []
component_builds = [
    component for component in components
    if isinstance(component, dict) and component.get("build_command")
]
test_generation = load_json(
    resolve_artifact_path("test_generation_result.json", BASE_DIR)
) or {}

reasons = []
build_result = {}
validation_result = {}

test_was_configured_or_generated = bool(test_command) or bool(
    test_generation.get("test_file")
)

if test_was_configured_or_generated:
    if test_result.get("return_code") != 0:
        reasons.append("The configured test command returned a non-zero exit code.")

    tests = test_result.get("tests")
    passed = test_result.get("passed")
    failed = test_result.get("failed")

    if isinstance(tests, int) and isinstance(passed, int) and passed != tests:
        reasons.append("Not all reported tests passed.")

    if isinstance(failed, int) and failed != 0:
        reasons.append("One or more test failures were reported.")

# Run and verify builds detected on individual application components.
if component_builds or build_command:
    build_result = load_json(BUILD_RESULT_PATH)

    if not isinstance(build_result, dict):
        reasons.append(
            "A build command is configured, but build_result.json is missing or invalid."
        )
    elif build_result.get("status") != "SKIPPED":
        if build_result.get("return_code") != 0:
            reasons.append("The build command returned a non-zero exit code.")

        if build_result.get("status") not in (None, "PASSED", "PASS", "SUCCESS"):
            reasons.append("The build result does not indicate success.")

validation_result = load_json(VALIDATION_RESULT_PATH)

if validation_result is not None:
    if not isinstance(validation_result, dict):
        reasons.append("validation_result.json is invalid.")
    elif validation_result.get("status") not in (
        "PASSED",
        "PASS",
        "SUCCESS",
        "FIXED",
        "SKIPPED",
    ):
        reasons.append("Runtime validation did not confirm the fix.")
elif validation_method and "http" in str(validation_method).lower():
    reasons.append("HTTP runtime validation result is missing.")

status = "PASS" if not reasons else "FAIL"

report = {
    "status": status,
    "reason": reasons or [
        "Verification succeeded: the configured test execution completed successfully."
    ],
    "technology": technology,
    "test_command": test_command,
    "build_command": build_command,
    "validation_method": validation_method,
    "test_framework": test_framework,
    "changed_files": [],
    "test_result": test_result,
    "build_result": build_result,
    "validation_result": validation_result,
    "diagnosis": diagnosis,
}

save_report(report)