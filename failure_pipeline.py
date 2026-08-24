import subprocess
import sys
import re
from pathlib import Path

# -----------------------------
# TEST FILE
# -----------------------------

PROJECT_DIR = Path(__file__).resolve().parent

test_file = PROJECT_DIR / "demo_project" / "test_calculator.py"


# -----------------------------
# GET FAILURE INFORMATION
# -----------------------------

def get_failure_info():

    # -----------------------------
    # RUN PYTEST
    # -----------------------------

    result = subprocess.run(
        [sys.executable, "-m", "pytest", test_file, "-v"],
        capture_output=True,
        text=True
    )

    output = result.stdout

    # -----------------------------
    # PRINT PYTEST RESULT
    # -----------------------------

    print("\n=============================")
    print("PYTEST RESULT")
    print("=============================")

    print(output)

    # -----------------------------
    # CHECK RESULT
    # -----------------------------

    if result.returncode == 0:

        print("\nSTATUS: ALL TESTS PASSED")

        return None

    else:

        print("\nSTATUS: TESTS FAILED")

        # -----------------------------
        # EXTRACT FAILED TEST
        # -----------------------------

        failed_tests = re.findall(
            r"FAILED\s+(.+?)::(test_\w+)",
            output
        )

        # -----------------------------
        # EXTRACT FAILURE LOCATION
        # -----------------------------

        failure_locations = re.findall(
            r"(\S+\.py):(\d+):\s*(.*)",
            output
        )

        # -----------------------------
        # EXTRACT FAILURE INFORMATION
        # -----------------------------

        failure_info = {
            "failed_test": (
                failed_tests[0][1]
                if failed_tests
                else "Unknown"
            ),

            "file_path": (
                failure_locations[0][0]
                if failure_locations
                else "Unknown"
            ),

            "line_number": (
                failure_locations[0][1]
                if failure_locations
                else "Unknown"
            ),

            "reason": (
                "AssertionError"
                if "AssertionError" in output
                else "Unknown"
            )
        }

        # -----------------------------
        # PRINT FAILURE INFORMATION
        # -----------------------------

        print("\n=============================")
        print("EXTRACTED FAILURE INFORMATION")
        print("=============================")

        print("\nFailure Info:")
        print(failure_info)

        return failure_info


# -----------------------------
# DIRECT TEST
# -----------------------------

if __name__ == "__main__":

    failure_info = get_failure_info()