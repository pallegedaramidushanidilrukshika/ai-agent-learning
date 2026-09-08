import json
import os
import shutil
import subprocess
import sys
from datetime import datetime


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

REPOSITORY_CONTEXT_FILE = os.path.join(
    BASE_DIR,
    "repository_context.json"
)

REPRODUCTION_RESULT_FILE = os.path.join(
    BASE_DIR,
    "reproduction_result.json"
)

DIAGNOSIS_FILE = os.path.join(
    BASE_DIR,
    "diagnosis.json"
)

TEST_RESULT_FILE = os.path.join(
    BASE_DIR,
    "test_result.json"
)

VERIFICATION_REPORT_FILE = os.path.join(
    BASE_DIR,
    "verification_report.json"
)

WORKFLOW_REPORT_FILE = os.path.join(
    BASE_DIR,
    "workflow_report.json"
)


# ============================================================
# STALE ARTIFACTS
# ============================================================

STALE_ARTIFACTS = (
    DIAGNOSIS_FILE,
    TEST_RESULT_FILE,
    VERIFICATION_REPORT_FILE,
    WORKFLOW_REPORT_FILE,
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
    except (OSError, json.JSONDecodeError):
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

    try:
        result = subprocess.run(
            command,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
        )
    except OSError as error:
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

    return (
        result.get("status") in ("PASSED", "PASS")
        and result.get("return_code") == 0
        and isinstance(result.get("tests"), int)
        and result.get("tests") > 0
        and isinstance(result.get("passed"), int)
        and result.get("passed") == result.get("tests")
        and isinstance(result.get("failed"), int)
        and result.get("failed") == 0
    )


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

    return validate_test_result()


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

    archive_path = os.path.join(
        BASE_DIR,
        f"{prefix}_{attempt}.json"
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
    original_bug = load_json(BUG_REPORT_FILE)

    if not isinstance(original_bug, dict):
        original_bug = {}

    retry_path = os.path.join(
        BASE_DIR,
        f"bug_report_retry_{attempt}.json"
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


# ============================================================
# WORKFLOW REPORT
# ============================================================

def create_workflow_report(
    stage_results,
    retry_history,
    final_status,
    context,
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

    save_json(
        WORKFLOW_REPORT_FILE,
        report
    )

    return report


# ============================================================
# MAIN WORKFLOW
# ============================================================

def main():

    print_header(
        "AI AGENTIC SOFTWARE DEBUGGING WORKFLOW"
    )

    # Remove only generated stale artifacts.
    remove_stale_artifacts()

    stage_results = []
    retry_history = []

    workflow_failed = False
    final_status = "FAIL"

    context = {}


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

        if (
            result["success"]
            and validate_reproduction()
        ):

            add_stage_result(
                stage_results,
                2,
                "Bug Reproduction",
                "SUCCESS",
                reproduction,
            )

        else:

            add_stage_result(
                stage_results,
                2,
                "Bug Reproduction",
                "FAILED",
                {
                    "status": (
                        reproduction.get("status")
                        if isinstance(
                            reproduction,
                            dict
                        )
                        else None
                    ),

                    "reason": (
                        "Bug reproduction did not "
                        "return REPRODUCED. "
                        "The workflow will not continue."
                    ),
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

        result = run_agent(
            AGENTS["bug_diagnosis"]
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
                    "reason": (
                        "AI diagnosis failed "
                        "or was invalid."
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

            failure_context = {
                "previous_test_result": (
                    load_json(
                        TEST_RESULT_FILE
                    )
                ),

                "previous_verification_result": (
                    load_json(
                        VERIFICATION_REPORT_FILE
                    )
                ),

                "previous_diagnosis": (
                    load_json(
                        DIAGNOSIS_FILE
                    )
                ),
            }

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

            result = run_agent(
                AGENTS["bug_diagnosis"],
                [retry_report]
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
                        "reason": (
                            "Re-diagnosis failed "
                            "or was invalid."
                        )
                    },
                )

                retry_history.append(
                    {
                        "attempt": attempt,
                        "overall": "FAILED",
                        "reason": (
                            "Re-diagnosis failed."
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

        result = run_agent(
            AGENTS["code_fix"],
            [DIAGNOSIS_FILE]
        )

        if not result["success"]:

            add_stage_result(
                stage_results,
                4,
                f"AI Code Fix (Attempt {attempt})",
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
                    "overall": "FAILED",
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
                    "overall": "FAILED",
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


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    sys.exit(main())