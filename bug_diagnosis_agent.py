import argparse
import json
import os
import re
import sys

from dotenv import load_dotenv
from langchain_groq import ChatGroq
from groq import APIStatusError, Groq

from workflow_artifacts import resolve_artifact_path


# =========================================================
# Configuration
# =========================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

REPOSITORY_CONTEXT_FILE = resolve_artifact_path(
    "repository_context.json",
    BASE_DIR,
)

REPRODUCTION_RESULT_FILE = resolve_artifact_path(
    "reproduction_result.json",
    BASE_DIR,
)

DEFAULT_BUG_REPORT_FILE = os.path.join(
    BASE_DIR,
    "bug_report.json"
)

BUG_REPORT_PATH = os.getenv(
    "BUG_REPORT_FILE",
    DEFAULT_BUG_REPORT_FILE,
)

OUTPUT_FILE = resolve_artifact_path(
    "diagnosis.json",
    BASE_DIR,
)

MODEL_CALL_RESULT_FILE = resolve_artifact_path(
    "diagnosis_model_call.json",
    BASE_DIR,
)
DIAGNOSIS_PROMPT_CHAR_LIMIT = 8500
DIAGNOSIS_MAX_COMPLETION_TOKENS = 1536


class DiagnosisInputError(ValueError):
    pass


def load_json_object(path, label):
    try:
        with open(path, "r", encoding="utf-8") as file:
            value = json.load(file)
    except FileNotFoundError:
        raise DiagnosisInputError(f"{label} file was not found: {path}") from None
    except json.JSONDecodeError as error:
        raise DiagnosisInputError(
            f"{label} contains malformed JSON at line {error.lineno}, "
            f"column {error.colno}: {path}"
        ) from None
    except (OSError, UnicodeError) as error:
        raise DiagnosisInputError(
            f"{label} could not be read ({type(error).__name__}): {path}"
        ) from None

    if not isinstance(value, dict):
        raise DiagnosisInputError(f"{label} must contain a JSON object: {path}")
    return value


def validate_manual_reproduction(manual_reproduction):
    if not isinstance(manual_reproduction, dict):
        return "Manual reproduction context must be a JSON object."

    steps = manual_reproduction.get("steps")
    supporting_evidence = manual_reproduction.get("supporting_evidence", [])
    if manual_reproduction.get("status") != "MANUAL_SUBMITTED":
        return "Manual reproduction context has an invalid status."
    if manual_reproduction.get("source") != "user":
        return "Manual reproduction context must be identified as user-reported."
    if (
        not isinstance(steps, list)
        or not steps
        or any(not isinstance(step, str) or not step.strip() for step in steps)
    ):
        return "Manual reproduction context must include non-empty ordered steps."
    if (
        not isinstance(manual_reproduction.get("expected_behavior"), str)
        or not manual_reproduction["expected_behavior"].strip()
    ):
        return "Manual reproduction context must include expected behavior."
    if (
        not isinstance(manual_reproduction.get("observed_behavior"), str)
        or not manual_reproduction["observed_behavior"].strip()
    ):
        return "Manual reproduction context must include observed behavior."
    if (
        not isinstance(supporting_evidence, list)
        or any(not isinstance(item, str) for item in supporting_evidence)
    ):
        return "Manual reproduction supporting evidence must be a list of strings."
    return None


def load_reproduction_result(path, manual_mode):
    try:
        reproduction_result = load_json_object(path, "Reproduction result")
    except DiagnosisInputError as error:
        if not manual_mode:
            raise

        availability = "INVALID" if os.path.exists(path) else "UNAVAILABLE"
        return {
            "status": availability,
            "source": "automatic",
            "automatically_reproduced": False,
            "reason": str(error),
        }

    if manual_mode and reproduction_result.get("status") == "REPRODUCED":
        raise DiagnosisInputError(
            "Manual reproduction context cannot replace an automatic "
            "result marked REPRODUCED."
        )
    return reproduction_result


