import os

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.agents import create_agent
from langchain_core.tools import tool

from failure_pipeline import get_failure_info


# -----------------------------
# LOAD ENVIRONMENT VARIABLES
# -----------------------------

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("GEMINI_API_KEY was not found.")


# -----------------------------
# CREATE GEMINI MODEL
# -----------------------------

llm = ChatGoogleGenerativeAI(
    model="gemini-3.5-flash",
    temperature=0
)


# -----------------------------
# TOOL: READ FILE
# -----------------------------

@tool
def read_file(file_path: str) -> str:
    """
    Read the contents of a Python file.
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


# -----------------------------
# FAILURE ANALYSIS AGENT
# -----------------------------

analysis_agent = create_agent(
    model=llm,

    tools=[
        read_file
    ],

    system_prompt="""
You are a Python test failure analysis agent.

Your job is to analyze failed pytest tests.

You have access to:

1. read_file
   Use this tool to read the source or test file.

When given a test failure:

1. Identify the failed test.
2. Identify the file and line number.
3. Understand the failure reason.
4. Read the relevant file if necessary.
5. Explain why the test failed.
6. Suggest an appropriate fix.

Do not modify any files.

Provide a concise failure analysis report.
"""
)


# -----------------------------
# FULL WORKFLOW
# -----------------------------

print("\n=============================")
print("FULL WORKFLOW")
print("=============================")


# ============================================================
# NODE 1: FAILURE DETECTION
# ============================================================

print("\n[1] Running pytest and detecting failures...")

failure_info = get_failure_info()

if failure_info is None:

    print("\nNo test failures found.")

else:

    print("\nFailure detected!")

    print("\nFailed Test:")
    print(failure_info["failed_test"])

    print("\nFile:")
    print(failure_info["file_path"])

    print("\nLine:")
    print(failure_info["line_number"])

    print("\nReason:")
    print(failure_info["reason"])


    # ========================================================
    # NODE 2: FAILURE ANALYSIS
    # ========================================================

    print("\n=============================")
    print("[2] FAILURE ANALYSIS AGENT")
    print("=============================")

    result = analysis_agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": f"""
Analyze this pytest failure.

Failed Test:
{failure_info["failed_test"]}

Test File:
{failure_info["file_path"]}

Line Number:
{failure_info["line_number"]}

Failure Reason:
{failure_info["reason"]}

Read the test file if necessary.

Explain:

1. Why the test failed.
2. What caused the failure.
3. What should be changed to fix it.

Do not modify the file.
"""
                }
            ]
        }
    )


    # ========================================================
    # PRINT NODE 2 RESULT
    # ========================================================

    print("\n-----------------------------")
    print("FAILURE ANALYSIS RESULT")
    print("-----------------------------")

    for message in result["messages"]:

        if message.content:
            print("\nMessage:")
            print(message.content)

    # ========================================================
    # NODE 3: CODE FIX AGENT
    # ========================================================

    print("\n=============================")
    print("[3] CODE FIX AGENT")
    print("=============================")

    import subprocess
    import sys

    fix_result = subprocess.run(
        [sys.executable, "code_fix_agent.py"],
        capture_output=True,
        text=True
    )

    print(fix_result.stdout)

    if fix_result.stderr:
        print("\nCODE FIX STDERR:")
        print(fix_result.stderr)


    # ========================================================
    # NODE 4: VERIFY FIX
    # ========================================================

    print("\n=============================")
    print("[4] VERIFYING FIX")
    print("=============================")

    test_file = failure_info["file_path"]

    verify_result = subprocess.run(
        [sys.executable, "-m", "pytest", test_file, "-v"],
        capture_output=True,
        text=True
    )

    print(verify_result.stdout)

    if verify_result.returncode == 0:

        print("\n✅ VERIFICATION SUCCESSFUL")
        print("All tests passed after the AI code fix.")

    else:

        print("\n❌ VERIFICATION FAILED")
        print("Tests are still failing after the AI code fix.")