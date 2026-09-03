import json
import os
import sys

from dotenv import load_dotenv
from google import genai


# Load environment variables
load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY")

if not API_KEY:
    raise ValueError("GEMINI_API_KEY is not set in the .env file")


client = genai.Client(api_key=API_KEY)


# ---------------------------------------------------------
# Load bug report
# ---------------------------------------------------------

if len(sys.argv) > 1:
    bug_report_file = sys.argv[1]
else:
    bug_report_file = "bug_report.json"

with open(bug_report_file, "r", encoding="utf-8") as f:
    bug_report = json.load(f)

print(f"\nBug report loaded: {bug_report_file}")


# ---------------------------------------------------------
# Load repository context
# ---------------------------------------------------------

with open("repository_context.json", "r", encoding="utf-8") as f:
    repository_context = json.load(f)


# ---------------------------------------------------------
# AI diagnosis prompt
# ---------------------------------------------------------

prompt = f"""
You are an AI software bug diagnosis agent.

Your job is to analyze a REAL application bug.

IMPORTANT:
- Do not create a unit test.
- Do not assume the bug is a Python bug.
- The target application can use different technologies.
- Analyze the provided repository context.
- Identify the most likely root cause.
- Identify the exact file(s) responsible.
- Explain why the bug happens.
- Propose a minimal and safe source-code fix.
- Do not modify files yet.

BUG REPORT:
{json.dumps(bug_report, indent=2)}

REPOSITORY CONTEXT:
{json.dumps(repository_context, indent=2)}

Return ONLY valid JSON using this structure:

{{
    "bug_title": "...",
    "project_type": "...",
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


# ---------------------------------------------------------
# Call Gemini
# ---------------------------------------------------------

response = client.models.generate_content(
    model="gemini-3.6-flash",
    contents=prompt
)


# ---------------------------------------------------------
# Parse AI response
# ---------------------------------------------------------

result = response.text.strip()

# Remove markdown JSON fences if Gemini adds them
if result.startswith("```json"):
    result = result[7:]

if result.endswith("```"):
    result = result[:-3]

result = result.strip()

diagnosis = json.loads(result)


# ---------------------------------------------------------
# Display diagnosis
# ---------------------------------------------------------

print("\n===== AI BUG DIAGNOSIS =====\n")

print(json.dumps(diagnosis, indent=2))


# ---------------------------------------------------------
# Save diagnosis to JSON file
# ---------------------------------------------------------

base_name = os.path.splitext(
    os.path.basename(bug_report_file)
)[0]

diagnosis_file = f"{base_name.replace('bug_report', 'diagnosis')}.json"

with open(diagnosis_file, "w", encoding="utf-8") as f:
    json.dump(
        diagnosis,
        f,
        indent=2,
        ensure_ascii=False
    )

print(f"\nDiagnosis saved to {diagnosis_file}")