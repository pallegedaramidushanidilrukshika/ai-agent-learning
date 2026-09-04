import os
import sys
import subprocess
import json
from datetime import datetime


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TARGET_REPOSITORY = r"C:\Users\SINGER\OneDrive\Desktop\target-projects\nodejs-todo"

# Maximum number of re-fix/re-test attempts after the initial attempt
MAX_RETRIES = 2

# Initial diagnosis files
BUG_REPORT_FILE = os.path.join(
    BASE_DIR,
    "diagnosis.json"
)

BUG_REPORT_FILE_2 = os.path.join(
    BASE_DIR,
    "diagnosis_2.json"
)

# Repository context
REPOSITORY_CONTEXT_FILE = os.path.join(
    BASE_DIR,
    "repository_context.json"
)

# Test and verification outputs
TEST_RESULT_FILE = os.path.join(
    BASE_DIR,
    "test_result.json"
)

VERIFICATION_REPORT_FILE = os.path.join(
    BASE_DIR,
    "verification_report.json"
)

# Final workflow report
WORKFLOW_REPORT_FILE = os.path.join(
    BASE_DIR,
    "workflow_report.json"
)

# Target test file
TARGET_TEST_FILE = os.path.join(
    TARGET_REPOSITORY,
    "test",
    "todo.test.js"
)


# ============================================================
# AGENT STAGES
# ============================================================

AGENTS = {
    "repository_analysis": "repository_agent.py",
    "bug_diagnosis": "bug_diagnosis_agent.py",
    "code_fix": "code_fix_agent.py",
    "test_generation": "test_generation_agent.py",
    "test_result_checker": "test_result_checker.py",
    "verification": "verification_agent.py",
}


# ============================================================
# DISPLAY HELPERS
# ============================================================

def print_header(title):

    print("\n")
    print("=" * 70)
    print(title)
    print("=" * 70)


def print_stage(number, name):

    print("\n")
    print("-" * 70)
    print(f"STAGE {number}: {name}")
    print("-" * 70)


def print_attempt(attempt):

    print("\n")
    print("#" * 70)
    print(f"DEBUGGING ATTEMPT {attempt}")
    print("#" * 70)


# ============================================================
# QUOTA DETECTION
# ============================================================

def is_quota_error(output):

    if not output:
        return False

    text = output.lower()

    quota_keywords = [
        "resource_exhausted",
        "quota exceeded",
        "quotaexceeded",
        "generaterequestsperday",
        "generaterequestsperminute",
        "ratelimit",
        "rate limit",
        "too many requests",
        "429",
        "free tier",
    ]

    return any(
        keyword in text
        for keyword in quota_keywords
    )


# ============================================================
# RUN AGENT
# ============================================================

def run_agent(script_name, arguments=None):

    script_path = os.path.join(
        BASE_DIR,
        script_name
    )

    if not os.path.exists(script_path):

        print(
            f"ERROR: Agent file not found: "
            f"{script_name}"
        )

        return {
            "success": False,
            "quota_error": False,
            "return_code": -1,
            "output":
                f"Agent file not found: {script_name}"
        }

    print(
        f"\nRunning agent: {script_name}"
    )

    print("-" * 70)

    try:

        command = [
            sys.executable,
            script_path
        ]

        if arguments:

            command.extend(
                arguments
            )

        result = subprocess.run(
            command,
            cwd=BASE_DIR,
            text=True,
            capture_output=True
        )

        output = ""

        if result.stdout:

            output += result.stdout

        if result.stderr:

            output += "\n" + result.stderr

        if output:

            print(output)

        quota_error = is_quota_error(
            output
        )

        if result.returncode == 0:

            print(
                f"\nCOMPLETED: {script_name}"
            )

            return {
                "success": True,
                "quota_error": False,
                "return_code":
                    result.returncode,
                "output": output
            }

        if quota_error:

            print(
                f"\nQUOTA ERROR DETECTED: "
                f"{script_name}"
            )

            return {
                "success": False,
                "quota_error": True,
                "return_code":
                    result.returncode,
                "output": output
            }

        print(
            f"\nFAILED: {script_name}"
        )

        print(
            f"Return code: "
            f"{result.returncode}"
        )

        return {
            "success": False,
            "quota_error": False,
            "return_code":
                result.returncode,
            "output": output
        }

    except Exception as e:

        error_text = str(e)

        print(
            f"\nERROR running "
            f"{script_name}: "
            f"{error_text}"
        )

        return {
            "success": False,
            "quota_error":
                is_quota_error(
                    error_text
                ),
            "return_code": -1,
            "output": error_text
        }


