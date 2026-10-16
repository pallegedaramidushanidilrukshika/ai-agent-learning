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
from local_websocket_demo import run_level3_websocket_demo


# ============================================================
# BASE CONFIGURATION
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_RETRIES = 2


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
}


# ============================================================
# DISPLAY HELPERS
# ============================================================

def print_header(title):
    print(f"\n{'=' * 75}\n{title}\n{'=' * 75}")


def print_stage(number, name):
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

    child_environment = os.environ.copy()
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

    return {
        "success": result.returncode == 0,
        "return_code": result.returncode,
        "output": stdout,
        "error": stderr,
    }


def diagnosis_failure_reason(result, fallback):
    if not isinstance(result, dict):
        return fallback

    error = result.get("error")
    if isinstance(error, str) and error.strip():
        return f"{fallback} Details: {error.strip()[-2000:]}"
    return fallback


def run_code_fix_stage():
    if os.path.exists(CODE_FIX_RESULT_FILE):
        os.remove(CODE_FIX_RESULT_FILE)

    result = run_agent(
        AGENTS["code_fix"],
        [DIAGNOSIS_FILE],
    )
    return result, load_json(CODE_FIX_RESULT_FILE)


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

        expected_behavior = input(
            "Expected behavior ('cancel' to stop): "
        ).strip()
        if expected_behavior.lower() == "cancel":
            return cancelled()

        observed_behavior = input(
            "Observed behavior ('cancel' to stop): "
        ).strip()
        if observed_behavior.lower() == "cancel":
            return cancelled()

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
            "Manual reproduction evidence was captured and validated. "
            "The workflow can continue to diagnosis using the user-provided "
            "reproduction steps and supporting evidence."
        ),
    }



# ============================================================
# VALIDATE DIAGNOSIS
# ============================================================

def validate_diagnosis():
    diagnosis = load_json(DIAGNOSIS_FILE)

    if not isinstance(diagnosis, dict):
        return False

    required_fields = (
        "bug_title",
        "project_type",
        "failure_analysis",
        "root_cause",
        "affected_files",
        "evidence",
        "proposed_fix",
        "verification_plan",
    )

    return all(
        field in diagnosis
        for field in required_fields
    )


# ============================================================
# VALIDATE TEST RESULT
# ============================================================

def validate_test_result():
    result = load_json(TEST_RESULT_FILE)

    if not isinstance(result, dict):
        return False

    if (
        result.get("status") not in ("PASSED", "PASS", "SUCCESS")
        or result.get("return_code") != 0
    ):
        return False

    tests = result.get("tests")
    passed = result.get("passed")
    failed = result.get("failed")

    if all(isinstance(value, int) for value in (tests, passed, failed)):
        return tests > 0 and passed == tests and failed == 0

    return True


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
        install_command = None
        if package_manager in lockfiles:
            lockfile_path = os.path.join(working_directory, lockfiles[package_manager])
            if os.path.isfile(lockfile_path):
                install_command = [package_manager, "ci"] if package_manager == "npm" else [package_manager, "install", "--frozen-lockfile"]
            elif os.path.isfile(os.path.join(working_directory, "package.json")):
                install_command = [package_manager, "install"]

        try:
            if install_command:
                install_parts = command_parts(install_command)
                install_result = subprocess.run(
                    install_parts,
                    cwd=working_directory,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if install_result.returncode != 0:
                    results.append({
                        "component": component.get("name"),
                        "status": "FAILED",
                        "return_code": install_result.returncode,
                        "command": " ".join(install_parts),
                        "stdout": install_result.stdout or "",
                        "stderr": install_result.stderr or "",
                    })
                    continue

            build_parts = command_parts(component["build_command"])
            build_result = subprocess.run(
                build_parts,
                cwd=working_directory,
                capture_output=True,
                text=True,
                check=False,
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
        aggregate = {
            "status": "PASSED" if successful else "FAILED",
            "return_code": 0 if successful else 1,
            "components": results,
        }

    save_json(BUILD_RESULT_FILE, aggregate)
    return aggregate


def run_validation_only(context):
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

    if backend is None:
        setup_errors.append("No backend component was detected.")
    elif str(backend.get("test_framework") or "").lower() == "jest":
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
    else:
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
            process = subprocess.run(
                command_args,
                cwd=working_directory,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "npm_config_offline": "true"},
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
        and len(test_checks) == 2
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
    ):
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

    if details is not None:
        item["details"] = details

    stage_results.append(item)


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
    return {
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
    configure_run_artifact_paths(create_run_artifact_dir())

    print_header(
        "AI AGENTIC SOFTWARE DEBUGGING WORKFLOW"
    )

    stage_results = []
    retry_history = []

    workflow_failed = False
    final_status = "FAIL"
    tests_passed = False
    builds_passed = False
    runtime_passed = False
    verification_passed = False

    context = {}
    initial_reproduction = None
    reproduction_source = None
    tests_passed = False
    builds_passed = False
    runtime_passed = False


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
            run_validation_only(context)
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
            manual_fallback = collect_manual_reproduction(reproduction)
            manual_context_valid = (
                manual_fallback["status"] == "CAPTURED"
                and validate_manual_reproduction()
            )
            if manual_context_valid:
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
                    },
                )
                workflow_failed = True


    # ========================================================
    # STAGE 3 - INITIAL DIAGNOSIS
    # ========================================================

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

    while (
        not workflow_failed
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
        add_stage_result(
            stage_results,
            6,
            f"Component Build Validation (Attempt {attempt})",
            "SUCCESS" if builds_passed else "FAILED",
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
        "PASS"
        if (
            not workflow_failed
            and validate_test_result()
            and builds_passed
            and runtime_passed
            and verification_passed
            and validate_verification()
        )
        else "FAIL"
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
    return run_with_optional_langfuse(_run_workflow)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    sys.exit(main())