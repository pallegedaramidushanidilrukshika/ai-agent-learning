import os
import subprocess
import sys
from typing import TypedDict, Optional

from dotenv import load_dotenv
from langgraph.graph import StateGraph, START, END


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

TEST_FILE = r"C:\Users\SINGER\OneDrive\Desktop\agent_workspace\ai-agent-demo-project\test_calculator.py"


# ============================================================
# WORKFLOW STATE
# ============================================================

class WorkflowState(TypedDict, total=False):
    test_output: str
    test_passed: bool

    failed_test: Optional[str]
    file_path: Optional[str]
    line_number: Optional[str]
    failure_reason: Optional[str]

    analysis: Optional[str]
    fix_applied: bool

    verification_output: str
    verification_passed: bool


# ============================================================
# NODE 1 — RUN TESTS
# ============================================================

def run_tests(state: WorkflowState):

    print("\n=============================")
    print("STEP 1 — RUN TESTS")
    print("=============================")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", TEST_FILE, "-v"],
        capture_output=True,
        text=True
    )

    output = result.stdout

    print(output)

    passed = result.returncode == 0

    if passed:
        print("\nSTATUS: ALL TESTS PASSED")
    else:
        print("\nSTATUS: TESTS FAILED")

    return {
        "test_output": output,
        "test_passed": passed
    }


# ============================================================
# NODE 2 — DETECT FAILURE
# ============================================================

def detect_failure(state: WorkflowState):

    print("\n=============================")
    print("STEP 2 — DETECT FAILURE")
    print("=============================")

    output = state["test_output"]

    if state.get("test_passed"):
        print("No failure detected.")
        return {}

    failed_test = None
    file_path = None
    line_number = None
    failure_reason = None

    # Find failed test
    for line in output.splitlines():

        if line.startswith("FAILED "):

            failed_part = line.replace("FAILED ", "").strip()

            if "::" in failed_part:

                file_path, failed_test = failed_part.rsplit(
                    "::",
                    1
                )

            break

    # Find failure location
    for line in output.splitlines():

        if "AssertionError" in line and ":" in line:

            failure_reason = "AssertionError"

    # Find line number from pytest traceback
    for line in output.splitlines():

        if "test_calculator.py:" in line:

            try:
                location = line.split("test_calculator.py:")[1]

                line_number = location.split(":")[0]

                file_path = (
                    "..\\agent_workspace\\ai-agent-demo-project"
                    "\\test_calculator.py"
                )

                break

            except Exception:
                pass

    print("\nExtracted Failure Information:")

    print("Failed Test:", failed_test)
    print("File:", file_path)
    print("Line:", line_number)
    print("Reason:", failure_reason)

    return {
        "failed_test": failed_test,
        "file_path": file_path,
        "line_number": line_number,
        "failure_reason": failure_reason
    }


# ============================================================
# NODE 3 — ANALYZE FAILURE
# ============================================================

def analyze_failure(state: WorkflowState):

    print("\n=============================")
    print("STEP 3 — ANALYZE FAILURE")
    print("=============================")

    failed_test = state.get("failed_test")
    file_path = state.get("file_path")
    line_number = state.get("line_number")
    reason = state.get("failure_reason")

    if not failed_test:
        return {
            "analysis": "No failure to analyze."
        }

    analysis = f"""
Failed test: {failed_test}

File: {file_path}

Line: {line_number}

Reason: {reason}

The test failure needs to be investigated and corrected.
"""

    print(analysis)

    return {
        "analysis": analysis
    }


# ============================================================
# NODE 4 — FIX CODE
# ============================================================

def fix_code(state: WorkflowState):

    print("\n=============================")
    print("STEP 4 — FIX CODE")
    print("=============================")

    file_path = state.get("file_path")

    if not file_path:
        print("No file available for fixing.")

        return {
            "fix_applied": False
        }

    # Convert relative path to absolute path
    absolute_path = os.path.abspath(
        os.path.join(
            os.getcwd(),
            file_path
        )
    )

    # If the relative path cannot resolve correctly,
    # use the known project path.
    if not os.path.exists(absolute_path):

        absolute_path = TEST_FILE

    print("Target file:")
    print(absolute_path)

    try:

        with open(
            absolute_path,
            "r",
            encoding="utf-8"
        ) as file:

            content = file.read()

        # Demonstration fix for our intentional failure.
        old_code = "assert 1 == 2"
        new_code = "assert 1 == 1"

        if old_code in content:

            content = content.replace(
                old_code,
                new_code,
                1
            )

            with open(
                absolute_path,
                "w",
                encoding="utf-8"
            ) as file:

                file.write(content)

            print("\nFix applied:")
            print("assert 1 == 2")
            print("        ↓")
            print("assert 1 == 1")

            return {
                "fix_applied": True
            }

        print("No matching failure found.")

        return {
            "fix_applied": False
        }

    except Exception as e:

        print("Fix error:", e)

        return {
            "fix_applied": False
        }


# ============================================================
# NODE 5 — VERIFY TESTS
# ============================================================

def verify_tests(state: WorkflowState):

    print("\n=============================")
    print("STEP 5 — VERIFY FIX")
    print("=============================")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", TEST_FILE, "-v"],
        capture_output=True,
        text=True
    )

    output = result.stdout

    print(output)

    passed = result.returncode == 0

    if passed:

        print("\n=============================")
        print("WORKFLOW SUCCESS")
        print("=============================")

        print("All tests passed.")

    else:

        print("\n=============================")
        print("VERIFICATION FAILED")
        print("=============================")

    return {
        "verification_output": output,
        "verification_passed": passed
    }


# ============================================================
# ROUTING FUNCTION
# ============================================================

def check_test_result(state: WorkflowState):

    if state.get("test_passed"):

        return "end"

    return "analyze"


# ============================================================
# BUILD LANGGRAPH
# ============================================================

workflow = StateGraph(WorkflowState)


# Add nodes

workflow.add_node(
    "run_tests",
    run_tests
)

workflow.add_node(
    "detect_failure",
    detect_failure
)

workflow.add_node(
    "analyze_failure",
    analyze_failure
)

workflow.add_node(
    "fix_code",
    fix_code
)

workflow.add_node(
    "verify_tests",
    verify_tests
)


# ============================================================
# DEFINE EDGES
# ============================================================

workflow.add_edge(
    START,
    "run_tests"
)


workflow.add_conditional_edges(
    "run_tests",
    check_test_result,
    {
        "analyze": "detect_failure",
        "end": END
    }
)


workflow.add_edge(
    "detect_failure",
    "analyze_failure"
)


workflow.add_edge(
    "analyze_failure",
    "fix_code"
)


workflow.add_edge(
    "fix_code",
    "verify_tests"
)


workflow.add_edge(
    "verify_tests",
    END
)


# ============================================================
# COMPILE
# ============================================================

app = workflow.compile()


# ============================================================
# RUN WORKFLOW
# ============================================================

if __name__ == "__main__":

    print("\n")
    print("==============================================")
    print("LANGGRAPH AI CODE TESTING WORKFLOW")
    print("==============================================")

    result = app.invoke({})

    print("\n==============================================")
    print("FINAL WORKFLOW STATUS")
    print("==============================================")

    if result.get("verification_passed"):

        print("SUCCESS — All tests passed.")

    elif result.get("test_passed"):

        print("SUCCESS — Tests were already passing.")

    else:

        print("Workflow completed with test failures.")