import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


DEMO_BUG_REPORT_NAME = "bug_report_level3_demo.json"
DEMO_TEST_NAME = "websocket_retention.demo.test.js"
DEMO_TEST_TITLE = "Level 3 demo retains each accepted message"


DEMO_TEST_SOURCE = r'''const mockDynamoSend = jest.fn();
const mockManagementSend = jest.fn();
const keyAttributes = JSON.parse(process.env.DEMO_DYNAMODB_KEY_ATTRIBUTES || "[]");
const persistedRows = new Map();

jest.mock("@aws-sdk/client-dynamodb", () => ({
  DynamoDBClient: jest.fn().mockImplementation(() => ({ send: mockDynamoSend })),
  PutItemCommand: jest.fn().mockImplementation((input) => ({ input })),
}));

jest.mock("@aws-sdk/client-apigatewaymanagementapi", () => ({
  ApiGatewayManagementApiClient: jest.fn().mockImplementation(() => ({
    send: mockManagementSend,
  })),
  PostToConnectionCommand: jest.fn().mockImplementation((input) => ({ input })),
}));

process.env.TABLE_NAME = "local-demo-table";

const { message } = require("../src/websocket");

const makeEvent = (requestId, messageText) => ({
  body: JSON.stringify({ action: "message", requestId, message: messageText }),
  requestContext: {
    connectionId: "demo-connection",
    domainName: "local.execute-api.invalid",
    stage: "demo",
  },
});

beforeEach(() => {
  jest.clearAllMocks();
  persistedRows.clear();
  mockManagementSend.mockResolvedValue({});
  mockDynamoSend.mockImplementation(async (command) => {
    const item = command.input.Item;
    const key = keyAttributes.map((name) => item[name]?.S);
    if (key.length === 0 || key.some((value) => !value)) {
      throw new Error("PutItem is missing a configured DynamoDB key attribute.");
    }
    persistedRows.set(JSON.stringify(key), item);
    return {};
  });
});

test("Level 3 demo retains each accepted message", async () => {
  const first = await message(makeEvent("request-1", "first message"));
  const second = await message(makeEvent("request-2", "second message"));

  expect(first.statusCode).toBe(200);
  expect(second.statusCode).toBe(200);
  expect(mockDynamoSend).toHaveBeenCalledTimes(2);
  expect(persistedRows.size).toBe(2);
  expect([...persistedRows.values()].map((item) => item.message.S)).toEqual([
    "first message",
    "second message",
  ]);
});
'''


def save_json(path, value):
    with open(path, "w", encoding="utf-8") as file:
        json.dump(value, file, indent=2, ensure_ascii=False)


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as file:
            value = json.load(file)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def validate_groq_diagnosis(diagnosis):
    field_types = {
        "bug_title": str,
        "project_type": str,
        "failure_analysis": str,
        "root_cause": str,
        "affected_files": list,
        "evidence": list,
        "proposed_fix": str,
        "verification_plan": list,
    }
    errors = []
    if not isinstance(diagnosis, dict):
        return {"valid": False, "errors": ["Diagnosis must be a JSON object."]}

    for field, expected_type in field_types.items():
        value = diagnosis.get(field)
        if not isinstance(value, expected_type):
            errors.append(f"{field} must be {expected_type.__name__}.")
        elif expected_type is str and not value.strip():
            errors.append(f"{field} must not be empty.")
        elif expected_type is list and (
            not value or any(not isinstance(item, str) or not item.strip() for item in value)
        ):
            errors.append(f"{field} must be a non-empty list of non-empty strings.")

    return {"valid": not errors, "errors": errors}


