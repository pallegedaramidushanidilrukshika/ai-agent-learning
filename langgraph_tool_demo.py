import os

from dotenv import load_dotenv
from typing import TypedDict

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.tools import tool

from langgraph.graph import StateGraph, START, END


# ==========================================
# 1. LOAD ENVIRONMENT VARIABLES
# ==========================================

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("GEMINI_API_KEY was not found.")


# ==========================================
# 2. CREATE GEMINI MODEL
# ==========================================

llm = ChatGoogleGenerativeAI(
    model="gemini-3.5-flash",
    temperature=0
)


# ==========================================
# 3. CREATE CALCULATOR TOOL
# ==========================================

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


# ==========================================
# 4. BIND TOOL TO GEMINI
# ==========================================

llm_with_tools = llm.bind_tools(
    [calculator]
)


# ==========================================
# 5. DEFINE STATE
# ==========================================

class State(TypedDict):
    messages: list


# ==========================================
# 6. GEMINI NODE
# ==========================================

def gemini_node(state: State):

    print("\n=============================")
    print("GEMINI NODE")
    print("=============================")

    response = llm_with_tools.invoke(
        state["messages"]
    )

    print("Gemini response:")
    print(response)

    return {
        "messages": state["messages"] + [response]
    }


# ==========================================
# 7. TOOL NODE
# ==========================================

def tool_node(state: State):

    print("\n=============================")
    print("TOOL NODE")
    print("=============================")

    last_message = state["messages"][-1]

    tool_call = last_message.tool_calls[0]

    tool_name = tool_call["name"]
    tool_args = tool_call["args"]

    print("Tool selected:", tool_name)
    print("Tool arguments:", tool_args)

    if tool_name == "calculator":

        result = calculator.invoke(
            tool_args
        )

        print("Tool result:", result)

        return {
            "messages": state["messages"] + [result]
        }


# ==========================================
# 8. DECISION NODE
# ==========================================

def should_use_tool(state: State):

    last_message = state["messages"][-1]

    if last_message.tool_calls:
        return "tool"

    return "end"


# ==========================================
# 9. CREATE GRAPH
# ==========================================

builder = StateGraph(State)


# Add nodes
builder.add_node(
    "gemini",
    gemini_node
)

builder.add_node(
    "tool",
    tool_node
)


# ==========================================
# 10. CONNECT GRAPH
# ==========================================

builder.add_edge(
    START,
    "gemini"
)

builder.add_conditional_edges(
    "gemini",
    should_use_tool,
    {
        "tool": "tool",
        "end": END
    }
)

builder.add_edge(
    "tool",
    "gemini"
)


# ==========================================
# 11. COMPILE GRAPH
# ==========================================

graph = builder.compile()


# ==========================================
# 12. RUN GRAPH
# ==========================================

result = graph.invoke(
    {
        "messages": [
            "Calculate 125 + 375"
        ]
    }
)


# ==========================================
# 13. FINAL RESULT
# ==========================================

print("\n=============================")
print("FINAL RESULT")
print("=============================")

for message in result["messages"]:
    print("\nMessage:")
    print(message)