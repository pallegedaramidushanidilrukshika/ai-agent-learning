import os
import sys
import subprocess

from dotenv import load_dotenv
from langfuse import get_client


# ---------------------------------
# LOAD ENVIRONMENT VARIABLES
# ---------------------------------

load_dotenv()

langfuse = get_client()


# ---------------------------------
# PROJECT PATHS
# ---------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TEST_FILE = os.path.abspath(
    os.path.join(
        BASE_DIR,
        "..",
        "agent_workspace",
        "ai-agent-demo-project",
        "test_calculator.py"
    )
)


# ---------------------------------
# RUN PYTHON SCRIPT
# ---------------------------------

def run_script(script_name):

    result = subprocess.run(
        [sys.executable, script_name],
        cwd=BASE_DIR,
        capture_output=True,
        text=True
    )

    output = result.stdout

    if result.stderr:
        output += "\n" + result.stderr

    return result.returncode, output


# ---------------------------------
# RUN PYTEST
# ---------------------------------

def run_pytest():

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            TEST_FILE,
            "-v"
        ],
        cwd=BASE_DIR,
        capture_output=True,
        text=True
    )

    output = result.stdout

    if result.stderr:
        output += "\n" + result.stderr

    return result.returncode, output


# ---------------------------------
# MAIN WORKFLOW
# ---------------------------------

print("\n======================================")
print("LANGFUSE AI AGENT WORKFLOW")
print("======================================")


# This variable will store the trace URL
trace_url = None


with langfuse.start_as_current_observation(
    as_type="agent",
    name="AI-Code-Fix-Workflow",
    input={
        "test_file": TEST_FILE,
        "workflow": "pytest -> analysis -> code fix -> verification"
    }
) as workflow:

    # ---------------------------------
    # 1. TEST EXECUTION / FAILURE DETECTION
    # ---------------------------------

    print("\n[1] TEST EXECUTION")

    with langfuse.start_as_current_observation(
        as_type="chain",
        name="test_execution"
    ) as span:

        return_code, output = run_script(
            "failure_pipeline.py"
        )

        span.update(
            output=output,
            metadata={
                "stage": "failure_detection"
            }
        )

        print(output)

    # ---------------------------------
    # 2. FAILURE ANALYSIS
    # ---------------------------------

    print("\n[2] FAILURE ANALYSIS")

    with langfuse.start_as_current_observation(
        as_type="agent",
        name="failure_analysis"
    ) as span:

        return_code, output = run_script(
            "failure_analysis_agent.py"
        )

        span.update(
            output=output,
            metadata={
                "stage": "failure_analysis"
            }
        )

        print(output)

    # ---------------------------------
    # 3. CODE FIX
    # ---------------------------------

    print("\n[3] CODE FIX")

    with langfuse.start_as_current_observation(
        as_type="agent",
        name="code_fix"
    ) as span:

        return_code, output = run_script(
            "code_fix_agent.py"
        )

        span.update(
            output=output,
            metadata={
                "stage": "code_fix"
            }
        )

        print(output)

    # ---------------------------------
    # 4. VERIFICATION
    # ---------------------------------

    print("\n[4] VERIFICATION")

    with langfuse.start_as_current_observation(
        as_type="chain",
        name="verification"
    ) as span:

        return_code, output = run_pytest()

        span.update(
            output=output,
            metadata={
                "stage": "final_verification"
            }
        )

        print(output)

        if return_code == 0:

            print("\n======================================")
            print("FINAL RESULT: ALL TESTS PASSED")
            print("======================================")

            workflow.update(
                output={
                    "status": "success",
                    "message": "Code fixed and all tests passed"
                }
            )

        else:

            print("\n======================================")
            print("FINAL RESULT: TESTS STILL FAILING")
            print("======================================")

            workflow.update(
                output={
                    "status": "failed",
                    "message": "Tests still failing after code fix"
                }
            )

    # ---------------------------------
    # GET TRACE URL
    # ---------------------------------
    # IMPORTANT:
    # This must happen while the root
    # observation is still active.

    trace_id = langfuse.get_current_trace_id()

    trace_url = langfuse.get_trace_url(
        trace_id=trace_id
    )

    print("\n======================================")
    print("LANGFUSE TRACE URL")
    print("======================================")

    print(trace_url)


# ---------------------------------
# FLUSH LANGFUSE
# ---------------------------------

langfuse.flush()

print("\n======================================")
print("LANGFUSE TRACE COMPLETED")
print("======================================")