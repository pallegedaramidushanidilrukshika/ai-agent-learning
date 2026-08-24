import os
import subprocess
import sys

# -----------------------------
# TEST PROJECT PATH
# -----------------------------

project_dir = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "demo_project"
)

test_file = os.path.join(
    project_dir,
    "test_calculator.py"
)

result = subprocess.run(
    [sys.executable, "-m", "pytest", test_file, "-v"],
    capture_output=True,
    text=True
)

print("\n=============================")
print("TEST RESULT CHECK")
print("=============================")

print("\nReturn Code:")
print(result.returncode)

print("\nPytest STDOUT:")
print(result.stdout)

print("\nPytest STDERR:")
print(result.stderr)

if result.returncode == 0:
    print("\nSTATUS: ALL TESTS PASSED")
else:
    print("\nSTATUS: TESTS FAILED")

    import re

    output = result.stdout

    failed_tests = re.findall(
        r"FAILED\s+(.+?)::(test_\w+)",
        output
    )

    if failed_tests:
        print("\n=============================")
        print("FAILURE DETAILS")
        print("=============================")

        for file_path, test_name in failed_tests:
            print("\nTest Name:")
            print(test_name)

            print("\nFile:")
            print(file_path)

        failure_lines = re.findall(
            r"(\S+\.py):(\d+):\s*(.*)",
            output
        )

        for file_path, line_number, reason in failure_lines:
            print("\nFailure Location:")
            print(f"File: {file_path}")
            print(f"Line: {line_number}")
            print(f"Reason: {reason}")