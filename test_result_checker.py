import os
import subprocess
import sys
import json
import re

# -----------------------------
# TARGET PROJECT
# -----------------------------

project_dir = r"C:\Users\SINGER\OneDrive\Desktop\target-projects\nodejs-todo"

test_file = os.path.join(
    project_dir,
    "test",
    "todo.test.js"
)

# Result file will be stored inside the learning project
result_file = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "test_result.json"
)

# -----------------------------
# HEADER
# -----------------------------

print("\n=============================")
print("AI TEST RESULT CHECKER")
print("=============================")

print("\nTarget Repository:")
print(project_dir)

print("\nTest File:")
print(test_file)

# -----------------------------
# CHECK TEST FILE
# -----------------------------

if not os.path.exists(test_file):
    print("\nERROR: Test file does not exist.")

    result_data = {
        "status": "FAILED",
        "return_code": 1,
        "tests": 0,
        "passed": 0,
        "failed": 0,
        "test_file": test_file,
        "error": "Test file does not exist."
    }

    with open(result_file, "w", encoding="utf-8") as f:
        json.dump(result_data, f, indent=4)

    sys.exit(1)

# -----------------------------
# RUN NODE TESTS
# -----------------------------

print("\nRunning Node.js tests...\n")

result = subprocess.run(
    [
        "node",
        "--test",
        test_file
    ],
    cwd=project_dir,
    capture_output=True,
    text=True
)

# -----------------------------
# DISPLAY RESULTS
# -----------------------------

print("=============================")
print("TEST EXECUTION RESULT")
print("=============================")

print("\nReturn Code:")
print(result.returncode)

print("\nTest Output:")
print(result.stdout)

if result.stderr:
    print("\nTest Errors:")
    print(result.stderr)

# -----------------------------
# EXTRACT TEST COUNTS
# -----------------------------

output = result.stdout + result.stderr

tests_match = re.search(r"(?:ℹ|#)\s*tests\s+(\d+)", output)
pass_match = re.search(r"(?:ℹ|#)\s*pass\s+(\d+)", output)
fail_match = re.search(r"(?:ℹ|#)\s*fail\s+(\d+)", output)

total_tests = int(tests_match.group(1)) if tests_match else 0
passed_tests = int(pass_match.group(1)) if pass_match else 0
failed_tests = int(fail_match.group(1)) if fail_match else 0

# -----------------------------
# FINAL STATUS
# -----------------------------

if result.returncode == 0 and total_tests > 0 and failed_tests == 0 and total_tests > 0 and failed_tests == 0:

    status = "PASSED"

    print("\n=============================")
    print("STATUS: ALL TESTS PASSED")
    print("=============================")

else:

    status = "FAILED"

    print("\n=============================")
    print("STATUS: TESTS FAILED")
    print("=============================")

# -----------------------------
# SAVE MACHINE-READABLE RESULT
# -----------------------------

result_data = {
    "status": status,
    "return_code": result.returncode,
    "tests": total_tests,
    "passed": passed_tests,
    "failed": failed_tests,
    "test_file": test_file
}

with open(result_file, "w", encoding="utf-8") as f:
    json.dump(result_data, f, indent=4)

print("\nTest result saved to:")
print(result_file)

# -----------------------------
# EXIT
# -----------------------------

if result.returncode != 0:
    sys.exit(result.returncode)