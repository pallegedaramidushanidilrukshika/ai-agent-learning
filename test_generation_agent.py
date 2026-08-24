import os

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.tools import tool
from langchain.agents import create_agent


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
    Read the contents of a Python or text file.
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
# TOOL: CREATE TEST FILE
# -----------------------------

@tool
def create_test_file(
    file_path: str,
    content: str
) -> str:
    """
    Create a new test file with the provided Python code.
    """

    try:

        with open(
            file_path,
            "w",
            encoding="utf-8"
        ) as file:

            file.write(content)

        return f"Test file created successfully: {file_path}"

    except Exception as e:

        return f"File creation error: {e}"

@tool
def run_pytest(test_file_path: str) -> str:
    """
    Run pytest on a test file and return the test results.
    """

    import subprocess

    try:
        if not os.path.exists(test_file_path):
            return f"Test file does not exist: {test_file_path}"

        result = subprocess.run(
            ["python", "-m", "pytest", test_file_path, "-v"],
            capture_output=True,
            text=True,
            timeout=30
        )

        return (
            f"Return code: {result.returncode}\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )

    except subprocess.TimeoutExpired:
        return "Pytest execution timed out."

    except Exception as e:
        return f"Pytest execution error: {e}"


# -----------------------------
# CREATE AGENT
# -----------------------------

agent = create_agent(
    model=llm,

    tools=[
        read_file,
        create_test_file,
        run_pytest
        
    ],

    system_prompt="""
You are a Python testing agent.

Your goal is to analyze Python source code
and generate pytest test cases.
create the test file,
and run the generated tests.

You have access to these tools:

1. read_file
   Use this to read the source code.

2. create_test_file
   Use this to create a Python test file.

3. run_pytest
   Use this to run the generated pytest test file
   and inspect the test results.

Your workflow is:

1. Read the source Python file.
2. Understand the functions.
3. Identify normal and edge cases.
4. Generate pytest test cases.
5. Create a test file using create_test_file.
6. Run the generated tests using run_pytest.
7. Analyze the test results.
8. Report the final result.

Do not modify the original source file.

Only create a separate test file.
"""
)


# -----------------------------
# TEST: GENERATE TEST FILE
# -----------------------------

print("\n=============================")
print("TEST: GENERATE PYTEST FILE")
print("=============================")

# -----------------------------
# TEST PROJECT PATHS
# -----------------------------

project_dir = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "demo_project"
)

source_file = os.path.join(
    project_dir,
    "calculator.py"
)

test_file = os.path.join(
    project_dir,
    "test_calculator.py"
)


result = agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Read this Python file: {source_file}\n\n"
                    f"Analyze it, create pytest tests in: {test_file}, "
                    f"then run the generated pytest test file and report the results."
                )
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