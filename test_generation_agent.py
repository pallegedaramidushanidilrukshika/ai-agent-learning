import contextlib
import json
import importlib.util
import os
import re
import shlex
import subprocess
import sys

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_groq import ChatGroq
from workflow_artifacts import resolve_artifact_path
from workflow_validation import parse_test_summary, project_process_environment, run_bounded

# The Groq free tier allows 8,000 tokens per minute. The SDK waits for the
# retry-after time the API returns, so a few retries let the minute reset.
GROQ_MAX_RETRIES = 5

# Jest prints symbols such as ●; a cp1252 console cannot encode them.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ============================================================
# PATHS
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

REPOSITORY_CONTEXT_FILE = resolve_artifact_path(
    "repository_context.json",
    BASE_DIR,
)

OUTPUT_FILE = resolve_artifact_path("test_generation_result.json", BASE_DIR)


# ============================================================
# JSON HELPERS
# ============================================================

def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)

        return data if isinstance(data, dict) else None

    except (OSError, json.JSONDecodeError):
        return None


def save_json(data):
    try:
        with open(
            OUTPUT_FILE,
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                data,
                file,
                indent=2,
                ensure_ascii=False,
            )
    except OSError as error:
        print(f"WARNING: Could not write result file: {error}")


def fail(message):
    result = {
        "status": "FAILED",
        "message": message,
        "technology": None,
        "project_category": None,
        "languages": [],
        "test_framework": None,
        "test_command": None,
        "test_file": None,
    }

    save_json(result)

    print(f"ERROR: {message}")

    return 1


# For HTTP apps tested with node:test the workflow starts the app and stops
# it after the run. A test that starts the app itself (and may never stop
# it) would hang the run, so such tests are rejected and rewritten.
APP_BASE_URL_VARIABLE = "REPRODUCTION_BASE_URL"
APP_START_PATTERNS = (
    (re.compile(r"child_process"), "do not use child_process"),
    (re.compile(r"\b(?:spawn|spawnSync|exec|execSync|execFile|fork)\s*\("), "do not start processes"),
    (re.compile(r"\.listen\s*\("), "do not call listen(); the app is already running"),
    (
        re.compile(r"""(?:require\(|from\s+|import\()\s*['"]\.{1,2}/(?:[\w./-]*/)?(?:index|app|server|main)(?:\.[cm]?js)?['"]"""),
        "do not require or import the application; send HTTP requests to it",
    ),
)
APP_RUN_RULES = f"""

    APPLICATION UNDER TEST (MANDATORY):

    The workflow starts the application before every test run
    and stops it afterwards. Its base URL is in the environment
    variable {APP_BASE_URL_VARIABLE}.

    - Send HTTP requests with the built-in fetch to
      new URL(path, process.env.{APP_BASE_URL_VARIABLE}).
    - Do NOT start, spawn, fork, require or import the
      application, do NOT use child_process, and do NOT call
      listen(). create_test_file rejects such tests.
    - Use node:test and node:assert only; no new packages.
    - For HTML pages, assert on the response body text.
    - For form posts, send application/x-www-form-urlencoded
      bodies with URLSearchParams, then GET the page again.
    """


ESM_TEST_RULE = """
    The test file is an ES module (the package uses "type": "module"):
    use import statements, not require().
    """


MAX_TOOL_CALL_ATTEMPTS = 3
SHORTER_TEST_REMINDER = """

    Your previous create_test_file call was malformed and rejected.
    Write a shorter test file this time (under 40 lines, no comments)
    and make sure the content is one valid JSON string.
    """


EDGE_CASE_RULES = """

    EDGE CASES (in the same test file):

    - Besides the reported case, check the closest variants of
      the same input that the bug implies. For empty or missing
      input, also check whitespace-only input ("   ") and that a
      normal valid value still works.
    - Do not test unrelated features or invent requirements.
    - Keep the whole file short (about 40 lines): no comments,
      at most one small helper. Long tool calls often fail.
    - If an edge-case check fails because the application still
      mishandles it, keep the check: it shows the fix is
      incomplete. Only correct checks that are wrong themselves.
    """


def app_start_problems(content):
    """Reasons a generated test would start or import the app itself."""
    problems = [message for pattern, message in APP_START_PATTERNS if pattern.search(content)]
    if APP_BASE_URL_VARIABLE not in content:
        problems.append(f"read the app URL from process.env.{APP_BASE_URL_VARIABLE}")
    return problems


def app_base_url_from_entry(source):
    """The local URL an app's entry file listens on, e.g. app.listen(3000)."""
    match = re.search(r"process\.env\.PORT\s*\|\|\s*(\d{2,5})", source) or re.search(
        r"\.listen\(\s*(\d{2,5})", source
    )
    return f"http://localhost:{match.group(1)}" if match else None


