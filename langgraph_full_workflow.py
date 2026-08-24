import os
import subprocess

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.agents import create_agent
from langchain_core.tools import tool
from langgraph.graph import StateGraph, START, END
from typing import TypedDict


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("GEMINI_API_KEY was not found.")


TEST_FILE = (
    r"C:\Users\SINGER\OneDrive\Desktop"
    r"\agent_workspace\ai-agent-demo-project"
    r"\test_calculator.py"
)


# ============================================================
# GEMINI MODEL
# ============================================================

llm = ChatGoogleGenerativeAI(
    model="gemini-3.5-flash",
    temperature=0
)


# ============================================================
# LANGGRAPH STATE
# ============================================================

class WorkflowState(TypedDict):

    test_output: str
    failed_test: str
    file_path: str
    line_number: str
    failure_reason: str
    analysis: str
    fix_result: str
    test_passed: bool
    attempts: int


# ============================================================
# TOOL: READ FILE
# ============================================================

@tool
def read_file(file_path: str) -> str:
    """
    Read a Python file.
    """

    try:

        if not os.path.exists(file_path):
            return f"File does not exist: {file_path}"

        with open(
            file_path,
            "r",
            encoding="utf-8"
        ) as file:

            return file.read()

    except Exception as e:

        return f"File reading error: {e}"


# ============================================================
# TOOL: WRITE FILE
# ============================================================

@tool
def write_file(file_path: str, content: str) -> str:
    """
    Write updated contents to a Python file.
    """

    try:

        with open(
            file_path,
            "w",
            encoding="utf-8"
        ) as file:

            file.write(content)

        return f"File successfully updated: {file_path}"

    except Exception as e:

        return f"File writing error: {e}"


# ============================================================
# FAILURE ANALYSIS AGENT
# ============================================================

failure_analysis_agent = create_agent(

    model=llm,

    tools=[
        read_file
    ],

    system_prompt="""
You are a Python test failure analysis agent.

Analyze the provided pytest failure.

Steps:

1. Identify the failed test.
2. Identify the file and line number.
3. Read the relevant file using read_file.
4. Understand the actual cause.
5. Explain why the test failed.
6. Provide a concise recommended fix.

Do not modify files.
"""
)


# ============================================================
# CODE FIX AGENT
# ============================================================

code_fix_agent = create_agent(

    model=llm,

    tools=[
        read_file,
        write_file
    ],

    system_prompt="""
You are a Python Code Fix Agent.

Your job is to fix the provided pytest failure.

Steps:

1. Read the relevant file using read_file.
2. Locate the failed test.
3. Understand the failure.
4. Make the minimum necessary change.
5. Use write_file to save the corrected file.

Rules:

- Do not make unrelated changes.
- Preserve working functionality.
- Do not delete working tests.
- Actually modify the file using write_file.
- Report what was changed.
"""
)


# ============================================================
# STEP 1 — RUN TESTS
# ============================================================

def run_tests(state: WorkflowState):

    print("\n================================")
    print("STEP 1 — RUN TESTS")
    print("================================")

    result = subprocess.run(

        [
            "python",
            "-m",
            "pytest",
            TEST_FILE,
            "-v"
        ],

        capture_output=True,
        text=True
    )

    output = result.stdout + "\n" + result.stderr
    print(output)

    passed = result.returncode == 0

    print(
        "\nTEST STATUS:",
        "PASSED" if passed else "FAILED"
    )

    return {

        "test_output": output,

        "test_passed": passed,

        "attempts": state.get("attempts", 0) + 1
    }


# ============================================================
# STEP 2 — EXTRACT FAILURE
# ============================================================

