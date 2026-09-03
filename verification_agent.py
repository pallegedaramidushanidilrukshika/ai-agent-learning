import json
import os
import sys


# =========================================================
# CONFIGURATION
# =========================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TEST_RESULT_FILE = os.path.join(
    BASE_DIR,
    "test_result.json"
)

BUG_REPORT_FILE = (
    sys.argv[1]
    if len(sys.argv) > 1
    else os.path.join(BASE_DIR, "bug_report.json")
)

VERIFICATION_REPORT_FILE = os.path.join(
    BASE_DIR,
    "verification_report.json"
)


# =========================================================
# DISPLAY HELPERS
# =========================================================

def print_header(title):

    print("\n")
    print("=" * 65)
    print(title)
    print("=" * 65)


# =========================================================
# LOAD JSON FILE
# =========================================================

def load_json_file(file_path):

    if not os.path.exists(file_path):

        print(f"\nERROR: File not found:")
        print(file_path)

        return None

    try:

        with open(
            file_path,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except json.JSONDecodeError as error:

        print(f"\nERROR: Invalid JSON file:")
        print(error)

        return None


# =========================================================
# VERIFY TEST RESULTS
# =========================================================

def verify_test_results(test_result):

    results = []

    # -----------------------------------------------------
    # Check test execution status
    # -----------------------------------------------------

    status = test_result.get("status")

    total_tests = test_result.get("tests", 0)

    passed_tests = test_result.get("passed", 0)

    failed_tests = test_result.get("failed", 0)

    return_code = test_result.get("return_code")

    # -----------------------------------------------------
    # Application test execution
    # -----------------------------------------------------

    if return_code == 0:

        results.append({
            "check": "Test execution",
            "status": "PASS",
            "details": "Test command completed successfully."
        })

    else:

        results.append({
            "check": "Test execution",
            "status": "FAIL",
            "details": f"Test command returned code {return_code}."
        })

    # -----------------------------------------------------
    # Test count
    # -----------------------------------------------------

    results.append({
        "check": "Total tests",
        "status": "INFO",
        "value": total_tests
    })

    # -----------------------------------------------------
    # Passed tests
    # -----------------------------------------------------

    results.append({
        "check": "Passed tests",
        "status": "INFO",
        "value": passed_tests
    })

    # -----------------------------------------------------
    # Failed tests
    # -----------------------------------------------------

    if failed_tests == 0:

        results.append({
            "check": "Failed tests",
            "status": "PASS",
            "value": 0
        })

    else:

        results.append({
            "check": "Failed tests",
            "status": "FAIL",
            "value": failed_tests
        })

    # -----------------------------------------------------
    # Overall test status
    # -----------------------------------------------------

    if (
        status == "PASSED"
        and return_code == 0
        and failed_tests == 0
        and total_tests > 0
        and passed_tests == total_tests
    ):

        overall_status = "PASS"

    else:

        overall_status = "FAIL"

    return overall_status, results


# =========================================================
# MAIN VERIFICATION
# =========================================================

def main():

    print_header(
        "AI APPLICATION VERIFICATION AGENT"
    )

    print("\nVerification Agent")
    print("Purpose: Verify the result of the AI debugging workflow.")

    # -----------------------------------------------------
    # Load bug report
    # -----------------------------------------------------

    print("\nBug Report:")

    bug_report = load_json_file(BUG_REPORT_FILE)

    if bug_report:

        print(
            bug_report.get(
                "bug_title",
                "Bug information available"
            )
        )

    else:

        print("Bug report not found or unavailable.")

    # -----------------------------------------------------
    # Load test result
    # -----------------------------------------------------

    print("\nLoading test result...")

    test_result = load_json_file(
        TEST_RESULT_FILE
    )

    if test_result is None:

        print_header("VERIFICATION FAILED")

        print(
            "\ntest_result.json could not be loaded."
        )

        return 1

    # -----------------------------------------------------
    # Display test result
    # -----------------------------------------------------

    print("\nTest Result:")
    print(
        json.dumps(
            test_result,
            indent=4
        )
    )

    # -----------------------------------------------------
    # Verify results
    # -----------------------------------------------------

    print_header(
        "VERIFICATION ANALYSIS"
    )

    overall_status, verification_results = (
        verify_test_results(test_result)
    )

    for result in verification_results:

        if result["status"] == "INFO":

            print(
                f"{result['check']}: "
                f"{result['value']}"
            )

        else:

            print(
                f"{result['check']}: "
                f"{result['status']}"
            )

            if "details" in result:

                print(
                    f"  {result['details']}"
                )

    # -----------------------------------------------------
    # Final conclusion
    # -----------------------------------------------------

    print_header(
        "FINAL VERIFICATION"
    )

    if overall_status == "PASS":

        print(
            "\nPASS - The application changes "
            "passed all generated tests."
        )

        bug_status = "FIXED"

        recommendation = (
            "The bug fix is verified successfully. "
            "The workflow can proceed to the final report."
        )

    else:

        print(
            "\nFAIL - The application changes "
            "did not pass all tests."
        )

        bug_status = "NOT FIXED"

        recommendation = (
            "Review the failed tests and return "
            "to the diagnosis/code-fix stage."
        )

    print(f"\nBug Status: {bug_status}")
    print(f"Overall Status: {overall_status}")
    print(f"Recommendation: {recommendation}")

    # -----------------------------------------------------
    # Create final verification report
    # -----------------------------------------------------

    verification_report = {

        "verification_agent":
            "AI Application Verification Agent",

        "bug_report":
            BUG_REPORT_FILE,

        "test_result":
            TEST_RESULT_FILE,

        "bug_status":
            bug_status,

        "overall_status":
            overall_status,

        "test_summary": {

            "total":
                test_result.get("tests", 0),

            "passed":
                test_result.get("passed", 0),

            "failed":
                test_result.get("failed", 0),

            "return_code":
                test_result.get("return_code")

        },

        "verification_results":
            verification_results,

        "recommendation":
            recommendation

    }

    with open(
        VERIFICATION_REPORT_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            verification_report,
            f,
            indent=4
        )

    print("\nVerification report saved to:")

    print(
        VERIFICATION_REPORT_FILE
    )

    print_header(
        "VERIFICATION COMPLETED"
    )

    print(
        f"\nFINAL STATUS: {overall_status}"
    )

    return 0 if overall_status == "PASS" else 1


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":

    sys.exit(main())