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

        os.makedirs(
        os.path.dirname(file_path),
        exist_ok=True
        )

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
def run_node_test(test_file_path: str) -> str:
    """
    Run a Node.js test file and return the test results.
    """

    import subprocess

    try:
        if not os.path.exists(test_file_path):
            return f"Test file does not exist: {test_file_path}"

        result = subprocess.run(
            ["node", "--test", test_file_path],
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
        return "Node.js test execution timed out."

    except Exception as e:
        return f"Node.js test execution error: {e}"


# -----------------------------
# CREATE AGENT
# -----------------------------

agent = create_agent(
    model=llm,

    tools=[
        read_file,
        create_test_file,
        run_node_test
        
    ],

    system_prompt="""
You are an AI Software Testing Agent.

Your job is to analyze a real application codebase
and generate automated tests for the application.

The target project may use different programming languages
or frameworks.

IMPORTANT:

- Do NOT assume the project is Python.
- Identify the technology used by the target project.
- For Node.js projects, generate Node.js tests.
- Use the built-in Node.js test runner when appropriate.
- Do not modify the original application source code.
- Create tests in a separate test directory.
- Tests must verify actual application behavior.
- Do not create debug-only tests.
- Every test must contain meaningful assertions.
- Cover the reported bugs and important edge cases.

WORKFLOW:

1. Read the relevant application source files.
2. Understand the application behavior.
3. Read the provided bug diagnosis if available.
4. Identify the bugs that should be tested.
5. Generate automated tests.
6. Create the test file using create_test_file.
7. Run the generated tests using run_node_test.
8. Analyze the test results.
9. Report:
   - tests created
   - behavior tested
   - test results
   - failures if any

IMPORTANT:

The tests must be based on the actual target application.

Do not create placeholder tests.
Do not create tests that only print output.
Every test must use assertions.

Only create or modify test files.
Do not modify application source files.
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

project_dir = r"C:\Users\SINGER\OneDrive\Desktop\target-projects\nodejs-todo"

source_file = os.path.join(
    project_dir,
    "index.js"
)

test_file = os.path.join(
    project_dir,
    "test",
    "todo.test.js"
)


result = agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": f"""
You are testing the real Node.js application.

TARGET PROJECT:
{project_dir}

MAIN SOURCE FILE:
{source_file}

TEST FILE:
{test_file}

BUG 1 DIAGNOSIS:
{json.dumps(diagnosis_1, indent=2)}

BUG 2 DIAGNOSIS:
{json.dumps(diagnosis_2, indent=2)}

Your task:

1. Read the actual index.js file.
2. Read the relevant views/index.ejs file.
3. Understand how the Express application works.
4. Create automated Node.js tests.
5. Test Bug 1:
   - empty task should not be added
   - whitespace-only task should not be added
   - valid task should still be added
6. Test Bug 2:
   - select an existing task
   - submit it to /removetask
   - verify it is removed from Added Task
   - verify it appears in Completed Task
7. Use meaningful assertions.
8. Create the test file at:
   {test_file}
9. Run the generated tests using run_node_test.
10. Report the test results.

Do not modify index.js or views/index.ejs.
Only create the test file.
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