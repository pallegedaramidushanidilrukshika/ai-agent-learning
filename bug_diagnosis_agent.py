import json
import os
import sys

from dotenv import load_dotenv
from langchain_groq import ChatGroq
from groq import Groq


# =========================================================
# Configuration
# =========================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

REPOSITORY_CONTEXT_FILE = os.path.join(
    BASE_DIR,
    "repository_context.json"
)

REPRODUCTION_RESULT_FILE = os.path.join(
    BASE_DIR,
    "reproduction_result.json"
)

DEFAULT_BUG_REPORT_FILE = os.path.join(
    BASE_DIR,
    "bug_report.json"
)

OUTPUT_FILE = os.path.join(
    BASE_DIR,
    "diagnosis.json"
)


# =========================================================
# Load environment variables
# =========================================================

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not GROQ_API_KEY:
    raise ValueError(
        "GROQ_API_KEY is not set in the .env file"
    )


# =========================================================
# Initialize Groq
# =========================================================

client = Groq(api_key=GROQ_API_KEY)


# =========================================================
# Load bug report
# =========================================================

if len(sys.argv) > 1:

    bug_report_file = sys.argv[1]

else:

    bug_report_file = DEFAULT_BUG_REPORT_FILE


if os.path.exists(bug_report_file):

    with open(
        bug_report_file,
        "r",
        encoding="utf-8"
    ) as file:

        bug_report = json.load(file)

    print(
        f"\nBug report loaded: {bug_report_file}"
    )

else:

    bug_report = {
        "source": "reproduction_result",
        "description": (
            "Analyze the reported application behavior "
            "using the reproduction result and repository "
            "evidence."
        )
    }

    print(
        "\nNo separate bug_report.json found."
    )

    print(
        "Using reproduction_result.json as bug evidence."
    )


# =========================================================
# Load repository context
# =========================================================

with open(
    REPOSITORY_CONTEXT_FILE,
    "r",
    encoding="utf-8"
) as file:

    repository_context = json.load(file)


# =========================================================
# Load reproduction result
# =========================================================

if not os.path.exists(REPRODUCTION_RESULT_FILE):

    raise FileNotFoundError(
        "reproduction_result.json was not found. "
        "Run bug_reproduction_agent.py first."
    )


with open(
    REPRODUCTION_RESULT_FILE,
    "r",
    encoding="utf-8"
) as file:

    reproduction_result = json.load(file)


print(
    "\nReproduction result loaded:",
    REPRODUCTION_RESULT_FILE
)


# =========================================================
# AI Diagnosis Prompt
# =========================================================

prompt = f"""
You are an AI software bug diagnosis agent.

Your job is to analyze a REAL application bug.

IMPORTANT RULES:

1. Analyze the repository using the provided repository context.
2. Analyze the reproduction result as execution evidence.
3. Do NOT create or modify tests.
4. Do NOT modify source files.
5. Do NOT assume the project is Python.
6. The project may use Node.js, Python, Java, .NET,
   React, Angular, or another technology.
7. Respect the detected technology and execution strategy.
8. If the bug was reproduced, use the observed failure
   as primary evidence.
9. If the bug was NOT_REPRODUCED because the current
   source is already fixed, do NOT claim that the bug
   was reproduced.
10. In the NOT_REPRODUCED case, use the reported bug,
    repository evidence, and current source to identify
    the most likely original root cause.
11. Identify the exact affected files.
12. Explain why the bug occurs.
13. Propose the minimum safe source-code fix.
14. Provide a verification plan.
15. Return ONLY valid JSON.
16. Do not wrap the JSON in markdown code fences.
17. Your response MUST contain a complete JSON object.
18. Never return an empty response.
19. Do not add explanations before or after the JSON.

BUG REPORT:
{json.dumps(bug_report, indent=2)}

BUG REPRODUCTION RESULT:
{json.dumps(reproduction_result, indent=2)}

REPOSITORY CONTEXT:
{json.dumps(repository_context, indent=2)}

Return exactly this JSON structure:

{{
    "bug_title": "...",
    "project_type": "...",
    "failure_analysis": "...",
    "root_cause": "...",
    "affected_files": [
        "..."
    ],
    "evidence": [
        "..."
    ],
    "proposed_fix": "...",
    "verification_plan": [
        "..."
    ]
}}
"""


# =========================================================
# Call Groq
# =========================================================

print(
    "\nRunning AI diagnosis with Groq..."
)

response = client.chat.completions.create(
    model="openai/gpt-oss-20b",
    messages=[
        {
            "role": "user",
            "content": prompt
        }
    ],
    temperature=0,
    max_completion_tokens=4096,
    reasoning_effort="low",
    include_reasoning=False
)

message = response.choices[0].message

print("\n===== GROQ DEBUG =====")
print("Finish reason:", response.choices[0].finish_reason)
print("Content:", repr(message.content))
print("Reasoning:", repr(getattr(message, "reasoning", None)))
print("======================")

result = message.content or ""
result = result.strip()

print("\nRaw Groq response:")
print(result)

# =========================================================
# Clean AI response
# =========================================================

if result.startswith("```json"):
    result = result[7:]

elif result.startswith("```"):
    result = result[3:]

if result.endswith("```"):
    result = result[:-3]

result = result.strip()


# =========================================================
# Handle empty response
# =========================================================

if not result:

    print("\nERROR: Groq returned an empty response.")
    print("The diagnosis was not generated.")

    sys.exit(1)


# =========================================================
# Parse JSON
# =========================================================

try:

    diagnosis = json.loads(result)

except json.JSONDecodeError:

    print("\nERROR: Groq returned invalid JSON.")

    print("\nRaw response:\n")
    print(result)

    sys.exit(1)


# =========================================================
# Validate required fields
# =========================================================

required_fields = [
    "bug_title",
    "project_type",
    "failure_analysis",
    "root_cause",
    "affected_files",
    "evidence",
    "proposed_fix",
    "verification_plan"
]

missing_fields = [
    field
    for field in required_fields
    if field not in diagnosis
]

if missing_fields:

    raise ValueError(
        "Diagnosis is missing required fields: "
        + ", ".join(missing_fields)
    )


# =========================================================
# Display diagnosis
# =========================================================

print(
    "\n===== AI BUG DIAGNOSIS =====\n"
)

print(
    json.dumps(
        diagnosis,
        indent=2,
        ensure_ascii=False
    )
)


# =========================================================
# Save diagnosis
# =========================================================

with open(
    OUTPUT_FILE,
    "w",
    encoding="utf-8"
) as file:

    json.dump(
        diagnosis,
        file,
        indent=2,
        ensure_ascii=False
    )


print(
    f"\nDiagnosis saved to: {OUTPUT_FILE}"
)