def build_dynamodb_message_regression_test(
        diagnosis,
        has_node_backend,
        handler_import,
        config_relative_path,
):
        if not has_node_backend or not isinstance(diagnosis, dict):
                return None

        diagnosis_text = json.dumps(diagnosis, ensure_ascii=False).lower()
        describes_message_overwrite = (
                "dynamodb" in diagnosis_text
                and "message" in diagnosis_text
                and any(
                        phrase in diagnosis_text
                        for phrase in (
                                "overwrite",
                                "sort key",
                                "messageid",
                                "separate record",
                        )
                )
        )
        if not describes_message_overwrite:
                return None

        template = r'''const mockSend = jest.fn().mockResolvedValue({});

jest.mock("@aws-sdk/client-dynamodb", () => ({
    DynamoDBClient: jest.fn().mockImplementation(() => ({ send: mockSend })),
    PutItemCommand: jest.fn().mockImplementation((input) => ({ input })),
}));

const fs = require("node:fs");
const path = require("node:path");
const { message } = require("__HANDLER_IMPORT__");

test("stores repeated WebSocket messages as separate records", async () => {
    const eventFor = (messageText, requestId) => ({
        body: JSON.stringify({ message: messageText, requestId }),
        requestContext: { connectionId: "connection-regression" },
    });

    await message(eventFor("first message", "request-1"));
    await message(eventFor("second message", "request-2"));

    const items = mockSend.mock.calls.map(([command]) => command.input.Item);
    expect(items).toHaveLength(2);
    expect(items.map((item) => item.connectionId.S)).toEqual([
        "connection-regression",
        "connection-regression",
    ]);
    expect(items.map((item) => item.message.S)).toEqual([
        "first message",
        "second message",
    ]);

    // Read the table key from the configuration instead of assuming an
    // attribute name, so any correct composite key passes.
    const tableDefinition = fs.readFileSync(
        path.resolve(__dirname, "__CONFIG_RELATIVE_PATH__"),
        "utf8",
    );
    const lines = tableDefinition.split(/\r?\n/);
    const start = lines.findIndex((line) => line.trim() === "KeySchema:");
    expect(start).toBeGreaterThanOrEqual(0);
    const indent = lines[start].search(/\S/);
    const keyAttributes = [];
    for (const line of lines.slice(start + 1)) {
        if (line.trim() === "") continue;
        if (line.search(/\S/) <= indent) break;
        const match = line.match(/AttributeName:\s*["']?([\w-]+)["']?/);
        if (match) keyAttributes.push(match[1]);
    }
    expect(keyAttributes.length).toBeGreaterThan(1);

    const keys = items.map((item) =>
        keyAttributes.map((name) => item[name] && item[name].S).join("|")
    );
    for (const item of items) {
        for (const name of keyAttributes) {
            expect(item[name] && item[name].S).toBeTruthy();
        }
    }
    expect(new Set(keys).size).toBe(2);
});
'''
        return template.replace(
                "__HANDLER_IMPORT__", handler_import
        ).replace(
                "__CONFIG_RELATIVE_PATH__", config_relative_path
        )


