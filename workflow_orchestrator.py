from ast import arguments
import os
import sys
import subprocess
import json

from click import command
from datetime import datetime


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TARGET_REPOSITORY = r"C:\Users\SINGER\OneDrive\Desktop\target-projects\nodejs-todo"

BUG_REPORT_FILE = os.path.join(BASE_DIR, "diagnosis.json")
BUG_REPORT_FILE_2 = os.path.join(BASE_DIR, "diagnosis_2.json")

REPOSITORY_CONTEXT_FILE = os.path.join(
    BASE_DIR,
    "repository_context.json"
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


# ============================================================
# QUOTA DETECTION
# ============================================================

def is_quota_error(output):
    """
    Detect Gemini/API quota errors from agent output.

    We do not treat every agent failure as a quota failure.
    Only known quota/rate-limit indicators are accepted.
    """

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

    return any(keyword in text for keyword in quota_keywords)


# ============================================================
# RUN AGENT
# ============================================================

def run_agent(script_name, arguments=None):
    """
    Execute an agent and return detailed execution information.

    Returns:
        {
            "success": True/False,
            "quota_error": True/False,
            "return_code": int,
            "output": str
        }
    """

    script_path = os.path.join(BASE_DIR, script_name)

    if not os.path.exists(script_path):
        print(f"ERROR: Agent file not found: {script_name}")

        return {
            "success": False,
            "quota_error": False,
            "return_code": -1,
            "output": f"Agent file not found: {script_name}"
        }

    print(f"\nRunning agent: {script_name}")
    print("-" * 70)

    try:

        command = [sys.executable, script_path]

        if arguments:
            command.extend(arguments)

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

        # Print agent output to terminal
        if output:
            print(output)

        quota_error = is_quota_error(output)

        if result.returncode == 0:

            print(f"\nCOMPLETED: {script_name}")

            return {
                "success": True,
                "quota_error": False,
                "return_code": result.returncode,
                "output": output
            }

        if quota_error:

            print(f"\nQUOTA ERROR DETECTED: {script_name}")

            return {
                "success": False,
                "quota_error": True,
                "return_code": result.returncode,
                "output": output
            }

        print(f"\nFAILED: {script_name}")
        print(f"Return code: {result.returncode}")

        return {
            "success": False,
            "quota_error": False,
            "return_code": result.returncode,
            "output": output
        }

    except Exception as e:

        error_text = str(e)

        print(f"\nERROR running {script_name}: {error_text}")

        return {
            "success": False,
            "quota_error": is_quota_error(error_text),
            "return_code": -1,
            "output": error_text
        }


# ============================================================
# FILE CHECK
# ============================================================

def file_exists(path):
    return os.path.exists(path)


def check_file(filename):
    path = os.path.join(BASE_DIR, filename)

    if os.path.exists(path):
        print(f"Output created: {filename}")
        return True

    print(f"WARNING: Expected output not found: {filename}")
    return False


# ============================================================
# TARGET SOURCE CHANGE CHECK
# ============================================================

def has_source_changes():
    """
    Check whether the target repository contains source changes
    compared with its current Git HEAD.

    This is used only as evidence that an existing fix is present.
    It does NOT create or fake a fix.
    """

    try:

        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=TARGET_REPOSITORY,
            text=True,
            capture_output=True
        )

        output = result.stdout.strip()

        if not output:
            return False

        print("\nExisting target repository changes detected:")
        print(output)

        return True

    except Exception as e:

        print(f"Could not inspect Git status: {e}")
        return False


# ============================================================
# EXISTING TEST CHECK
# ============================================================

def existing_tests_available():

    if os.path.exists(TARGET_TEST_FILE):

        print("\nExisting test suite found:")
        print(TARGET_TEST_FILE)

        return True

    print("\nNo existing target test suite found.")

    return False


# ============================================================
# LOAD JSON
# ============================================================

def load_json(path):

    if not os.path.exists(path):
        return None

    try:

        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception as e:

        print(f"Could not read JSON file: {path}")
        print(f"Reason: {e}")

        return None


# ============================================================
# TEST RESULT VALIDATION
# ============================================================

def validate_test_result():

    data = load_json(TEST_RESULT_FILE)

    if not data:

        print("\nERROR: test_result.json is missing or invalid.")
        return False

    print("\nTest result information:")

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

    status = str(data.get("status", "")).upper()

    if status in [
        "PASS",
        "PASSED",
        "ALL TESTS PASSED",
        "SUCCESS"
    ]:

        return True

    # Some versions of the test checker may use numeric results.
    total = data.get("total_tests")
    passed = data.get("passed_tests")
    failed = data.get("failed_tests")

    if (
        isinstance(total, int)
        and isinstance(passed, int)
        and isinstance(failed, int)
    ):

        return total > 0 and passed == total and failed == 0

    return False


# ============================================================
# VERIFICATION VALIDATION
# ============================================================

