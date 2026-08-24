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
# TOOL 1: CALCULATOR
# -----------------------------

@tool
def calculator(expression: str) -> str:
    """
    Calculate a mathematical expression.
    Example: 25 + 17
    """

    try:
        result = eval(expression, {"__builtins__": {}})
        return str(result)

    except Exception as e:
        return f"Calculation error: {e}"


# -----------------------------
# TOOL 2: READ FILE
# -----------------------------

@tool
def read_file(file_path: str) -> str:
    """
    Read the contents of a text or Python file.
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

@tool
def run_python(file_path: str) -> str:
    """
    Run a Python file and return its output.
    """

    import subprocess

    try:
        if not os.path.exists(file_path):
            return f"File does not exist: {file_path}"

        result = subprocess.run(
            ["python", file_path],
            capture_output=True,
            text=True,
            timeout=10
        )

        return (
            f"Return code: {result.returncode}\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )

    except subprocess.TimeoutExpired:
        return "Python execution timed out."

    except Exception as e:
        return f"Python execution error: {e}"


# -----------------------------
# CREATE AGENT
# -----------------------------

agent = create_agent(
    model=llm,
    tools=[
    calculator,
    read_file,
    run_python
],
    system_prompt="""
You are a helpful AI agent.

You have access to three tools:

1. calculator
   Use this for mathematical calculations.

2. read_file
   Use this when you need to read the contents of a file.

3. run_python
   Use this when you need to execute a Python file and inspect its output or errors.

Choose the appropriate tool based on the user's request.

After using a tool, provide a clear final answer.
"""
)


# # -----------------------------
# # TEST 1: CALCULATOR
# # -----------------------------

# print("\n=============================")
# print("TEST 1: CALCULATOR")
# print("=============================")

# result = agent.invoke(
#     {
#         "messages": [
#             {
#                 "role": "user",
#                 "content": "Calculate 125 + 375"
#             }
#         ]
#     }
# )

# for message in result["messages"]:
#     print("\nMessage:")
#     print(message)


# # -----------------------------
# # TEST 2: READ FILE
# # -----------------------------

# print("\n=============================")
# print("TEST 2: READ FILE")
# print("=============================")

# file_path = r"C:\Users\SINGER\OneDrive\Desktop\agent_workspace\ai-agent-demo-project\calculator.py"

# result = agent.invoke(
#     {
#         "messages": [
#             {
#                 "role": "user",
#                 "content": f"Read this file and explain what it does: {file_path}"
#             }
#         ]
#     }
# )

# for message in result["messages"]:
#     print("\nMessage:")
#     print(message)


# -----------------------------
# TEST 3: RUN PYTHON FILE
# -----------------------------

print("\n=============================")
print("TEST 3: RUN PYTHON FILE")
print("=============================")

file_path = r"C:\Users\SINGER\OneDrive\Desktop\agent_workspace\ai-agent-demo-project\calculator.py"

result = agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": f"Run this Python file and tell me whether it executed successfully: {file_path}"
            }
        ]
    }
)

for message in result["messages"]:

    print("\nMessage:")

    print(message)