import json
import os 
import re
import sys
import time

from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain.agents import create_agent
from langchain_core.tools import tool
from groq import Groq

from workflow_artifacts import ARTIFACT_DIR_ENV, resolve_artifact_path



# ---------------------------------------------------------
# LOAD ENVIRONMENT VARIABLES
# ---------------------------------------------------------

load_dotenv()

api_key = os.getenv("GROQ_API_KEY")

if not api_key:
    raise ValueError("GROQ_API_KEY was not found.")

groq_client = Groq(api_key=api_key)

def print_utf8(value):
    text = str(value)
    try:
        sys.stdout.buffer.write((text + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except AttributeError:
        print(text)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(
        encoding="utf-8",
        errors="replace",
    )


# ---------------------------------------------------------
# PATHS
# ---------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

REPOSITORY_CONTEXT_FILE = resolve_artifact_path(
    "repository_context.json",
    BASE_DIR,
)

with open(REPOSITORY_CONTEXT_FILE, "r", encoding="utf-8") as f:
    repository_context = json.load(f)

TARGET_REPOSITORY = os.path.abspath(
    repository_context["repository_path"]
)

CODE_FIX_RESULT_FILE = resolve_artifact_path(
    "code_fix_result.json",
    BASE_DIR,
)

ORIGINAL_FILE_CONTENTS = {}
WRITE_RESULTS = {}

if len(sys.argv) > 1:
    DIAGNOSIS_FILE = sys.argv[1]
elif os.getenv(ARTIFACT_DIR_ENV):
    DIAGNOSIS_FILE = resolve_artifact_path(
        "diagnosis.json",
        BASE_DIR,
    )
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
    temperature=0,
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
# TOOL: SEARCH FILE
# ---------------------------------------------------------

@tool
def search_file(file_path: str, search_text: str) -> str:
    """
    Search a source file from the target repository for text.
    """

    repository_root = os.path.abspath(TARGET_REPOSITORY)
    full_path = os.path.abspath(
        os.path.join(repository_root, file_path)
    )
    repository_prefix = repository_root + os.sep

    if (
        full_path != repository_root
        and not full_path.startswith(repository_prefix)
    ):
        return "Error: Access outside target repository is not allowed."

    if not os.path.isfile(full_path):
        return f"File does not exist: {file_path}"

    try:
        with open(full_path, "r", encoding="utf-8") as file:
            lines = file.readlines()

        matches = [
            f"{line_number}: {line.rstrip()}"
            for line_number, line in enumerate(lines, start=1)
            if search_text in line
        ]

        return "\n".join(matches) if matches else "No matches found."
    except Exception as error:
        return f"File search error: {error}"


# ---------------------------------------------------------
# TOOL: WRITE FILE
# ---------------------------------------------------------

@tool
def write_file(file_path: str, content: str) -> str:
    """
    Write a source file and verify the actual disk change.
    """
    repository_root = os.path.abspath(TARGET_REPOSITORY)
    full_path = os.path.abspath(
        os.path.join(repository_root, file_path)
    )

    repository_prefix = repository_root + os.sep

    if (
        full_path != repository_root
        and not full_path.startswith(repository_prefix)
    ):
        return json.dumps(
            {
                "file": file_path,
                "verified": False,
                "message": "Writing outside target repository is not allowed.",
            }
        )

    try:
        existed_before = os.path.exists(full_path)

        before_content = None
        if existed_before:
            with open(full_path, "r", encoding="utf-8") as file:
                before_content = file.read()

        ORIGINAL_FILE_CONTENTS[full_path] = before_content

        with open(full_path, "w", encoding="utf-8") as file:
            file.write(content)

        if not os.path.isfile(full_path):
            message = "File does not exist after writing."
            WRITE_RESULTS[full_path] = {
                "verified": False,
                "message": message,
            }
            return json.dumps(
                {
                    "file": file_path,
                    "verified": False,
                    "message": message,
                }
            )

        with open(full_path, "r", encoding="utf-8") as file:
            after_content = file.read()

        changed = (
            before_content is not None
            and after_content != before_content
        )

        verified = (
            os.path.isfile(full_path)
            and after_content == content
            and changed
        )

        message = (
            "File successfully modified and verified."
            if verified
            else "File write did not produce a verified source change."
        )

        WRITE_RESULTS[full_path] = {
            "verified": verified,
            "message": message,
        }

        return json.dumps(
            {
                "file": file_path,
                "verified": verified,
                "message": message,
            }
        )

    except Exception as error:
        WRITE_RESULTS[full_path] = {
            "verified": False,
            "message": f"File writing error: {error}",
        }

        return json.dumps(
            {
                "file": file_path,
                "verified": False,
                "message": f"File writing error: {error}",
            }
        )

# ---------------------------------------------------------
# CREATE CODE FIX AGENT
# ---------------------------------------------------------

agent = create_agent(
    model=llm,

    tools=[
        read_file,
        write_file,
        search_file,
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

IMPORTANT DIAGNOSIS RULE:

- Treat "affected_files" in the diagnosis as a starting point,
  not as an absolute restriction.
- Read every file listed in "affected_files".
- If the diagnosed bug clearly depends on related application
  source files, inspect those files as well.
- For example, if a UI form sends data to a backend route,
  inspect both the view and the backend route handling that data.
- If the diagnosis is incomplete, identify the additional
  source file(s) responsible for the actual bug.
- Only modify source files that are directly required to fix
  the diagnosed bug.
- Do not modify unrelated files.
TOOL USAGE RULES:

- read_file reads one specific file.
- search_file searches text inside one specific file.
- search_file requires exactly:
  file_path = the path to one file
  search_text = the text to search for
- Do NOT pass a directory/folder path to search_file.
- Do NOT use "path" as the argument name for search_file.
- When searching a file, always use the exact file path with file_path.
- Use read_file when you already know the file path and need to inspect its content.
- You MUST use the write_file tool.
- You are NOT allowed to finish the task by only describing the fix.
- After reading the affected file, immediately determine the corrected content.
- Then call write_file with the exact corrected content.
- After write_file returns, verify the file content using read_file.
- Do not report the fix as completed unless write_file was successfully executed and the corrected content was verified.

WORKFLOW:

1. Read the diagnosis provided by the user.
2. Identify the files listed in "affected_files".
3. Read every listed file using read_file.
4. Trace the bug across the relevant application flow.
5. If the bug involves interaction between files,
   inspect the related source file(s) such as backend routes,
   controllers, services, or UI components.
6. Identify the exact code responsible for the bug.
7. Apply the smallest safe fix to all source files that are
   directly required.

8. MUST call write_file for every corrected source file.

9. After writing each file, MUST call read_file again to verify
   that the corrected code exists on disk.

10. Do not report success unless the write_file and verification
    steps have completed successfully.

IMPORTANT SAFETY RULE:

Only modify files inside the target repository.

Do not change:
- .git
- node_modules
- package-lock.json
- unrelated files
"""
)


def apply_direct_fallback(file_names, reason):
    print_utf8(
        "\nTool-based code fixing failed; trying direct code generation."
    )
    print_utf8(f"Tool-agent error: {reason}")

    for file_name in file_names:
        if not isinstance(file_name, str):
            continue

        repository_root = os.path.realpath(TARGET_REPOSITORY)
        full_path = os.path.abspath(
            os.path.join(TARGET_REPOSITORY, file_name)
        )
        resolved_path = os.path.realpath(full_path)
        if os.path.commonpath(
            [repository_root, resolved_path]
        ) != repository_root:
            print_utf8(
                f"Fallback skipped outside target repository: {file_name}"
            )
            continue

        previous_result = WRITE_RESULTS.get(full_path, {})
        if previous_result.get("verified") is True:
            continue

        current_content = read_file.invoke(
            {"file_path": file_name}
        )
        if current_content.startswith("File does not exist:") or current_content.startswith(
            "Error:"
        ):
            print_utf8(
                f"Fallback skipped for {file_name}: {current_content}"
            )
            continue

        prompt = f"""
Fix the diagnosed bug in this source file. Return only the complete
replacement file inside one fenced code block. Do not call tools, add
explanations, or include a diff.

Diagnosis:
{json.dumps(diagnosis, indent=2)}

File path: {file_name}
Current file contents:
```text
{current_content}
```
"""

        try:
            response = invoke_with_rate_limit_retry(
                lambda: groq_client.chat.completions.create(
                    model="openai/gpt-oss-20b",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                    max_completion_tokens=4096,
                    reasoning_effort="low",
                    include_reasoning=False,
                )
            )
            response_text = response.choices[0].message.content
            if not isinstance(response_text, str):
                raise ValueError(
                    "The model did not return text content."
                )

            code_blocks = re.findall(
                r"```[^\r\n]*\r?\n(.*?)\r?\n```",
                response_text,
                flags=re.DOTALL,
            )
            if len(code_blocks) != 1:
                raise ValueError(
                    "The model response must contain exactly one "
                    "complete-file code block."
                )

            replacement = code_blocks[0]
            write_result = json.loads(
                write_file.invoke(
                    {
                        "file_path": file_name,
                        "content": replacement,
                    }
                )
            )
            verified_content = read_file.invoke(
                {"file_path": file_name}
            )
            verified = (
                write_result.get("verified") is True
                and verified_content == replacement
            )
            WRITE_RESULTS[full_path] = {
                "verified": verified,
                "message": (
                    "Direct fallback modification verified."
                    if verified
                    else "Direct fallback modification failed verification."
                ),
            }
            print_utf8(
                f"Fallback for {file_name}: "
                f"{'verified' if verified else 'not verified'}"
            )
        except Exception as error:
            WRITE_RESULTS[full_path] = {
                "verified": False,
                "message": f"Direct fallback failed: {error}",
            }
            print_utf8(
                f"Fallback failed for {file_name}: {error}"
            )


def invoke_with_rate_limit_retry(invoke, attempts=3):
    for attempt in range(attempts):
        try:
            return invoke()
        except Exception as error:
            error_text = str(error)
            if (
                attempt + 1 >= attempts
                or "rate_limit_exceeded" not in error_text.lower()
            ):
                raise

            retry_match = re.search(
                r"try again in (?:(\d+)m)?([\d.]+)\s*"
                r"(ms|milliseconds|s|seconds)",
                error_text,
                flags=re.IGNORECASE,
            )
            if retry_match:
                minutes = int(retry_match.group(1) or 0)
                delay = minutes * 60 + float(retry_match.group(2))
                if retry_match.group(3).lower() in {"ms", "milliseconds"}:
                    delay /= 1000
            else:
                delay = 1.0

            delay = min(max(delay + 0.1, 0.25), 600.0)
            print_utf8(
                f"Groq rate limit reached; retrying in {delay:.2f}s "
                f"(attempt {attempt + 2}/{attempts})."
            )
            time.sleep(delay)


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


agent_error = None
result = None

try:
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
3. Trace the reported bug through the relevant application flow.
4. Inspect related source files when necessary to identify the
   real root cause.
5. Implement the proposed fix and correct any directly related
   application logic required for the fix.
6. Modify only source files directly required to fix the bug.
7. Preserve all unrelated functionality.
8. Use the write_file tool to actually save the changes.
9. Do not create tests.
10. Do not modify package-lock.json.
11. Do not modify node_modules.
12. After fixing, explain exactly what was changed.

This is a real application bug-fixing task.
The goal is to modify the actual application source code.
"""
                }
            ]
        }
    )