def validate_verification():

    data = load_json(VERIFICATION_REPORT_FILE)

    if not data:

        print("\nERROR: verification_report.json is missing.")
        return False

    print("\nVerification information:")

    bug_status = str(
        data.get("bug_status", data.get("Bug Status", ""))
    ).upper()

    overall_status = str(
        data.get("overall_status", data.get("Overall Status", ""))
    ).upper()

    print(f"Bug Status: {bug_status}")
    print(f"Overall Status: {overall_status}")

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
        "stage": stage_number,
        "name": stage_name,
        "status": status,
        "timestamp": datetime.now().isoformat()
    }

    if details:
        stage["details"] = details

    stage_results.append(stage)

    print("\nStage result:")
    print(json.dumps(stage, indent=4))


# ============================================================
# WORKFLOW REPORT
# ============================================================

def create_workflow_report(
    stage_results,
    final_status
):

    test_result = load_json(TEST_RESULT_FILE)
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

        "stages":
            stage_results,

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
            indent=4
        )

    print("\n")
    print("=" * 70)
    print("WORKFLOW REPORT CREATED")
    print("=" * 70)

    print(
        f"\nReport: {WORKFLOW_REPORT_FILE}"
    )

    return report


# ============================================================
# MAIN WORKFLOW
# ============================================================

