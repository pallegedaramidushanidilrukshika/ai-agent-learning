import os
import re
import signal
import subprocess
import tempfile
import uuid


# Tests and builds come from the target repository (and AI-written tests),
# so they may never finish, for example a test that starts a server and
# never stops it. Without a limit, the stage and Stop Safely would hang.
TEST_COMMAND_TIMEOUT_SECONDS = 300
BUILD_COMMAND_TIMEOUT_SECONDS = 600
TIMEOUT_RETURN_CODE = 124


def _kill_process_tree(process):
    if os.name == "nt":
        # /T also stops grandchildren such as a server the test started.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()


def run_bounded(command, timeout=TEST_COMMAND_TIMEOUT_SECONDS, popen=subprocess.Popen, **kwargs):
    """subprocess.run with a time limit that stops the whole process tree.

    Accepts the same keyword arguments as the subprocess.run calls in the
    workflow (cwd, env, capture_output, text, encoding, errors, check=False).
    On timeout it returns a failed CompletedProcess (return code 124,
    timed_out=True) whose stderr says so; it never looks like a pass.
    """
    kwargs.pop("check", None)
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    # Windows: taskkill /T follows the parent/child tree, so no separate
    # process group is needed, and staying in the console's group means
    # Ctrl+C in the terminal still stops the test. POSIX needs a session
    # so the whole group can be killed.
    if os.name != "nt":
        kwargs["start_new_session"] = True
    process = popen(command, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        text_mode = bool(kwargs.get("text") or kwargs.get("encoding"))
        note = f"\nThe command did not finish within {timeout} seconds and was stopped."
        stdout = stdout or ("" if text_mode else b"")
        stderr = (stderr or ("" if text_mode else b"")) + (note if text_mode else note.encode())
        completed = subprocess.CompletedProcess(command, TIMEOUT_RETURN_CODE, stdout, stderr)
        completed.timed_out = True
        return completed
    completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    completed.timed_out = False
    return completed


PROJECT_ENV_ALLOWLIST = {
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "VIRTUAL_ENV",
    "JAVA_HOME",
    "JDK_HOME",
    "DOTNET_ROOT",
    "DOTNET_ROOT_X64",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "NODE_ENV",
    "PORT",
    "HOST",
    "REPRODUCTION_BASE_URL",
}


def project_process_environment(source=None, overrides=None):
    source = os.environ if source is None else source
    environment = {
        name: source[name]
        for name in PROJECT_ENV_ALLOWLIST
        if name in source
    }
    if isinstance(overrides, dict):
        environment.update({
            name: overrides[name]
            for name in PROJECT_ENV_ALLOWLIST
            if name in overrides
        })
    npm_config_directory = os.path.join(
        tempfile.gettempdir(),
        f"ai-agent-workflow-npm-{uuid.uuid4().hex}",
    )
    environment.update({
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
        "npm_config_offline": "true",
        "NPM_CONFIG_OFFLINE": "true",
        "NPM_CONFIG_USERCONFIG": os.path.join(
            npm_config_directory,
            "user.npmrc",
        ),
        "NPM_CONFIG_GLOBALCONFIG": os.path.join(
            npm_config_directory,
            "global.npmrc",
        ),
    })
    return environment


def counts_are_consistent(total, passed, failed, skipped):
    values = (total, passed, failed, skipped)
    if any(type(value) is not int or value < 0 for value in values):
        return False
    return total > 0 and passed + failed + skipped == total


def test_result_has_success_evidence(result):
    if not isinstance(result, dict):
        return False
    return (
        result.get("status") in ("PASSED", "PASS", "SUCCESS")
        and result.get("return_code") == 0
        and counts_are_consistent(
            result.get("tests"),
            result.get("passed"),
            result.get("failed"),
            result.get("skipped", 0),
        )
        and result["passed"] > 0
        and result["failed"] == 0
    )


def build_result_has_success_evidence(result, expected_components):
    if not isinstance(result, dict) or result.get("return_code") != 0:
        return False
    if result.get("status") == "SKIPPED":
        return (
            expected_components == 0
            and isinstance(result.get("message"), str)
            and bool(result["message"].strip())
        )
    components = result.get("components")
    return (
        result.get("status") in ("PASSED", "PASS", "SUCCESS")
        and isinstance(components, list)
        and len(components) == expected_components
        and all(
            isinstance(component, dict)
            and component.get("status") in ("PASSED", "PASS", "SUCCESS")
            and component.get("return_code") == 0
            and isinstance(component.get("command"), str)
            and bool(component["command"].strip())
            for component in components
        )
    )


def runtime_result_has_success_evidence(result):
    if not isinstance(result, dict) or result.get("return_code") != 0:
        return False
    if result.get("status") == "SKIPPED":
        return isinstance(result.get("message"), str) and bool(result["message"].strip())
    if result.get("status") not in ("PASSED", "PASS", "SUCCESS", "FIXED"):
        return False

    checks = result.get("checks")
    if isinstance(checks, list) and checks and all(
        isinstance(check, dict)
        and check.get("status") in ("PASSED", "PASS", "SUCCESS")
        and check.get("return_code") == 0
        and isinstance(check.get("name"), str)
        and bool(check["name"].strip())
        and isinstance(check.get("command"), list)
        and bool(check["command"])
        for check in checks
    ):
        return True

    observed = result.get("observed_post_fix_result")
    evidence = (
        observed.get("bugs", [{}])[0].get("evidence", [])
        if isinstance(observed, dict)
        and isinstance(observed.get("bugs"), list)
        and observed.get("bugs")
        and isinstance(observed["bugs"][0], dict)
        else []
    )
    action_status = result.get("observed_action_http_status")
    return (
        result.get("expected_post_fix_status") == "NOT_REPRODUCED"
        and isinstance(observed, dict)
        and observed.get("status") == "NOT_REPRODUCED"
        and type(action_status) is int
        and 400 <= action_status < 500
        and any(
            isinstance(item, str)
            and "No newly persisted record containing the invalid request fields" in item
            for item in evidence
        )
    )


def _complete_test_counts(total, passed, failed, skipped):
    return (total, passed, failed, skipped) if counts_are_consistent(
        total, passed, failed, skipped
    ) else None


def _placeholder(value):
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return normalized in {
        "unknown",
        "tbd",
        "todo",
        "to be determined",
        "not known",
        "not determined",
        "none",
        "n a",
        "na",
        "insufficient evidence",
        "not enough evidence",
    }


def diagnosis_validation_errors(diagnosis, manual_reproduction=None):
    if not isinstance(diagnosis, dict):
        return ["Diagnosis must be a JSON object."]

    errors = []
    text_fields = (
        "bug_title",
        "project_type",
        "failure_analysis",
        "root_cause",
        "proposed_fix",
    )
    for field in text_fields:
        value = diagnosis.get(field)
        if not isinstance(value, str) or not value.strip() or _placeholder(value):
            errors.append(f"Diagnosis field '{field}' must be non-empty actionable text.")

    list_fields = ("affected_files", "evidence", "verification_plan")
    for field in list_fields:
        value = diagnosis.get(field)
        if (
            not isinstance(value, list)
            or not value
            or any(
                not isinstance(item, str) or not item.strip() or _placeholder(item)
                for item in value
            )
        ):
            errors.append(f"Diagnosis field '{field}' must be a non-empty list of actionable text.")

    root_cause = diagnosis.get("root_cause")
    if isinstance(root_cause, str):
        normalized_root_cause = root_cause.strip().lower()
        insufficient_evidence_phrases = (
            "insufficient evidence",
            "not enough evidence",
            "unable to determine",
            "cannot determine",
            "could not determine",
        )
        if any(phrase in normalized_root_cause for phrase in insufficient_evidence_phrases):
            errors.append("Diagnosis does not establish a root cause.")

    proposed_fix = diagnosis.get("proposed_fix")
    if isinstance(proposed_fix, str):
        normalized_fix = proposed_fix.strip().lower()
        non_fixes = (
            "none at this time",
            "no fix at this time",
            "further investigation is required",
            "insufficient evidence",
        )
        if normalized_fix in non_fixes or any(
            phrase in normalized_fix for phrase in non_fixes
        ):
            errors.append("Diagnosis does not contain an actionable proposed fix.")

    if isinstance(manual_reproduction, dict):
        expected = manual_reproduction.get("expected_behavior", "")
        if isinstance(expected, str) and isinstance(proposed_fix, str):
            expects_preservation = (
                re.search(r"\b(separate|distinct|retain|preserve|keep|remain)\b", expected, re.I)
                and re.search(r"\b(record|records|message|messages|item|items|entry|entries|data)\b", expected, re.I)
            )
            proposes_removal = re.search(
                r"\b(delete|remove|purge|discard|overwrite)\b"
                r".{0,160}\b(previous|prior|existing|earlier|older|old)\b",
                proposed_fix,
                re.I | re.S,
            )
            if expects_preservation and proposes_removal:
                errors.append(
                    "Proposed fix conflicts with the manual expected behavior "
                    "by removing previously retained data."
                )

    return errors


def parse_test_summary(output_text):
    text = output_text if isinstance(output_text, str) else ""

    # node:test prints "ℹ tests 3" with the spec reporter (a terminal) and
    # "# tests 3" with the TAP reporter (output piped to the workflow).
    node_summary = {
        key: re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        for key, pattern in {
            "tests": r"^\s*(?:[ℹ#]\s*)?tests?\s+(\d+)\s*$",
            "passed": r"^\s*(?:[ℹ#]\s*)?pass(?:ed)?\s+(\d+)\s*$",
            "failed": r"^\s*(?:[ℹ#]\s*)?fail(?:ed|ures)?\s+(\d+)\s*$",
            "skipped": r"^\s*(?:[ℹ#]\s*)?skipped\s+(\d+)\s*$",
        }.items()
    }
    if any(node_summary.values()):
        if not all(node_summary[key] for key in ("tests", "passed", "failed")):
            return None, None, None, None
        todo_match = re.search(
            r"^\s*(?:[ℹ#]\s*)?todo\s+(\d+)\s*$",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        skipped = int(node_summary["skipped"].group(1)) if node_summary["skipped"] else 0
        if todo_match:
            skipped += int(todo_match.group(1))
        counts = _complete_test_counts(
            int(node_summary["tests"].group(1)),
            int(node_summary["passed"].group(1)),
            int(node_summary["failed"].group(1)),
            skipped,
        )
        if counts is not None:
            return counts
        return None, None, None, None

    jest = re.search(r"^\s*Tests:\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
    if jest:
        jest_counts = {}
        for count, label in re.findall(
            r"(\d+)\s+(failed|passed|skipped|total)\b",
            jest.group(1),
            re.IGNORECASE,
        ):
            normalized_label = label.lower()
            if normalized_label in jest_counts:
                return None, None, None, None
            jest_counts[normalized_label] = int(count)
        if "total" in jest_counts:
            counts = _complete_test_counts(
                jest_counts["total"],
                jest_counts.get("passed", 0),
                jest_counts.get("failed", 0),
                jest_counts.get("skipped", 0),
            )
            if counts is not None:
                return counts
        return None, None, None, None

    jasmine = re.search(
        r"Executed\s+(\d+)\s+of\s+(\d+)\s+SUCCESS(?:\s*\((\d+)\s+SKIPPED\))?",
        text,
        re.IGNORECASE,
    )
    if jasmine:
        passed = int(jasmine.group(1))
        total = int(jasmine.group(2))
        skipped = int(jasmine.group(3) or 0)
        counts = _complete_test_counts(total, passed, total - passed - skipped, skipped)
        if counts is not None:
            return counts

    pytest_summary = re.search(
        r"^\s*(.*?)\s+in\s+[\d:.]+\s*(?:s|seconds?)\s*$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if pytest_summary:
        pytest_counts = {"passed": 0, "failed": 0, "skipped": 0}
        for count, result in re.findall(
            r"\b(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed)\b",
            pytest_summary.group(1),
            re.IGNORECASE,
        ):
            normalized = result.lower()
            if normalized in ("passed", "xpassed"):
                pytest_counts["passed"] += int(count)
            elif normalized in ("skipped", "xfailed"):
                pytest_counts["skipped"] += int(count)
            else:
                pytest_counts["failed"] += int(count)
        pytest_total = sum(pytest_counts.values())
        counts = _complete_test_counts(
            pytest_total,
            pytest_counts["passed"],
            pytest_counts["failed"],
            pytest_counts["skipped"],
        )
        if counts is not None:
            return counts
        return None, None, None, None

    maven = re.search(
        r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)",
        text,
        re.IGNORECASE,
    )
    if maven:
        total, failed, errors, skipped = map(int, maven.groups())
        counts = _complete_test_counts(
            total,
            total - failed - errors - skipped,
            failed + errors,
            skipped,
        )
        if counts is not None:
            return counts

    dotnet = re.search(
        r"(?:Passed|Failed)!.*?Failed:\s*(\d+),\s*Passed:\s*(\d+),"
        r"\s*Skipped:\s*(\d+),\s*Total:\s*(\d+)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if dotnet:
        failed, passed, skipped, total = map(int, dotnet.groups())
        counts = _complete_test_counts(total, passed, failed, skipped)
        if counts is not None:
            return counts

    unittest = re.search(r"Ran\s+(\d+)\s+tests?", text, re.IGNORECASE)
    if unittest:
        total = int(unittest.group(1))
        summary = re.search(r"(?:FAILED|OK)\s*\(([^)]*)\)", text, re.IGNORECASE)
        summary_values = {
            name.lower(): int(count)
            for name, count in re.findall(
                r"(failures?|errors?|skipped)\s*=\s*(\d+)",
                summary.group(1) if summary else "",
                re.IGNORECASE,
            )
        }
        failed = summary_values.get("failure", summary_values.get("failures", 0))
        failed += summary_values.get("error", summary_values.get("errors", 0))
        skipped = summary_values.get("skipped", 0)
        passed = total - failed - skipped
        counts = _complete_test_counts(total, passed, failed, skipped)
        if counts is not None:
            return counts

    mocha = re.search(
        r"(\d+)\s+passing(?:\s+\([^)]*\))?"
        r"(?:[\s\S]*?(\d+)\s+failing)?"
        r"(?:[\s\S]*?(\d+)\s+pending)?",
        text,
        re.IGNORECASE,
    )
    if mocha:
        passed = int(mocha.group(1))
        failed = int(mocha.group(2) or 0)
        skipped = int(mocha.group(3) or 0)
        counts = _complete_test_counts(passed + failed + skipped, passed, failed, skipped)
        if counts is not None:
            return counts

    return None, None, None, None


# ------------------------------------------------------------
# Sanity checks for AI-written source changes
# ------------------------------------------------------------

NODE_BUILTIN_MODULES = {
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console",
    "constants", "crypto", "dgram", "diagnostics_channel", "dns", "domain",
    "events", "fs", "http", "http2", "https", "inspector", "module", "net",
    "os", "path", "perf_hooks", "process", "punycode", "querystring",
    "readline", "repl", "stream", "string_decoder", "sys", "timers", "tls",
    "trace_events", "tty", "url", "util", "v8", "vm", "wasi",
    "worker_threads", "zlib", "test",
}
_JS_MODULE_PATTERN = re.compile(
    r"""(?:require\(\s*|import\s*\(\s*|\bfrom\s+|^\s*import\s+)["']([^"']+)["']""",
    re.MULTILINE,
)


def _structured_document(text, suffix):
    """Parse YAML or JSON text. Unknown YAML tags (e.g. CloudFormation !Ref) are allowed."""
    import json

    if suffix == ".json":
        return json.loads(text)
    import yaml

    class TolerantLoader(yaml.SafeLoader):
        pass

    TolerantLoader.add_multi_constructor("!", lambda loader, suffix_, node: None)
    return yaml.load(text, Loader=TolerantLoader)


def _package_name(specifier):
    if specifier.startswith("node:"):
        return None
    if specifier.startswith((".", "/")) or re.match(r"^[A-Za-z]:[\\/]", specifier):
        return None
    parts = specifier.split("/")
    name = "/".join(parts[:2]) if specifier.startswith("@") else parts[0]
    return None if name in NODE_BUILTIN_MODULES else name


def _declared_packages(file_path, repository_root):
    import json

    directory = os.path.dirname(os.path.abspath(file_path))
    root = os.path.abspath(repository_root)
    declared = set()
    while True:
        manifest = os.path.join(directory, "package.json")
        if os.path.isfile(manifest):
            try:
                with open(manifest, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                for section in (
                    "dependencies", "devDependencies",
                    "peerDependencies", "optionalDependencies",
                ):
                    if isinstance(data.get(section), dict):
                        declared.update(data[section])
            except (OSError, ValueError):
                pass
        if directory == root or os.path.dirname(directory) == directory:
            break
        directory = os.path.dirname(directory)
    return declared


def source_change_errors(file_path, original, replacement, repository_root, node_check=None):
    """Return reasons an AI-written replacement must not be saved; empty means acceptable.

    Catches changes that cannot be correct whatever the bug is: an empty file,
    a YAML/JSON file replaced by something unparsable or with its top-level
    sections removed, JavaScript that does not parse, or a new package import
    that the project does not declare.
    """
    if not isinstance(replacement, str) or not replacement.strip():
        return ["The replacement content is empty."]
    errors = []
    suffix = os.path.splitext(file_path)[1].lower()

    if suffix in (".yml", ".yaml", ".json"):
        try:
            new_document = _structured_document(replacement, suffix)
        except Exception as error:
            return [f"The replacement is not valid {suffix[1:].upper()}: {str(error)[:200]}"]
        if isinstance(original, str):
            try:
                old_document = _structured_document(original, suffix)
            except Exception:
                old_document = None
            if isinstance(old_document, dict):
                if not isinstance(new_document, dict):
                    errors.append(
                        "The replacement changes the file's structure: the original "
                        "is a mapping of settings but the replacement is not."
                    )
                else:
                    missing = [key for key in old_document if key not in new_document]
                    if missing:
                        errors.append(
                            "The replacement removes top-level sections: "
                            + ", ".join(str(key) for key in missing[:10])
                        )

    if suffix in (".js", ".cjs", ".mjs"):
        check = node_check or _node_syntax_errors
        syntax_error = check(replacement, suffix)
        if syntax_error:
            errors.append(f"The replacement is not valid JavaScript: {syntax_error[:300]}")
        old_packages = {
            name for name in map(_package_name, _JS_MODULE_PATTERN.findall(original or "")) if name
        }
        new_packages = {
            name for name in map(_package_name, _JS_MODULE_PATTERN.findall(replacement)) if name
        }
        undeclared = sorted(
            new_packages - old_packages - _declared_packages(
                os.path.join(repository_root, file_path), repository_root
            )
        )
        if undeclared:
            errors.append(
                "The replacement imports packages the project does not declare: "
                + ", ".join(undeclared)
                + ". Use only installed dependencies."
            )
    return errors


def _node_syntax_errors(source, suffix):
    import subprocess

    directory = tempfile.mkdtemp(prefix="ai-agent-syntax-")
    path = os.path.join(directory, f"check{suffix}")
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source)
        try:
            process = subprocess.run(
                ["node", "--check", path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                env=project_process_environment(),
            )
        except (OSError, subprocess.SubprocessError):
            return None  # Node unavailable: skip this check, tests still run later.
        if process.returncode == 0:
            return None
        return (process.stderr or process.stdout or "node --check failed").strip()
    finally:
        try:
            os.remove(path)
            os.rmdir(directory)
        except OSError:
            pass