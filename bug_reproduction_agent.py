import json
import os
import re
import shlex
import subprocess
import time
import urllib.parse
import urllib.request
import urllib.error


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

REPOSITORY_CONTEXT_FILE = os.path.join(
    BASE_DIR,
    "repository_context.json",
)

BUG_REPORT_FILE = os.path.join(
    BASE_DIR,
    "bug_report.json",
)

REPRODUCTION_RESULT_FILE = os.path.join(
    BASE_DIR,
    "reproduction_result.json",
)

BASE_URL = "http://localhost:3000"


def load_json(file_path):
    with open(file_path, "r", encoding="utf-8") as file:
        return json.load(file)


def read_file(file_path):
    with open(file_path, "r", encoding="utf-8") as file:
        return file.read()


def command_parts(command):
    if isinstance(command, list):
        return [str(part) for part in command]

    if isinstance(command, str):
        return shlex.split(command)

    return []


def stop_process(process):
    if process is None or process.poll() is not None:
        return

    process.terminate()

    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def start_application(repository_path, start_command):
    parts = command_parts(start_command)

    if not parts:
        return None, "The configured start_command is empty."

    try:
        # Install dependencies for Node.js projects before starting.
        package_json = os.path.join(
            repository_path,
            "package.json",
        )

        if os.path.exists(package_json):
            print("\n--- Installing Node.js Dependencies ---")

            npm_command = "npm.cmd" if os.name == "nt" else "npm"

            install_process = subprocess.run(
            [npm_command, "install"],
            cwd=repository_path,
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
            process = subprocess.Popen(
                parts,
                cwd=repository_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            deadline = time.time() + 10

            while time.time() < deadline:
                if process.poll() is not None:
                    stdout, stderr = process.communicate(timeout=2)
                    last_error = (
                        "Application exited before validation.\n"
                        f"STDOUT:\n{stdout}\n"
                        f"STDERR:\n{stderr}"
                    )
                    break

                probe = http_request("GET", "/")

                if probe["success"] or probe["status_code"] is not None:
                    return process, None

                time.sleep(0.25)
            else:
                stop_process(process)
                stdout, stderr = process.communicate(timeout=2)
                last_error = (
                    "Application did not become ready within 10 seconds.\n"
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
):
    url = f"{BASE_URL}{path}"

    encoded_data = None

    if data is not None:
        encoded_data = urllib.parse.urlencode(
            data,
            doseq=True,
        ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=encoded_data,
        method=method,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    try:
        response = urllib.request.urlopen(
            request,
            timeout=5,
        )

        body = response.read().decode(
            "utf-8",
            errors="ignore",
        )

        return {
            "success": True,
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
        return {
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
        }

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
        return {
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
        }

    before_response = http_request(
        "GET",
        "/",
    )

    if not before_response["success"]:
        evidence.append(
            "Initial GET / failed: "
            f"{before_response['error']}"
        )

        return {
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
        }

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
    )

    if not after_response["success"]:
        evidence.append(
            "GET / after empty-task submission failed: "
            f"{after_response['error']}"
        )

        return {
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
        }

    if contains_empty_task(after_response["body"]):
        evidence.append(
            "After submitting the empty value, an empty task was "
            "detected in the Added Task section."
        )

        return {
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
        }

    evidence.append(
        "No empty task was detected after the actual POST /addtask "
        "request."
    )

    return {
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
    }


def reproduce_remove_task_bug(
    repository_path,
    start_command,
    validation_method,
    process,
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
        return {
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
        }

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
        return {
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
        }

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
    )

    evidence.append(
        f"Created reproduction task: {test_task}"
    )

    evidence.append(
        "POST /addtask returned HTTP "
        f"{add_response['status_code']}."
    )

    if not add_response["success"]:
        return {
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
        }

    before_remove = http_request(
        "GET",
        "/",
    )

    if not before_remove["success"]:
        evidence.append(
            "GET / before Remove failed: "
            f"{before_remove['error']}"
        )

        return {
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
        }

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
    )

    if not after_remove["success"]:
        evidence.append(
            "GET / after Remove failed: "
            f"{after_remove['error']}"
        )

        return {
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
        }

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

        return {
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
        }

    evidence.append(
        "The selected task was successfully moved to Completed Tasks."
    )

    return {
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
    }


def build_failure_result(
    message,
    technology=None,
    bug_title=None,
):
    return {
        "project": None,
        "technology": technology,
        "bug_title": bug_title,
        "status": "NOT_REPRODUCED",
        "observed_behavior": message,
        "expected_behavior": None,
        "evidence": [message],
        "application_status": None,
        "bugs": [],
    }


def requested_bug_matches(bug_title, bug):
    title = str(bug_title or "").lower()
    bug_name = str(bug.get("bug", "")).lower()

    if "remove" in title or "completed" in title:
        return "remove" in bug_name or "completed" in bug_name

    if "empty" in title or "task" in title:
        return "empty" in bug_name

    return True


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

        if os.path.exists(BUG_REPORT_FILE):
            bug_report = load_json(
                BUG_REPORT_FILE
            )

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

        technology = execution_strategy.get(
            "technology"
        )

        start_command = execution_strategy.get(
            "start_command"
        )

        validation_method = execution_strategy.get(
            "validation_method"
        )

        if not start_command:
            raise ValueError(
                "execution_strategy does not contain start_command."
            )

        print(f"Technology: {technology}")
        print(
            f"Repository: {repository_path}"
        )
        print(
            f"Bug report: "
            f"{bug_report.get('bug_title')}"
        )

        if technology != "Node.js":

            result = build_failure_result(
                (
                    "The current runtime reproduction strategy "
                    f"is not implemented for technology: {technology}."
                ),
                technology=technology,
                bug_title=bug_report.get(
                    "bug_title"
                ),
            )

        else:

            print("\n--- Starting Target Application ---")

            process, start_error = start_application(
                repository_path,
                start_command,
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
                )

            else:

                print(
                    "Target application started successfully."
                )

                try:

                    print(
                        "\n--- Reproducing Bug 1: Empty Task ---"
                    )

                    empty_task_result = (
                        reproduce_empty_task_bug(
                            repository_path,
                            start_command,
                            validation_method,
                            process,
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
                        )
                    else:
                        reproduced_count = sum(
                             1
                             for bug in bugs
                            if bug.get("status") == "REPRODUCED"
                        )

                        if reproduced_count == len(bugs):
                            overall_status = "REPRODUCED"
                        else:
                            overall_status = "NOT_REPRODUCED"

                        result = {
                            "status": overall_status,
                            "bugs": bugs,
                        }

                finally:
                    stop_process(process)

        final_result = {
            "project": repository_path,
            "technology": technology,
            "bug_title": bug_report.get(
                "bug_title"
            ),
            "status": result.get(
                "status",
                "NOT_REPRODUCED",
            ),
            "bugs": result.get(
                "bugs",
                [],
            ),
        }

        if not final_result["bugs"]:

            final_result.update({
                "observed_behavior": result.get(
                    "observed_behavior"
                ),
                "expected_behavior": result.get(
                    "expected_behavior"
                ),
                "evidence": result.get(
                    "evidence",
                    [],
                ),
                "application_status": result.get(
                    "application_status"
                ),
            })

    except (
        OSError,
        ValueError,
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