import os
from typing import TypedDict, Annotated

from dotenv import load_dotenv

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.tools import tool
from langchain_core.messages import BaseMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode


# =========================================
# 1. LOAD ENVIRONMENT VARIABLES
# =========================================

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("GEMINI_API_KEY was not found.")


# =========================================
# 2. CREATE GEMINI MODEL
# =========================================

llm = ChatGoogleGenerativeAI(
    model="gemini-3.5-flash",
    temperature=0
)


# =========================================
# 3. TOOL 1 - CALCULATOR
# =========================================

@tool
def calculator(expression: str) -> str:
    """
    Calculate a mathematical expression.

    Example:
    25 + 17
    """

    try:
        result = eval(expression, {"__builtins__": {}})
        return str(result)

    except Exception as e:
        return f"Calculation error: {e}"


# =========================================
# 4. TOOL 2 - READ FILE
# =========================================

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


# =========================================
# 5. TOOL 3 - RUN PYTHON
# =========================================

@tool
def run_python(file_path: str) -> str:
    """
    Run a Python file and return its output or errors.
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


# =========================================
# 6. COLLECT TOOLS
# =========================================

tools = [
    calculator,
    read_file,
    run_python
]


# =========================================
# 7. BIND TOOLS TO GEMINI
# =========================================

llm_with_tools = llm.bind_tools(tools)


# =========================================
# 8. STATE
# =========================================

class State(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


# =========================================
# 9. GEMINI NODE
# =========================================

def chatbot_node(state: State):

    print("\n--- Gemini Node ---")

    response = llm_with_tools.invoke(
        state["messages"]
    )

    return {
        "messages": [response]
    }


# =========================================
# 10. CREATE TOOL NODE
# =========================================

tool_node = ToolNode(tools)


# =========================================
# 11. DECISION FUNCTION
# =========================================

def should_continue(state: State):

    last_message = state["messages"][-1]

    if last_message.tool_calls:

        print("\n--- Tool Requested ---")

        for tool_call in last_message.tool_calls:

            print(
                "Tool:",
                tool_call["name"]
            )

            print(
                "Arguments:",
                tool_call["args"]
            )

        return "tools"

    return END


# =========================================
# 12. CREATE GRAPH
# =========================================

builder = StateGraph(State)


# Add nodes
builder.add_node(
    "chatbot",
    chatbot_node
)

builder.add_node(
    "tools",
    tool_node
)


# =========================================
# 13. CONNECT GRAPH
# =========================================

builder.add_edge(
    START,
    "chatbot"
)

builder.add_conditional_edges(
    "chatbot",
    should_continue,
    {
        "tools": "tools",
        END: END
    }
)

builder.add_edge(
    "tools",
    "chatbot"
)


# =========================================
# 14. COMPILE GRAPH
# =========================================

graph = builder.compile()


# =========================================
# 15. TEST
# =========================================

print("\n=============================")
print("LANGGRAPH MULTI-TOOL AGENT")
print("=============================")


result = graph.invoke(
    {
        "messages": [
            (
                "user",
                r"Run this Python file and tell me whether it executed successfully: C:\Users\SINGER\OneDrive\Desktop\agent_workspace\ai-agent-demo-project\calculator.py"
            )
        ]
    }
)


# =========================================
# 16. PRINT FINAL RESPONSE
# =========================================

print("\n=============================")
print("FINAL RESULT")
print("=============================")

for message in result["messages"]:

    print("\nMessage:")
    print(message)