def sanitized_groq_status_error(error, api_key):
    if not isinstance(error, APIStatusError):
        return {}

    status_code = getattr(error, "status_code", None)
    metadata = {
        "http_status_code": status_code if isinstance(status_code, int) else None,
    }

    message = getattr(error, "message", None)
    if not isinstance(message, str):
        message = "Groq API request failed."

    message = re.sub(r"[\r\n\t]+", " ", message).strip()
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    message = re.sub(
        r"(?i)\bBearer\s+[^\s,;]+",
        "Bearer [REDACTED]",
        message,
    )
    message = re.sub(
        r"(?i)\b(?:authorization|x-api-key)\s*[:=]\s*[^,;]+",
        "[REDACTED_HEADER]",
        message,
    )
    message = re.sub(
        r"(?i)\b(?:api[_ -]?key|token|secret|password)\s*[:=]\s*[^,;\s]+",
        "[REDACTED]",
        message,
    )
    message = re.sub(
        r"\b(?:gsk_|sk-)[A-Za-z0-9_-]{8,}\b",
        "[REDACTED_KEY]",
        message,
    )
    message = re.sub(
        r"(?i)\b(?:prompt|request(?:[_ -]?(?:body|data))?|messages?)\s*[:=].*$",
        "[request data redacted]",
        message,
    )
    message = message[:400] or "Groq API request failed."
    metadata["provider_error_message"] = message
    return metadata


def write_model_call_result(model_call_result, error=None, api_key=None):
    if error is not None:
        model_call_result.update({
            "succeeded": False,
            "outcome": "failed",
            "error_type": type(error).__name__,
        })
        model_call_result.update(
            sanitized_groq_status_error(error, api_key)
        )

    with open(MODEL_CALL_RESULT_FILE, "w", encoding="utf-8") as file:
        json.dump(model_call_result, file, indent=2)


# =========================================================
# Load environment variables
# =========================================================

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not GROQ_API_KEY:
    raise ValueError(
        "GROQ_API_KEY is not set in the .env file"
    )

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(
        encoding="utf-8",
        errors="replace",
    )

# =========================================================
# Initialize Groq
# =========================================================

client = Groq(api_key=GROQ_API_KEY)


# =========================================================
# Parse optional diagnosis inputs
# =========================================================

argument_parser = argparse.ArgumentParser()
argument_parser.add_argument("bug_report", nargs="?")
argument_parser.add_argument("--manual-reproduction")
arguments = argument_parser.parse_args(sys.argv[1:])

manual_reproduction_file = arguments.manual_reproduction
bug_report_file = arguments.bug_report
if not bug_report_file:
    bug_report_file = BUG_REPORT_PATH


if os.path.exists(bug_report_file):
    try:
        bug_report = load_json_object(bug_report_file, "Bug report")
    except DiagnosisInputError as error:
        argument_parser.error(str(error))

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
try:
    repository_context = load_json_object(
        REPOSITORY_CONTEXT_FILE,
        "Repository context",
    )
    manual_reproduction = None
    if manual_reproduction_file:
        manual_reproduction = load_json_object(
            manual_reproduction_file,
            "Manual reproduction context",
        )
        manual_error = validate_manual_reproduction(manual_reproduction)
        if manual_error:
            raise DiagnosisInputError(manual_error)

    reproduction_result = load_reproduction_result(
        REPRODUCTION_RESULT_FILE,
        manual_mode=manual_reproduction is not None,
    )
except DiagnosisInputError as error:
    argument_parser.error(str(error))


print(
    "\nReproduction result loaded:",
    REPRODUCTION_RESULT_FILE
)