def main():
    load_dotenv()

    if len(sys.argv) > 1:
        DIAGNOSIS_FILE = os.path.abspath(sys.argv[1])
    else:
        DIAGNOSIS_FILE = resolve_artifact_path("diagnosis.json", BASE_DIR)
    # ============================================================
    # LOAD REPOSITORY CONTEXT
    # ============================================================

    repository_context = load_json(
        REPOSITORY_CONTEXT_FILE
    )

    if not isinstance(repository_context, dict):
        sys.exit(
            fail(
                "repository_context.json is missing or invalid."
            )
        )


    TARGET_REPOSITORY = repository_context.get(
        "repository_path"
    )

    if (
        not isinstance(TARGET_REPOSITORY, str)
        or not TARGET_REPOSITORY.strip()
    ):
        sys.exit(
            fail(
                "repository_context.json does not contain "
                "a valid repository_path."
            )
        )


    TARGET_REPOSITORY = os.path.abspath(
        TARGET_REPOSITORY
    )


    if not os.path.isdir(TARGET_REPOSITORY):
        sys.exit(
            fail(
                f"Repository path does not exist: "
                f"{TARGET_REPOSITORY}"
            )
        )


    BUG_REPORT_PATH = os.getenv(
        "BUG_REPORT_FILE",
        os.path.join(BASE_DIR, "bug_report.json"),
    )
    bug_report = load_json(BUG_REPORT_PATH) or {}
    http_reproduction = bug_report.get("reproduction", {})
    if not isinstance(http_reproduction, dict):
        http_reproduction = {}


    # ============================================================
    # EXECUTION STRATEGY
    # ============================================================

    execution_strategy = repository_context.get(
        "execution_strategy"
    )

    if not isinstance(execution_strategy, dict):
        execution_strategy = {}


    technology = execution_strategy.get(
        "technology"
    )

    package_manager = execution_strategy.get(
        "package_manager"
    )

    test_command = execution_strategy.get(
        "test_command"
    )

    test_framework = execution_strategy.get(
        "test_framework"
    )

    strategy_language = execution_strategy.get(
        "language"
    )


    # ============================================================
    # PROJECT TYPES / LANGUAGES
    # ============================================================

    project_types = repository_context.get(
        "project_types",
        []
    )

    if not isinstance(project_types, list):
        project_types = []


    languages = []

    for project in project_types:

        if not isinstance(project, dict):
            continue

        language = project.get("language")

        if (
            language
            and language not in languages
        ):
            languages.append(language)


    if (
        strategy_language
        and strategy_language not in languages
    ):
        languages.append(strategy_language)


    if not test_command:
        sys.exit(
            fail(
                "No test_command is configured in "
                "execution_strategy."
            )
        )


    # ============================================================
    # NORMALIZED DETECTION
    # ============================================================

    technology_text = str(
        technology or ""
    ).strip().lower()

    framework_text = str(
        test_framework or ""
    ).strip().lower()

    language_text = " ".join(
        str(language).lower()
        for language in languages
    )


    def is_python_project():
        return (
            "python" in technology_text
            or "python" in language_text
            or "pytest" in framework_text
            or "unittest" in framework_text
        )


    def is_java_project():
        return (
            bool(re.search(r"\bjava\b", technology_text))
            or bool(re.search(r"\bjava\b", language_text))
            or "junit" in framework_text
            or "gradle" in str(
                package_manager or ""
            ).lower()
        )


    def is_dotnet_project():
        return (
            ".net" in technology_text
            or "dotnet" in technology_text
            or "c#" in technology_text
            or "csharp" in technology_text
            or ".net" in language_text
            or "c#" in language_text
            or "xunit" in framework_text
            or "nunit" in framework_text
            or "mstest" in framework_text
        )


    def is_react_project():
        return (
            "react" in technology_text
            or "react" in language_text
        )


    def is_angular_project():
        return (
            "angular" in technology_text
            or "angular" in language_text
        )


    def is_node_project():
        return (
            "node" in technology_text
            or "node.js" in technology_text
            or "javascript" in language_text
            or "typescript" in language_text
            or "jest" in framework_text
            or "vitest" in framework_text
            or "mocha" in framework_text
            or "node:test" in framework_text
        )


    # ============================================================
    # PROJECT CATEGORY
    # ============================================================

    if (
        "node.js" in technology_text
        or "nodejs" in technology_text
        or technology_text == "node"
    ):
        PROJECT_CATEGORY = "node"

    elif is_dotnet_project():
        PROJECT_CATEGORY = "dotnet"

    elif is_java_project():
        PROJECT_CATEGORY = "java"

    elif is_python_project():
        PROJECT_CATEGORY = "python"

    elif is_angular_project():
        PROJECT_CATEGORY = "angular"

    elif is_react_project():
        PROJECT_CATEGORY = "react"

    elif is_node_project():
        PROJECT_CATEGORY = "node"

    else:
        PROJECT_CATEGORY = "unknown"


    # ============================================================
    # SAFE PATH
    # ============================================================

    def safe_repository_path(relative_path):

        candidate = os.path.abspath(
            os.path.join(
                TARGET_REPOSITORY,
                relative_path,
            )
        )

        repository_prefix = (
            TARGET_REPOSITORY + os.sep
        )

        if (
            candidate != TARGET_REPOSITORY
            and not candidate.startswith(
                repository_prefix
            )
        ):
            raise ValueError(
                "Requested path is outside "
                "the target repository."
            )

        return candidate


    # ============================================================
    # TEST DISCOVERY
    # ============================================================

    READ_FILE_LIMIT = 3000
    TEST_OUTPUT_LIMIT = 2500

    TEST_EXTENSIONS = {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".java",
        ".cs",
    }


    IGNORED_DIRECTORIES = {
        "node_modules",
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        "coverage",
        ".next",
    }


    def discover_existing_tests():

        discovered = []

        for root, directories, files in os.walk(
            TARGET_REPOSITORY
        ):

            directories[:] = [
                directory
                for directory in directories
                if directory not in IGNORED_DIRECTORIES
            ]

            for filename in files:

                extension = os.path.splitext(
                    filename
                )[1].lower()

                if extension not in TEST_EXTENSIONS:
                    continue

                lower_name = filename.lower()

                looks_like_test = (
                    "test" in lower_name
                    or "spec" in lower_name
                )

                normalized_root = root.lower()

                looks_like_test_directory = (
                    os.path.sep + "test" + os.path.sep
                    in normalized_root
                    or os.path.sep + "tests" + os.path.sep
                    in normalized_root
                    or os.path.sep + "__tests__" + os.path.sep
                    in normalized_root
                )

                if (
                    looks_like_test
                    or looks_like_test_directory
                ):

                    full_path = os.path.join(
                        root,
                        filename,
                    )

                    relative_path = os.path.relpath(
                        full_path,
                        TARGET_REPOSITORY,
                    )

                    discovered.append(
                        relative_path
                    )

        return sorted(discovered)


    existing_tests = discover_existing_tests()

    architecture = repository_context.get("architecture", {})
    architecture_components = (
        architecture.get("components", [])
        if isinstance(architecture, dict)
        else []
    )
    has_node_backend = (
        "node.js" in str(execution_strategy.get("backend", "")).lower()
        or any(
            isinstance(component, dict)
            and component.get("type") == "backend"
            and "node" in str(component.get("technology", "")).lower()
            for component in architecture_components
        )
        or any(
            isinstance(project, dict)
            and project.get("technology") == "Node.js"
            for project in project_types
        )
    )

    configured_http_test = (
        has_node_backend
        and isinstance(http_reproduction.get("action"), dict)
        and isinstance(http_reproduction.get("state_check"), dict)
    )

    backend_relative = execution_strategy.get("backend_path") or "."

    def backend_package():
        try:
            with open(
                os.path.join(TARGET_REPOSITORY, backend_relative, "package.json"),
                "r",
                encoding="utf-8",
            ) as handle:
                package = json.load(handle)
        except (OSError, ValueError):
            return {}
        return package if isinstance(package, dict) else {}

    def backend_declares_jest():
        package = backend_package()
        return any(
            isinstance(package.get(section), dict) and "jest" in package[section]
            for section in ("dependencies", "devDependencies")
        )

    # The detected framework is a guess ("Cypress / Jest" for MERN). An HTTP
    # backend whose package does not declare Jest is tested with Node's
    # built-in runner instead, which needs no install.
    node_test_for_backend = (
        not configured_http_test
        and has_node_backend
        and "jest" in framework_text
        and "http" in str(execution_strategy.get("validation_method") or "").lower()
        and not backend_declares_jest()
    )


    # ============================================================
    # GENERATED TEST PATH
    # ============================================================

    def choose_generated_test_path():

        if node_test_for_backend:
            return os.path.join(
                TARGET_REPOSITORY,
                backend_relative,
                "test",
                "generated-regression.test.js",
            )

        if configured_http_test:
            return os.path.join(
                TARGET_REPOSITORY,
                "test",
                "generated-http-regression.test.cjs",
            )

        if has_node_backend and "jest" in framework_text:
            backend_path = execution_strategy.get("backend_path") or "backend"
            return os.path.join(
                TARGET_REPOSITORY,
                backend_path,
                "test",
                "generated-regression.test.js",
            )

        if PROJECT_CATEGORY == "python":

            return os.path.join(
                TARGET_REPOSITORY,
                "tests",
                "test_generated_regression.py",
            )


        if PROJECT_CATEGORY == "java":

            return os.path.join(
                TARGET_REPOSITORY,
                "src",
                "test",
                "java",
                "GeneratedRegressionTest.java",
            )


        if PROJECT_CATEGORY == "dotnet":

            return os.path.join(
                TARGET_REPOSITORY,
                "Tests",
                "GeneratedRegressionTests.cs",
            )


        if PROJECT_CATEGORY in {
            "node",
            "react",
            "angular",
        }:

            framework = framework_text

            # Existing Jest / Vitest / Mocha structure.
            if (
                "vitest" in framework
                or "jest" in framework
                or "mocha" in framework
            ):

                for existing in existing_tests:

                    lower_existing = existing.lower()

                    if (
                        "test/" in lower_existing
                        or "tests/" in lower_existing
                        or "__tests__/" in lower_existing
                    ):

                        extension = os.path.splitext(
                            existing
                        )[1].lower()

                        if extension in {
                            ".js",
                            ".jsx",
                            ".ts",
                            ".tsx",
                        }:

                            directory = os.path.dirname(
                                existing
                            )

                            filename = (
                                "generated-regression"
                                + extension
                            )

                            return os.path.join(
                                TARGET_REPOSITORY,
                                directory,
                                filename,
                            )

            # Node built-in test runner.
            if (
                "node:test" in framework
                or "node --test" in str(
                    test_command
                ).lower()
            ):

                return os.path.join(
                    TARGET_REPOSITORY,
                    "test",
                    "generated-regression.test.js",
                )

            extension = (
                ".ts"
                if "typescript" in language_text
                else ".js"
            )

            return os.path.join(
                TARGET_REPOSITORY,
                "test",
                "generated-regression"
                + extension,
            )


        return None


    TEST_FILE = choose_generated_test_path()


    if TEST_FILE is None:
        sys.exit(
            fail(
                "Unable to determine generated test path "
                f"for technology={technology}, "
                f"framework={test_framework}, "
                f"languages={languages}"
            )
        )


    TEST_FILE_RELATIVE = os.path.relpath(
        TEST_FILE,
        TARGET_REPOSITORY,
    )

    if configured_http_test:
        test_framework = "node:test (HTTP integration)"
        test_command = ["node", "--test", TEST_FILE_RELATIVE]

    if node_test_for_backend:
        test_framework = "node:test"
        test_command = ["node", "--test", TEST_FILE_RELATIVE]

    # An HTTP app tested with node:test: the workflow runs the app for each
    # test run, and the AI's test only sends requests to it.
    workflow_runs_app = (
        not configured_http_test
        and is_node_project()
        and "http" in str(execution_strategy.get("validation_method") or "").lower()
        and str(test_framework or "").strip().lower() == "node:test"
        and bool(execution_strategy.get("start_command"))
    )

    def load_reproduction_helpers():
        spec = importlib.util.spec_from_file_location(
            "workflow_reproduction_helpers",
            os.path.join(BASE_DIR, "bug_reproduction_agent.py"),
        )
        helpers = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helpers)
        return helpers

    def application_base_url():
        configured = execution_strategy.get("backend_base_url") or execution_strategy.get("base_url")
        if isinstance(configured, str) and configured.strip():
            return configured.strip().rstrip("/")
        for token in str(execution_strategy.get("start_command")).split():
            if re.fullmatch(r"[\w./\\-]+\.[cm]?js", token):
                entry = safe_repository_path(token)
                if entry and os.path.isfile(entry):
                    with open(entry, "r", encoding="utf-8", errors="replace") as handle:
                        return app_base_url_from_entry(handle.read())
        return None

    @contextlib.contextmanager
    def application_for_tests():
        """Start the app for one test run and always stop it afterwards.

        Yields the extra environment for the test command."""
        if not workflow_runs_app:
            yield {}
            return
        helpers = load_reproduction_helpers()
        import browser_replay_agent as full_stack

        frontend, backend = full_stack.web_components(repository_context)
        if frontend and backend:
            # Full-stack app: the test talks to the backend API. The backend
            # gets a temporary database, never the user's real one.
            backend_directory = os.path.join(TARGET_REPOSITORY, backend["path"])
            url = helpers.normalize_base_url(
                backend.get("base_url") or execution_strategy.get("backend_base_url")
            )
            if not url:
                raise RuntimeError("The backend URL is not known.")
            if full_stack.port_in_use(url):
                raise RuntimeError(f"{url} is already in use; stop your own copy of the backend.")
            with contextlib.ExitStack() as services:
                explicit = {}
                if full_stack.uses_mongodb(repository_context):
                    names = full_stack.mongodb_uri_variables(backend_directory)
                    if not names:
                        raise RuntimeError(
                            "The MongoDB setting was not found in an example env file; "
                            "refusing to run tests against the real database."
                        )
                    uri = services.enter_context(full_stack.isolated_mongodb())
                    explicit = {name: uri for name in names}
                process, start_error = helpers.start_application(
                    TARGET_REPOSITORY,
                    backend.get("start_command") or execution_strategy.get("backend_start_command"),
                    base_url=url,
                    readiness_path="/",
                    working_directory=backend_directory,
                    environment={},
                    explicit_environment=explicit,
                )
                if start_error:
                    raise RuntimeError(f"The backend could not be started: {start_error}")
                services.callback(helpers.stop_process, process)
                yield {APP_BASE_URL_VARIABLE: url}
            return

        base_url = application_base_url()
        if not base_url:
            raise RuntimeError("The application's local URL could not be determined.")
        runtime = helpers.runtime_configuration(
            TARGET_REPOSITORY, repository_context, execution_strategy, {"base_url": base_url}
        )
        url, overrides, endpoint_error = helpers.isolate_local_endpoint(
            TARGET_REPOSITORY, repository_context, runtime["base_url"]
        )
        if endpoint_error:
            raise RuntimeError(endpoint_error)
        app_environment = project_process_environment()
        app_environment.update(overrides)
        process, start_error = helpers.start_application(
            TARGET_REPOSITORY,
            runtime["start_command"],
            base_url=url,
            readiness_path="/",
            working_directory=runtime["working_directory"],
            environment=app_environment,
        )
        if start_error:
            raise RuntimeError(f"The application could not be started: {start_error}")
        try:
            yield {APP_BASE_URL_VARIABLE: url}
        finally:
            helpers.stop_process(process)

    def run_test_command(command):
        with application_for_tests() as app_environment:
            environment = project_process_environment()
            environment.update(app_environment)
            return run_bounded(
                command,
                cwd=TARGET_REPOSITORY,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )


    def configured_http_test_source():
        action = http_reproduction["action"]
        state_check = http_reproduction["state_check"]
        test_configuration = {
            "action": action,
            "state_check": state_check,
            "base_url": (
                execution_strategy.get("backend_base_url")
                or "http://localhost:5050"
            ),
            "bug_title": bug_report.get("bug_title", "HTTP regression"),
        }

        return f'''const test = require("node:test");
    const assert = require("node:assert/strict");

    const configuration = {json.dumps(test_configuration, ensure_ascii=False)};
    const baseUrl = process.env.REPRODUCTION_BASE_URL || configuration.base_url;

    async function sendRequest(method, path, data, bodyType) {{
        const options = {{ method, headers: {{}} }};
        if (data !== undefined && data !== null) {{
            if (bodyType === "json") {{
                options.headers["Content-Type"] = "application/json";
                options.body = JSON.stringify(data);
            }} else if (bodyType === "form") {{
                options.headers["Content-Type"] = "application/x-www-form-urlencoded";
                options.body = new URLSearchParams(data).toString();
            }}
        }}
        return fetch(new URL(path, baseUrl), options);
    }}

    async function readState() {{
        const response = await sendRequest(
            configuration.state_check.method,
            configuration.state_check.path,
        );
        assert.equal(response.ok, true, `State request failed: HTTP ${{response.status}}`);
        return response.json();
    }}

    function invalidFields(data) {{
        return Object.entries(data || {{}}).filter(([, value]) =>
            value == null || (typeof value === "string" && value.trim() === "")
        ).map(([field]) => field);
    }}

    function isInvalidRecord(record, fields) {{
        return fields.length > 0 && fields.every((field) =>
            record[field] == null || String(record[field]).trim() === ""
        );
    }}

    async function removeCreatedRecord(response, records, fields) {{
        let payload = {{}};
        try {{ payload = await response.clone().json(); }} catch {{}}
        let id = payload.insertedId || payload._id;
        if (!id && Array.isArray(records)) {{
            const created = records.find((record) => isInvalidRecord(record, fields));
            id = created && created._id;
        }}
        if (id != null) {{
            const collectionPath = configuration.action.path.replace(/\\/$/, "");
            await sendRequest("DELETE", `${{collectionPath}}/${{encodeURIComponent(id)}}`);
        }}
    }}

    test(`${{configuration.bug_title}}: invalid request is rejected without state change`, async () => {{
        const before = await readState();
        assert.ok(Array.isArray(before), "Configured state endpoint must return an array");
        const fields = invalidFields(configuration.action.data);
        assert.ok(fields.length > 0, "Configured action must contain invalid field values");
        const beforeInvalidCount = before.filter((record) => isInvalidRecord(record, fields)).length;

        const response = await sendRequest(
            configuration.action.method,
            configuration.action.path,
            configuration.action.data,
            configuration.action.body_type,
        );
        const after = await readState();

        try {{
            assert.ok(response.status >= 400 && response.status < 500,
                `Expected a 4xx rejection, received HTTP ${{response.status}}`);
            const afterInvalidCount = after.filter((record) => isInvalidRecord(record, fields)).length;
            assert.equal(afterInvalidCount, beforeInvalidCount,
                "Invalid submission created a new invalid record");
        }} finally {{
            if (response.ok) await removeCreatedRecord(response, after, fields);
        }}
    }});

    test(`${{configuration.bug_title}}: valid request remains accepted`, async () => {{
        const validData = Object.fromEntries(
            Object.entries(configuration.action.data || {{}}).map(([field, value]) => [
                field,
                typeof value === "string" && value.trim()
                    ? value
                    : `AI regression ${{field}} ${{Date.now()}}`,
            ]),
        );
        const response = await sendRequest(
            configuration.action.method,
            configuration.action.path,
            validData,
            configuration.action.body_type,
        );

        try {{
            assert.ok(response.ok, `Valid submission failed: HTTP ${{response.status}}`);
        }} finally {{
            if (response.ok) await removeCreatedRecord(response, [], []);
        }}
    }});
    '''


    # ============================================================
    # TOOL: READ FILE
    # ============================================================

    @tool
    def read_file(file_path: str) -> str:
        """
        Read a source file from the target repository.
        """

        try:

            full_path = safe_repository_path(
                file_path
            )

            if not os.path.isfile(full_path):
                return (
                    f"ERROR: File not found: "
                    f"{file_path}"
                )

            with open(
                full_path,
                "r",
                encoding="utf-8",
            ) as file:

                text = file.read()
            # Keep each tool result small: the provider's per-minute token
            # limit (8,000 on the free tier) covers the whole conversation.
            if len(text) > READ_FILE_LIMIT:
                return (
                    text[:READ_FILE_LIMIT]
                    + f"\n... [truncated: showing {READ_FILE_LIMIT} of {len(text)} characters]"
                )
            return text

        except Exception as error:

            return (
                f"ERROR reading {file_path}: "
                f"{error}"
            )


    # ============================================================
    # TOOL: CREATE TEST FILE
    # ============================================================

    @tool
    def create_test_file(content: str) -> str:
        """
        Create the selected generated regression test.
        """

        try:

            if not isinstance(content, str):
                return (
                    "ERROR: Test content must be a string."
                )

            content = content.strip()

            if not content:
                return (
                    "ERROR: Generated test content is empty."
                )

            if workflow_runs_app:
                problems = app_start_problems(content)
                if problems:
                    return (
                        "ERROR: The test was not saved. The workflow starts "
                        "the application itself; rewrite the test so that it "
                        "only sends HTTP requests: " + "; ".join(problems) + "."
                    )

            test_directory = os.path.dirname(
                TEST_FILE
            )

            os.makedirs(
                test_directory,
                exist_ok=True,
            )

            # Safety: only the selected generated file
            # may be written.
            with open(
                TEST_FILE,
                "w",
                encoding="utf-8",
            ) as file:

                file.write(content)

            if not os.path.isfile(TEST_FILE):
                return (
                    "ERROR: File write completed but "
                    "generated test file cannot be found."
                )

            return (
                "SUCCESS: Generated test file created: "
                f"{TEST_FILE_RELATIVE}"
            )

        except Exception as error:

            return (
                f"ERROR creating generated test file: "
                f"{error}"
            )


    # ============================================================
    # TEST COMMAND
    # ============================================================

    def build_test_command():

        if isinstance(test_command, list):

            return [
                str(part)
                for part in test_command
            ]

        try:

            return shlex.split(
                str(test_command),
                posix=False,
            )

        except ValueError as error:

            raise ValueError(
                f"Invalid test command: {error}"
            )


    # ============================================================
    # TOOL: RUN TESTS
    # ============================================================

    @tool
    def run_tests() -> str:
        """
        Run the exact test command detected from
        repository_context.json.
        """

        try:

            command = build_test_command()

            if not command:
                return (
                    "ERROR: Test command is empty."
                )

            result = run_test_command(command)

            stdout = result.stdout or ""
            stderr = result.stderr or ""

            output = (
                stdout
                + "\n"
                + stderr
            )

            # The whole conversation is resent each turn; keep the part of
            # the output that matters (summary and failures are at the end)
            # so the request stays under the model's per-minute token limit.
            if len(output) > TEST_OUTPUT_LIMIT:
                output = (
                    f"[... first {len(output) - TEST_OUTPUT_LIMIT} characters omitted ...]\n"
                    + output[-TEST_OUTPUT_LIMIT:]
                )

            return (
                f"RETURN CODE: {result.returncode}\n"
                f"COMMAND: {' '.join(command)}\n"
                f"TEST OUTPUT:\n{output}"
            )

        except Exception as error:

            return (
                f"ERROR running tests: {error}"
            )


    # ============================================================
    # LOAD DIAGNOSIS
    # ============================================================

    diagnosis = load_json(
        DIAGNOSIS_FILE
    )

    if not isinstance(diagnosis, dict):
        sys.exit(
            fail(
                f"Could not load diagnosis file: "
                f"{DIAGNOSIS_FILE}"
            )
        )

    def create_controlled_fallback_test():
        if not has_node_backend or "jest" not in framework_text:
            return None

        backend_path = execution_strategy.get("backend_path") or "backend"
        candidate_paths = [
            affected_file
            for affected_file in diagnosis.get("affected_files", [])
            if isinstance(affected_file, str)
        ]
        candidate_paths.extend([
            os.path.join(backend_path, "src", "websocket.js"),
            os.path.join(backend_path, "index.js"),
        ])

        handler_path = None
        for relative_path in candidate_paths:
            try:
                candidate = safe_repository_path(relative_path)
            except ValueError:
                continue
            if not os.path.isfile(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as source_file:
                    source = source_file.read()
            except (OSError, UnicodeError):
                continue
            if (
                "PutItemCommand" in source
                and "connectionId" in source
                and re.search(r"exports\.message\s*=", source)
            ):
                handler_path = candidate
                break

        config_candidates = [
            os.path.join(backend_path, "serverless.yml"),
            "serverless.yml",
        ]
        config_path = None
        for relative_path in config_candidates:
            try:
                candidate = safe_repository_path(relative_path)
            except ValueError:
                continue
            if os.path.isfile(candidate):
                config_path = candidate
                break
        if handler_path is None or config_path is None:
            return None

        handler_import = os.path.relpath(
            handler_path,
            os.path.dirname(TEST_FILE),
        ).replace(os.sep, "/")
        handler_import = os.path.splitext(handler_import)[0]
        if not handler_import.startswith("."):
            handler_import = "./" + handler_import

        config_relative_path = os.path.relpath(
            config_path,
            os.path.dirname(TEST_FILE),
        ).replace(os.sep, "/")

        return build_dynamodb_message_regression_test(
            diagnosis,
            has_node_backend,
            handler_import,
            config_relative_path,
        )


    # ============================================================
    # LLM
    # ============================================================

    llm = ChatGroq(
        model="openai/gpt-oss-20b",
        temperature=0,
        max_retries=GROQ_MAX_RETRIES,
    )


    # ============================================================
    # SYSTEM PROMPT
    # ============================================================

    system_prompt = f"""
    You are an expert AI Test Generation Agent inside a
    technology-agnostic autonomous software debugging system.

    Your job is to generate ONE regression test that proves
    the diagnosed bug has been fixed.

    Repository:
    {TARGET_REPOSITORY}

    Technology:
    {technology}

    Project category:
    {PROJECT_CATEGORY}

    Languages:
    {languages}

    Test framework:
    {test_framework}

    Configured test command:
    {test_command}

    Generated test path:
    {TEST_FILE_RELATIVE}

    Existing tests:
    {existing_tests}

    Diagnosis:
    {json.dumps(diagnosis, ensure_ascii=False, separators=(",", ":"))}


    MANDATORY WORKFLOW:

    1. Inspect every relevant source file identified by the
       diagnosis using read_file.

    2. Understand the actual root cause.

    3. Design ONE focused regression test.

    4. The test must validate observable behavior related
       directly to the diagnosed bug.

    5. Use the detected technology and testing framework.

    6. You MUST call create_test_file.

    7. Pass the COMPLETE test source code to create_test_file.

    8. The generated test MUST be written to:

       {TEST_FILE_RELATIVE}

    9. Do not merely describe the test.

    10. Do not return only a code block.

    11. Actually create the file using create_test_file.

    12. After creating the file, verify that it exists.

    13. Then call run_tests.

    14. Use exactly the configured test command.

    15. If the test fails because of the generated test,
        correct the generated test and call create_test_file
        again.

    16. Never modify application source files.

    17. Never modify existing tests.

    18. Never modify package manifests.

    19. Never modify lock files.

    20. Never modify node_modules.

    21. Never modify .env files.

    22. Never create unrelated files.

    23. Never assume a specific application type unless
        confirmed by repository inspection.

    24. Do not hardcode Todo, Express, Node.js, React,
        Angular, Python, Java or .NET unless confirmed.

    25. Finish only after the generated test file exists
        and the configured test command has been executed.
    """ + EDGE_CASE_RULES + (APP_RUN_RULES if workflow_runs_app else "") + (
        ESM_TEST_RULE
        if workflow_runs_app and node_test_for_backend and backend_package().get("type") == "module"
        else ""
    )


    # ============================================================
    # USER MESSAGE
    # ============================================================

    message = f"""
    Generate a regression test for the diagnosed bug.

    First inspect the affected source files.

    Then create exactly ONE generated regression test at:

    {TEST_FILE_RELATIVE}

    You MUST use the create_test_file tool.

    After creating it, use run_tests with this exact command:

    {test_command}

    If the generated test needs correction, update ONLY the
    generated test and execute the same test command again.

    Do not modify application code or existing tests.

    The diagnosis is in the system instructions.
    """


    # ============================================================
    # START
    # ============================================================

    print("=============================")
    print("AI TEST GENERATION AGENT")
    print("=============================")

    print(f"Technology: {technology}")
    print(f"Project category: {PROJECT_CATEGORY}")
    print(f"Repository: {TARGET_REPOSITORY}")
    print(f"Test framework: {test_framework}")
    print(f"Test command: {test_command}")
    print(f"Generated test: {TEST_FILE_RELATIVE}")


    if configured_http_test:
        try:
            os.makedirs(os.path.dirname(TEST_FILE), exist_ok=True)
            with open(TEST_FILE, "w", encoding="utf-8") as file:
                file.write(configured_http_test_source())

            backend_process = None
            backend_url = None
            backend_environment = {}
            environment_for_test = project_process_environment()

            if http_reproduction.get("component_type") == "backend":
                reproduction_agent_path = os.path.join(
                    BASE_DIR,
                    "bug_reproduction_agent.py",
                )
                spec = importlib.util.spec_from_file_location(
                    "workflow_reproduction_helpers",
                    reproduction_agent_path,
                )
                reproduction_helpers = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(reproduction_helpers)

                backend_runtime = reproduction_helpers.runtime_configuration(
                    TARGET_REPOSITORY,
                    repository_context,
                    execution_strategy,
                    http_reproduction,
                )
                backend_url, backend_environment, endpoint_error = (
                    reproduction_helpers.isolate_local_endpoint(
                        TARGET_REPOSITORY,
                        repository_context,
                        backend_runtime["base_url"],
                    )
                )
                if endpoint_error:
                    raise RuntimeError(endpoint_error)

                environment_for_test["REPRODUCTION_BASE_URL"] = backend_url
                backend_process_environment = project_process_environment()
                backend_process_environment.update(backend_environment)
                backend_process, start_error = reproduction_helpers.start_application(
                    TARGET_REPOSITORY,
                    backend_runtime["start_command"],
                    base_url=backend_url,
                    readiness_path=backend_runtime["readiness_path"],
                    working_directory=backend_runtime["working_directory"],
                    environment=backend_process_environment,
                )
                if start_error:
                    raise RuntimeError(start_error)

            command = build_test_command()
            try:
                process = run_bounded(
                    command,
                    cwd=TARGET_REPOSITORY,
                    env=environment_for_test,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
            finally:
                if backend_process:
                    reproduction_helpers.stop_process(backend_process)

            stdout = process.stdout or ""
            stderr = process.stderr or ""
            combined = f"{stdout}\n{stderr}"

            total_tests, passed_tests, failed_tests, skipped_tests = parse_test_summary(combined)
            status = (
                "SUCCESS"
                if (
                    process.returncode == 0
                    and isinstance(total_tests, int)
                    and total_tests > 0
                    and isinstance(passed_tests, int)
                    and passed_tests > 0
                    and failed_tests == 0
                    and passed_tests + skipped_tests == total_tests
                )
                else "FAILED"
            )

            result_data = {
                "status": status,
                "message": "Configured HTTP regression tests generated and executed.",
                "technology": technology,
                "project_category": PROJECT_CATEGORY,
                "languages": languages,
                "package_manager": package_manager,
                "test_framework": test_framework,
                "test_command": test_command,
                "execution_command": command,
                "working_directory": ".",
                "test_file": TEST_FILE_RELATIVE,
                "tests": total_tests,
                "passed": passed_tests,
                "failed": failed_tests,
                "skipped": skipped_tests,
                "return_code": process.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "configured_http_reproduction": True,
            }
            save_json(result_data)
            print(combined)
            print(json.dumps(result_data, indent=2, ensure_ascii=False))
            sys.exit(0 if status == "SUCCESS" else 1)

        except Exception as error:
            result_data = {
                "status": "FAILED",
                "message": f"Configured HTTP regression test failed: {error}",
                "technology": technology,
                "project_category": PROJECT_CATEGORY,
                "test_framework": test_framework,
                "test_command": test_command,
                "test_file": TEST_FILE_RELATIVE,
                "return_code": 1,
            }
            save_json(result_data)
            print(result_data["message"])
            sys.exit(1)


    # ============================================================
    # CREATE AGENT
    # ============================================================

    try:

        agent = create_agent(
            model=llm,
            tools=[
                read_file,
                create_test_file,
                run_tests,
            ],
            system_prompt=system_prompt,
        )

        # The provider sometimes rejects a malformed tool call
        # ("tool_use_failed"), more often for long test files. Ask again
        # for a shorter file before using the fallback.
        attempt_message = message
        for generation_attempt in range(1, MAX_TOOL_CALL_ATTEMPTS + 1):
            try:
                result = agent.invoke(
                    {
                        "messages": [
                            {
                                "role": "user",
                                "content": attempt_message,
                            }
                        ]
                    }
                )
                break
            except Exception as generation_error:
                if (
                    generation_attempt == MAX_TOOL_CALL_ATTEMPTS
                    or "tool_use_failed" not in str(generation_error)
                ):
                    raise
                print(
                    "\nThe model returned a malformed tool call; retrying test "
                    f"generation ({generation_attempt + 1}/{MAX_TOOL_CALL_ATTEMPTS})."
                )
                attempt_message = message + SHORTER_TEST_REMINDER


    except Exception as error:

        error_message = str(error)

        print("\n=============================")
        print("TEST GENERATION ERROR")
        print("=============================")
        print(error_message)

        print("\nAttempting diagnosis-specific regression fallback...")
        fallback_code = create_controlled_fallback_test()
        if fallback_code:
            try:
                os.makedirs(os.path.dirname(TEST_FILE), exist_ok=True)
                with open(TEST_FILE, "w", encoding="utf-8") as handle:
                    handle.write(fallback_code)

                command = build_test_command()
                process = run_bounded(
                    command,
                    cwd=TARGET_REPOSITORY,
                    env=project_process_environment(),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                stdout = process.stdout or ""
                stderr = process.stderr or ""
                status = "SUCCESS" if process.returncode == 0 else "FAILED"
                result_data = {
                    "status": status,
                    "message": "A targeted regression fallback was generated and executed.",
                    "technology": technology,
                    "project_category": PROJECT_CATEGORY,
                    "languages": languages,
                    "test_framework": test_framework,
                    "test_command": test_command,
                    "execution_command": command,
                    "test_file": TEST_FILE_RELATIVE,
                    "fallback": True,
                    "error": error_message,
                    "return_code": process.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                }
                save_json(result_data)
                print("\n" + stdout + stderr)
                sys.exit(0 if status == "SUCCESS" else 1)

            except Exception as fallback_error:
                error_message = (
                    f"Targeted fallback generation or execution failed: "
                    f"{fallback_error}"
                )

        result_data = {
            "status": "FAILED",
            "message": error_message,
            "technology": technology,
            "project_category": PROJECT_CATEGORY,
            "languages": languages,
            "test_framework": test_framework,
            "test_command": test_command,
            "test_file": TEST_FILE_RELATIVE,
        }

        save_json(result_data)

        sys.exit(1)

    # ============================================================
    # EXTRACT AGENT OUTPUT
    # ============================================================

    agent_text = ""

    if isinstance(result, dict):

        messages = result.get(
            "messages",
            []
        )

        if messages:

            final_message = messages[-1]

            if hasattr(
                final_message,
                "content",
            ):

                agent_text = (
                    final_message.content
                    or ""
                )

            else:

                agent_text = str(
                    final_message
                )

    else:

        agent_text = str(result)


    # ============================================================
    # VERIFY FILE CREATION
    # ============================================================

    file_created = os.path.isfile(
        TEST_FILE
    )


    # ============================================================
    # FALLBACK EXTRACTION
    # ============================================================

    def extract_code_block(text):

        if not text:
            return None

        matches = re.findall(
            r"```(?:javascript|js|typescript|ts|python|java|csharp|cs)?\s*(.*?)```",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )

        if not matches:
            return None

        candidates = [
            item.strip()
            for item in matches
            if item.strip()
        ]

        if not candidates:
            return None

        return candidates[-1]

    # ============================================================
    # CONTROLLED FALLBACK
    # ============================================================

    if not file_created:

        print(
            "\nAgent did not create the generated test file."
        )

        # First try to recover test code from the agent response.
        fallback_code = extract_code_block(
            agent_text
        )

        # Use a fallback only when it directly covers the diagnosis.
        if not fallback_code:
            fallback_code = create_controlled_fallback_test()

            if fallback_code:
                print(
                    "Using controlled technology-aware "
                    "fallback test generation."
                )

        if fallback_code:

            try:

                test_directory = os.path.dirname(
                    TEST_FILE
                )

                os.makedirs(
                    test_directory,
                    exist_ok=True,
                )

                with open(
                    TEST_FILE,
                    "w",
                    encoding="utf-8",
                ) as file:

                    file.write(
                        fallback_code.strip()
                        + "\n"
                    )

                file_created = os.path.isfile(
                    TEST_FILE
                )

            except OSError as error:

                print(
                    f"Fallback file creation failed: {error}"
                )


    # ============================================================
    # FINAL FILE CHECK
    # ============================================================

    if not file_created:

        result_data = {
            "status": "FAILED",
            "message": (
                "The test generation agent completed, "
                "but no generated test file was created."
            ),
            "technology": technology,
            "project_category": PROJECT_CATEGORY,
            "languages": languages,
            "test_framework": test_framework,
            "test_command": test_command,
            "test_file": TEST_FILE_RELATIVE,
            "agent_output": agent_text,
        }

        save_json(result_data)

        print(
            "\nERROR: Generated test file was not created."
        )

        sys.exit(1)


    # ============================================================
    # VERIFY GENERATED TEST CONTENT
    # ============================================================

    try:

        with open(
            TEST_FILE,
            "r",
            encoding="utf-8",
        ) as file:

            generated_content = file.read()

    except OSError as error:

        sys.exit(
            fail(
                f"Generated test file could not be read: "
                f"{error}"
            )
        )


    if not generated_content.strip():

        sys.exit(
            fail(
                "Generated test file exists but is empty."
            )
        )


    # ============================================================
    # RUN FINAL TEST COMMAND
    # ============================================================

    print("\n=============================")
    print("RUNNING GENERATED TEST")
    print("=============================")

    try:

        command = build_test_command()

        process = run_test_command(command)

        stdout = process.stdout or ""
        stderr = process.stderr or ""

        combined = (
            stdout
            + "\n"
            + stderr
        )

    except Exception as error:

        result_data = {
            "status": "FAILED",
            "message": (
                f"Could not execute test command: {error}"
            ),
            "technology": technology,
            "project_category": PROJECT_CATEGORY,
            "languages": languages,
            "test_framework": test_framework,
            "test_command": test_command,
            "test_file": TEST_FILE_RELATIVE,
        }

        save_json(result_data)

        print(
            f"ERROR: {error}"
        )

        sys.exit(1)


    print(
        f"Command: {' '.join(command)}"
    )

    print(
        combined
    )


    # ============================================================
    # TEST DISCOVERY CHECK
    # ============================================================

    total_tests, passed_tests, failed_tests, skipped_tests = parse_test_summary(combined)


    # ============================================================
    # RESULT
    # ============================================================

    status = "FAILED"

    if (
        process.returncode == 0
        and isinstance(total_tests, int)
        and total_tests > 0
        and isinstance(passed_tests, int)
        and passed_tests > 0
        and failed_tests == 0
        and passed_tests + skipped_tests == total_tests
    ):

        status = "SUCCESS"


    result_data = {
        "status": status,
        "message": (
            "Generated regression test created and "
            "test command executed."
            if status == "SUCCESS"
            else
            "Generated regression test was created, "
            "but test execution did not pass validation."
        ),
        "technology": technology,
        "project_category": PROJECT_CATEGORY,
        "languages": languages,
        "package_manager": package_manager,
        "test_framework": test_framework,
        "test_command": test_command,
        "test_file": TEST_FILE_RELATIVE,
        "existing_tests": existing_tests,
        "tests": total_tests,
        "passed": passed_tests,
        "failed": failed_tests,
        "skipped": skipped_tests,
        "return_code": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        # The test needs the app the workflow started; Test Execution
        # reuses this result instead of running it without the app.
        "workflow_started_app": workflow_runs_app,
    }


    save_json(result_data)


    print("\n=============================")
    print("TEST GENERATION COMPLETE")
    print("=============================")

    print(
        f"Status: {status}"
    )

    print(
        f"Generated test: {TEST_FILE_RELATIVE}"
    )

    print(
        f"Tests: {total_tests}"
    )

    print(
        f"Passed: {passed_tests}"
    )

    print(
        f"Failed: {failed_tests}"
    )

    print(
        f"Result saved to: {OUTPUT_FILE}"
    )


    if status != "SUCCESS":
        sys.exit(1)
if __name__ == "__main__":
    sys.exit(main())
