"""Replay a validated browser recording against a freshly started app.

    python browser_replay_agent.py --recording <recording.json> --output <result.json>
        [--screenshot <failure.png>] [--trace <trace.zip>]

The recording JSON holds the step list produced by browser_recording.py. This
agent starts the target app from repository_context.json (same start logic
as the bug reproduction agent), runs browser_replay/replay.mjs with the
steps, stops the app, and writes a classified result:

  FAILS_ON_CURRENT_CODE   a recorded assertion failed (bug shown)
  PASSES_ON_CURRENT_CODE  every step and assertion passed
  ERROR                   an action could not run, or the app did not start
  UNAVAILABLE             Playwright / Chromium is not installed
"""

import argparse
import contextlib
import functools
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

import bug_reproduction_agent as runtime
from workflow_artifacts import resolve_artifact_path
from workflow_validation import _kill_process_tree, project_process_environment


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPLAY_DIRECTORY = os.path.join(BASE_DIR, "browser_replay")
REPLAY_SCRIPT = os.path.join(REPLAY_DIRECTORY, "replay.mjs")
REPLAY_TIMEOUT_SECONDS = 180
OUTPUT_TAIL_CHARS = 4000

FAILS_ON_CURRENT_CODE = "FAILS_ON_CURRENT_CODE"
PASSES_ON_CURRENT_CODE = "PASSES_ON_CURRENT_CODE"
ERROR = "ERROR"
UNAVAILABLE = "UNAVAILABLE"


# ------------------------------------------------------------------
# Full-stack apps (separate frontend and backend, e.g. MERN)
# ------------------------------------------------------------------

DATABASE_START_SECONDS = 30
ENV_EXAMPLE_FILES = ("config.env.example", ".env.example", "example.env")
MONGOD_SEARCH_PATTERNS = (
    r"C:\Program Files\MongoDB\Server\*\bin\mongod.exe",
    "/usr/bin/mongod",
    "/usr/local/bin/mongod",
)


def web_components(repository_context):
    """(frontend, backend) components that have a path and a start command."""
    architecture = repository_context.get("architecture")
    components = architecture.get("components") if isinstance(architecture, dict) else None
    found = {}
    for component in components if isinstance(components, list) else []:
        if (
            isinstance(component, dict)
            and component.get("type") in ("frontend", "backend")
            and isinstance(component.get("path"), str)
            and component.get("start_command")
            and component["type"] not in found
        ):
            found[component["type"]] = component
    return found.get("frontend"), found.get("backend")


def uses_mongodb(repository_context):
    architecture = repository_context.get("architecture")
    components = architecture.get("components") if isinstance(architecture, dict) else []
    strategy = repository_context.get("execution_strategy") or {}
    return "mongodb" in str(strategy.get("database", "")).lower() or any(
        isinstance(component, dict)
        and component.get("type") == "database"
        and "mongodb" in str(component.get("technology", "")).lower()
        for component in components or []
    )


