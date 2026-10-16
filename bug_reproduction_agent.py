import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit
import urllib.parse
import urllib.request
import urllib.error

from workflow_artifacts import resolve_artifact_path


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

REPOSITORY_CONTEXT_FILE = resolve_artifact_path(
    "repository_context.json",
    BASE_DIR,
)

BUG_REPORT_FILE = os.path.join(
    BASE_DIR,
    "bug_report_level2.json",
)

REPRODUCTION_RESULT_FILE = resolve_artifact_path(
    "reproduction_result.json",
    BASE_DIR,
)

LEVEL1_BASE_URL = "http://localhost:3000"
APPLICATION_STARTUP_TIMEOUT_SECONDS = 35


def normalize_base_url(base_url):
    if not isinstance(base_url, str) or not base_url.strip():
        return None

    return base_url.rstrip("/")


def parse_response_body(body):
    try:
        return json.loads(body)
    except (TypeError, json.JSONDecodeError):
        return None


def component_for_type(repository_context, component_type):
    architecture = repository_context.get("architecture", {})

    if not isinstance(architecture, dict):
        return {}

    components = architecture.get("components", [])

    if not isinstance(components, list):
        return {}

    for component in components:
        if (
            isinstance(component, dict)
            and component.get("type") == component_type
        ):
            return component

    return {}


def detect_backend_base_url(repository_path, repository_context):
    configured_url = os.getenv("REPRODUCTION_BASE_URL")

    if configured_url:
        return normalize_base_url(configured_url)

    backend = component_for_type(repository_context, "backend")
    evidence_files = backend.get("evidence", [])

    if not isinstance(evidence_files, list):
        evidence_files = []

    candidate_files = list(evidence_files)
    repository_files = repository_context.get("files", [])

    if isinstance(repository_files, list):
        candidate_files.extend(
            file
            for file in repository_files
            if os.path.basename(file).lower() in {
                "server.js",
                "app.js",
                "index.js",
                "main.js",
            }
        )

    for relative_path in dict.fromkeys(candidate_files):
        try:
            content = read_file(
                os.path.join(repository_path, relative_path)
            )
        except (OSError, UnicodeError):
            continue

        port_match = re.search(
            r"process\.env\.PORT\s*\|\|\s*(\d+)",
            content,
        )

        if port_match:
            return f"http://localhost:{port_match.group(1)}"

    return None


def resolve_base_url(repository_path, repository_context, bug_definition):
    configured_url = bug_definition.get("base_url")

    if configured_url:
        return normalize_base_url(configured_url)

    component_type = bug_definition.get("component_type")

    if component_type:
        component_url = component_for_type(
            repository_context,
            component_type,
        ).get("base_url")

        if component_url:
            return normalize_base_url(component_url)

    if component_type == "backend":
        execution_strategy = repository_context.get(
            "execution_strategy",
            {},
        )

        if isinstance(execution_strategy, dict):
            strategy_url = execution_strategy.get(
                "backend_base_url"
            )

            if strategy_url:
                return normalize_base_url(strategy_url)

        return detect_backend_base_url(
            repository_path,
            repository_context,
        )

    return normalize_base_url(
        os.getenv("REPRODUCTION_BASE_URL")
    )


def configured_port_variable(repository_path, repository_context):
    backend = component_for_type(repository_context, "backend")
    component_path = backend.get("path", "")
    repository_files = repository_context.get("files", [])

    if not isinstance(repository_files, list):
        return None

    entry_names = {"server.js", "app.js", "index.js", "main.js"}
    for relative_path in repository_files:
        normalized_path = relative_path.replace("\\", "/")
        if component_path:
            component_prefix = component_path.replace("\\", "/").rstrip("/") + "/"
            if not normalized_path.startswith(component_prefix):
                continue

        if os.path.basename(normalized_path).lower() not in entry_names:
            continue

        try:
            source = read_file(os.path.join(repository_path, relative_path))
        except (OSError, UnicodeError):
            continue

        match = re.search(
            r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)\s*\|\|\s*(\d+)",
            source,
        )
        if match:
            return match.group(1), int(match.group(2))

    return None


def isolate_local_endpoint(repository_path, repository_context, base_url):
    """Avoid reproducing against an unrelated process already on the port."""

    parsed = urlsplit(base_url)

    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "localhost",
        "127.0.0.1",
    }:
        return base_url, {}, None

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection(
            (parsed.hostname, port),
            timeout=0.25,
        ):
            occupied = True
    except OSError:
        occupied = False

    if not occupied:
        return base_url, {}, None

    port_setting = configured_port_variable(
        repository_path,
        repository_context,
    )
    if not port_setting:
        return None, {}, (
            f"Configured application URL {base_url} is already in use, "
            "and no configurable port setting was found in the backend. "
            "Refusing to reproduce against a potentially unrelated process."
        )

    variable_name, configured_port = port_setting
    if configured_port != port:
        return None, {}, (
            f"Configured application URL {base_url} is already in use, "
            f"but the backend's {variable_name} default does not match "
            "the configured URL port."
        )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        free_port = listener.getsockname()[1]

    netloc = f"{parsed.hostname}:{free_port}"
    isolated_url = urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )
    return isolated_url, {variable_name: str(free_port)}, None


def level1_empty_task_definition():
    return {
        "bug": "Empty Task can be added",
        "base_url": LEVEL1_BASE_URL,
        "component_type": "backend",
        "readiness_path": "/",
        "action": {
            "method": "POST",
            "path": "/addtask",
            "data": {"newtask": ""},
            "body_type": "form",
        },
    }


def level1_remove_task_definition():
    return {
        "bug": "Selected task cannot be moved to Completed Tasks",
        "base_url": LEVEL1_BASE_URL,
        "component_type": "backend",
        "readiness_path": "/",
    }


def mern_empty_employee_definition():
    return {
        "bug": "Employee creation accepts empty required fields",
        "expected_behavior": (
            "Submitting the Create Employee form with empty required fields "
            "should be rejected and must not create a blank employee record."
        ),
        "component_type": "backend",
        "readiness_path": "/record",
        "action": {
            "method": "POST",
            "path": "/record",
            "data": {
                "name": "",
                "position": "",
                "level": "",
            },
            "body_type": "json",
        },
        "state_check": {
            "method": "GET",
            "path": "/record",
        },
    }