# ============================================================
# FILE CHECK
# ============================================================

def file_exists(path):

    return os.path.exists(path)


def check_file(filename):

    path = os.path.join(
        BASE_DIR,
        filename
    )

    if os.path.exists(path):

        print(
            f"Output created: "
            f"{filename}"
        )

        return True

    print(
        f"WARNING: Expected output "
        f"not found: {filename}"
    )

    return False


# ============================================================
# TARGET SOURCE CHANGE CHECK
# ============================================================

def has_source_changes():

    try:

        result = subprocess.run(
            [
                "git",
                "status",
                "--short"
            ],
            cwd=TARGET_REPOSITORY,
            text=True,
            capture_output=True
        )

        output = result.stdout.strip()

        if not output:

            return False

        print(
            "\nExisting target repository "
            "changes detected:"
        )

        print(output)

        return True

    except Exception as e:

        print(
            f"Could not inspect Git status: "
            f"{e}"
        )

        return False


# ============================================================
# EXISTING TEST CHECK
# ============================================================

def existing_tests_available():

    if os.path.exists(
        TARGET_TEST_FILE
    ):

        print(
            "\nExisting test suite found:"
        )

        print(
            TARGET_TEST_FILE
        )

        return True

    print(
        "\nNo existing target "
        "test suite found."
    )

    return False


# ============================================================
# LOAD JSON
# ============================================================

def load_json(path):

    if not os.path.exists(path):

        return None

    try:

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception as e:

        print(
            f"Could not read JSON file: "
            f"{path}"
        )

        print(
            f"Reason: {e}"
        )

        return None


# ============================================================
# TEST RESULT VALIDATION
# ============================================================

def validate_test_result():

    data = load_json(
        TEST_RESULT_FILE
    )

    if not data:

        print(
            "\nERROR: test_result.json "
            "is missing or invalid."
        )

        return False

    print(
        "\nTest result information:"
    )

    print(
        f"Total tests: "
        f"{data.get('total_tests', data.get('tests', 'N/A'))}"
    )

    print(
        f"Passed tests: "
        f"{data.get('passed_tests', data.get('passed', 'N/A'))}"
    )

    print(
        f"Failed tests: "
        f"{data.get('failed_tests', data.get('failed', 'N/A'))}"
    )

    status = str(
        data.get(
            "status",
            ""
        )
    ).upper()

    if status in [
        "PASS",
        "PASSED",
        "ALL TESTS PASSED",
        "SUCCESS"
    ]:

        return True

    total = data.get(
        "total_tests"
    )

    passed = data.get(
        "passed_tests"
    )

    failed = data.get(
        "failed_tests"
    )

    if total is None:

        total = data.get(
            "tests"
        )

    if passed is None:

        passed = data.get(
            "passed"
        )

    if failed is None:

        failed = data.get(
            "failed"
        )

    if (
        isinstance(total, int)
        and isinstance(passed, int)
        and isinstance(failed, int)
    ):

        return (
            total > 0
            and passed == total
            and failed == 0
        )

    return False


# ============================================================
# VERIFICATION VALIDATION
# ============================================================