def mongodb_uri_variables(backend_directory):
    """Names of variables holding a MongoDB URI, read from example env files
    only (never from the real .env / config.env, which may hold secrets)."""
    names = []
    for filename in ENV_EXAMPLE_FILES:
        path = os.path.join(backend_directory, filename)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*['\"]?mongodb(?:\+srv)?://", line)
                if match and match.group(1) not in names:
                    names.append(match.group(1))
    return names


def find_mongod():
    found = shutil.which("mongod")
    if found:
        return found
    for pattern in MONGOD_SEARCH_PATTERNS:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[-1]
    return None


def free_local_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def port_in_use(url):
    parts = urlsplit(url)
    try:
        with socket.create_connection((parts.hostname, parts.port or 80), timeout=0.25):
            return True
    except OSError:
        return False


@contextlib.contextmanager
def isolated_mongodb():
    """A throwaway MongoDB (temporary data folder, free port) for one replay,
    so replays never read or change the user's own database and every
    replay starts from the same empty state."""
    mongod = find_mongod()
    if not mongod:
        raise RuntimeError("MongoDB (mongod) was not found, so the database cannot be isolated for the replay.")
    data_directory = tempfile.mkdtemp(prefix="replay-mongodb-")
    port = free_local_port()
    # Same console group on Windows, so Ctrl+C in the terminal also stops
    # mongod instead of leaving it (and its data folder) behind.
    flags = {} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(
        [mongod, "--dbpath", data_directory, "--port", str(port), "--bind_ip", "127.0.0.1", "--quiet"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **flags,
    )
    try:
        deadline = time.monotonic() + DATABASE_START_SECONDS
        while not port_in_use(f"http://127.0.0.1:{port}"):
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("The temporary MongoDB did not start.")
            time.sleep(0.25)
        yield f"mongodb://127.0.0.1:{port}/"
    finally:
        if process.poll() is None:
            _kill_process_tree(process)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
        shutil.rmtree(data_directory, ignore_errors=True)


def vite_command(frontend_directory, port):
    """Run Vite's own script directly (no npm wrapper, so stopping the
    process stops the server) on a free port."""
    try:
        with open(os.path.join(frontend_directory, "package.json"), "r", encoding="utf-8") as handle:
            scripts = json.load(handle).get("scripts") or {}
    except (OSError, ValueError):
        return None, "The frontend package.json could not be read."
    script = str(scripts.get("dev") or scripts.get("start") or "")
    if not script.strip().startswith("vite"):
        return None, "Only Vite frontends can be replayed for now."
    vite = os.path.join(frontend_directory, "node_modules", "vite", "bin", "vite.js")
    if not os.path.isfile(vite):
        return None, "Vite is not installed in the frontend (run npm install there first)."
    return ["node", vite, "--port", str(port), "--strictPort", "--host", "127.0.0.1"], None


class AppStartError(RuntimeError):
    """The app (or its isolated database) could not be started."""

    def __init__(self, summary, app_error=None):
        super().__init__(summary)
        self.summary = summary
        self.app_error = app_error


@contextlib.contextmanager
def full_stack_app(repository_path, repository_context, frontend, backend, start, stop, database):
    """Start the isolated database, the backend and the frontend; stop them
    (and delete the database) when the block ends. Yields the URLs."""
    strategy = repository_context.get("execution_strategy") or {}
    backend_directory = os.path.join(repository_path, backend["path"])
    frontend_directory = os.path.join(repository_path, frontend["path"])
    backend_url = runtime.normalize_base_url(backend.get("base_url") or strategy.get("backend_base_url"))
    if not backend_url:
        raise AppStartError("The backend URL is not known.")
    if port_in_use(backend_url):
        raise AppStartError(
            f"{backend_url} is already in use. The frontend sends its requests there, "
            "so stop your own copy of the backend, then try again."
        )
    frontend_url = f"http://127.0.0.1:{free_local_port()}"
    frontend_command, error = vite_command(frontend_directory, urlsplit(frontend_url).port)
    if error:
        raise AppStartError(error)

    backend_environment = {}
    with contextlib.ExitStack() as services:
        if uses_mongodb(repository_context):
            variables = mongodb_uri_variables(backend_directory)
            if not variables:
                raise AppStartError(
                    "The MongoDB connection setting could not be found in an example env file, "
                    "so the app would use your real database. Refused."
                )
            try:
                uri = services.enter_context(database())
            except RuntimeError as database_error:
                raise AppStartError(str(database_error)) from None
            backend_environment = {name: uri for name in variables}

        backend_process, error = start(
            repository_path,
            backend.get("start_command") or strategy.get("backend_start_command"),
            base_url=backend_url,
            readiness_path="/",
            working_directory=backend_directory,
            environment={},
            # Not allowlisted names (e.g. ATLAS_URI): set them explicitly, or
            # the backend would silently fall back to its real database.
            explicit_environment=backend_environment,
        )
        if backend_process is None:
            raise AppStartError("The backend could not be started.", str(error)[-OUTPUT_TAIL_CHARS:])
        services.callback(stop, backend_process)

        frontend_process, error = start(
            repository_path,
            frontend_command,
            base_url=frontend_url,
            readiness_path="/",
            working_directory=frontend_directory,
            environment={},
        )
        if frontend_process is None:
            raise AppStartError("The frontend could not be started.", str(error)[-OUTPUT_TAIL_CHARS:])
        services.callback(stop, frontend_process)

        yield {
            "frontend_url": frontend_url,
            "backend_url": backend_url,
            "isolated_database": bool(backend_environment),
        }


def replay_full_stack(repository_path, repository_context, frontend, backend, steps_path,
                      screenshot_path, start, stop, replay, database):
    try:
        with full_stack_app(repository_path, repository_context, frontend, backend, start, stop, database) as app:
            result = replay(steps_path, app["frontend_url"], screenshot_path)
    except AppStartError as error:
        failure = {"classification": ERROR, "summary": error.summary}
        if error.app_error:
            failure["app_error"] = error.app_error
        return failure

    classification = classify_replay(result)
    return {
        "classification": classification,
        "summary": summarize(result, classification),
        "base_url": app["frontend_url"],
        "backend_url": app["backend_url"],
        "isolated_database": app["isolated_database"],
        "replay": result,
    }


def classify_replay(replay):
    if not isinstance(replay, dict):
        return ERROR
    if replay.get("status") == "UNAVAILABLE":
        return UNAVAILABLE
    if replay.get("status") != "COMPLETED":
        return ERROR
    results = replay.get("results")
    if not isinstance(results, list):
        return ERROR
    if replay.get("failed_index") is None:
        # Every step must have run and passed; a short list is not a pass.
        if len(results) == replay.get("steps_total") and all(
            isinstance(item, dict) and item.get("status") == "passed" for item in results
        ):
            return PASSES_ON_CURRENT_CODE
        return ERROR
    if replay.get("failure_type") == "assertion":
        return FAILS_ON_CURRENT_CODE
    return ERROR


def parse_runner_output(stdout):
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                return None
            return value if isinstance(value, dict) else None
    return None


def run_replay(steps_path, base_url, screenshot_path=None, runner=subprocess.run, trace_path=None):
    command = ["node", REPLAY_SCRIPT, steps_path, base_url]
    if screenshot_path or trace_path:
        command.append(screenshot_path or "")
    if trace_path:
        command.append(trace_path)
    try:
        completed = runner(
            command,
            cwd=REPLAY_DIRECTORY,
            env=project_process_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=REPLAY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {"status": "ERROR", "reason": f"The replay did not finish within {REPLAY_TIMEOUT_SECONDS} seconds."}
    except OSError as error:
        return {"status": "ERROR", "reason": f"Node.js could not run the replay: {error}"}
    replay = parse_runner_output(completed.stdout)
    if replay is None:
        return {
            "status": "ERROR",
            "reason": "The replay runner did not report a result.",
            "output_tail": ((completed.stdout or "") + (completed.stderr or ""))[-OUTPUT_TAIL_CHARS:],
        }
    return replay


def summarize(replay, classification):
    """One sentence for people: what happened at the step that decided it."""
    if classification == UNAVAILABLE:
        return replay.get("reason") or "Playwright is not installed."
    if classification == PASSES_ON_CURRENT_CODE:
        return "Every recorded step and assertion passed."
    results = replay.get("results") if isinstance(replay.get("results"), list) else []
    failed = results[-1] if results else None
    if isinstance(failed, dict) and failed.get("status") in ("failed", "error"):
        kind = "Assertion failed" if failed["status"] == "failed" else "Step could not run"
        return f"{kind} at line {failed.get('line')}: {failed.get('source')} ({failed.get('message')})"
    return replay.get("reason") or "The replay did not complete."


def replay_recording(recording, repository_context, steps_path, screenshot_path=None,
                     start=None, stop=None, replay=None, database=None):
    start = start or runtime.start_application
    stop = stop or runtime.stop_process
    replay = replay or run_replay
    database = database or isolated_mongodb

    repository_path = os.path.abspath(str(repository_context.get("repository_path") or ""))
    if not os.path.isdir(repository_path):
        return {"classification": ERROR, "summary": "The repository path is not available."}
    strategy = repository_context.get("execution_strategy")
    if not isinstance(strategy, dict):
        return {"classification": ERROR, "summary": "The repository has no execution strategy."}

    frontend, backend = web_components(repository_context)
    if frontend and backend:
        return replay_full_stack(
            repository_path, repository_context, frontend, backend, steps_path,
            screenshot_path, start, stop, replay, database,
        )

    configuration = runtime.runtime_configuration(
        repository_path,
        repository_context,
        strategy,
        {"base_url": recording.get("recorded_origin")},
    )
    if not configuration.get("start_command"):
        return {"classification": ERROR, "summary": "No start command is known for this app."}
    base_url, environment, error = runtime.isolate_local_endpoint(
        repository_path, repository_context, configuration["base_url"]
    )
    if error:
        return {
            "classification": ERROR,
            "summary": error + " Stop the copy of the app you recorded against, then upload again.",
        }

    process, error = start(
        repository_path,
        configuration["start_command"],
        base_url=base_url,
        readiness_path="/",
        working_directory=configuration["working_directory"],
        environment=environment,
    )
    if process is None:
        return {"classification": ERROR, "summary": "The app could not be started.", "app_error": str(error)[-OUTPUT_TAIL_CHARS:]}
    try:
        result = replay(steps_path, base_url, screenshot_path)
    finally:
        stop(process)
    classification = classify_replay(result)
    return {
        "classification": classification,
        "summary": summarize(result, classification),
        "base_url": base_url,
        "replay": result,
    }


def analyse_repository(repository_path):
    """Run the repository agent on a local folder; return its context."""
    output_directory = tempfile.mkdtemp(prefix="record-context-")
    environment = dict(os.environ, LOCAL_REPOSITORY_PATH=repository_path, WORKFLOW_ARTIFACT_DIR=output_directory)
    subprocess.run(
        [sys.executable, os.path.join(BASE_DIR, "repository_agent.py")],
        cwd=BASE_DIR, env=environment, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300, check=False,
    )
    try:
        with open(os.path.join(output_directory, "repository_context.json"), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None
    finally:
        shutil.rmtree(output_directory, ignore_errors=True)


def serve_for_recording(repository_path, wait=input, start=None, stop=None, database=None, context=None):
    """Record mode: start a full-stack app with an isolated database so the
    user can record a bug with playwright codegen without touching real data."""
    repository_path = os.path.abspath(repository_path)
    context = context or analyse_repository(repository_path)
    if not isinstance(context, dict):
        print("The repository could not be analysed.")
        return 1
    frontend, backend = web_components(context)
    if not (frontend and backend):
        print("This app has no separate frontend and backend. Start it the usual way and record against it.")
        return 1
    try:
        with full_stack_app(
            repository_path, context, frontend, backend,
            start or runtime.start_application, stop or runtime.stop_process, database or isolated_mongodb,
        ) as app:
            print()
            print("=" * 70)
            print("READY TO RECORD")
            print(f"  App URL:   {app['frontend_url']}")
            print(f"  Database:  {'temporary (deleted when you stop)' if app['isolated_database'] else 'none'}")
            print("  In another terminal run:")
            print(f"    npx playwright codegen {app['frontend_url']}")
            print("=" * 70)
            wait("Press Enter here when you have finished recording, to stop the app... ")
    except AppStartError as error:
        print(error.summary)
        if error.app_error:
            print(error.app_error)
        return 1
    except KeyboardInterrupt:
        pass
    print("App stopped. The temporary database was deleted.")
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", metavar="REPOSITORY", help="record mode: start the app with an isolated database")
    parser.add_argument("--recording")
    parser.add_argument("--output")
    parser.add_argument("--screenshot")
    parser.add_argument("--trace", help="save a Playwright trace (.zip) of the replay")
    arguments = parser.parse_args(argv)
    if arguments.serve:
        return serve_for_recording(arguments.serve)
    if not (arguments.recording and arguments.output):
        parser.error("--recording and --output are required (or use --serve)")

    print("\n===== BROWSER RECORDING REPLAY =====")
    try:
        with open(arguments.recording, "r", encoding="utf-8") as handle:
            recording = json.load(handle)
        with open(resolve_artifact_path("repository_context.json", BASE_DIR), "r", encoding="utf-8") as handle:
            repository_context = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        result = {"classification": ERROR, "summary": f"Inputs could not be read: {error}"}
    else:
        steps = recording.get("steps") if isinstance(recording, dict) else None
        if not isinstance(steps, list) or not steps or not isinstance(repository_context, dict):
            result = {"classification": ERROR, "summary": "The recording has no validated steps."}
        else:
            steps_path = os.path.splitext(arguments.output)[0] + ".steps.json"
            with open(steps_path, "w", encoding="utf-8") as handle:
                json.dump(steps, handle)
            replay = functools.partial(run_replay, trace_path=arguments.trace) if arguments.trace else None
            result = replay_recording(
                recording, repository_context, steps_path, arguments.screenshot, replay=replay,
            )

    with open(arguments.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
    print(f"Classification: {result['classification']}")
    print(result["summary"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