def bug_definition_for_report(bug_report):
    title = str(
        bug_report.get("bug_title", "")
    ).strip().lower()

    reproduction = bug_report.get(
        "reproduction"
    )

    if isinstance(reproduction, dict):
        action = reproduction.get("action")

        if isinstance(action, dict):
            return {
                "bug": bug_report.get(
                    "bug_title",
                    "Configured bug",
                ),
                "component_type": reproduction.get(
                    "component_type",
                    "http",
                ),
                "readiness_path": reproduction.get(
                    "readiness_path",
                    "/",
                ),
                "action": action,
                "state_check": reproduction.get(
                    "state_check"
                ),
                "expected_behavior": bug_report.get(
                    "expected_behavior"
                ),
            }

    level1_remove_titles = {
        "selected task cannot be moved to completed tasks",
        "selected task cannot be removed or moved to completed tasks",
    }

    if title in level1_remove_titles:
        return level1_remove_task_definition()

    if "empty" in title and "task" in title:
        return level1_empty_task_definition()

    if title == "employee creation accepts empty required fields":
        return mern_empty_employee_definition()

    return None

def load_json(file_path):
    with open(file_path, "r", encoding="utf-8") as file:
        return json.load(file)


def read_file(file_path):
    with open(file_path, "r", encoding="utf-8") as file:
        return file.read()


def command_parts(command):
    if isinstance(command, list):
        parts = [str(part) for part in command]
    elif isinstance(command, str):
        parts = shlex.split(command)
    else:
        return []

    if os.name == "nt" and parts:
        executable = parts[0].lower()
        windows_shims = {
            "npm": "npm.cmd",
            "npx": "npx.cmd",
            "pnpm": "pnpm.cmd",
            "yarn": "yarn.cmd",
        }

        if executable in windows_shims:
            parts[0] = windows_shims[executable]

    return parts


def resolve_start_command(working_directory, command):
    """Run simple npm start scripts directly when they invoke Node itself."""

    parts = command_parts(command)

    if not parts or os.path.basename(parts[0]).lower() not in {
        "npm",
        "npm.cmd",
    }:
        return parts

    script_index = None
    if len(parts) == 2 and parts[1] == "start":
        script_index = 1
    elif len(parts) == 3 and parts[1:3] == ["run", "start"]:
        script_index = 2

    if script_index is None:
        return parts

    package_path = os.path.join(working_directory, "package.json")
    try:
        with open(package_path, "r", encoding="utf-8") as file:
            package_data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return parts

    scripts = package_data.get("scripts", {})
    start_script = scripts.get("start") if isinstance(scripts, dict) else None
    if not isinstance(start_script, str):
        return parts

    try:
        script_parts = shlex.split(start_script)
    except ValueError:
        return parts

    if not script_parts or os.path.basename(script_parts[0]).lower() not in {
        "node",
        "node.exe",
    }:
        return parts

    return script_parts


def stop_process(process):
    if process is None or process.poll() is not None:
        return

    process.terminate()

    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def prepare_runtime_configuration(working_directory):
    """Create ignored runtime config only when a tracked example exists."""

    config_path = os.path.join(
        working_directory,
        "config.env",
    )
    example_path = os.path.join(
        working_directory,
        "config.env.example",
    )

    if os.path.exists(config_path) or not os.path.isfile(example_path):
        return None

    shutil.copyfile(example_path, config_path)
    return config_path


