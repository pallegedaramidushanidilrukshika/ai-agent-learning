import os
from dotenv import load_dotenv
from failure_pipeline import get_failure_info


from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.agents import create_agent
from langchain_core.tools import tool


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
# CREATE AGENT
# -----------------------------

agent = create_agent(
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
# GET FAILURE INFORMATION
# -----------------------------

failure_info = get_failure_info()

failed_test = failure_info["failed_test"]
test_file = failure_info["file_path"]
line_number = failure_info["line_number"]
failure_reason = failure_info["reason"]



# -----------------------------
# RUN AGENT
# -----------------------------

print("\n=============================")
print("FAILURE ANALYSIS AGENT")
print("=============================")

result = agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": f"""
Analyze this pytest failure.

Failed Test:
{failed_test}

Test File:
{test_file}

Line Number:
{line_number}

Failure Reason:
{failure_reason}

Read the test file if necessary and explain:

1. Why the test failed.
2. What caused the failure.
3. What should be changed to fix it.

Do not modify the file.
"""
            }
        ]
    }
)


# -----------------------------
# PRINT RESULT
# -----------------------------

for message in result["messages"]:

    print("\nMessage:")
    print(message)