def run_groq_diagnosis(
    repository_context,
    disposable_repository,
    reproduction_result,
    bug_report_path,
    artifact_directory,
    command_runner=None,
):
    diagnosis_directory = os.path.join(artifact_directory, "ai_diagnosis")
    os.makedirs(diagnosis_directory, exist_ok=True)
    diagnosis_context = {
        **repository_context,
        "repository_path": disposable_repository,
    }
    save_json(
        os.path.join(diagnosis_directory, "repository_context.json"),
        diagnosis_context,
    )
    save_json(
        os.path.join(diagnosis_directory, "reproduction_result.json"),
        reproduction_result,
    )

    diagnosis_script = Path(__file__).resolve().with_name("bug_diagnosis_agent.py")
    command = [
        sys.executable,
        os.fspath(diagnosis_script),
        bug_report_path,
    ]
    environment = os.environ.copy()
    environment["WORKFLOW_ARTIFACT_DIR"] = diagnosis_directory
    environment["BUG_REPORT_FILE"] = bug_report_path

    process = None
    process_error = None
    try:
        process = (command_runner or subprocess.run)(
            command,
            cwd=os.fspath(diagnosis_script.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        process_error = type(error).__name__

    model_call = read_json(
        os.path.join(diagnosis_directory, "diagnosis_model_call.json")
    )
    if not model_call:
        model_call = {
            "attempted": False,
            "succeeded": False,
            "provider": "Groq",
            "model": "openai/gpt-oss-20b",
            "outcome": (
                "agent_failed_before_call"
                if process is None or process.returncode != 0
                else "telemetry_missing"
            ),
        }
    elif (
        model_call.get("outcome") == "in_progress"
        and (process is None or process.returncode != 0)
    ):
        model_call["outcome"] = "agent_failed_during_call"
    model_call["agent_return_code"] = (
        process.returncode if process is not None else 1
    )
    if process_error:
        model_call["agent_error_type"] = process_error

    diagnosis = read_json(os.path.join(diagnosis_directory, "diagnosis.json"))
    validation = validate_groq_diagnosis(diagnosis)
    succeeded = (
        process is not None
        and process.returncode == 0
        and model_call.get("attempted") is True
        and model_call.get("succeeded") is True
        and validation["valid"]
    )
    result = {
        "succeeded": succeeded,
        "diagnosis": diagnosis if validation["valid"] else None,
        "diagnosis_validation": validation,
        "model_call": model_call,
        "agent_error_type": process_error,
    }
    save_json(os.path.join(artifact_directory, "diagnosis_model_call.json"), model_call)
    return result


def table_key_attributes(serverless_file):
    text = Path(serverless_file).read_text(encoding="utf-8")
    attributes = re.findall(
        r"(?m)^[ \t]*-[ \t]*AttributeName:[ \t]*([A-Za-z0-9_]+)[ \t]*\r?\n"
        r"[ \t]*KeyType:[ \t]*(HASH|RANGE)[ \t]*$",
        text,
    )
    order = {"HASH": 0, "RANGE": 1}
    return [name for name, _key_type in sorted(attributes, key=lambda item: order[item[1]])]


def test_summary(output):
    match = re.search(
        r"Tests:\s*(.*?)\s*(\d+)\s+total",
        output,
        re.DOTALL,
    )
    if not match:
        return {"tests": 0, "passed": 0, "failed": 0}
    counts = {
        name: int(count)
        for count, name in re.findall(
            r"(\d+)\s+(passed|failed)",
            match.group(1),
        )
    }
    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0)
    total = int(match.group(2))
    return {"tests": total, "passed": passed, "failed": failed}


def run_backend_tests(
    source_repository,
    demo_repository,
    key_attributes,
    runner=None,
):
    source_backend = os.path.join(source_repository, "backend")
    demo_backend = os.path.join(demo_repository, "backend")
    jest_runner = os.path.join(
        source_backend,
        "node_modules",
        "jest",
        "bin",
        "jest.js",
    )
    if not os.path.isfile(jest_runner):
        return {
            "status": "FAILED",
            "return_code": 1,
            "tests": 0,
            "passed": 0,
            "failed": 0,
            "command": None,
            "stdout": "",
            "stderr": f"Installed Jest runner not found: {jest_runner}",
        }

    command = ["node", jest_runner, "--runInBand"]
    environment = os.environ.copy()
    environment["DEMO_DYNAMODB_KEY_ATTRIBUTES"] = json.dumps(key_attributes)
    existing_node_path = environment.get("NODE_PATH")
    environment["NODE_PATH"] = os.pathsep.join(
        value
        for value in (source_backend + os.sep + "node_modules", existing_node_path)
        if value
    )

    try:
        process = (runner or subprocess.run)(
            command,
            cwd=demo_backend,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=environment,
        )
        stdout = process.stdout or ""
        stderr = process.stderr or ""
        summary = test_summary(stdout + "\n" + stderr)
        passed = process.returncode == 0 and summary["tests"] > 0 and summary["failed"] == 0
        return {
            "status": "PASSED" if passed else "FAILED",
            "return_code": process.returncode,
            **summary,
            "command": command,
            "working_directory": demo_backend,
            "stdout": stdout,
            "stderr": stderr,
        }
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return {
            "status": "FAILED",
            "return_code": 1,
            "tests": 0,
            "passed": 0,
            "failed": 0,
            "command": command,
            "working_directory": demo_backend,
            "stdout": "",
            "stderr": str(error),
        }


def apply_message_retention_fix(demo_repository, _attempt):
    backend = Path(demo_repository) / "backend"
    serverless_path = backend / "serverless.yml"
    handler_path = backend / "src" / "websocket.js"
    serverless_text = serverless_path.read_text(encoding="utf-8")
    handler_text = handler_path.read_text(encoding="utf-8")

    old_schema = (
        "        AttributeDefinitions:\n"
        "          - AttributeName: connectionId\n"
        "            AttributeType: S\n"
        "        KeySchema:\n"
        "          - AttributeName: connectionId\n"
        "            KeyType: HASH"
    )
    new_schema = (
        "        AttributeDefinitions:\n"
        "          - AttributeName: connectionId\n"
        "            AttributeType: S\n"
        "          - AttributeName: messageId\n"
        "            AttributeType: S\n"
        "        KeySchema:\n"
        "          - AttributeName: connectionId\n"
        "            KeyType: HASH\n"
        "          - AttributeName: messageId\n"
        "            KeyType: RANGE"
    )
    old_item = (
        "          connectionId: {\n"
        "            S: event.requestContext.connectionId,\n"
        "          },\n"
        "          message: {"
    )
    new_item = (
        "          connectionId: {\n"
        "            S: event.requestContext.connectionId,\n"
        "          },\n"
        "          messageId: {\n"
        "            S: requestId,\n"
        "          },\n"
        "          message: {"
    )
    if (
        new_schema in serverless_text
        and new_item in handler_text
    ):
        return {
            "status": "SUCCESS",
            "modified_files": [
                "backend/serverless.yml",
                "backend/src/websocket.js",
            ],
            "change": "The targeted per-message key fix is already present.",
        }

    if serverless_text.count(old_schema) != 1 or handler_text.count(old_item) != 1:
        return {
            "status": "FAILED",
            "modified_files": [],
            "error": "Disposable project no longer matches the diagnosed overwrite pattern.",
        }

    serverless_path.write_text(
        serverless_text.replace(old_schema, new_schema, 1),
        encoding="utf-8",
    )
    handler_path.write_text(
        handler_text.replace(old_item, new_item, 1),
        encoding="utf-8",
    )
    return {
        "status": "SUCCESS",
        "modified_files": [
            "backend/serverless.yml",
            "backend/src/websocket.js",
        ],
        "change": (
            "Added messageId as the DynamoDB sort key and wrote requestId as "
            "the per-message key."
        ),
    }


def run_level3_websocket_demo(
    repository_context,
    artifact_directory,
    max_retries=2,
    command_runner=None,
    fixer=apply_message_retention_fix,
    preflight_validation=None,
    diagnosis_mode="deterministic",
    diagnosis_runner=None,
):
    if diagnosis_mode not in {"deterministic", "groq_ai"}:
        raise ValueError("diagnosis_mode must be 'deterministic' or 'groq_ai'.")

    source_repository = repository_context.get("repository_path")
    source_backend = os.path.join(source_repository or "", "backend")
    source_config = os.path.join(source_backend, "serverless.yml")
    source_handler = os.path.join(source_backend, "src", "websocket.js")
    key_attributes = table_key_attributes(source_config) if os.path.isfile(source_config) else []
    backend_component = next(
        (
            component
            for component in repository_context.get("architecture", {}).get("components", [])
            if isinstance(component, dict) and component.get("type") == "backend"
        ),
        {},
    )
    supported = (
        isinstance(source_repository, str)
        and os.path.isdir(source_repository)
        and os.path.isfile(source_handler)
        and backend_component.get("technology") == "AWS Lambda"
        and len(key_attributes) == 1
        and key_attributes[0] == "connectionId"
    )

    os.makedirs(artifact_directory, exist_ok=True)
    report_path = os.path.join(artifact_directory, DEMO_BUG_REPORT_NAME)
    demo_repository = os.path.join(artifact_directory, "level3_websocket_demo")
    bug_report = {
        "bug_title": "Messages from one WebSocket connection overwrite each other",
        "project_type": "AWS Lambda API Gateway WebSocket with DynamoDB",
        "diagnosis_mode": diagnosis_mode,
        "expected_behavior": (
            "Each accepted message from a connection remains independently retrievable."
        ),
        "reproduction": {
            "kind": "local_lambda_handler",
            "scenario": "Send two messages with different request IDs on one connection.",
            "assertion": "The DynamoDB model contains both accepted messages.",
        },
        "network_access": False,
        "aws_access": False,
        "disposable_project": demo_repository,
    }
    save_json(report_path, bug_report)

    if not supported:
        error = "The repository does not match the local Lambda/WebSocket retention demo prerequisites."
        result = {
            "mode": "local-websocket-demo",
            "status": "FAILED",
            "final_status": "FAIL",
            "return_code": 1,
            "diagnosis_mode": diagnosis_mode,
            "model_call": {
                "attempted": False,
                "succeeded": False,
                "provider": "Groq",
                "outcome": "not_attempted",
            },
            "ai_powered_stages": [],
            "error": error,
            "stages": [],
        }
        save_json(os.path.join(artifact_directory, "validation_result.json"), result)
        return result

    shutil.copytree(
        source_repository,
        demo_repository,
        ignore=shutil.ignore_patterns(
            ".git",
            "node_modules",
            "dist",
            ".serverless",
            ".vite",
            "coverage",
            "venv",
            ".venv",
            "__pycache__",
        ),
    )
    demo_backend = os.path.join(demo_repository, "backend")
    demo_test = os.path.join(demo_backend, "tests", DEMO_TEST_NAME)
    Path(demo_test).write_text(DEMO_TEST_SOURCE, encoding="utf-8")

    initial_test_result = run_backend_tests(
        source_repository,
        demo_repository,
        key_attributes,
        command_runner,
    )
    initial_output = initial_test_result["stdout"] + "\n" + initial_test_result["stderr"]
    baseline_reproduced = (
        initial_test_result["status"] == "FAILED"
        and DEMO_TEST_TITLE in initial_output
        and "Expected: 2" in initial_output
        and "Received: 1" in initial_output
    )
    reproduction_result = {
        "status": "REPRODUCED" if baseline_reproduced else "NOT_REPRODUCED",
        "executed": initial_test_result.get("tests", 0) > 0,
        "expected_failure_observed": baseline_reproduced,
        "bug_report": report_path,
        "table_key_attributes": key_attributes,
        "test_result": initial_test_result,
        "evidence": [
            "The actual copied Lambda message handler was invoked twice with mocked API Gateway event objects.",
            "DynamoDB PutItem calls were applied to an in-memory table using the configured primary-key attributes.",
            "The regression assertion expected 2 retained rows and observed 1 before the fix."
            if baseline_reproduced
            else "The expected two-to-one row-count failure was not observed.",
        ],
    }
    save_json(os.path.join(artifact_directory, "reproduction_result.json"), reproduction_result)
    save_json(os.path.join(artifact_directory, "test_generation_result.json"), {
        "status": "SUCCESS" if initial_test_result.get("tests", 0) > 0 else "FAILED",
        "test_file": os.path.relpath(demo_test, demo_repository),
        "generated_for": bug_report["bug_title"],
        "execution_command": initial_test_result.get("command"),
    })

    deterministic_diagnosis = {
        "bug_title": bug_report["bug_title"],
        "project_type": bug_report["project_type"],
        "failure_analysis": (
            "The failing regression test submits two messages with distinct request IDs "
            "on the same connection, while the in-memory table retains only one item."
            if baseline_reproduced
            else "The expected two-message retention failure was not reproduced; root cause remains unconfirmed."
        ),
        "root_cause": (
            "The configured DynamoDB table uses connectionId as its only key, and the "
            "handler writes every message with that same key. DynamoDB PutItem replaces "
            "an existing item with an identical primary key."
            if baseline_reproduced
            else "Not established because the expected regression failure was not observed."
        ),
        "affected_files": ["backend/src/websocket.js", "backend/serverless.yml"],
        "evidence": {
            "observed_test_failure": baseline_reproduced,
            "expected_item_count": 2,
            "observed_item_count": 1 if baseline_reproduced else None,
            "configured_key_attributes": key_attributes,
            "handler_uses_request_id_as_key": False,
        },
        "proposed_fix": (
            "Use requestId as a DynamoDB sort key alongside connectionId, and include it "
            "in each PutItem. This demo change is applied only to the disposable copy."
            if baseline_reproduced
            else "No fix proposed because reproduction did not confirm the reported behavior."
        ),
        "verification_plan": [
            "Re-run the original local handler regression test and require both messages to remain stored.",
            "Run the copied backend Jest suite and require zero failing tests.",
        ],
        "analysis_mode": "deterministic offline evidence analysis; no model/API call",
    }
    diagnosis = deterministic_diagnosis
    diagnosis_validation = {
        "valid": baseline_reproduced,
        "schema": "deterministic_demo_diagnosis_v1",
        "errors": [] if baseline_reproduced else ["Bug reproduction was not confirmed."],
    }
    diagnosis_success = baseline_reproduced
    model_call = {
        "attempted": False,
        "succeeded": False,
        "provider": "Groq",
        "model": "openai/gpt-oss-20b",
        "outcome": "not_requested",
    }
    diagnosis_failure = None

    if diagnosis_mode == "groq_ai":
        diagnosis = None
        diagnosis_success = False
        if baseline_reproduced:
            selected_runner = diagnosis_runner or run_groq_diagnosis
            try:
                ai_result = selected_runner(
                    repository_context,
                    demo_repository,
                    reproduction_result,
                    report_path,
                    artifact_directory,
                )
            except Exception as error:
                ai_result = {
                    "succeeded": False,
                    "diagnosis": None,
                    "diagnosis_validation": {
                        "valid": False,
                        "errors": ["Groq diagnosis adapter failed."],
                    },
                    "model_call": {
                        "attempted": False,
                        "succeeded": False,
                        "provider": "Groq",
                        "model": "openai/gpt-oss-20b",
                        "outcome": "adapter_failed",
                        "error_type": type(error).__name__,
                    },
                    "agent_error_type": type(error).__name__,
                }

            if not isinstance(ai_result, dict):
                ai_result = {"succeeded": False}
            candidate_model_call = ai_result.get("model_call")
            if isinstance(candidate_model_call, dict):
                model_call = candidate_model_call
            candidate_validation = ai_result.get("diagnosis_validation")
            candidate_diagnosis = ai_result.get("diagnosis")
            diagnosis_validation = (
                validate_groq_diagnosis(candidate_diagnosis)
                if isinstance(candidate_diagnosis, dict)
                else candidate_validation
                if isinstance(candidate_validation, dict)
                else {"valid": False, "errors": ["Diagnosis validation was not reported."]}
            )
            if (
                ai_result.get("succeeded") is True
                and model_call.get("attempted") is True
                and model_call.get("succeeded") is True
                and diagnosis_validation.get("valid") is True
                and isinstance(candidate_diagnosis, dict)
            ):
                diagnosis = candidate_diagnosis
                diagnosis_success = True
            else:
                diagnosis_failure = {
                    "model_call": model_call,
                    "diagnosis_validation": diagnosis_validation,
                    "agent_error_type": ai_result.get("agent_error_type"),
                }
        else:
            model_call["outcome"] = "skipped_reproduction_not_confirmed"
            diagnosis_validation = {
                "valid": False,
                "errors": ["Groq diagnosis requires a confirmed reproduction."],
            }
            diagnosis_failure = {
                "model_call": model_call,
                "diagnosis_validation": diagnosis_validation,
            }

    if diagnosis is not None:
        diagnosis_path = os.path.join(artifact_directory, "diagnosis.json")
        save_json(diagnosis_path, diagnosis)
    else:
        save_json(
            os.path.join(artifact_directory, "diagnosis_attempt.json"),
            {
                "diagnosis_mode": diagnosis_mode,
                "status": "FAILED",
                **(diagnosis_failure or {}),
            },
        )
    save_json(
        os.path.join(artifact_directory, "diagnosis_model_call.json"),
        model_call,
    )

    attempts = []
    final_test_result = initial_test_result
    final_fix_result = {"status": "FAILED", "modified_files": []}
    verified = False
    if baseline_reproduced and diagnosis_success:
        for attempt in range(1, max_retries + 2):
            fix_result = fixer(demo_repository, attempt)
            final_test_result = run_backend_tests(
                source_repository,
                demo_repository,
                table_key_attributes(os.path.join(demo_backend, "serverless.yml")),
                command_runner,
            )
            current_keys = table_key_attributes(os.path.join(demo_backend, "serverless.yml"))
            handler_text = Path(os.path.join(demo_backend, "src", "websocket.js")).read_text(
                encoding="utf-8"
            )
            semantic_fix_present = (
                "connectionId" in current_keys
                and "messageId" in current_keys
                and re.search(r"(?m)^\s*messageId:\s*\{\s*$", handler_text)
                is not None
                and re.search(r"(?m)^\s*S:\s*requestId,\s*$", handler_text)
                is not None
            )
            attempt_verified = (
                fix_result.get("status") == "SUCCESS"
                and final_test_result["status"] == "PASSED"
                and semantic_fix_present
            )
            attempts.append({
                "attempt": attempt,
                "fix_result": fix_result,
                "test_result": final_test_result,
                "semantic_fix_present": semantic_fix_present,
                "verified": attempt_verified,
            })
            final_fix_result = fix_result
            if attempt_verified:
                verified = True
                break
            if attempt < max_retries + 1:
                retry_diagnosis = {
                    **diagnosis,
                    "retry_attempt": attempt + 1,
                    "previous_test_result": final_test_result,
                    "instruction": "Re-evaluate the current disposable source and retry the targeted fix.",
                }
                save_json(
                    os.path.join(artifact_directory, f"diagnosis_retry_{attempt + 1}.json"),
                    retry_diagnosis,
                )

    semantic_fix_present = bool(
        attempts and attempts[-1].get("semantic_fix_present")
    )
    applied_files = (
        final_fix_result.get("modified_files", [])
        if semantic_fix_present
        else []
    )
    code_fix_result = {
        **final_fix_result,
        "fixer_status": final_fix_result.get("status"),
        "status": (
            "SUCCESS"
            if semantic_fix_present
            else "SKIPPED"
            if not diagnosis_success
            else "FAILED"
        ),
        "modified_files": applied_files,
        "semantic_fix_present": semantic_fix_present,
        "verified": verified,
        "attempts": attempts,
        "disposable_project_only": True,
    }
    save_json(os.path.join(artifact_directory, "code_fix_result.json"), code_fix_result)
    save_json(os.path.join(artifact_directory, "test_result.json"), final_test_result)
    preflight_build_result = read_json(
        os.path.join(artifact_directory, "build_result.json")
    )
    if not preflight_build_result:
        preflight_build_result = {
            "status": "SKIPPED",
            "return_code": 0,
            "message": "No frontend preflight was supplied; the demo defect is backend-only.",
            "components": [],
        }
    save_json(os.path.join(artifact_directory, "build_result.json"), preflight_build_result)

    preflight_passed = (
        preflight_validation is None
        or preflight_validation.get("status") == "PASSED"
    )
    final_status = (
        "PASS"
        if baseline_reproduced and diagnosis_success and verified and preflight_passed
        else "FAIL"
    )
    verification_reason = (
        "Groq diagnosis did not succeed and validate; no fixer was run."
        if diagnosis_mode == "groq_ai" and not diagnosis_success
        else "Initial failing reproduction was observed, the targeted disposable fix changed both key schema and handler item, and the same regression plus backend suite passed."
        if final_status == "PASS"
        else "The initial failure, diagnosis, targeted fix, and post-fix regression were not all verified."
    )
    verification = {
        "status": "PASS" if final_status == "PASS" else "FAIL",
        "reason": [verification_reason],
        "initial_reproduction_status": reproduction_result["status"],
        "post_fix_test_status": final_test_result["status"],
        "modified_files": applied_files,
        "attempts": attempts,
        "disposable_project": demo_repository,
        "preflight_validation": preflight_validation,
        "aws_accessed": False,
        "network_calls": False,
    }
    save_json(os.path.join(artifact_directory, "verification_report.json"), verification)
    result = {
        "mode": "local-websocket-demo",
        "diagnosis_mode": diagnosis_mode,
        "model_call": model_call,
        "diagnosis_validation": diagnosis_validation,
        "ai_powered_stages": (
            ["diagnosis"]
            if diagnosis_success
            and model_call.get("attempted") is True
            and model_call.get("succeeded") is True
            else []
        ),
        "code_fix_mode": "deterministic_demo",
        "test_generation_mode": "deterministic_demo",
        "workflow_logic": [
            "Local repository analysis",
            "Deterministic local WebSocket reproduction",
            (
                "Groq-backed root-cause diagnosis"
                if "diagnosis" in (
                    ["diagnosis"]
                    if diagnosis_success
                    and model_call.get("attempted") is True
                    and model_call.get("succeeded") is True
                    else []
                )
                else "Groq diagnosis attempted but not accepted"
                if diagnosis_mode == "groq_ai"
                else "Deterministic evidence diagnosis"
            ),
            "Deterministic disposable-project code fix",
            "Deterministic regression test generation and Jest execution",
            "Frontend build/lint preflight",
            "Deterministic fix verification",
        ],
        "status": "PASSED" if final_status == "PASS" else "FAILED",
        "final_status": final_status,
        "return_code": 0 if final_status == "PASS" else 1,
        "baseline_reproduced": baseline_reproduced,
        "root_cause_identified": (
            bool(diagnosis.get("root_cause"))
            if isinstance(diagnosis, dict)
            else False
        ) and baseline_reproduced,
        "fix_applied": semantic_fix_present,
        "post_fix_verified": verified,
        "bug_report": report_path,
        "disposable_project": demo_repository,
        "attempts": attempts,
        "preflight_validation": preflight_validation,
        "aws_accessed": False,
        "network_calls": False,
        "stages": [
            {
                "stage": 2,
                "name": "Generate and run local WebSocket regression",
                "status": "SUCCESS" if baseline_reproduced else "FAILED",
                "details": reproduction_result,
            },
            {
                "stage": 3,
                "name": "Evidence-based root cause diagnosis",
                "status": "SUCCESS" if diagnosis_success else "FAILED",
                "details": diagnosis if diagnosis_success else diagnosis_failure or diagnosis,
            },
            {
                "stage": 4,
                "name": "Apply targeted disposable-project fix",
                "status": (
                    "SUCCESS"
                    if semantic_fix_present
                    else "SKIPPED"
                    if not diagnosis_success
                    else "FAILED"
                ),
                "details": code_fix_result,
            },
            {
                "stage": 5,
                "name": "Post-fix backend Jest verification",
                "status": (
                    "SUCCESS"
                    if diagnosis_success and final_test_result["status"] == "PASSED"
                    else "SKIPPED"
                    if not diagnosis_success
                    else "FAILED"
                ),
                "details": (
                    final_test_result
                    if diagnosis_success
                    else {"status": "SKIPPED", "reason": "Diagnosis did not succeed."}
                ),
            },
            {
                "stage": 6,
                "name": "Frontend build/lint and backend preflight",
                "status": (
                    "SUCCESS"
                    if preflight_validation is not None
                    and preflight_validation.get("status") == "PASSED"
                    else "FAILED"
                    if preflight_validation is not None
                    else "SKIPPED"
                ),
                "details": preflight_validation,
            },
            {
                "stage": 7,
                "name": "Fix verification",
                "status": "SUCCESS" if verified and final_status == "PASS" else "FAILED",
                "details": verification,
            },
        ],
    }
    save_json(os.path.join(artifact_directory, "validation_result.json"), result)
    return result