def start_application(
    repository_path,
    start_command,
    base_url=None,
    readiness_path="/",
    working_directory=None,
    environment=None,
):
    working_directory = working_directory or repository_path
    parts = resolve_start_command(
        working_directory,
        start_command,
    )

    if not parts:
        return None, "The configured start_command is empty."

    base_url = normalize_base_url(base_url)

    if not base_url:
        return None, "No application base URL was configured."

    try:
        prepared_config = prepare_runtime_configuration(
            working_directory,
        )

        if prepared_config:
            print(
                "Created runtime configuration from config.env.example: "
                f"{prepared_config}"
            )

        package_json = os.path.join(
            working_directory,
            "package.json",
        )

        if os.path.exists(package_json):
            print("\n--- Installing Application Dependencies ---")

            npm_command = "npm.cmd" if os.name == "nt" else "npm"
            install_command = [npm_command, "install"]
            if os.path.isfile(os.path.join(working_directory, "package-lock.json")):
                install_command = [npm_command, "ci"]

            install_process = subprocess.run(
                install_command,
                cwd=working_directory,
                capture_output=True,
                text=True,
                timeout=300,
            )

            if install_process.returncode != 0:
                return None, (
                    "Dependency installation failed.\n"
                    f"STDOUT:\n{install_process.stdout}\n"
                    f"STDERR:\n{install_process.stderr}"
                )

            print("Dependencies installed successfully.")

        last_error = "Application did not become ready."

        for attempt in range(1, 4):
            process_environment = os.environ.copy()
            if environment:
                process_environment.update(environment)

            process = subprocess.Popen(
                parts,
                cwd=working_directory,
                env=process_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            deadline = (
                time.time() + APPLICATION_STARTUP_TIMEOUT_SECONDS
            )

            while time.time() < deadline:
                if process.poll() is not None:
                    stdout, stderr = process.communicate(timeout=2)
                    last_error = (
                        "Application exited before validation.\n"
                        f"STDOUT:\n{stdout}\n"
                        f"STDERR:\n{stderr}"
                    )
                    break

                probe = http_request(
                    "GET",
                    readiness_path,
                    base_url=base_url,
                )

                if probe["success"] or probe["status_code"] is not None:
                    time.sleep(0.25)

                    if process.poll() is not None:
                        stdout, stderr = process.communicate(timeout=2)
                        last_error = (
                            "A readiness response was received, but the "
                            "application process started for this run exited. "
                            "Another process may already be serving the "
                            "configured URL.\n"
                            f"STDOUT:\n{stdout}\n"
                            f"STDERR:\n{stderr}"
                        )
                        break

                    return process, None

                time.sleep(0.25)
            else:
                stop_process(process)
                stdout, stderr = process.communicate(timeout=2)
                last_error = (
                    "Application did not become ready within "
                    f"{APPLICATION_STARTUP_TIMEOUT_SECONDS} seconds.\n"
                    f"STDOUT:\n{stdout}\n"
                    f"STDERR:\n{stderr}"
                )

            if attempt < 3:
                time.sleep(1)

        return None, last_error

    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return None, f"Application could not be started: {error}"


def http_request(
    method,
    path,
    data=None,
    base_url=None,
    body_type="form",
    timeout=5,
):
    base_url = normalize_base_url(base_url)

    if not base_url:
        return {
            "success": False,
            "status_code": None,
            "body": "",
            "error": "No application base URL was configured.",
        }

    normalized_path = path if str(path).startswith("/") else f"/{path}"
    url = f"{base_url}{normalized_path}"

    encoded_data = None
    headers = {}

    if data is not None:
        if body_type == "json":
            encoded_data = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif body_type == "form":
            encoded_data = urllib.parse.urlencode(
                data,
                doseq=True,
            ).encode("utf-8")
            headers["Content-Type"] = (
                "application/x-www-form-urlencoded"
            )
        else:
            return {
                "success": False,
                "status_code": None,
                "body": "",
                "error": f"Unsupported request body type: {body_type}",
            }

    request = urllib.request.Request(
        url,
        data=encoded_data,
        method=method,
        headers=headers,
    )

    try:
        response = urllib.request.urlopen(
            request,
            timeout=timeout,
        )

        body = response.read().decode(
            "utf-8",
            errors="ignore",
        )

        return {
            "success": 200 <= response.status < 400,
            "status_code": response.status,
            "body": body,
            "error": None,
        }

    except urllib.error.HTTPError as error:
        try:
            body = error.read().decode(
                "utf-8",
                errors="ignore",
            )
        except Exception:
            body = ""

        return {
            "success": False,
            "status_code": error.code,
            "body": body,
            "error": str(error),
        }

    except Exception as error:
        return {
            "success": False,
            "status_code": None,
            "body": "",
            "error": str(error),
        }


def normalize_html(html):
    return re.sub(
        r"\s+",
        " ",
        html,
    ).strip()


def extract_task_area(html):
    normalized = normalize_html(html)

    patterns = [
        r"<h2[^>]*>\s*Added Task\s*</h2>(.*?)(?=<h2|</form>|$)",
        r"Added Task(.*?)(?:Completed Tasks|$)",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            normalized,
            re.IGNORECASE,
        )

        if match:
            return match.group(1)

    return normalized


def task_present_in_active_list(html, task):
    task_area = extract_task_area(html)

    return task.lower() in task_area.lower()


def task_present_in_completed_list(html, task):
    normalized = normalize_html(html)

    completed_patterns = [
        r"Completed Tasks(.*)$",
        r"Completed Task(.*)$",
    ]

    for pattern in completed_patterns:
        match = re.search(
            pattern,
            normalized,
            re.IGNORECASE,
        )

        if match:
            completed_area = match.group(1)

            if task.lower() in completed_area.lower():
                return True

    return False


def contains_empty_task(html):
    normalized = normalize_html(html)

    added_task_match = re.search(
        r"Added Task(.*?)(?:Completed Tasks|$)",
        normalized,
        re.IGNORECASE,
    )

    if not added_task_match:
        return False

    added_area = added_task_match.group(1)

    # Detect list item with no meaningful task text.
    list_items = re.findall(
        r"<li[^>]*>(.*?)</li>",
        added_area,
        re.IGNORECASE,
    )

    for item in list_items:
        cleaned = re.sub(
            r"<[^>]+>",
            " ",
            item,
        )

        cleaned = re.sub(
            r"\s+",
            " ",
            cleaned,
        ).strip()

        # Remove checkbox-related text/attributes.
        cleaned = re.sub(
            r"name\s*=\s*[\"']check[\"']",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )

        if not cleaned:
            return True

    return False


def reproduce_empty_task_bug(
    repository_path,
    start_command,
    validation_method,
    process,
    base_url=LEVEL1_BASE_URL,
):
    """
    Actually reproduce the Empty Task bug by submitting:

        POST /addtask
        newtask=""

    Then GET / and inspect the rendered task list.
    """

    evidence = []

    index_file = os.path.join(
        repository_path,
        "index.js",
    )

    view_file = os.path.join(
        repository_path,
        "views",
        "index.ejs",
    )

    try:
        index_content = read_file(index_file)
        view_content = read_file(view_file)
    except (OSError, UnicodeError) as error:
        return apply_reproduction_contract({
            "status": "NOT_REPRODUCED",
            "bug": "Empty Task can be added",
            "observed_behavior": (
                f"Required application files could not be read: {error}"
            ),
            "expected_behavior": (
                "Submitting Add Task with an empty value should not "
                "create a task."
            ),
            "evidence": [],
            "application_status": None,
        }, executed=False, execution_error=True)

    normalized_view = normalize_html(view_content)
    normalized_index = normalize_html(index_content)

    if "/addtask" in normalized_view.lower():
        evidence.append(
            "Add Task form uses the /addtask endpoint."
        )

    if 'name="newtask"' in normalized_view.lower():
        evidence.append(
            "Add Task input uses the newtask field."
        )

    if "/addtask" in normalized_index.lower():
        evidence.append(
            "Backend source contains the /addtask endpoint."
        )

    if process is None:
        return apply_reproduction_contract({
            "status": "NOT_REPRODUCED",
            "bug": "Empty Task can be added",
            "observed_behavior": (
                "The application was not running, so the actual "
                "HTTP reproduction could not be performed."
            ),
            "expected_behavior": (
                "Submitting Add Task with an empty value should not "
                "create a task."
            ),
            "evidence": evidence,
            "application_status": None,
        }, executed=False, execution_error=True)

    before_response = http_request(
        "GET",
        "/",
        base_url=base_url,
    )

    if not before_response["success"]:
        evidence.append(
            "Initial GET / failed: "
            f"{before_response['error']}"
        )

        return apply_reproduction_contract({
            "status": "NOT_REPRODUCED",
            "bug": "Empty Task can be added",
            "observed_behavior": (
                "The application was running, but the initial page "
                "could not be loaded."
            ),
            "expected_behavior": (
                "Submitting Add Task with an empty value should not "
                "create a task."
            ),
            "evidence": evidence,
            "application_status": before_response["status_code"],
        }, executed=False, execution_error=True)

    evidence.append(
        "Initial GET / returned HTTP "
        f"{before_response['status_code']}."
    )

    # ACTUAL BUG REPRODUCTION
    add_response = http_request(
        "POST",
        "/addtask",
        {
            "newtask": "",
        },
        base_url=base_url,
        body_type="form",
    )

    evidence.append(
        "Submitted POST /addtask with newtask=\"\"."
    )

    if add_response["status_code"] is not None:
        evidence.append(
            "Empty-task POST returned HTTP "
            f"{add_response['status_code']}."
        )

    after_response = http_request(
        "GET",
        "/",
        base_url=base_url,
    )

    if not after_response["success"]:
        evidence.append(
            "GET / after empty-task submission failed: "
            f"{after_response['error']}"
        )

        return apply_reproduction_contract({
            "status": "NOT_REPRODUCED",
            "bug": "Empty Task can be added",
            "observed_behavior": (
                "The empty-task submission was sent, but the resulting "
                "task list could not be inspected."
            ),
            "expected_behavior": (
                "Submitting Add Task with an empty value should not "
                "create a task."
            ),
            "evidence": evidence,
            "application_status": after_response["status_code"],
        },
            executed=True,
            evidence_complete=False,
            execution_error=add_response["status_code"] is None,
            attempted=[{
                "method": "POST",
                "path": "/addtask",
                "data_fields": ["newtask"],
                "body_type": "form",
                "values_redacted": True,
            }],
        )

    if contains_empty_task(after_response["body"]):
        evidence.append(
            "After submitting the empty value, an empty task was "
            "detected in the Added Task section."
        )

        return apply_reproduction_contract({
            "status": "REPRODUCED",
            "bug": "Empty Task can be added",
            "observed_behavior": (
                "Submitting Add Task with an empty value resulted in "
                "an empty task appearing in the Added Task list."
            ),
            "expected_behavior": (
                "Submitting Add Task with an empty or whitespace-only "
                "value should not create a task."
            ),
            "evidence": evidence,
            "application_status": after_response["status_code"],
        }, executed=True, attempted=[{
            "method": "POST",
            "path": "/addtask",
            "data_fields": ["newtask"],
            "body_type": "form",
            "values_redacted": True,
        }])

    evidence.append(
        "No empty task was detected after the actual POST /addtask "
        "request."
    )

    return apply_reproduction_contract({
        "status": "NOT_REPRODUCED",
        "bug": "Empty Task can be added",
        "observed_behavior": (
            "The actual empty-task submission did not result in an "
            "empty task being detected."
        ),
        "expected_behavior": (
            "Submitting Add Task with an empty or whitespace-only "
            "value should not create a task."
        ),
        "evidence": evidence,
        "application_status": after_response["status_code"],
    },
        executed=True,
        evidence_complete=add_response["status_code"] is not None
        and add_response["status_code"] < 500,
        execution_error=add_response["status_code"] is None,
        attempted=[{
            "method": "POST",
            "path": "/addtask",
            "data_fields": ["newtask"],
            "body_type": "form",
            "values_redacted": True,
        }],
    )


def reproduce_remove_task_bug(
    repository_path,
    start_command,
    validation_method,
    process,
    base_url=LEVEL1_BASE_URL,
):
    """
    Actually reproduce the Remove -> Completed Task bug.

    A unique task is first created, then submitted through:

        POST /removetask
        check=<task>

    The resulting page is inspected to determine whether the task
    moved to Completed Tasks.
    """

    evidence = []

    index_file = os.path.join(
        repository_path,
        "index.js",
    )

    view_file = os.path.join(
        repository_path,
        "views",
        "index.ejs",
    )

    try:
        index_content = read_file(index_file)
        view_content = read_file(view_file)
    except (OSError, UnicodeError) as error:
        return apply_reproduction_contract({
            "status": "NOT_REPRODUCED",
            "bug": "Selected task cannot be moved to Completed Tasks",
            "observed_behavior": (
                f"Required application files could not be read: {error}"
            ),
            "expected_behavior": (
                "Selecting a task and clicking Remove should move the "
                "selected task to Completed Tasks."
            ),
            "evidence": [],
            "application_status": None,
        }, executed=False, execution_error=True)

    normalized_view = normalize_html(view_content)
    normalized_index = normalize_html(index_content)

    if "/removetask" in normalized_index.lower():
        evidence.append(
            "Backend source contains the /removetask endpoint."
        )

    if 'name="check"' in normalized_view.lower():
        evidence.append(
            "Task checkboxes use the check field."
        )

    if "/removetask" in normalized_view.lower():
        evidence.append(
            "The view contains /removetask submission logic."
        )

    if process is None:
        return apply_reproduction_contract({
            "status": "NOT_REPRODUCED",
            "bug": "Selected task cannot be moved to Completed Tasks",
            "observed_behavior": (
                "The application was not running, so the actual "
                "Remove Task reproduction could not be performed."
            ),
            "expected_behavior": (
                "Selecting a task and clicking Remove should move the "
                "selected task to Completed Tasks."
            ),
            "evidence": evidence,
            "application_status": None,
        }, executed=False, execution_error=True)

    # Create a unique task so the reproduction does not depend on
    # manually existing tasks.
    test_task = (
        "AI_REPRODUCTION_TEST_"
        f"{int(time.time())}"
    )

    add_response = http_request(
        "POST",
        "/addtask",
        {
            "newtask": test_task,
        },
        base_url=base_url,
        body_type="form",
    )

    evidence.append(
        f"Created reproduction task: {test_task}"
    )

    evidence.append(
        "POST /addtask returned HTTP "
        f"{add_response['status_code']}."
    )

    if not add_response["success"]:
        return apply_reproduction_contract({
            "status": "NOT_REPRODUCED",
            "bug": "Selected task cannot be moved to Completed Tasks",
            "observed_behavior": (
                "The reproduction task could not be created."
            ),
            "expected_behavior": (
                "Selecting a task and clicking Remove should move the "
                "selected task to Completed Tasks."
            ),
            "evidence": evidence,
            "application_status": add_response["status_code"],
        }, executed=False, execution_error=True, attempted=[{
            "method": "POST",
            "path": "/addtask",
            "data_fields": ["newtask"],
            "body_type": "form",
            "purpose": "create_reproduction_task",
            "values_redacted": True,
        }])

    before_remove = http_request(
        "GET",
        "/",
        base_url=base_url,
    )

    if not before_remove["success"]:
        evidence.append(
            "GET / before Remove failed: "
            f"{before_remove['error']}"
        )

        return apply_reproduction_contract({
            "status": "NOT_REPRODUCED",
            "bug": "Selected task cannot be moved to Completed Tasks",
            "observed_behavior": (
                "The application page could not be inspected after "
                "creating the reproduction task."
            ),
            "expected_behavior": (
                "Selecting a task and clicking Remove should move the "
                "selected task to Completed Tasks."
            ),
            "evidence": evidence,
            "application_status": before_remove["status_code"],
        }, executed=False, execution_error=True, attempted=[{
            "method": "POST",
            "path": "/addtask",
            "data_fields": ["newtask"],
            "body_type": "form",
            "purpose": "create_reproduction_task",
            "values_redacted": True,
        }])

    if task_present_in_active_list(
        before_remove["body"],
        test_task,
    ):
        evidence.append(
            "The reproduction task was found in the active task list."
        )
    else:
        evidence.append(
            "The reproduction task was not detected in the active "
            "task list before Remove."
        )

    # ACTUAL REMOVE ACTION
    remove_response = http_request(
        "POST",
        "/removetask",
        {
            "check": test_task,
        },
        base_url=base_url,
        body_type="form",
    )

    evidence.append(
        "Submitted POST /removetask with "
        f"check={test_task}."
    )

    evidence.append(
        "POST /removetask returned HTTP "
        f"{remove_response['status_code']}."
    )

    after_remove = http_request(
        "GET",
        "/",
        base_url=base_url,
    )

    if not after_remove["success"]:
        evidence.append(
            "GET / after Remove failed: "
            f"{after_remove['error']}"
        )

        return apply_reproduction_contract({
            "status": "NOT_REPRODUCED",
            "bug": "Selected task cannot be moved to Completed Tasks",
            "observed_behavior": (
                "The Remove request was sent, but the resulting "
                "application state could not be inspected."
            ),
            "expected_behavior": (
                "Selecting a task and clicking Remove should move the "
                "selected task to Completed Tasks."
            ),
            "evidence": evidence,
            "application_status": after_remove["status_code"],
        },
            executed=True,
            evidence_complete=False,
            execution_error=remove_response["status_code"] is None,
            attempted=[
                {
                    "method": "POST",
                    "path": "/addtask",
                    "data_fields": ["newtask"],
                    "body_type": "form",
                    "purpose": "create_reproduction_task",
                    "values_redacted": True,
                },
                {
                    "method": "POST",
                    "path": "/removetask",
                    "data_fields": ["check"],
                    "body_type": "form",
                    "values_redacted": True,
                },
            ],
        )

    still_active = task_present_in_active_list(
        after_remove["body"],
        test_task,
    )

    in_completed = task_present_in_completed_list(
        after_remove["body"],
        test_task,
    )

    if in_completed:
        evidence.append(
            "The reproduction task was found in Completed Tasks "
            "after the Remove request."
        )

    if still_active:
        evidence.append(
            "The reproduction task is still present in the active "
            "task list after the Remove request."
        )

    # Actual bug:
    # task was not moved to Completed Tasks.
    if not in_completed:
        evidence.append(
            "The selected task was NOT detected in Completed Tasks "
            "after POST /removetask."
        )

        return apply_reproduction_contract({
            "status": "REPRODUCED",
            "bug": "Selected task cannot be moved to Completed Tasks",
            "observed_behavior": (
                "After selecting a task and submitting Remove, the "
                "task was not moved to the Completed Tasks section."
            ),
            "expected_behavior": (
                "Selecting a task and clicking Remove should move the "
                "selected task from the active task list to Completed "
                "Tasks."
            ),
            "evidence": evidence,
            "application_status": after_remove["status_code"],
        },
            executed=True,
            evidence_complete=remove_response["status_code"] is not None
            and remove_response["status_code"] < 500,
            execution_error=remove_response["status_code"] is None,
            attempted=[
                {
                    "method": "POST",
                    "path": "/addtask",
                    "data_fields": ["newtask"],
                    "body_type": "form",
                    "purpose": "create_reproduction_task",
                    "values_redacted": True,
                },
                {
                    "method": "POST",
                    "path": "/removetask",
                    "data_fields": ["check"],
                    "body_type": "form",
                    "values_redacted": True,
                },
            ],
        )

    evidence.append(
        "The selected task was successfully moved to Completed Tasks."
    )

    return apply_reproduction_contract({
        "status": "NOT_REPRODUCED",
        "bug": "Selected task cannot be moved to Completed Tasks",
        "observed_behavior": (
            "The actual Remove request successfully moved the "
            "selected task to Completed Tasks."
        ),
        "expected_behavior": (
            "Selecting a task and clicking Remove should move the "
            "selected task to Completed Tasks."
        ),
        "evidence": evidence,
        "application_status": after_remove["status_code"],
    }, executed=True, attempted=[
        {
            "method": "POST",
            "path": "/addtask",
            "data_fields": ["newtask"],
            "body_type": "form",
            "purpose": "create_reproduction_task",
            "values_redacted": True,
        },
        {
            "method": "POST",
            "path": "/removetask",
            "data_fields": ["check"],
            "body_type": "form",
            "values_redacted": True,
        },
    ])


def classify_reproduction_outcome(
    executed,
    bug_observed=False,
    evidence_complete=True,
    execution_error=False,
):
    if execution_error or not executed:
        return "EXECUTION_ERROR"

    if not evidence_complete:
        return "INCONCLUSIVE"

    return "REPRODUCED" if bug_observed else "NOT_REPRODUCED"


def sanitize_attempted_evidence(attempted):
    safe_attempts = []
    allowed_body_types = {
        "form",
        "json",
        "application/json",
        "application/x-www-form-urlencoded",
    }
    allowed_methods = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
    allowed_purposes = {"create_reproduction_task"}
    allowed_operations = {
        "start_application",
        "select_reproduction_strategy",
        "prepare_reproduction",
    }

    for item in attempted if isinstance(attempted, (list, tuple)) else []:
        if not isinstance(item, dict):
            continue

        safe_item = {}
        method = item.get("method")
        if isinstance(method, str) and method.upper() in allowed_methods:
            safe_item["method"] = method.upper()

        path = item.get("path")
        if isinstance(path, str):
            safe_path = urllib.parse.urlsplit(path).path
            if safe_path:
                safe_item["path"] = safe_path

        body_type = item.get("body_type")
        if isinstance(body_type, str) and body_type.lower() in allowed_body_types:
            safe_item["body_type"] = body_type.lower()

        fields = item.get("data_fields", [])
        if not isinstance(fields, (list, tuple)):
            fields = []
        data = item.get("data")
        if isinstance(data, dict):
            fields = [*fields, *data.keys()]

        safe_fields = list(dict.fromkeys(
            field
            for field in fields
            if isinstance(field, str)
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,79}", field)
        ))
        if safe_fields:
            safe_item["data_fields"] = safe_fields
            safe_item["values_redacted"] = True

        purpose = item.get("purpose")
        if purpose in allowed_purposes:
            safe_item["purpose"] = purpose

        operation = item.get("operation")
        if operation in allowed_operations:
            safe_item["operation"] = operation

        if isinstance(item.get("executed"), bool):
            safe_item["executed"] = item["executed"]

        nested_steps = item.get("steps")
        if isinstance(nested_steps, (list, tuple)):
            safe_item["steps"] = sanitize_attempted_evidence(nested_steps)

        safe_attempts.append(safe_item)

    return safe_attempts


