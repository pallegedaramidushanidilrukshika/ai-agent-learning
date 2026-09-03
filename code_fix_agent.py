import json
import os
import sys

from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain.agents import create_agent
from langchain_core.tools import tool


# ---------------------------------------------------------
# LOAD ENVIRONMENT VARIABLES
# ---------------------------------------------------------

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("GEMINI_API_KEY was not found.")


# ---------------------------------------------------------
# PATHS
# ---------------------------------------------------------

TARGET_REPOSITORY = os.path.abspath(
    r"C:\Users\SINGER\OneDrive\Desktop\target-projects\nodejs-todo"
)

if len(sys.argv) > 1:
    DIAGNOSIS_FILE = sys.argv[1]
else:
    DIAGNOSIS_FILE = "diagnosis.json"


# ---------------------------------------------------------
# LOAD AI DIAGNOSIS
# ---------------------------------------------------------

with open(DIAGNOSIS_FILE, "r", encoding="utf-8") as f:
    diagnosis = json.load(f)


# ---------------------------------------------------------
# CREATE GEMINI MODEL
# ---------------------------------------------------------

llm = ChatGroq(
    model="openai/gpt-oss-20b",
    temperature=0
)


# ---------------------------------------------------------
# TOOL: READ FILE
# ---------------------------------------------------------

@tool
def read_file(file_path: str) -> str:
    """
    Read a source file from the target repository.
    """

    full_path = os.path.abspath(
        os.path.join(TARGET_REPOSITORY, file_path)
    )

    # Security: prevent access outside target repository
    if not full_path.startswith(TARGET_REPOSITORY):
        return "Error: Access outside target repository is not allowed."

    if not os.path.exists(full_path):
        return f"File does not exist: {file_path}"

    try:
        with open(
            full_path,
            "r",
            encoding="utf-8"
        ) as file:
            return file.read()

    except Exception as error:
        return f"File reading error: {error}"


# ---------------------------------------------------------
# TOOL: WRITE FILE
# ---------------------------------------------------------

@tool
def write_file(file_path: str, content: str) -> str:
    """
    Write corrected source code to a file in the target repository.
    """

    full_path = os.path.abspath(
        os.path.join(TARGET_REPOSITORY, file_path)
    )

    # Security: prevent writing outside target repository
    if not full_path.startswith(TARGET_REPOSITORY):
        return "Error: Writing outside target repository is not allowed."

    try:
        with open(
            full_path,
            "w",
            encoding="utf-8"
        ) as file:
            file.write(content)

        return f"File successfully updated: {file_path}"

    except Exception as error:
        return f"File writing error: {error}"


# ---------------------------------------------------------
# CREATE CODE FIX AGENT
# ---------------------------------------------------------

agent = create_agent(
    model=llm,

    tools=[
        read_file,
        write_file
    ],

    system_prompt="""
You are an AI Software Code Fix Agent.

Your job is to fix a REAL application bug based on an
AI-generated diagnosis.

IMPORTANT:

- The target application can use any programming language
  or framework.
- Do NOT assume the project is Python.
- Do NOT create pytest tests.
- Do NOT create new unit tests unless explicitly requested.
- Fix the actual application source code.
- Make the minimum necessary changes.
- Preserve existing functionality.
- Do not modify unrelated files.
- Read the affected source files before modifying them.
- Actually use the write_file tool to save the fix.
- Do not only describe the fix.
- After modifying the files, explain exactly what changed.

WORKFLOW:

1. Read the diagnosis provided by the user.
2. Identify the affected files.
3. Read each affected file using read_file.
4. Understand the existing implementation.
5. Identify the exact code responsible for the bug.
6. Apply the smallest safe fix.
7. Use write_file to save the corrected source code.
8. Report:
   - root cause
   - files modified
   - exact changes made
   - how the fix should be verified

IMPORTANT SAFETY RULE:

Only modify files inside the target repository.

Do not change:
- .git
- node_modules
- package-lock.json
- unrelated files
"""
)


# ---------------------------------------------------------
# RUN CODE FIX AGENT
# ---------------------------------------------------------

print("\n=============================")
print("AI CODE FIX AGENT")
print("=============================")

print("\nTarget Repository:")
print(TARGET_REPOSITORY)

print("\nDiagnosis:")
print(json.dumps(diagnosis, indent=2))


result = agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": f"""
Fix the following real application bug.

TARGET REPOSITORY:
{TARGET_REPOSITORY}

AI DIAGNOSIS:
{json.dumps(diagnosis, indent=2)}

Instructions:

1. Read every file listed in "affected_files".
2. Understand the current implementation.
3. Implement the proposed fix.
4. Modify only the affected source files.
5. Preserve all unrelated functionality.
6. Use the write_file tool to actually save the changes.
7. Do not create tests.
8. Do not modify package-lock.json.
9. Do not modify node_modules.
10. After fixing, explain exactly what was changed.

This is a real application bug-fixing task.
The goal is to modify the actual application source code.
"""
            }
        ]
    }
)


# ---------------------------------------------------------
# PRINT RESULT
# ---------------------------------------------------------

print("\n=============================")
print("CODE FIX RESULT")
print("=============================")

for message in result["messages"]:

    if message.content:
        print("\nMessage:")
        print(message.content)