def validate_verification():

    data = load_json(
        VERIFICATION_REPORT_FILE
    )

    if not data:

        print(
            "\nERROR: "
            "verification_report.json "
            "is missing."
        )

        return False

    print(
        "\nVerification information:"
    )

    bug_status = str(
        data.get(
            "bug_status",
            data.get(
                "Bug Status",
                ""
            )
        )
    ).upper()

    overall_status = str(
        data.get(
            "overall_status",
            data.get(
                "Overall Status",
                ""
            )
        )
    ).upper()

    print(
        f"Bug Status: {bug_status}"
    )

    print(
        f"Overall Status: "
        f"{overall_status}"
    )

    if bug_status == "FIXED":

        return True

    if overall_status in [
        "PASS",
        "PASSED",
        "SUCCESS"
    ]:

        return True

    return False


# ============================================================
# STAGE RECORD
# ============================================================

def add_stage_result(
    stage_results,
    stage_number,
    stage_name,
    status,
    details=None
):

    stage = {

        "stage":
            stage_number,

        "name":
            stage_name,

        "status":
            status,

        "timestamp":
            datetime.now().isoformat()
    }

    if details:

        stage["details"] = details

    stage_results.append(
        stage
    )

    print(
        "\nStage result:"
    )

    print(
        json.dumps(
            stage,
            indent=4
        )
    )


# ============================================================
# CREATE RETRY FAILURE CONTEXT
# ============================================================

def create_retry_failure_context(
    attempt,
    diagnosis_file
):

    test_result = load_json(
        TEST_RESULT_FILE
    )

    verification = load_json(
        VERIFICATION_REPORT_FILE
    )

    context = {

        "retry_attempt":
            attempt,

        "previous_diagnosis":
            diagnosis_file,

        "test_result":
            test_result,

        "verification_result":
            verification,

        "instruction":
            (
                "The previous code fix did not "
                "successfully verify. Re-analyze "
                "the repository and the previous "
                "failure. Identify the actual root "
                "cause and propose a corrected "
                "minimal and safe fix."
            )
    }

    failure_file = os.path.join(
        BASE_DIR,
        f"bug_report_retry_{attempt}.json"
    )

    with open(
        failure_file,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            context,
            f,
            indent=4,
            ensure_ascii=False
        )

    print(
        "\nRetry failure context created:"
    )

    print(
        failure_file
    )

    return failure_file


# ============================================================
# CREATE WORKFLOW REPORT
# ============================================================

def create_workflow_report(
    stage_results,
    final_status,
    retry_history
):

    test_result = load_json(
        TEST_RESULT_FILE
    )

    verification_report = load_json(
        VERIFICATION_REPORT_FILE
    )

    report = {

        "workflow":
            "AI Agentic Software Debugging Workflow",

        "target_repository":
            TARGET_REPOSITORY,

        "timestamp":
            datetime.now().isoformat(),

        "workflow_logic":
            [
                "Repository Analysis",
                "Bug Diagnosis",
                "Code Fix",
                "Test Generation",
                "Test Execution",
                "Verification",
                "Retry on Failure",
                "Final Report"
            ],

        "max_retries":
            MAX_RETRIES,

        "stages":
            stage_results,

        "retry_history":
            retry_history,

        "test_result":
            test_result,

        "verification_report":
            verification_report,

        "final_status":
            final_status
    }

    with open(
        WORKFLOW_REPORT_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            report,
            f,
            indent=4,
            ensure_ascii=False
        )

    print("\n")
    print("=" * 70)
    print(
        "WORKFLOW REPORT CREATED"
    )
    print("=" * 70)

    print(
        f"\nReport: "
        f"{WORKFLOW_REPORT_FILE}"
    )

    return report


# ============================================================
# MAIN WORKFLOW
# ============================================================