def apply_reproduction_contract(
    result,
    executed,
    evidence_complete=True,
    execution_error=False,
    attempted=None,
):
    """Set executed when the bug-specific reproduction action was invoked."""
    result["status"] = classify_reproduction_outcome(
        executed,
        bug_observed=result.get("status") == "REPRODUCED",
        evidence_complete=evidence_complete,
        execution_error=execution_error,
    )
    result["executed"] = bool(executed)
    result["attempted"] = sanitize_attempted_evidence(attempted)
    return result


def build_structured_evidence(result):
    bugs = result.get("bugs", [])
    attempted = []
    observed = []
    errors = []

    for bug in bugs:
        bug_name = bug.get("bug")
        bug_evidence = bug.get("evidence", [])
        attempted_steps = sanitize_attempted_evidence(
            bug.get("attempted", [])
        )

        if attempted_steps:
            attempted.append({
                "executed": bool(bug.get("executed")),
                "steps": attempted_steps,
            })

        observed.append({
            "bug": bug_name,
            "status": bug.get("status"),
            "summary": bug.get("observed_behavior"),
            "application_status": bug.get("application_status"),
        })

        if bug.get("status") in ("INCONCLUSIVE", "EXECUTION_ERROR"):
            errors.append({
                "status": bug.get("status"),
                "details": bug.get("observed_behavior"),
            })

    if not bugs and result.get("status") == "EXECUTION_ERROR":
        message = result.get("observed_behavior")
        if message:
            errors.append({
                "stage": "reproduction_setup",
                "details": message,
            })

    return {
        "attempted": attempted,
        "observed": observed,
        "errors": errors,
    }