def compact_evidence_text(value, max_chars, prioritize_diagnostics=False):
    text = value if isinstance(value, str) else str(value)
    if len(text) <= max_chars:
        return text

    if not prioritize_diagnostics or max_chars < 160:
        half = max(1, (max_chars - 32) // 2)
        return f"{text[:half]}\n[... evidence clipped ...]\n{text[-half:]}"

    lines = text.splitlines()
    priority_patterns = (
        (r"expected|received|assert", 10),
        (r"\b(?:[A-Za-z]:)?[^\s:]+\.(?:py|js|ts|java|cs):\d+", 9),
        (r"^\s*at\s+", 8),
        (r"traceback|exception|error|failed|failure", 4),
        (r"status code|return code", 3),
    )
    scored = []
    for index, line in enumerate(lines):
        score = sum(
            weight
            for pattern, weight in priority_patterns
            if re.search(pattern, line, re.IGNORECASE)
        )
        if score:
            scored.append((score, index, line))

    selected_by_index = {}
    used = 0
    per_line_limit = max(80, max_chars // 3)
    candidates = sorted(scored, key=lambda item: (-item[0], item[1]))
    if not candidates:
        candidates = [(0, 0, lines[0]), (0, len(lines) - 1, lines[-1])]

    for score, index, line in candidates:
        if index in selected_by_index or max_chars - used <= 24:
            continue
        line_limit = min(per_line_limit, max_chars - used - 1)
        clipped_line = compact_evidence_text(line, line_limit)
        selected_by_index[index] = clipped_line
        used += len(clipped_line) + 1

    selected = [selected_by_index[index] for index in sorted(selected_by_index)]

    if not selected:
        return f"{text[:max_chars - 24]} [... clipped]"
    compacted = "\n".join(selected)
    if len(compacted) > max_chars:
        compacted = compacted[:max_chars - 24] + " [... clipped]"
    return compacted


def compact_json_value(value, max_chars, prioritize_diagnostics=False, depth=0):
    if max_chars <= 0 or depth > 8:
        return None
    if isinstance(value, str):
        return compact_evidence_text(value, max_chars, prioritize_diagnostics)
    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, dict):
        key_priority = (
            "bug_title", "status", "expected_behavior", "actual_behavior",
            "failure_analysis", "root_cause", "error", "errors", "return_code",
            "test_result", "stderr", "stdout", "traceback", "stack_trace",
            "command", "evidence", "observed", "attempted", "bugs", "execution_strategy",
            "architecture", "technology", "framework", "components", "affected_files",
        )
        keys = sorted(
            value,
            key=lambda key: (
                key_priority.index(str(key).lower())
                if str(key).lower() in key_priority
                else len(key_priority),
                str(key),
            ),
        )
        compact = {}
        for key in keys:
            candidate_size = len(json.dumps(compact, ensure_ascii=False))
            remaining = max_chars - candidate_size - len(str(key)) - 8
            if remaining < 32:
                break
            per_value_limit = min(remaining, max(96, max_chars // 4))
            compact_value = compact_json_value(
                value[key],
                per_value_limit,
                prioritize_diagnostics,
                depth + 1,
            )
            compact[str(key)] = compact_value
            if len(json.dumps(compact, ensure_ascii=False)) > max_chars:
                compact.pop(str(key), None)
        return compact

    if isinstance(value, (list, tuple)):
        values = list(value)
        if prioritize_diagnostics:
            values.sort(
                key=lambda item: -sum(
                    bool(re.search(pattern, str(item), re.IGNORECASE))
                    for pattern in (
                        r"error|exception|failed|failure",
                        r"expected|received|assert",
                        r"traceback|\bat\s+",
                    )
                )
            )
        compact = []
        for item in values:
            candidate_size = len(json.dumps(compact, ensure_ascii=False))
            remaining = max_chars - candidate_size - 4
            if remaining < 32:
                break
            item_limit = min(remaining, max(96, max_chars // 5))
            candidate = compact_json_value(
                item,
                item_limit,
                prioritize_diagnostics,
                depth + 1,
            )
            compact.append(candidate)
            if len(json.dumps(compact, ensure_ascii=False)) > max_chars:
                compact.pop()
                break
        return compact

    return compact_evidence_text(value, max_chars, prioritize_diagnostics)


def compact_repository_context(context, bug, reproduction=None, max_chars=3600):
    """Keep architecture and the most relevant source excerpts only."""
    context = context if isinstance(context, dict) else {}
    compact = {
        key: context[key]
        for key in (
            "repository_path",
            "file_count",
            "project_types",
            "execution_strategy",
            "architecture",
        )
        if key in context
    }
    bug_text = json.dumps(bug, ensure_ascii=False).lower()
    reproduction_text = json.dumps(reproduction or {}, ensure_ascii=False).lower()
    signals = bug_text + "\n" + reproduction_text
    file_contents = context.get("file_contents", {})
    if not isinstance(file_contents, dict):
        file_contents = {}

    scored_files = []
    for path, content in file_contents.items():
        normalized_path = str(path).replace("\\", "/")
        basename = normalized_path.rsplit("/", 1)[-1].lower()
        score = 0
        if normalized_path.lower() in signals:
            score += 20
        score += sum(
            3
            for term in (
                "handler", "controller", "route", "service", "repository",
                "model", "error", "test", "spec", "serverless", "package.json",
            )
            if term in basename or term in normalized_path.lower()
        )
        score += min(8, signals.count(basename)) if basename else 0
        scored_files.append((score, normalized_path, content))

    scored_files.sort(key=lambda item: (-item[0], item[1]))
    relevant_files = [item for item in scored_files if item[0] > 0]
    if not relevant_files:
        fallback_terms = (
            "handler", "controller", "route", "service", "serverless",
            "app.", "server.", "readme", "package.json",
        )
        relevant_files = [
            item
            for item in scored_files
            if any(term in item[1].lower() for term in fallback_terms)
        ][:5]

    source_excerpts = {}
    source_budget = 2200
    for _score, path, content in relevant_files[:5]:
        if not isinstance(content, str):
            continue
        available = source_budget - sum(len(value) for value in source_excerpts.values())
        if available <= 0:
            break
        excerpt_limit = min(850, available)
        source_excerpts[path] = compact_evidence_text(content, excerpt_limit)

    compact["important_files"] = [
        path for _score, path, _content in relevant_files[:12]
    ]
    compact["file_contents"] = source_excerpts
    compact["context_note"] = (
        "Repository inventory and source excerpts are ranked and bounded; "
        "reproduction output is the primary runtime evidence."
    )
    return compact_json_value(compact, max_chars)


def compact_bug_report(bug, max_chars=700):
    if not isinstance(bug, dict):
        return compact_json_value(bug, max_chars)
    prioritized = (
        "bug_title", "project_type", "description", "bug", "expected_behavior",
        "actual_behavior", "symptoms", "reproduction", "steps_to_reproduce",
        "affected_files", "severity",
    )
    selected = {key: bug[key] for key in prioritized if key in bug}
    if not selected:
        selected = bug
    return compact_json_value(selected, max_chars, prioritize_diagnostics=True)


def compact_reproduction_result(reproduction, max_chars=2800):
    return compact_json_value(
        reproduction,
        max_chars,
        prioritize_diagnostics=True,
    )


def build_diagnosis_prompt(
    bug,
    reproduction,
    repository_context,
    manual_reproduction=None,
):
    if manual_reproduction is None:
        reproduction_context = reproduction
        source_guidance = (
            "Use the confirmed automatic reproduction as primary evidence and "
            "the bounded repository excerpts as supporting evidence."
        )
        reproduction_heading = "BUG REPRODUCTION RESULT"
    else:
        reproduction_context = {
            "source": "user-reported-manual-reproduction",
            "automatically_reproduced": False,
            "automatic_reproduction_result": reproduction,
            "manual_reproduction_report": manual_reproduction,
        }
        source_guidance = (
            "The automatic reproduction did not confirm the issue. Treat the "
            "manual reproduction report as user-provided evidence, not as an "
            "automated test or confirmed automatic reproduction. Distinguish "
            "reported observations from facts verified in repository code, and "
            "do not claim that an automated reproduction test passed. Use the "
            "bounded repository excerpts as supporting evidence."
        )
        reproduction_heading = "REPRODUCTION CONTEXT (MANUAL USER REPORT)"

    instruction = """You are an AI software bug diagnosis agent. Use the confirmed reproduction as primary evidence and the bounded repository excerpts as supporting evidence. Respect the detected language/framework; do not assume Python. Diagnose all reported symptoms, trace relevant data flow, identify exact affected files, explain the root cause, propose a minimal safe fix, and give a verification plan. Do not modify source or tests. Do not guess when evidence is insufficient. Return only a complete JSON object, no markdown or surrounding prose, using exactly this schema:
{"bug_title":"string","project_type":"string","failure_analysis":"string","root_cause":"string","affected_files":["string"],"evidence":["string"],"proposed_fix":"string","verification_plan":["string"]}

BUG REPORT:
__BUG_REPORT_JSON__

__REPRODUCTION_HEADING__:
__REPRODUCTION_RESULT_JSON__

BOUNDED REPOSITORY CONTEXT:
__REPOSITORY_CONTEXT_JSON__
"""
    instruction = instruction.replace(
        "Use the confirmed reproduction as primary evidence and the bounded repository excerpts as supporting evidence.",
        "__SOURCE_GUIDANCE__",
    ).replace("__SOURCE_GUIDANCE__", source_guidance)
    instruction = instruction.replace(
        "__REPRODUCTION_HEADING__",
        reproduction_heading,
    )
    budgets = {"bug": 650, "reproduction": 2600, "context": 3000}
    for _attempt in range(8):
        compact_bug = compact_bug_report(bug, budgets["bug"])
        compact_reproduction = compact_reproduction_result(
            reproduction_context,
            budgets["reproduction"],
        )
        compact_context = compact_repository_context(
            repository_context,
            bug,
            reproduction_context,
            budgets["context"],
        )
        prompt = instruction.replace(
            "__BUG_REPORT_JSON__",
            json.dumps(compact_bug, ensure_ascii=False, separators=(",", ":")),
        ).replace(
            "__REPRODUCTION_RESULT_JSON__",
            json.dumps(
                compact_reproduction,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ).replace(
            "__REPOSITORY_CONTEXT_JSON__",
            json.dumps(compact_context, ensure_ascii=False, separators=(",", ":")),
        )
        if len(prompt) <= DIAGNOSIS_PROMPT_CHAR_LIMIT:
            return prompt
        for name in ("reproduction", "context", "bug"):
            budgets[name] = max(128, int(budgets[name] * 0.75))

    raise ValueError(
        "Compacted diagnosis prompt exceeds the configured character budget."
    )


prompt = build_diagnosis_prompt(
    bug_report,
    reproduction_result,
    repository_context,
    manual_reproduction,
)


# =========================================================
# Call Groq
# =========================================================

print(
    "\nRunning AI diagnosis with Groq..."
)

model_call_result = {
    "attempted": True,
    "succeeded": False,
    "provider": "Groq",
    "model": "openai/gpt-oss-20b",
    "outcome": "in_progress",
    "prompt_character_count": len(prompt),
    "prompt_character_limit": DIAGNOSIS_PROMPT_CHAR_LIMIT,
    "max_completion_tokens": DIAGNOSIS_MAX_COMPLETION_TOKENS,
}

write_model_call_result(model_call_result)

try:
    response = client.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0,
        max_completion_tokens=DIAGNOSIS_MAX_COMPLETION_TOKENS,
        reasoning_effort="low",
        include_reasoning=False
    )
except Exception as error:
    write_model_call_result(model_call_result, error, GROQ_API_KEY)
    print("Groq diagnosis request failed; sanitized status metadata was saved.")
    sys.exit(1)
else:
    model_call_result.update({
        "succeeded": True,
        "outcome": "succeeded",
    })
    write_model_call_result(model_call_result)

message = response.choices[0].message

print("\n===== GROQ DEBUG =====")
print("Finish reason:", response.choices[0].finish_reason)
print("Content:", repr(message.content).encode("ascii", "backslashreplace").decode("ascii"))
print("Reasoning:", repr(getattr(message, "reasoning", None)).encode("ascii", "backslashreplace").decode("ascii"))
print("======================")

result = message.content or ""
result = result.strip()

print("\nRaw Groq response:")
print(json.dumps(result, ensure_ascii=False, indent=2))

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