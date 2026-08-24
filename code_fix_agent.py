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
# TOOL: WRITE FILE
# -----------------------------

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


# -----------------------------
# CREATE CODE FIX AGENT
# -----------------------------

agent = create_agent(
    model=llm,

    tools=[
        read_file,
        write_file
    ],

    system_prompt="""
You are a Python Code Fix Agent.

Your job is to fix a failed pytest test.

You have access to two tools:

1. read_file
   Use this to read the current contents of the relevant Python file.

2. write_file
   Use this to write the corrected contents back to the file.

Workflow:

1. Identify the failed test.
2. Identify the test file and line number.
3. Read the relevant file using read_file.
4. Understand the failure.
5. Determine the appropriate code change.
6. Modify ONLY what is necessary to fix the failure.
7. Use write_file to save the corrected file.
8. Clearly explain what was changed.

Important rules:

- Do not make unnecessary changes.
- Preserve the existing code structure.
- Do not delete working tests.
- Do not change unrelated functionality.
- Actually modify the file using write_file.
- After modifying the file, report the exact change made.
"""
)


# -----------------------------
# GET FAILURE INFORMATION
# -----------------------------

failure_info = get_failure_info()

if failure_info is None:
    print("\nNo test failures found.")
    exit()


failed_test = failure_info["failed_test"]
test_file = failure_info["file_path"]
line_number = failure_info["line_number"]
failure_reason = failure_info["reason"]


# -----------------------------
# RUN CODE FIX AGENT
# -----------------------------

print("\n=============================")
print("CODE FIX AGENT")
print("=============================")

result = agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": f"""
Fix the following pytest failure.

Failed Test:
{failed_test}

Test File:
{test_file}

Line Number:
{line_number}

Failure Reason:
{failure_reason}

Instructions:

1. Read the test file using the read_file tool.
2. Find the failed test.
3. Understand why it failed.
4. Make the minimum necessary change to fix it.
5. Use write_file to save the corrected file.
6. Do not modify unrelated tests or functionality.

After fixing the file, explain:
- What caused the failure.
- What change you made.
- Which file was modified.
"""
            }
        ]
    }
)


# -----------------------------
# PRINT AGENT RESULT
# -----------------------------

print("\n=============================")
print("CODE FIX RESULT")
print("=============================")

for message in result["messages"]:

    if message.content:
        print("\nMessage:")
        print(message.content)