def extract_failure(state: WorkflowState):

    print("\n================================")
    print("STEP 2 — EXTRACT FAILURE")
    print("================================")

    output = state["test_output"]

    import re

    failed_test = "Unknown"
    file_path = TEST_FILE
    line_number = "Unknown"
    reason = "Unknown"

    # --------------------------------------------------------
    # Extract failed test
    # Example:
    # test_calculator.py::test_intentional_failure FAILED
    # --------------------------------------------------------

    match = re.search(
        r"test_calculator\.py::(test_[A-Za-z0-9_]+)\s+FAILED",
        output
    )

    if match:
        failed_test = match.group(1)

    # --------------------------------------------------------
    # Extract file + line number
    # Example:
    # test_calculator.py:35: AssertionError
    # --------------------------------------------------------

    location_match = re.search(
        r"test_calculator\.py:(\d+):\s*(\w+)",
        output
    )

    if location_match:

        line_number = location_match.group(1)
        reason = location_match.group(2)

    # --------------------------------------------------------
    # Fallback: extract assertion error
    # --------------------------------------------------------

    if reason == "Unknown":

        if "AssertionError" in output:
            reason = "AssertionError"

    print("\nFailed Test:")
    print(failed_test)

    print("\nFile:")
    print(file_path)

    print("\nLine:")
    print(line_number)

    print("\nReason:")
    print(reason)

    return {

        "failed_test": failed_test,

        "file_path": file_path,

        "line_number": line_number,

        "failure_reason": reason
    }

# ============================================================
# STEP 3 — FAILURE ANALYSIS
# ============================================================

def analyze_failure(state: WorkflowState):

    print("\n================================")
    print("STEP 3 — FAILURE ANALYSIS AGENT")
    print("================================")

    prompt = f"""
Analyze this pytest failure.

Failed Test:
{state["failed_test"]}

Test File:
{state["file_path"]}

Line Number:
{state["line_number"]}

Failure Reason:
{state["failure_reason"]}

Pytest Output:
{state["test_output"]}

Read the test file if necessary.

Explain:

1. Why the test failed.
2. What caused the failure.
3. What should be changed to fix it.

Do not modify the file.
"""

    result = failure_analysis_agent.invoke(

        {
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        }
    )

    analysis = result["messages"][-1].content

    print("\nFAILURE ANALYSIS:")
    print(analysis)

    return {

        "analysis": str(analysis)
    }


# ============================================================
# STEP 4 — CODE FIX
# ============================================================

def fix_code(state: WorkflowState):

    print("\n================================")
    print("STEP 4 — CODE FIX AGENT")
    print("================================")

    prompt = f"""
Fix this pytest failure.

Failed Test:
{state["failed_test"]}

File:
{state["file_path"]}

Line:
{state["line_number"]}

Reason:
{state["failure_reason"]}

Failure Analysis:
{state["analysis"]}

Instructions:

1. Read the file.
2. Identify the failed code.
3. Apply the minimum necessary fix.
4. Use write_file.
5. Do not modify unrelated functionality.
"""

    result = code_fix_agent.invoke(

        {
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        }
    )

    fix_result = result["messages"][-1].content

    print("\nCODE FIX RESULT:")
    print(fix_result)

    return {

        "fix_result": str(fix_result)
    }


# ============================================================
# ROUTER — TEST RESULT
# ============================================================

def test_result_router(state: WorkflowState):

    if state["test_passed"]:

        return "end"

    return "failure"


# ============================================================
# BUILD LANGGRAPH
# ============================================================

workflow = StateGraph(WorkflowState)


workflow.add_node(
    "run_tests",
    run_tests
)

workflow.add_node(
    "extract_failure",
    extract_failure
)

workflow.add_node(
    "analyze_failure",
    analyze_failure
)

workflow.add_node(
    "fix_code",
    fix_code
)


workflow.add_edge(
    START,
    "run_tests"
)


workflow.add_conditional_edges(

    "run_tests",

    test_result_router,

    {
        "end": END,
        "failure": "extract_failure"
    }
)


workflow.add_edge(
    "extract_failure",
    "analyze_failure"
)


workflow.add_edge(
    "analyze_failure",
    "fix_code"
)


workflow.add_edge(
    "fix_code",
    "run_tests"
)


app = workflow.compile()


# ============================================================
# START WORKFLOW
# ============================================================

print("\n================================")
print("LANGGRAPH FULL AGENTIC WORKFLOW")
print("================================")


initial_state = {

    "test_output": "",
    "failed_test": "",
    "file_path": "",
    "line_number": "",
    "failure_reason": "",
    "analysis": "",
    "fix_result": "",
    "test_passed": False,
    "attempts": 0
}


final_state = app.invoke(initial_state)


# ============================================================
# FINAL RESULT
# ============================================================

print("\n================================")
print("FINAL WORKFLOW RESULT")
print("================================")

if final_state["test_passed"]:

    print("\nSUCCESS: ALL TESTS PASSED")

else:

    print("\nWORKFLOW FINISHED WITH FAILURES")