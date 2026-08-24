import os
import subprocess
from typing import TypedDict, Annotated

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.tools import tool
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages


# ==========================================
# LOAD ENVIRONMENT
# ==========================================

load_dotenv()

if not os.getenv("GEMINI_API_KEY"):
    raise ValueError("GEMINI_API_KEY was not found.")


# ==========================================
# GEMINI MODEL
# ==========================================

llm = ChatGoogleGenerativeAI(
    model="gemini-3.5-flash",
    temperature=0
)


# ==========================================
# TOOL 1 — READ FILE
# ==========================================

@tool
def read_file(file_path: str) -> str:
    """Read the contents of a Python or text file."""

    try:
        if not os.path.exists(file_path):
            return f"File does not exist: {file_path}"

        with open(file_path, "r", encoding="utf-8") as file:
            return file.read()

    except Exception as e:
        return f"File reading error: {e}"


# ==========================================
# TOOL 2 — RUN PYTHON FILE
# ==========================================

@tool
def run_python(file_path: str) -> str:
    """Run a Python file and return its output or errors."""

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


# ==========================================
# TOOLS
# ==========================================

tools = [
    read_file,
    run_python
]

llm_with_tools = llm.bind_tools(tools)


# ==========================================
# STATE
# ==========================================

class State(TypedDict):
    messages: Annotated[list, add_messages]


# ==========================================
# GEMINI NODE
# ==========================================

def gemini_node(state: State):

    print("\n--- Gemini Node ---")

    response = llm_with_tools.invoke(state["messages"])

    print("Gemini response received.")

    if response.tool_calls:
        for tool_call in response.tool_calls:
            print(f"Tool requested: {tool_call['name']}")

    return {
        "messages": [response]
    }


# ==========================================
# TOOL NODE
# ==========================================

def tool_node(state: State):

    print("\n--- Tool Node ---")

    last_message = state["messages"][-1]

    results = []

    for tool_call in last_message.tool_calls:

        tool_name = tool_call["name"]
        tool_args = tool_call["args"]

        print(f"Executing: {tool_name}")
        print(f"Arguments: {tool_args}")

        if tool_name == "read_file":

            result = read_file.invoke(tool_args)

        elif tool_name == "run_python":

            result = run_python.invoke(tool_args)

        else:

            result = f"Unknown tool: {tool_name}"

        print("Tool result received.")

        results.append(
            {
                "role": "tool",
                "content": result,
                "tool_call_id": tool_call["id"]
            }
        )

    return {
        "messages": results
    }


# ==========================================
# ROUTING
# ==========================================

def should_continue(state: State):

    last_message = state["messages"][-1]

    if getattr(last_message, "tool_calls", None):
        return "tools"

    return END


# ==========================================
# CREATE GRAPH
# ==========================================

builder = StateGraph(State)


builder.add_node(
    "gemini",
    gemini_node
)

builder.add_node(
    "tools",
    tool_node
)


# START → Gemini

builder.add_edge(
    START,
    "gemini"
)


# Gemini → Tools OR END

builder.add_conditional_edges(
    "gemini",
    should_continue,
    {
        "tools": "tools",
        END: END
    }
)


# Tools → Gemini

builder.add_edge(
    "tools",
    "gemini"
)


# ==========================================
# COMPILE
# ==========================================

graph = builder.compile()


# ==========================================
# CODE ANALYSIS TEST
# ==========================================

file_path = (
    r"C:\Users\SINGER\OneDrive\Desktop"
    r"\agent_workspace\ai-agent-demo-project"
    r"\calculator.py"
)


print("\n===================================")
print("CODE ANALYSIS AGENT")
print("===================================")


user_request = f"""
Analyze this Python file:

{file_path}

Your task:

1. Read the Python file.
2. Understand what the code does.
3. Identify any obvious code problems.
4. Run the Python file to check for runtime errors.
5. Combine the findings.
6. Give me a concise code analysis report.

Do not modify the file.
"""


# ==========================================
# RUN AGENT
# ==========================================

result = graph.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": user_request
            }
        ]
    }
)


# ==========================================
# FINAL RESULT
# ==========================================

print("\n===================================")
print("FINAL CODE ANALYSIS")
print("===================================")


final_message = result["messages"][-1]

print(final_message.content)

