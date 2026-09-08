import json
import os
import re
import shlex
import subprocess
import time
import urllib.request


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
        return shlex.split(command, posix=False)

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


def check_application(repository_path, start_command, validation_method):
    if validation_method != "HTTP":
        return None, None, (
            "HTTP validation was not selected by repository_context.json."
        )

    parts = command_parts(start_command)

    if not parts:
        return None, None, "The configured start_command is empty."

    try:
        process = subprocess.Popen(
            parts,
            cwd=repository_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, ValueError) as error:
        return None, None, f"Application could not be started: {error}"

    time.sleep(2)

    try:
        response = urllib.request.urlopen(
            "http://localhost:3000/",
            timeout=5,
        )

        status_code = response.status
        html = response.read().decode(
            "utf-8",
            errors="ignore",
        )

        return process, status_code, html

    except Exception as error:
        stop_process(process)
        return process, None, str(error)


def reproduce_remove_task_bug(repository_path, start_command, validation_method):
    index_file = os.path.join(repository_path, "index.js")
    view_file = os.path.join(repository_path, "views", "index.ejs")

    try:
        index_content = read_file(index_file)
        view_content = read_file(view_file)
    except (OSError, UnicodeError) as error:
        return {
            "status": "NOT_REPRODUCED",
            "observed_behavior": (
                "The current Remove Task reproduction files could not "
                f"be read: {error}"
            ),
            "expected_behavior": (
                "Selecting a task and clicking Remove should submit the "
                "selected task to the configured remove-task behavior."
            ),
            "evidence": [],
            "application_status": None,
        }

    evidence = []

    normalized_view = re.sub(r"\s+", " ", view_content)
    normalized_index = re.sub(r"\s+", " ", index_content)

    has_remove_route = (
        "app.post" in normalized_index
        and "/removetask" in normalized_index
    )

    if has_remove_route:
        evidence.append(
            "The application source contains the POST /removetask endpoint."
        )

    has_add_form = (
        "<form" in normalized_view.lower()
        and 'action ="/addtask"' in normalized_view
    ) or (
        "<form" in normalized_view.lower()
        and 'action="/addtask"' in normalized_view
    )

    if has_add_form:
        evidence.append(
            "The view contains a form with action=\"/addtask\"."
        )

    has_remove_button = (
        "Remove" in normalized_view
        and "formaction" in normalized_view
        and "/removetask" in normalized_view
    )

    if has_remove_button:
        evidence.append(
            "The Remove button uses formaction=\"/removetask\"."
        )

    has_checkboxes = (
        'type="checkbox"' in normalized_view
        and 'name="check"' in normalized_view
    )

    if has_checkboxes:
        evidence.append(
            "The task list contains checkboxes using name=\"check\"."
        )

    form_count = normalized_view.lower().count("<form")

    remove_formaction = (
        'formaction="/removetask"' in normalized_view.lower()
        or 'formaction ="/removetask"' in normalized_view.lower()
        or 'formaction= "/removetask"' in normalized_view.lower()
        or 'formaction = "/removetask"' in normalized_view.lower()
    )

    remove_inside_add_form = (
        has_add_form
        and remove_formaction
        and form_count == 1
    )

    if remove_inside_add_form:
        evidence.append(
            "The Remove button is inside the same /addtask form "
            "that contains the task checkboxes."
        )

    process = None
    status_code = None

    try:
        process, status_code, application_response = check_application(
            repository_path,
            start_command,
            validation_method,
        )

        if status_code == 200:
            evidence.append(
                "The application started successfully and HTTP validation "
                "returned status 200."
            )
        elif validation_method == "HTTP":
            evidence.append(
                f"HTTP validation did not return status 200: "
                f"{application_response}"
            )
        else:
            evidence.append(application_response)

    finally:
        stop_process(process)

    buggy_structure = (
        has_remove_route
        and has_add_form
        and has_remove_button
        and has_checkboxes
        and remove_inside_add_form
    )

    if buggy_structure:
        return {
            "status": "REPRODUCED",
            "observed_behavior": (
                "The Remove button and task-selection checkboxes are inside "
                "the /addtask form, reproducing the reported Remove Task "
                "form-routing bug."
            ),
            "expected_behavior": (
                "Selecting a task and clicking Remove should submit the "
                "selected task to the remove-task endpoint and move it from "
                "the active task list to the completed task list."
            ),
            "evidence": evidence,
            "application_status": status_code,
        }

    return {
        "status": "NOT_REPRODUCED",
        "observed_behavior": (
            "The expected buggy Remove Task form structure was not found "
            "in the current source."
        ),
        "expected_behavior": (
            "Selecting a task and clicking Remove should submit the "
            "selected task to the remove-task endpoint."
        ),
        "evidence": evidence,
        "application_status": status_code,
    }


def build_failure_result(message, technology=None, bug_title=None):
    return {
        "project": None,
        "technology": technology,
        "bug_title": bug_title,
        "status": "NOT_REPRODUCED",
        "observed_behavior": message,
        "expected_behavior": None,
        "evidence": [message],
        "application_status": None,
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

        repository_context = load_json(REPOSITORY_CONTEXT_FILE)

        if not isinstance(repository_context, dict):
            raise ValueError(
                "repository_context.json must contain a JSON object."
            )

        if os.path.exists(BUG_REPORT_FILE):
            bug_report = load_json(BUG_REPORT_FILE)

        if not isinstance(bug_report, dict):
            bug_report = {}

        repository_path = repository_context.get("repository_path")

        if not isinstance(repository_path, str) or not repository_path.strip():
            raise ValueError(
                "repository_context.json does not contain repository_path."
            )

        repository_path = os.path.abspath(repository_path)

        if not os.path.isdir(repository_path):
            raise FileNotFoundError(
                f"repository_path does not exist: {repository_path}"
            )

        execution_strategy = repository_context.get(
            "execution_strategy"
        )

        if not isinstance(execution_strategy, dict):
            raise ValueError(
                "repository_context.json does not contain execution_strategy."
            )

        technology = execution_strategy.get("technology")
        start_command = execution_strategy.get("start_command")
        validation_method = execution_strategy.get("validation_method")

        if not start_command:
            raise ValueError(
                "execution_strategy does not contain start_command."
            )

        print(f"Technology: {technology}")
        print(f"Bug: {bug_report.get('bug_title')}")

        if technology != "Node.js":
            result = build_failure_result(
                (
                    "The current Remove Task reproduction strategy is "
                    f"not implemented for technology: {technology}."
                ),
                technology=technology,
                bug_title=bug_report.get("bug_title"),
            )
        else:
            result = reproduce_remove_task_bug(
                repository_path,
                start_command,
                validation_method,
            )

        final_result = {
            "project": repository_path,
            "technology": technology,
            "bug_title": bug_report.get("bug_title"),
            **result,
        }

    except (OSError, ValueError, json.JSONDecodeError) as error:
        technology = None

        if isinstance(repository_context, dict):
            strategy = repository_context.get("execution_strategy", {})
            if isinstance(strategy, dict):
                technology = strategy.get("technology")

        final_result = build_failure_result(
            str(error),
            technology=technology,
            bug_title=bug_report.get("bug_title"),
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

    print(json.dumps(final_result, indent=2, ensure_ascii=False))
    print(
        "\nReproduction result saved to:",
        REPRODUCTION_RESULT_FILE,
    )


if __name__ == "__main__":
    main()