def reproduction_result(
    status,
    bug,
    observed_behavior,
    expected_behavior,
    evidence,
    application_status,
    executed,
    evidence_complete=True,
    execution_error=False,
    attempted=None,
):
    return apply_reproduction_contract({
        "status": status,
        "bug": bug,
        "observed_behavior": observed_behavior,
        "expected_behavior": expected_behavior,
        "evidence": evidence,
        "application_status": application_status,
    }, executed, evidence_complete, execution_error, attempted)


def build_final_reproduction_result(project, technology, bug_title, result):
    bugs = [dict(bug) for bug in result.get("bugs", [])]
    for bug in bugs:
        if "attempted" in bug:
            bug["attempted"] = sanitize_attempted_evidence(bug["attempted"])
    evidence = result.get("evidence")
    required_evidence_fields = {"attempted", "observed", "errors"}
    if (
        not isinstance(evidence, dict)
        or not required_evidence_fields.issubset(evidence)
    ):
        evidence = build_structured_evidence(result)
    else:
        evidence = {
            "attempted": sanitize_attempted_evidence(evidence["attempted"]),
            "observed": evidence["observed"],
            "errors": evidence["errors"],
        }

    final_result = {
        "project": project,
        "technology": technology,
        "bug_title": bug_title,
        "status": result.get("status", "EXECUTION_ERROR"),
        "executed": bool(
            result.get(
                "executed",
                any(bug.get("executed", False) for bug in bugs),
            )
        ),
        "evidence": evidence,
        "bugs": bugs,
    }

    if not bugs:
        final_result.update({
            "observed_behavior": result.get("observed_behavior"),
            "expected_behavior": result.get("expected_behavior"),
            "legacy_evidence": result.get(
                "legacy_evidence",
                result.get("evidence", []),
            ),
            "application_status": result.get("application_status"),
        })

    return final_result