def main():

    print_header(
        "AI AGENTIC SOFTWARE DEBUGGING WORKFLOW"
    )

    print("\nTarget Application:")
    print(TARGET_REPOSITORY)

    print("\nWorkflow:")
    print(
        "Repository Analysis"
        " -> Bug Diagnosis"
        " -> Code Fix"
        " -> Test Generation"
        " -> Test Execution"
        " -> Verification"
        " -> Final Report"
    )

    stage_results = []

    skipped_ai_stages = []

    workflow_failed = False
    ACTIVE_DIAGNOSIS_FILE = None

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

    if result["success"] and file_exists(
        REPOSITORY_CONTEXT_FILE
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
                    "Repository analysis did not complete successfully."
            }
        )

        workflow_failed = True


    # ========================================================
    # STAGE 2 - BUG DIAGNOSIS
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
            file_exists(BUG_REPORT_FILE)
            or
            file_exists(BUG_REPORT_FILE_2)
        )

        if result["success"] and diagnosis_available:

            diagnosis_file = (
                BUG_REPORT_FILE
                if file_exists(BUG_REPORT_FILE)
                else BUG_REPORT_FILE_2
            )
            ACTIVE_DIAGNOSIS_FILE = diagnosis_file
            add_stage_result(
                stage_results,
                2,
                "Bug Diagnosis",
                "SUCCESS",
                {
                    "diagnosis_file":
                        diagnosis_file
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
                        "Bug diagnosis did not complete successfully."
                }
            )

            workflow_failed = True


    # ========================================================
    # STAGE 3 - CODE FIX
    # ========================================================

    if not workflow_failed:

        print_stage(
            3,
            "AI Code Fix"
        )

        result = run_agent(
            AGENTS["code_fix"],
            [ACTIVE_DIAGNOSIS_FILE]
        )

        if result["success"]:

            add_stage_result(
                stage_results,
                3,
                "AI Code Fix",
                "SUCCESS",
                {
                    "message":
                        "AI code fix agent completed successfully."
                }
            )

        elif result["quota_error"]:

            print(
                "\nGemini quota was exhausted during Code Fix."
            )

            print(
                "Checking whether an existing source change "
                "is available..."
            )

            if has_source_changes():

                status = "SKIPPED_QUOTA"

                skipped_ai_stages.append(
                    "Code Fix"
                )

                add_stage_result(
                    stage_results,
                    3,
                    "AI Code Fix",
                    status,
                    {
                        "reason":
                            "Gemini quota exhausted.",
                        "fallback":
                            "Existing source changes detected.",
                        "note":
                            "This stage is NOT marked as AI SUCCESS."
                    }
                )

            else:

                add_stage_result(
                    stage_results,
                    3,
                    "AI Code Fix",
                    "FAILED",
                    {
                        "reason":
                            "Gemini quota exhausted and "
                            "no existing source change was found."
                    }
                )

                workflow_failed = True

        else:

            add_stage_result(
                stage_results,
                3,
                "AI Code Fix",
                "FAILED",
                {
                    "reason":
                        "Code fix agent failed for a non-quota reason."
                }
            )

            workflow_failed = True


    # ========================================================
    # STAGE 4 - TEST GENERATION
    # ========================================================

    if not workflow_failed:

        print_stage(
            4,
            "Test Generation"
        )

        result = run_agent(
            AGENTS["test_generation"],
            [ACTIVE_DIAGNOSIS_FILE]
        )

        if result["success"]:

            if existing_tests_available():

                add_stage_result(
                    stage_results,
                    4,
                    "Test Generation",
                    "SUCCESS",
                    {
                        "test_file":
                            TARGET_TEST_FILE,
                        "message":
                            "Test generation agent completed and "
                            "test file is available."
                    }
                )

            else:

                add_stage_result(
                    stage_results,
                    4,
                    "Test Generation",
                    "FAILED",
                    {
                        "reason":
                            "Agent completed but expected test file "
                            "was not found."
                    }
                )

                workflow_failed = True

        elif result["quota_error"]:

            print(
                "\nGemini quota was exhausted during "
                "Test Generation."
            )

            print(
                "Checking for an existing test suite..."
            )

            if existing_tests_available():

                skipped_ai_stages.append(
                    "Test Generation"
                )

                add_stage_result(
                    stage_results,
                    4,
                    "Test Generation",
                    "SKIPPED_QUOTA",
                    {
                        "reason":
                            "Gemini quota exhausted.",
                        "fallback":
                            "Existing test suite is available.",
                        "test_file":
                            TARGET_TEST_FILE,
                        "note":
                            "Existing tests will be executed "
                            "by the next stage."
                    }
                )

            else:

                add_stage_result(
                    stage_results,
                    4,
                    "Test Generation",
                    "FAILED",
                    {
                        "reason":
                            "Gemini quota exhausted and "
                            "no existing tests are available."
                    }
                )

                workflow_failed = True

        else:

            add_stage_result(
                stage_results,
                4,
                "Test Generation",
                "FAILED",
                {
                    "reason":
                        "Test generation failed for "
                        "a non-quota reason."
                }
            )

            workflow_failed = True


    # ========================================================
    # STAGE 5 - TEST RESULT CHECKER
    # ========================================================

    if not workflow_failed:

        print_stage(
            5,
            "Test Result Checker"
        )

        result = run_agent(
            AGENTS["code_fix"],
            [ACTIVE_DIAGNOSIS_FILE]
)

        if result["success"]:

            if validate_test_result():

                add_stage_result(
                    stage_results,
                    5,
                    "Test Result Checker",
                    "SUCCESS",
                    {
                        "test_result_file":
                            TEST_RESULT_FILE,
                        "tests":
                            "Executed and passed"
                    }
                )

            else:

                add_stage_result(
                    stage_results,
                    5,
                    "Test Result Checker",
                    "FAILED",
                    {
                        "reason":
                            "test_result.json exists but "
                            "validation indicates test failure."
                    }
                )

                workflow_failed = True

        else:

            add_stage_result(
                stage_results,
                5,
                "Test Result Checker",
                "FAILED",
                {
                    "reason":
                        "Test Result Checker failed."
                }
            )

            workflow_failed = True


    # ========================================================
    # STAGE 6 - VERIFICATION AGENT
    # ========================================================

    if not workflow_failed:

        print_stage(
            6,
            "Verification Agent"
        )

        result = run_agent(
            AGENTS["verification"]
        )

        if result["success"]:

            if validate_verification():

                add_stage_result(
                    stage_results,
                    6,
                    "Verification Agent",
                    "SUCCESS",
                    {
                        "verification_file":
                            VERIFICATION_REPORT_FILE
                    }
                )

            else:

                add_stage_result(
                    stage_results,
                    6,
                    "Verification Agent",
                    "FAILED",
                    {
                        "reason":
                            "Verification report does not "
                            "confirm the fix."
                    }
                )

                workflow_failed = True

        else:

            add_stage_result(
                stage_results,
                6,
                "Verification Agent",
                "FAILED",
                {
                    "reason":
                        "Verification agent failed."
                }
            )

            workflow_failed = True


    # ========================================================
    # STAGE 7 - FINAL VERIFICATION
    # ========================================================

    print_stage(
        7,
        "Final Verification"
    )

    if not workflow_failed:

        test_passed = validate_test_result()
        verification_passed = validate_verification()

        if test_passed and verification_passed:

            if skipped_ai_stages:

                final_status = (
                    "PASS_WITH_SKIPPED_AI_STAGE"
                )

                final_message = (
                    "Workflow passed, but one or more "
                    "AI stages were skipped because of "
                    "Gemini quota limitations."
                )

            else:

                final_status = "PASS"

                final_message = (
                    "Complete AI agentic debugging workflow "
                    "passed successfully."
                )

        else:

            final_status = "FAIL"

            final_message = (
                "Final verification failed."
            )

    else:

        final_status = "FAIL"

        final_message = (
            "Workflow failed before final verification."
        )

    print("\n")
    print("=" * 70)
    print("FINAL VERIFICATION RESULT")
    print("=" * 70)

    print(f"\nSTATUS: {final_status}")
    print(f"MESSAGE: {final_message}")

    if skipped_ai_stages:

        print("\nAI stages skipped due to quota:")

        for stage in skipped_ai_stages:
            print(f"  - {stage}")


    # ========================================================
    # STAGE 8 - WORKFLOW REPORT
    # ========================================================

    print_stage(
        8,
        "Workflow Report"
    )

    report = create_workflow_report(
        stage_results,
        final_status
    )

    print("\n")
    print("=" * 70)
    print("WORKFLOW COMPLETED")
    print("=" * 70)

    print(
        f"\nFinal Status: {report['final_status']}"
    )

    print(
        f"Workflow Report: {WORKFLOW_REPORT_FILE}"
    )

    print("\nStage Summary:")

    for stage in stage_results:

        print(
            f"  Stage {stage['stage']} - "
            f"{stage['name']}: "
            f"{stage['status']}"
        )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()