except Exception as error:
    agent_error = error
    print_utf8(f"Tool-based code fixing failed: {error}")


affected_files = diagnosis.get("affected_files", [])

if isinstance(affected_files, str):
    affected_files = [affected_files]

if not any(
    WRITE_RESULTS.get(
        os.path.abspath(os.path.join(TARGET_REPOSITORY, file_name)),
        {},
    ).get("verified") is True
    for file_name in affected_files
    if isinstance(file_name, str)
):
    apply_direct_fallback(
        affected_files,
        agent_error or "The tool agent did not verify a source change.",
    )


# ---------------------------------------------------------
# PRINT RESULT
# ---------------------------------------------------------

print("\n=============================")
print("CODE FIX RESULT")
print("=============================")

for message in result["messages"] if result is not None else []:

    if message.content:
        print("\nMessage:")
        print_utf8(message.content)



verified_files = []

for file_name in affected_files:
    if not isinstance(file_name, str):
        continue

    full_path = os.path.abspath(
        os.path.join(TARGET_REPOSITORY, file_name)
    )

    verification = WRITE_RESULTS.get(full_path)

    if (
        verification
        and verification.get("verified") is True
        and os.path.isfile(full_path)
    ):
        verified_files.append(
            {
                "file": file_name,
                "verified": True,
                "message": verification["message"],
            }
        )

if verified_files:
    code_fix_result = {
        "status": "SUCCESS",
        "modified_files": verified_files,
        "message": "Verified source modification(s) applied.",
    }
else:
    code_fix_result = {
        "status": "FAILED",
        "modified_files": [],
        "message": "No verified source modification was produced.",
    }

with open(
    CODE_FIX_RESULT_FILE,
    "w",
    encoding="utf-8",
) as file:
    json.dump(
        code_fix_result,
        file,
        indent=2,
        ensure_ascii=False,
    )

print_utf8(json.dumps(code_fix_result, indent=2))