def record_is_empty(record):
    if not isinstance(record, dict):
        return False

    return all(
        not str(record.get(field, "") or "").strip()
        for field in ("name", "position", "level")
    )


def record_id(record):
    if not isinstance(record, dict):
        return None

    value = record.get("_id")
    return str(value) if value is not None else None


def records_from_response(response):
    payload = parse_response_body(response.get("body", ""))
    return payload if isinstance(payload, list) else []


def invalid_fields_from_payload(payload):
    if not isinstance(payload, dict):
        return []

    return [
        field
        for field, value in payload.items()
        if value is None
        or (isinstance(value, str) and not value.strip())
    ]


def record_matches_invalid_fields(record, fields):
    return (
        isinstance(record, dict)
        and bool(fields)
        and all(
            record.get(field) is None
            or (
                isinstance(record.get(field), str)
                and not record[field].strip()
            )
            for field in fields
        )
    )


def reproduce_http_bug(
    repository_path,
    process,
    base_url,
    bug_definition,
):
    """Run a configured HTTP reproduction and validate observed state."""

    bug = bug_definition["bug"]
    action = bug_definition["action"]
    expected_behavior = bug_definition.get(
        "expected_behavior",
        "The configured invalid HTTP action should be rejected without "
        "creating a record containing the submitted invalid field values.",
    )
    evidence = []

    if process is None:
        return reproduction_result(
            "NOT_REPRODUCED",
            bug,
            "The application was not running, so the actual HTTP "
            "reproduction could not be performed.",
            expected_behavior,
            evidence,
            None,
            executed=False,
            execution_error=True,
        )

    state_check = bug_definition.get("state_check")
    before_response = http_request(
        state_check["method"],
        state_check["path"],
        base_url=base_url,
    )

    if not before_response["success"]:
        evidence.append(
            f"Initial {state_check['method']} {state_check['path']} "
            f"failed with HTTP {before_response['status_code']}."
        )
        return reproduction_result(
            "NOT_REPRODUCED",
            bug,
            "The application was running, but the initial employee "
            "state could not be loaded.",
            expected_behavior,
            evidence,
            before_response["status_code"],
            executed=False,
            execution_error=True,
        )

    before_payload = parse_response_body(before_response.get("body", ""))
    if not isinstance(before_payload, list):
        evidence.append("Initial state response was not a JSON array.")
        return reproduction_result(
            "NOT_REPRODUCED",
            bug,
            "The initial state response could not be interpreted.",
            expected_behavior,
            evidence,
            before_response["status_code"],
            executed=False,
            evidence_complete=False,
        )

    before_records = before_payload
    invalid_fields = invalid_fields_from_payload(action.get("data"))
    before_invalid_count = sum(
        1
        for record in before_records
        if record_matches_invalid_fields(record, invalid_fields)
    )
    evidence.append(
        f"Initial {state_check['method']} {state_check['path']} "
        f"returned HTTP {before_response['status_code']} with "
        f"{len(before_records)} records."
    )

    action_response = http_request(
        action["method"],
        action["path"],
        action.get("data"),
        base_url=base_url,
        body_type=action.get("body_type", "form"),
    )
    action_payload = parse_response_body(action_response.get("body", ""))
    inserted_id = None

    if isinstance(action_payload, dict):
        inserted_id = action_payload.get("insertedId")
        if inserted_id is not None:
            inserted_id = str(inserted_id)

    evidence.append(
        f"Submitted {action['method']} {action['path']} with "
        f"{action.get('body_type', 'form')} data."
    )
    evidence.append(
        f"Configured action request returned HTTP "
        f"{action_response['status_code']}."
    )
    response_body = action_response.get("body", "")
    if response_body:
        evidence.append(
            "POST response body: "
            f"{response_body[:500]}"
        )

    after_response = http_request(
        state_check["method"],
        state_check["path"],
        base_url=base_url,
    )

    if not after_response["success"]:
        evidence.append(
            f"Final {state_check['method']} {state_check['path']} "
            f"failed with HTTP {after_response['status_code']}."
        )
        return reproduction_result(
            "NOT_REPRODUCED",
            bug,
            "The empty-employee request was sent, but the resulting "
            "employee state could not be inspected.",
            expected_behavior,
            evidence,
            after_response["status_code"],
            executed=True,
            evidence_complete=False,
            execution_error=action_response["status_code"] is None,
            attempted=[{
                "method": action["method"],
                "path": action["path"],
                "data_fields": list(action.get("data", {}))
                if isinstance(action.get("data"), dict)
                else [],
                "body_type": action.get("body_type", "form"),
                "values_redacted": True,
            }],
        )

    after_payload = parse_response_body(after_response.get("body", ""))
    if not isinstance(after_payload, list):
        evidence.append("Final state response was not a JSON array.")
        return reproduction_result(
            "NOT_REPRODUCED",
            bug,
            "The action was submitted, but the final state response could not be interpreted.",
            expected_behavior,
            evidence,
            after_response["status_code"],
            executed=True,
            evidence_complete=False,
            execution_error=action_response["status_code"] is None,
            attempted=[{
                "method": action["method"],
                "path": action["path"],
                "data_fields": list(action.get("data", {}))
                if isinstance(action.get("data"), dict)
                else [],
                "body_type": action.get("body_type", "form"),
                "values_redacted": True,
            }],
        )

    after_records = after_payload
    after_invalid_records = [
        record
        for record in after_records
        if record_matches_invalid_fields(record, invalid_fields)
    ]
    created_blank_record = (
        len(after_invalid_records) > before_invalid_count
        or (
            inserted_id is not None
            and any(
                record_id(record) == inserted_id
                for record in after_invalid_records
            )
        )
    )

    if created_blank_record:
        evidence.append(
            "The final state contains a newly persisted record with "
            "empty values for the invalid fields in the request."
        )

        if inserted_id:
            cleanup_path = (
                state_check["path"].rstrip("/")
                + "/"
                + urllib.parse.quote(inserted_id, safe="")
            )
            cleanup_response = http_request(
                "DELETE",
                cleanup_path,
                base_url=base_url,
            )
            evidence.append(
                f"Cleanup DELETE {cleanup_path} returned HTTP "
                f"{cleanup_response['status_code']}."
            )

        return reproduction_result(
            "REPRODUCED",
            bug,
            "The configured invalid HTTP action created a new record "
            "containing the submitted invalid field values.",
            expected_behavior,
            evidence,
            after_response["status_code"],
            executed=True,
            attempted=[{
                "method": action["method"],
                "path": action["path"],
                "data_fields": list(action.get("data", {}))
                if isinstance(action.get("data"), dict)
                else [],
                "body_type": action.get("body_type", "form"),
                "values_redacted": True,
            }],
        )

    evidence.append(
        "No newly persisted record containing the invalid request fields "
        "was found after the actual HTTP request."
    )

    return reproduction_result(
        "NOT_REPRODUCED",
        bug,
        "The HTTP action did not create a newly persisted record with the "
        "submitted invalid field values.",
        expected_behavior,
        evidence,
        after_response["status_code"],
        executed=True,
        evidence_complete=(
            action_response["status_code"] is not None
            and action_response["status_code"] < 500
        ),
        execution_error=action_response["status_code"] is None,
        attempted=[{
            "method": action["method"],
            "path": action["path"],
            "data_fields": list(action.get("data", {}))
            if isinstance(action.get("data"), dict)
            else [],
            "body_type": action.get("body_type", "form"),
            "values_redacted": True,
        }],
    )


