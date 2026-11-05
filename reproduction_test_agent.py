"""AI reproduction-test agent.

When automatic reproduction cannot confirm a bug, this agent asks the model
to write ONE test that reproduces it, then runs that test on the current code.
A human reviews the test and its result before it is used as evidence; after
the fix the same test must pass.

Writes reproduction_test.json in the run's artifact directory.
Exit code 0 means an artifact was written (status GENERATED or UNSUPPORTED);
1 means no usable test was produced.
"""

import argparse
import json
import os
import re
import sys

from workflow_artifacts import resolve_artifact_path

import reproduction_tests as rt
from workflow_validation import source_change_errors

# The Groq free tier allows 8,000 tokens per minute. The SDK waits for the
# retry-after time the API returns, so a few retries let the minute reset.
GROQ_MAX_RETRIES = 5

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPOSITORY_CONTEXT_FILE = resolve_artifact_path("repository_context.json", BASE_DIR)
OUTPUT_FILE = resolve_artifact_path("reproduction_test.json", BASE_DIR)
MODEL = "openai/gpt-oss-20b"
MODEL_ATTEMPTS = 2
MAX_FILE_CHARS = 3500
MAX_CONTEXT_CHARS = 12000
IGNORED_DIRECTORIES = {
    "node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build",
    "coverage", ".next", ".serverless",
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_json(path):
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def save_result(result):
    with open(OUTPUT_FILE, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=False)


def inside(repository, relative_path):
    full = os.path.abspath(os.path.join(repository, relative_path))
    root = os.path.abspath(repository)
    return full if os.path.commonpath([root, full]) == root else None


def read_text(path, limit=MAX_FILE_CHARS):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    if len(text) > limit:
        return text[:limit] + "\n... [truncated]"
    return text


def discover_example_test(repository, framework, generated_path):
    """Find one existing test in the same framework as a style example."""
    wanted = (".test.js", ".spec.js", ".test.cjs") if framework == "jest" else (".py",)
    candidates = []
    for root, directories, files in os.walk(repository):
        directories[:] = [name for name in directories if name not in IGNORED_DIRECTORIES]
        for name in files:
            relative = os.path.relpath(os.path.join(root, name), repository).replace(os.sep, "/")
            if relative == generated_path or "generated-regression" in name:
                continue
            if framework == "pytest" and not name.startswith("test_"):
                continue
            if name.endswith(wanted):
                candidates.append(relative)
    return sorted(candidates, key=len)[:1]


def relevant_source_files(context, signals, limit=4):
    file_contents = context.get("file_contents")
    paths = list(file_contents) if isinstance(file_contents, dict) else []
    scored = []
    for path in paths:
        normalized = str(path).replace("\\", "/")
        lower = normalized.lower()
        if lower.endswith(("package-lock.json", ".md", ".html", ".css", "tsconfig.json")):
            continue
        basename = lower.rsplit("/", 1)[-1]
        score = 0
        if lower in signals:
            score += 20
        if basename and basename in signals:
            score += 10
        score += sum(3 for term in ("handler", "route", "service", "model", "src/", "serverless", "app.", "server.") if term in lower)
        scored.append((score, normalized))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [path for score, path in scored if score > 0][:limit]


def declared_packages(repository, test_path):
    names = set()
    directory = os.path.dirname(os.path.join(repository, test_path))
    root = os.path.abspath(repository)
    while True:
        manifest = load_json(os.path.join(directory, "package.json"))
        if manifest:
            for section in ("dependencies", "devDependencies"):
                if isinstance(manifest.get(section), dict):
                    names.update(manifest[section])
        if os.path.abspath(directory) == root or os.path.dirname(directory) == directory:
            break
        directory = os.path.dirname(directory)
    return sorted(names)


MAX_EVIDENCE_CHARS = 4000


def evidence_section(evidence):
    """Summaries of user-attached recordings and logs (secrets already redacted)."""
    files = evidence.get("files") if isinstance(evidence, dict) else None
    if not isinstance(files, list) or not files:
        return []
    parts = []
    budget = MAX_EVIDENCE_CHARS
    for item in files:
        if not isinstance(item, dict) or budget <= 0:
            continue
        text = f"--- {item.get('kind')}: {item.get('name')} ---\n{str(item.get('summary', ''))}"[:budget]
        budget -= len(text)
        parts.append(text)
    return [
        "",
        "ATTACHED EVIDENCE (user-provided recordings and logs; secrets were redacted):",
        "Use it to recreate the exact requests, messages and inputs in the test, "
        "with mocks instead of real network calls.",
        "\n\n".join(parts),
    ]


def import_path_hints(runner, source_paths):
    """Exact require() paths from the test file, so the model need not guess."""
    if runner["framework"] != "jest":
        return []
    test_directory = os.path.dirname(runner["test_path"]) or "."
    hints = []
    for path in source_paths:
        normalized = str(path).replace("\\", "/")
        if normalized.endswith((".yml", ".yaml", ".json")):
            relative = os.path.relpath(normalized, test_directory).replace(os.sep, "/")
            hints.append(f"- {normalized}: fs.readFileSync(path.join(__dirname, '{relative}'), 'utf8')")
            continue
        if not normalized.endswith((".js", ".cjs", ".mjs")):
            continue
        relative = os.path.relpath(os.path.splitext(normalized)[0], test_directory).replace(os.sep, "/")
        if not relative.startswith("."):
            relative = "./" + relative
        hints.append(f"- {normalized}: require('{relative}')")
    return hints


def build_prompt(bug, manual, runner, repository, context, feedback, evidence=None):
    signals = json.dumps([bug, manual], ensure_ascii=False).lower()
    files = []
    budget = MAX_CONTEXT_CHARS
    source_paths = relevant_source_files(context, signals)
    hints = import_path_hints(runner, source_paths)
    for path in source_paths + discover_example_test(
        repository, runner["framework"], runner["test_path"]
    ):
        full = inside(repository, path)
        text = read_text(full) if full else None
        if not text or budget <= 0:
            continue
        text = text[:budget]
        budget -= len(text)
        files.append(f"--- FILE: {path} ---\n{text}")

    framework_rules = (
        "Write a Jest test in CommonJS (require, not import). Mock external services "
        "(for example AWS SDK clients) with jest.mock exactly like the example test does, "
        "so the test never calls a real network or cloud service. "
        f"Only these packages are installed: {', '.join(declared_packages(repository, runner['test_path'])) or 'none'}. "
        "Require application files with paths relative to the test file location. "
        "Because jest.mock factories run when the application module is first loaded, "
        "define mock variables (names starting with 'mock') and call jest.mock BEFORE "
        "requiring the application module; never require it at the top of the file. "
        "Do not use instanceof on mocked classes; inspect the recorded call arguments instead. "
        "A mocked command class must keep its input, for example "
        "PutItemCommand: jest.fn((input) => ({ input })); a bare jest.fn() makes .input undefined. "
        "Only counting calls does not show lost or overwritten data: if the bug is about stored data, "
        "make the mocked client behave like the real store (for example save items in a Map keyed by "
        "the table's configured key attributes) and assert on what is stored afterwards. "
        "If the behaviour depends on configuration (for example a database key schema in "
        "serverless.yml), read that configuration file in the test instead of hard-coding it, "
        "so the test passes once the configuration is fixed. If no YAML package is installed, "
        "read the YAML as text and extract the values you need with a small regular expression. "
        "When a store is keyed by configuration, build each item's key from ALL configured key "
        "attributes (for example every AttributeName in the KeySchema), not only the first one. "
        "Do not rely on blank lines or exact layout when parsing configuration. If parsing finds "
        "nothing, throw new Error('...') at load time, so a broken test is reported as an error "
        "instead of looking like the bug."
        if runner["framework"] == "jest"
        else "Write a pytest test. Do not call real network or cloud services; use monkeypatch or "
        "unittest.mock for external services. Import application modules the way the example test does."
    )
    sections = [
        "You write ONE automated test that reproduces a reported bug.",
        f"The test file will be saved at: {runner['test_path']} (framework: {runner['framework']}).",
        "Requirements:",
        "- On the CURRENT code the test must FAIL with an assertion that shows the bug. "
        "A crash inside the test (missing file, undefined variable, wrong path) does not count; "
        "if the bug is a crash, assert it with expect(...).toThrow or .rejects.",
        "- After a correct fix the same test must PASS without changes.",
        "- Test observable behaviour, not implementation details. Keep it small and focused.",
        "- Do not modify or include application source code; only the test file.",
        f"- {framework_rules}",
        "Return only the complete test file inside one fenced code block, with no other text.",
    ]
    if hints:
        sections += [f"Use exactly these paths to load application files from {runner['test_path']}:"] + hints
    sections += [
        "",
        "BUG REPORT:",
        json.dumps(bug, ensure_ascii=False)[:2000],
    ]
    if manual:
        sections += [
            "",
            "USER-REPORTED REPRODUCTION (not automatically verified):",
            json.dumps(
                {key: manual.get(key) for key in ("steps", "expected_behavior", "observed_behavior", "supporting_evidence")},
                ensure_ascii=False,
            )[:3000],
        ]
    sections += evidence_section(evidence)
    if feedback:
        sections += [
            "",
            "PREVIOUS ATTEMPT (revise it):",
            f"Previous test:\n{str(feedback.get('previous_content', ''))[:4000]}",
            f"Result on current code: {feedback.get('previous_classification')}",
            f"Runner output (tail):\n{str(feedback.get('previous_output', ''))[-1500:]}",
            f"Reviewer feedback: {feedback.get('feedback') or 'none'}",
        ]
    sections += ["", "REPOSITORY FILES:", "\n\n".join(files) or "(none available)"]
    return "\n".join(sections)


def extract_single_code_block(text):
    blocks = re.findall(r"```[^\r\n]*\r?\n(.*?)\r?\n```", text or "", flags=re.DOTALL)
    return blocks[0] if len(blocks) == 1 else None


def jest_test_problems(content):
    """Mistakes that make a Jest test fail for reasons unrelated to the bug."""
    if "jest.mock(" not in content:
        return []
    mocked = set(re.findall(r"^\s*(\w+)\s*:\s*jest\.fn", content, flags=re.MULTILINE))
    used = sorted({name for name in re.findall(r"\binstanceof\s+(\w+)", content) if name in mocked})
    problems = []
    if used:
        problems.append(
            f"Do not use instanceof with mocked {', '.join(used)}: the mock returns a plain object, "
            "so the check is always false. Read command.input instead."
        )
    # Jest only lets a jest.mock factory use outer variables whose names start with "mock".
    declared = {
        name for name in re.findall(r"^(?:const|let|var)\s+(\w+)\s*=", content, flags=re.MULTILINE)
        if not name.lower().startswith("mock")
    }
    factories = " ".join(re.findall(r"jest\.mock\(.*?\n\}\)\);", content, flags=re.DOTALL))
    outside = sorted(name for name in declared if re.search(rf"\b{name}\b", factories))
    if outside:
        problems.append(
            f"jest.mock factories may only use outer variables named mock...; rename {', '.join(outside)} "
            "(for example mockPutItemCommand) or define it inside the factory."
        )
    return problems


def request_test(client, prompt, guard):
    """Ask the model for the test; retry once with the reason when unusable."""
    messages = [{"role": "user", "content": prompt}]
    last_reason = None
    for attempt in range(1, MODEL_ATTEMPTS + 1):
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0,
            max_completion_tokens=4096,
            reasoning_effort="low",
            include_reasoning=False,
        )
        content = extract_single_code_block(response.choices[0].message.content)
        if content is None:
            last_reason = "The response must contain exactly one fenced code block with the complete test file."
        else:
            errors = guard(content)
            if not errors:
                return content, attempt, None
            last_reason = " ".join(errors)
        print(f"Model output rejected ({last_reason}); attempt {attempt}/{MODEL_ATTEMPTS}.")
        messages = [{
            "role": "user",
            "content": prompt + "\n\nYOUR PREVIOUS RESPONSE WAS REJECTED: " + last_reason[:600],
        }]
    return None, MODEL_ATTEMPTS, last_reason


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("bug_report", nargs="?")
    parser.add_argument("--manual-reproduction")
    parser.add_argument("--feedback")
    parser.add_argument("--evidence")
    parser.add_argument("--round", type=int, default=1)
    arguments = parser.parse_args(argv)

    context = load_json(REPOSITORY_CONTEXT_FILE)
    bug = load_json(arguments.bug_report or os.getenv("BUG_REPORT_FILE")) or {}
    manual = load_json(arguments.manual_reproduction)
    feedback = load_json(arguments.feedback)
    evidence = load_json(arguments.evidence)
    if not context or not context.get("repository_path"):
        save_result({"status": "FAILED", "reason": "Repository context is missing."})
        return 1
    repository = os.path.abspath(context["repository_path"])
    runner = rt.select_runner(context)
    if runner is None:
        save_result({
            "status": "UNSUPPORTED",
            "reason": "AI reproduction tests currently support Jest and pytest projects only.",
        })
        print("AI reproduction test is not supported for this project; use manual reproduction.")
        return 0

    test_full_path = inside(repository, runner["test_path"])
    if test_full_path is None:
        save_result({"status": "FAILED", "reason": "Test path is outside the repository."})
        return 1

    from dotenv import load_dotenv
    from groq import Groq

    load_dotenv()
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        save_result({"status": "FAILED", "reason": "GROQ_API_KEY is not set."})
        return 1
    client = Groq(api_key=api_key, max_retries=GROQ_MAX_RETRIES)

    previous = read_text(test_full_path, limit=10**6) if os.path.isfile(test_full_path) else None
    prompt = build_prompt(bug, manual, runner, repository, context, feedback, evidence)
    try:
        content, model_attempts, reason = request_test(
            client,
            prompt,
            lambda text: source_change_errors(runner["test_path"], previous, text, repository)
            + (jest_test_problems(text) if runner["framework"] == "jest" else []),
        )
    except Exception as error:
        # Keep the provider's message (for example which rate limit was hit), never the request.
        detail = re.sub(r"(gsk_|sk-)[A-Za-z0-9_-]+", "[redacted]", str(error))[:400]
        save_result({
            "status": "FAILED",
            "reason": f"Model request failed ({type(error).__name__}).",
            "detail": detail,
            "round": arguments.round,
        })
        print(f"Model request failed: {type(error).__name__}: {detail}")
        return 1
    if content is None:
        save_result({"status": "FAILED", "reason": reason, "round": arguments.round})
        return 1

    os.makedirs(os.path.dirname(test_full_path), exist_ok=True)
    with open(test_full_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)

    run = rt.run_single_test(repository, runner)
    classification = rt.classify_run(run)
    result = {
        "status": "GENERATED",
        "round": arguments.round,
        "framework": runner["framework"],
        "test_file": runner["test_path"],
        "content": content,
        "content_sha256": rt.content_sha256(content),
        "classification": classification,
        "run": run,
        "model": MODEL,
        "model_attempts": model_attempts,
        "feedback_used": (feedback or {}).get("feedback"),
    }
    save_result(result)
    print(f"Reproduction test written to {runner['test_path']}")
    print(f"Result on current code: {classification}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