def main():

    print_header(
        "AI AGENTIC SOFTWARE DEBUGGING WORKFLOW"
    )

    print(
        "\nTarget Application:"
    )

    print(
        TARGET_REPOSITORY
    )

    print(
        "\nWorkflow:"
    )

    print(
        "Repository Analysis"
        " -> Bug Diagnosis"
        " -> Code Fix"
        " -> Test Generation"
        " -> Test Execution"
        " -> Verification"
        " -> Retry if Failed"
        " -> Final Report"
    )

    stage_results = []

    retry_history = []

    skipped_ai_stages = []

    workflow_failed = False

    active_diagnosis_file = None

    # ========================================================
    # STAGE 1 - REPOSITORY ANALYSIS
    # ========================================================

    print_stage(
        1,
        "Repository Analysis"
    )

    result = run_agent(
        AGENTS["repository_analysis"]
    )

    if (
        result["success"]
        and file_exists(
            REPOSITORY_CONTEXT_FILE
        )
    ):

        add_stage_result(
            stage_results,
            1,
            "Repository Analysis",
            "SUCCESS",
            {
                "output_file":
                    REPOSITORY_CONTEXT_FILE
            }
        )

    else:

        add_stage_result(
            stage_results,
            1,
            "Repository Analysis",
            "FAILED",
            {
                "reason":
                    "Repository analysis did not "
                    "complete successfully."
            }
        )

        workflow_failed = True

    # ========================================================
    # STAGE 2 - INITIAL BUG DIAGNOSIS
    # ========================================================

    if not workflow_failed:

        print_stage(
            2,
            "Bug Diagnosis"
        )

        result = run_agent(
            AGENTS["bug_diagnosis"]
        )

        diagnosis_available = (
            file_exists(
                BUG_REPORT_FILE
            )
            or
            file_exists(
                BUG_REPORT_FILE_2
            )
        )

        if (
            result["success"]
            and diagnosis_available
        ):

            if file_exists(
                BUG_REPORT_FILE
            ):

                active_diagnosis_file = (
                    BUG_REPORT_FILE
                )

            else:

                active_diagnosis_file = (
                    BUG_REPORT_FILE_2
                )

            add_stage_result(
                stage_results,
                2,
                "Bug Diagnosis",
                "SUCCESS",
                {
                    "diagnosis_file":
                        active_diagnosis_file
                }
            )

        else:

            add_stage_result(
                stage_results,
                2,
                "Bug Diagnosis",
                "FAILED",
                {
                    "reason":
                        "Bug diagnosis did not "
                        "complete successfully."
                }
            )

            workflow_failed = True

    # ========================================================
    # RETRY / DEBUGGING LOOP
    # ========================================================

    if not workflow_failed:

        attempt = 0

        while attempt <= MAX_RETRIES:

            attempt += 1

            print_attempt(
                attempt
            )

            # ====================================================
            # RETRY DIAGNOSIS
            # ====================================================

            if attempt > 1:

                print_stage(
                    2,
                    f"Re-Diagnosis "
                    f"(Retry Attempt {attempt})"
                )

                retry_bug_report = (
                    create_retry_failure_context(
                        attempt - 1,
                        active_diagnosis_file
                    )
                )

                result = run_agent(
                    AGENTS["bug_diagnosis"],
                    [
                        retry_bug_report
                    ]
                )

                retry_diagnosis_file = os.path.join(
                    BASE_DIR,
                    f"diagnosis_retry_{attempt - 1}.json"
                )

                # The diagnosis agent normally creates:
                # bug_report_retry_X.json
                # -> diagnosis_retry_X.json

                if (
                    result["success"]
                    and
                    file_exists(
                        retry_diagnosis_file
                    )
                ):

                    active_diagnosis_file = (
                        retry_diagnosis_file
                    )

                    add_stage_result(
                        stage_results,
                        2,
                        f"Re-Diagnosis "
                        f"(Attempt {attempt})",
                        "SUCCESS",
                        {
                            "diagnosis_file":
                                active_diagnosis_file
                        }
                    )

                elif result["quota_error"]:

                    add_stage_result(
                        stage_results,
                        2,
                        f"Re-Diagnosis "
                        f"(Attempt {attempt})",
                        "SKIPPED_QUOTA",
                        {
                            "reason":
                                "Gemini quota exhausted "
                                "during retry diagnosis."
                        }
                    )

                    retry_history.append(
                        {
                            "attempt":
                                attempt,
                            "status":
                                "STOPPED_QUOTA",
                            "reason":
                                "Re-diagnosis quota exhausted."
                        }
                    )

                    workflow_failed = True

                    break

                else:

                    add_stage_result(
                        stage_results,
                        2,
                        f"Re-Diagnosis "
                        f"(Attempt {attempt})",
                        "FAILED",
                        {
                            "reason":
                                "Re-diagnosis failed."
                        }
                    )

                    retry_history.append(
                        {
                            "attempt":
                                attempt,
                            "status":
                                "FAILED",
                            "reason":
                                "Re-diagnosis failed."
                        }
                    )

                    workflow_failed = True

                    break

            # ====================================================
            # STAGE 3 - CODE FIX
            # ====================================================

            print_stage(
                3,
                f"AI Code Fix "
                f"(Attempt {attempt})"
            )

            result = run_agent(
                AGENTS["code_fix"],
                [
                    active_diagnosis_file
                ]
            )

            if result["success"]:

                add_stage_result(
                    stage_results,
                    3,
                    f"AI Code Fix "
                    f"(Attempt {attempt})",
                    "SUCCESS",
                    {
                        "diagnosis_file":
                            active_diagnosis_file,
                        "message":
                            "AI code fix agent completed "
                            "successfully."
                    }
                )

            elif result["quota_error"]:

                print(
                    "\nGemini quota was exhausted "
                    "during Code Fix."
                )

                if has_source_changes():

                    skipped_ai_stages.append(
                        f"Code Fix "
                        f"(Attempt {attempt})"
                    )

                    add_stage_result(
                        stage_results,
                        3,
                        f"AI Code Fix "
                        f"(Attempt {attempt})",
                        "SKIPPED_QUOTA",
                        {
                            "reason":
                                "Gemini quota exhausted.",
                            "fallback":
                                "Existing source changes detected.",
                            "note":
                                "This stage is NOT marked "
                                "as AI SUCCESS."
                        }
                    )

                else:

                    add_stage_result(
                        stage_results,
                        3,
                        f"AI Code Fix "
                        f"(Attempt {attempt})",
                        "FAILED",
                        {
                            "reason":
                                "Gemini quota exhausted "
                                "and no existing source "
                                "change was found."
                        }
                    )

                    retry_history.append(
                        {
                            "attempt":
                                attempt,
                            "status":
                                "FAILED",
                            "reason":
                                "Code fix unavailable."
                        }
                    )

                    workflow_failed = True

                    break

            else:

                add_stage_result(
                    stage_results,
                    3,
                    f"AI Code Fix "
                    f"(Attempt {attempt})",
                    "FAILED",
                    {
                        "reason":
                            "Code fix agent failed."
                    }
                )

                retry_history.append(
                    {
                        "attempt":
                            attempt,
                        "status":
                            "FAILED",
                        "reason":
                            "Code fix failed."
                    }
                )

                workflow_failed = True

                break

            # ====================================================
            # STAGE 4 - TEST GENERATION
            # ====================================================

            print_stage(
                4,
                f"Test Generation "
                f"(Attempt {attempt})"
            )

            result = run_agent(
                AGENTS["test_generation"],
                [
                    active_diagnosis_file
                ]
            )

            if result["success"]:

                if existing_tests_available():

                    add_stage_result(
                        stage_results,
                        4,
                        f"Test Generation "
                        f"(Attempt {attempt})",
                        "SUCCESS",
                        {
                            "test_file":
                                TARGET_TEST_FILE,
                            "message":
                                "Test generation agent "
                                "completed and test file "
                                "is available."
                        }
                    )

                else:

                    add_stage_result(
                        stage_results,
                        4,
                        f"Test Generation "
                        f"(Attempt {attempt})",
                        "FAILED",
                        {
                            "reason":
                                "Agent completed but expected "
                                "test file was not found."
                        }
                    )

                    workflow_failed = True

                    break

            elif result["quota_error"]:

                print(
                    "\nGemini quota was exhausted "
                    "during Test Generation."
                )

                if existing_tests_available():

                    skipped_ai_stages.append(
                        f"Test Generation "
                        f"(Attempt {attempt})"
                    )

                    add_stage_result(
                        stage_results,
                        4,
                        f"Test Generation "
                        f"(Attempt {attempt})",
                        "SKIPPED_QUOTA",
                        {
                            "reason":
                                "Gemini quota exhausted.",
                            "fallback":
                                "Existing test suite is available.",
                            "test_file":
                                TARGET_TEST_FILE
                        }
                    )

                else:

                    add_stage_result(
                        stage_results,
                        4,
                        f"Test Generation "
                        f"(Attempt {attempt})",
                        "FAILED",
                        {
                            "reason":
                                "Gemini quota exhausted "
                                "and no existing tests "
                                "are available."
                        }
                    )

                    workflow_failed = True

                    break

            else:

                add_stage_result(
                    stage_results,
                    4,
                    f"Test Generation "
                    f"(Attempt {attempt})",
                    "FAILED",
                    {
                        "reason":
                            "Test generation failed."
                    }
                )

                workflow_failed = True

                break

            # ====================================================
            # STAGE 5 - TEST RESULT CHECKER
            # ====================================================

            print_stage(
                5,
                f"Test Result Checker "
                f"(Attempt {attempt})"
            )

            # IMPORTANT:
            # This MUST run test_result_checker.py
            # and NOT code_fix_agent.py.

            result = run_agent(
                AGENTS["test_result_checker"]
            )

            test_passed = False

            if result["success"]:

                test_passed = (
                    validate_test_result()
                )

                if test_passed:

                    add_stage_result(
                        stage_results,
                        5,
                        f"Test Result Checker "
                        f"(Attempt {attempt})",
                        "SUCCESS",
                        {
                            "test_result_file":
                                TEST_RESULT_FILE,
                            "tests":
                                "All tests passed."
                        }
                    )

                else:

                    add_stage_result(
                        stage_results,
                        5,
                        f"Test Result Checker "
                        f"(Attempt {attempt})",
                        "FAILED",
                        {
                            "reason":
                                "Tests were executed but "
                                "not all tests passed."
                        }
                    )

            else:

                add_stage_result(
                    stage_results,
                    5,
                    f"Test Result Checker "
                    f"(Attempt {attempt})",
                    "FAILED",
                    {
                        "reason":
                            "Test Result Checker failed."
                    }
                )

            # ====================================================
            # STAGE 6 - VERIFICATION AGENT
            # ====================================================

            print_stage(
                6,
                f"Verification Agent "
                f"(Attempt {attempt})"
            )

            result = run_agent(
                AGENTS["verification"],
                [
                    active_diagnosis_file
                ]
            )

            verification_passed = False

            if result["success"]:

                verification_passed = (
                    validate_verification()
                )

                if verification_passed:

                    add_stage_result(
                        stage_results,
                        6,
                        f"Verification Agent "
                        f"(Attempt {attempt})",
                        "SUCCESS",
                        {
                            "verification_file":
                                VERIFICATION_REPORT_FILE,
                            "bug_status":
                                "FIXED"
                        }
                    )

                else:

                    add_stage_result(
                        stage_results,
                        6,
                        f"Verification Agent "
                        f"(Attempt {attempt})",
                        "FAILED",
                        {
                            "reason":
                                "Verification does not "
                                "confirm the fix."
                        }
                    )

            else:

                add_stage_result(
                    stage_results,
                    6,
                    f"Verification Agent "
                    f"(Attempt {attempt})",
                    "FAILED",
                    {
                        "reason":
                            "Verification agent failed."
                    }
                )

            # ====================================================
            # ATTEMPT RESULT
            # ====================================================

            attempt_passed = (
                test_passed
                and
                verification_passed
            )

            retry_history.append(
                {
                    "attempt":
                        attempt,

                    "diagnosis":
                        active_diagnosis_file,

                    "test_result":
                        (
                            "PASS"
                            if test_passed
                            else "FAIL"
                        ),

                    "verification":
                        (
                            "PASS"
                            if verification_passed
                            else "FAIL"
                        ),

                    "overall":
                        (
                            "PASS"
                            if attempt_passed
                            else "FAIL"
                        )
                }
            )

            # ====================================================
            # SUCCESS -> EXIT LOOP
            # ====================================================

            if attempt_passed:

                print_header(
                    f"ATTEMPT {attempt} "
                    "VERIFICATION PASSED"
                )

                print(
                    "\nThe bug fix has been "
                    "successfully verified."
                )

                workflow_failed = False

                break

            # ====================================================
            # FAILURE -> RETRY
            # ====================================================

            print_header(
                f"ATTEMPT {attempt} FAILED"
            )

            if attempt <= MAX_RETRIES:

                print(
                    "\nRetrying the debugging workflow..."
                )

                print(
                    f"Retry {attempt} of "
                    f"{MAX_RETRIES}"
                )

                print(
                    "\nFailure information will be "
                    "provided to the re-diagnosis agent."
                )

                continue

            else:

                print(
                    "\nMaximum retry attempts reached."
                )

                workflow_failed = True

                break

    # ========================================================
    # STAGE 7 - FINAL VERIFICATION
    # ========================================================

    print_stage(
        7,
        "Final Verification"
    )

    test_passed = validate_test_result()

    verification_passed = (
        validate_verification()
    )

    if (
        not workflow_failed
        and test_passed
        and verification_passed
    ):

        if skipped_ai_stages:

            final_status = (
                "PASS_WITH_SKIPPED_AI_STAGE"
            )

            final_message = (
                "Workflow passed successfully, "
                "but one or more AI stages were "
                "skipped because of Gemini quota "
                "limitations."
            )

        else:

            final_status = "PASS"

            final_message = (
                "Complete AI agentic debugging "
                "workflow passed successfully."
            )

    else:

        final_status = "FAIL"

        final_message = (
            "The debugging workflow could not "
            "successfully verify the bug fix."
        )

    print("\n")
    print("=" * 70)
    print("FINAL VERIFICATION RESULT")
    print("=" * 70)

    print(
        f"\nSTATUS: {final_status}"
    )

    print(
        f"MESSAGE: {final_message}"
    )

    if skipped_ai_stages:

        print(
            "\nAI stages skipped due to quota:"
        )

        for stage in skipped_ai_stages:

            print(
                f"  - {stage}"
            )

    # ========================================================
    # STAGE 8 - WORKFLOW REPORT
    # ========================================================

    print_stage(
        8,
        "Workflow Report"
    )

    report = create_workflow_report(
        stage_results,
        final_status,
        retry_history
    )

    print("\n")
    print("=" * 70)
    print("WORKFLOW COMPLETED")
    print("=" * 70)

    print(
        f"\nFinal Status: "
        f"{report['final_status']}"
    )

    print(
        f"Workflow Report: "
        f"{WORKFLOW_REPORT_FILE}"
    )

    print(
        "\nStage Summary:"
    )

    for stage in stage_results:

        print(
            f"  Stage {stage['stage']} - "
            f"{stage['name']}: "
            f"{stage['status']}"
        )

    print(
        "\nRetry Summary:"
    )

    for retry in retry_history:

        print(
            f"  Attempt "
            f"{retry.get('attempt')}: "
            f"{retry.get('overall', 'N/A')}"
        )

    return 0 if final_status != "FAIL" else 1


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    sys.exit(
        main()
    )