def build_failure_result(
    message,
    technology=None,
    bug_title=None,
    attempted=None,
):
    return {
        "project": None,
        "technology": technology,
        "bug_title": bug_title,
        "status": "EXECUTION_ERROR",
        "executed": False,
        "observed_behavior": message,
        "expected_behavior": None,
        "evidence": {
            "attempted": sanitize_attempted_evidence(attempted),
            "observed": [],
            "errors": [{
                "stage": "reproduction_setup",
                "details": message,
            }],
        },
        "legacy_evidence": [message],
        "application_status": None,
        "bugs": [],
    }


def aggregate_reproduction_results(bugs):
    statuses = [bug.get("status") for bug in bugs]

    if not statuses:
        status = "EXECUTION_ERROR"
    elif all(status == "REPRODUCED" for status in statuses):
        status = "REPRODUCED"
    elif all(status == "NOT_REPRODUCED" for status in statuses):
        status = "NOT_REPRODUCED"
    elif all(status == "EXECUTION_ERROR" for status in statuses):
        status = "EXECUTION_ERROR"
    else:
        status = "INCONCLUSIVE"

    result = {
        "status": status,
        "executed": any(bug.get("executed", False) for bug in bugs),
        "bugs": bugs,
    }
    result["evidence"] = build_structured_evidence(result)
    return result


def requested_bug_matches(bug_title, bug):
    title = str(bug_title or "").lower()
    bug_name = str(bug.get("bug", "")).lower()

    if "remove" in title or "completed" in title:
        return "remove" in bug_name or "completed" in bug_name

    if "empty" in title or "task" in title:
        return "empty" in bug_name

    return True


def is_level1_bug_definition(bug_definition, bug_report):
    """Return true only for the existing Node.js Todo bug definitions."""

    level1_bugs = {
        level1_empty_task_definition()["bug"],
        "Selected task cannot be moved to Completed Tasks",
    }
    definition_name = (
        bug_definition.get("bug")
        if isinstance(bug_definition, dict)
        else None
    )
    report_title = (
        bug_report.get("bug_title")
        if isinstance(bug_report, dict)
        else None
    )

    return definition_name in level1_bugs or report_title in level1_bugs


def runtime_configuration(
    repository_path,
    repository_context,
    execution_strategy,
    bug_definition,
):
    component_type = bug_definition.get("component_type")
    component = component_for_type(
        repository_context,
        component_type,
    ) if component_type else {}

    if component_type == "backend":
        start_command = execution_strategy.get(
            "backend_start_command"
        ) or component.get("start_command")
        component_path = execution_strategy.get(
            "backend_path"
        ) or component.get("path")
    else:
        start_command = component.get("start_command")
        component_path = component.get("path")

    working_directory = repository_path

    if start_command:
        if isinstance(component_path, str) and component_path.strip():
            working_directory = os.path.join(
                repository_path,
                component_path,
            )
    else:
        start_command = execution_strategy.get("start_command")

    base_url = resolve_base_url(
        repository_path,
        repository_context,
        bug_definition,
    )

    return {
        "start_command": start_command,
        "working_directory": working_directory,
        "base_url": base_url,
        "readiness_path": bug_definition.get(
            "readiness_path",
            "/",
        ),
    }


def main():
    print("\n===== BUG REPRODUCTION AGENT =====")

    repository_context = None
    bug_report = {}

    try:
        if not os.path.exists(REPOSITORY_CONTEXT_FILE):
            raise FileNotFoundError(
                "repository_context.json was not found."
            )

        repository_context = load_json(
            REPOSITORY_CONTEXT_FILE
        )

        if not isinstance(repository_context, dict):
            raise ValueError(
                "repository_context.json must contain a JSON object."
            )

        bug_report_path = (
            sys.argv[1]
            if len(sys.argv) > 1 and sys.argv[1].strip()
            else os.getenv("BUG_REPORT_FILE", BUG_REPORT_FILE)
        )

        if os.path.exists(bug_report_path):
            bug_report = load_json(bug_report_path)

        if not isinstance(bug_report, dict):
            bug_report = {}

        repository_path = repository_context.get(
            "repository_path"
        )

        if (
            not isinstance(repository_path, str)
            or not repository_path.strip()
        ):
            raise ValueError(
                "repository_context.json does not contain "
                "repository_path."
            )

        repository_path = os.path.abspath(
            repository_path
        )

        if not os.path.isdir(repository_path):
            raise FileNotFoundError(
                f"repository_path does not exist: {repository_path}"
            )

        execution_strategy = repository_context.get(
            "execution_strategy"
        )

        if not isinstance(
            execution_strategy,
            dict,
        ):
            raise ValueError(
                "repository_context.json does not contain "
                "execution_strategy."
            )

        technology = execution_strategy.get("technology")

        validation_method = execution_strategy.get(
            "validation_method"
        )

        bug_definition = bug_definition_for_report(bug_report)

        if bug_definition is None:
            raise ValueError(
                "No reproduction definition matched the requested bug."
            )

        runtime = runtime_configuration(
            repository_path,
            repository_context,
            execution_strategy,
            bug_definition,
        )
        start_command = runtime["start_command"]
        base_url = runtime["base_url"]

        if not is_level1_bug_definition(bug_definition, bug_report):
            base_url, runtime_environment, endpoint_error = isolate_local_endpoint(
                repository_path,
                repository_context,
                base_url,
            )

            if endpoint_error:
                raise ValueError(endpoint_error)
        else:
            runtime_environment = {}

        if not start_command:
            raise ValueError(
                "No start command was found for the reproduction component."
            )

        if not base_url:
            raise ValueError(
                "No application base URL was found for the reproduction component."
            )

        print(f"Technology: {technology}")
        print(
            f"Repository: {repository_path}"
        )
        print(
            f"Bug report: "
            f"{bug_report.get('bug_title')}"
        )

        print("\n--- Starting Target Application ---")

        process, start_error = start_application(
            repository_path,
            start_command,
            base_url=base_url,
            readiness_path=runtime["readiness_path"],
            working_directory=runtime["working_directory"],
            environment=runtime_environment,
        )

        if start_error:
            print(
                "Application start failed:"
            )
            print(start_error)

            result = build_failure_result(
                start_error,
                technology=technology,
                bug_title=bug_report.get(
                    "bug_title"
                ),
                attempted=[{
                    "operation": "start_application",
                    "command": start_command,
                    "base_url": base_url,
                }],
            )

        else:

            print(
                "Target application started successfully."
            )

            try:
                is_level1 = is_level1_bug_definition(
                    bug_definition,
                    bug_report,
                )

                if is_level1:
                    print(
                        "\n--- Reproducing Bug 1: Empty Task ---"
                    )

                    empty_task_result = (
                        reproduce_empty_task_bug(
                            repository_path,
                            start_command,
                            validation_method,
                            process,
                            base_url=base_url,
                        )
                    )

                    print(
                        "Empty Task Bug:",
                        empty_task_result["status"],
                    )

                    print(
                        "\n--- Reproducing Bug 2: "
                        "Remove -> Completed ---"
                    )

                    remove_task_result = (
                        reproduce_remove_task_bug(
                            repository_path,
                            start_command,
                            validation_method,
                            process,
                            base_url=base_url,
                        )
                    )

                    print(
                        "Remove Task Bug:",
                        remove_task_result["status"],
                    )

                    requested_bug_title = bug_report.get("bug_title")

                    all_bugs = [
                        empty_task_result,
                        remove_task_result,
                    ]

                    bugs = [
                        bug
                        for bug in all_bugs
                        if requested_bug_matches(requested_bug_title, bug)
                    ]

                    if not bugs:
                        result = build_failure_result(
                            "No reproduction strategy matched the requested bug.",
                            technology=technology,
                            bug_title=requested_bug_title,
                            attempted=[{
                                "operation": "select_reproduction_strategy",
                                "bug_title": requested_bug_title,
                            }],
                        )
                    else:
                        result = aggregate_reproduction_results(bugs)

                else:
                    print(
                        "\n--- Reproducing Configured HTTP Bug ---"
                    )

                    generic_result = reproduce_http_bug(
                        repository_path,
                        process,
                        base_url,
                        bug_definition,
                    )
                    result = {
                        "status": generic_result["status"],
                        "executed": generic_result["executed"],
                        "bugs": [generic_result],
                    }
                    result["evidence"] = build_structured_evidence(result)
                    print(
                        "Configured Bug:",
                        generic_result["status"],
                    )

            finally:
                stop_process(process)

        final_result = build_final_reproduction_result(
            repository_path,
            technology,
            bug_report.get("bug_title"),
            result,
        )

    except (
        OSError,
        UnicodeError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ) as error:

        technology = None

        if isinstance(
            repository_context,
            dict,
        ):

            strategy = repository_context.get(
                "execution_strategy",
                {},
            )

            if isinstance(
                strategy,
                dict,
            ):
                technology = strategy.get(
                    "technology"
                )

        final_result = build_failure_result(
            str(error),
            technology=technology,
            bug_title=bug_report.get(
                "bug_title"
            ),
            attempted=[{
                "operation": "prepare_reproduction",
            }],
        )

    with open(
        REPRODUCTION_RESULT_FILE,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            final_result,
            file,
            indent=2,
            ensure_ascii=False,
        )

    print(
        "\n===== FINAL REPRODUCTION RESULT ====="
    )

    print(
        json.dumps(
            final_result,
            indent=2,
            ensure_ascii=False,
        )
    )

    print(
        "\nReproduction result saved to:",
        REPRODUCTION_RESULT_FILE,
    )


if __name__ == "__main__":
    main()