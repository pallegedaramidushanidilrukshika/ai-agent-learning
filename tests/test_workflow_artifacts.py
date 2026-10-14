import io
import importlib.util
import json
import os
import subprocess
import sys
import builtins
from types import SimpleNamespace
from types import ModuleType
from pathlib import Path
from unittest.mock import Mock

import pytest

import local_websocket_demo
import workflow_orchestrator
from workflow_artifacts import ARTIFACT_DIR_ENV, resolve_artifact_path


def load_repository_agent_module(monkeypatch):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    agent_path = Path(__file__).resolve().parents[1] / "repository_agent.py"
    spec = importlib.util.spec_from_file_location(
        "repository_agent_path_test",
        agent_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_bug_reproduction_agent_module(monkeypatch):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    agent_path = Path(__file__).resolve().parents[1] / "bug_reproduction_agent.py"
    spec = importlib.util.spec_from_file_location(
        "bug_reproduction_agent_path_test",
        agent_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_bug_diagnosis_config(monkeypatch):
    class StopBeforeDiagnosisExecution(Exception):
        pass

    class MockAPIStatusError(Exception):
        def __init__(self, message, status_code, request, body):
            super().__init__("authorization: raw-auth-header request body: raw-request")
            self.message = message
            self.status_code = status_code
            self.request = request
            self.body = body

    dotenv_module = ModuleType("dotenv")
    load_dotenv = Mock(side_effect=StopBeforeDiagnosisExecution)
    dotenv_module.load_dotenv = load_dotenv

    langchain_groq_module = ModuleType("langchain_groq")
    chat_groq = Mock()
    langchain_groq_module.ChatGroq = chat_groq

    groq_module = ModuleType("groq")
    groq_client = Mock()
    groq_module.Groq = groq_client
    groq_module.APIStatusError = MockAPIStatusError

    monkeypatch.setitem(sys.modules, "dotenv", dotenv_module)
    monkeypatch.setitem(sys.modules, "langchain_groq", langchain_groq_module)
    monkeypatch.setitem(sys.modules, "groq", groq_module)
    monkeypatch.setenv("GROQ_API_KEY", "offline-test-key")
    monkeypatch.setattr(sys, "dont_write_bytecode", True)

    agent_path = Path(__file__).resolve().parents[1] / "bug_diagnosis_agent.py"
    spec = importlib.util.spec_from_file_location(
        "bug_diagnosis_agent_path_test",
        agent_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    with pytest.raises(StopBeforeDiagnosisExecution):
        spec.loader.exec_module(module)

    load_dotenv.assert_called_once_with()
    groq_client.assert_not_called()
    chat_groq.assert_not_called()
    return module, groq_client, chat_groq


def capture_groq_diagnosis_prompt(
    monkeypatch,
    tmp_path,
    repository_context,
    reproduction_result,
    bug_report,
):
    class StopBeforeDiagnosisResult(Exception):
        pass

    run_dir = tmp_path / "prompt-capture-run"
    run_dir.mkdir()
    context_path = run_dir / "repository_context.json"
    reproduction_path = run_dir / "reproduction_result.json"
    bug_report_path = tmp_path / "prompt-capture-bug-report.json"
    context_path.write_text(json.dumps(repository_context), encoding="utf-8")
    reproduction_path.write_text(json.dumps(reproduction_result), encoding="utf-8")
    bug_report_path.write_text(json.dumps(bug_report), encoding="utf-8")

    dotenv_module = ModuleType("dotenv")
    dotenv_module.load_dotenv = Mock()
    langchain_groq_module = ModuleType("langchain_groq")
    langchain_groq_module.ChatGroq = Mock()
    groq_module = ModuleType("groq")
    groq_module.Groq = Mock()
    groq_module.APIStatusError = type("APIStatusError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "dotenv", dotenv_module)
    monkeypatch.setitem(sys.modules, "langchain_groq", langchain_groq_module)
    monkeypatch.setitem(sys.modules, "groq", groq_module)
    monkeypatch.setenv("GROQ_API_KEY", "mock-prompt-test-key")
    monkeypatch.setenv(ARTIFACT_DIR_ENV, os.fspath(run_dir))
    monkeypatch.setattr(sys, "argv", ["bug_diagnosis_agent.py", os.fspath(bug_report_path)])
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    groq_client = Mock()
    captured = {}

    def stop_before_diagnosis(**kwargs):
        captured.update(kwargs)
        raise StopBeforeDiagnosisResult

    groq_client.chat.completions.create.side_effect = stop_before_diagnosis
    groq_module.Groq = Mock(return_value=groq_client)
    agent_path = Path(__file__).resolve().parents[1] / "bug_diagnosis_agent.py"
    spec = importlib.util.spec_from_file_location(
        "bug_diagnosis_prompt_capture_test",
        agent_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(SystemExit) as exit_error:
        spec.loader.exec_module(module)

    assert exit_error.value.code == 1
    assert groq_client.chat.completions.create.call_count == 1
    return captured["messages"][0]["content"], captured, run_dir


def test_groq_diagnosis_prompt_bounds_oversized_context_and_preserves_evidence(
    tmp_path, monkeypatch
):
    affected_excerpt = (
        "exports.message = async (event) => {\n"
        "  // affected handler excerpt\n"
        "  await client.send(new PutItemCommand({ connectionId: event.requestContext.connectionId }));\n"
        "};\n"
    )
    repository_context = {
        "repository_path": "C:/demo/repository",
        "file_count": 12000,
        "files": [f"generated/irrelevant_{index}.js" for index in range(4000)],
        "important_files": [f"generated/irrelevant_{index}.js" for index in range(1000)],
        "project_types": [
            {"technology": "Node.js", "language": "JavaScript"},
            {"technology": "AWS Lambda", "runtime": "nodejs22.x"},
        ],
        "execution_strategy": {
            "technology": "React/Vite + AWS Serverless",
            "backend": "AWS Lambda",
            "test_command": ["node", "backend/node_modules/jest/bin/jest.js"],
        },
        "architecture": {
            "application_type": "full_stack",
            "components": [
                {"type": "backend", "path": "backend", "technology": "AWS Lambda"},
                {"type": "frontend", "path": "frontend", "technology": "React"},
            ],
        },
        "file_contents": {
            "backend/src/websocket.js": affected_excerpt,
            **{
                f"generated/irrelevant_{index}.js": "ignore-me-content-" * 1000
                for index in range(40)
            },
        },
    }
    reproduction_result = {
        "status": "REPRODUCED",
        "executed": True,
        "expected_failure_observed": True,
        "evidence": ["One retained row observed for two messages."],
        "test_result": {
            "status": "FAILED",
            "return_code": 1,
            "tests": 12,
            "passed": 11,
            "failed": 1,
            "stderr": (
                "irrelevant failure output " * 1500
                + "\nAssertionError: Expected: 2 Received: 1\n"
                + "Traceback (most recent call last):\n"
                + "  at message (backend/src/websocket.js:88:11)\n"
            ),
        },
    }
    bug_report = {
        "bug_title": "Messages overwrite per connection",
        "project_type": "AWS Lambda WebSocket",
        "expected_behavior": "Both messages remain stored.",
        "affected_files": ["backend/src/websocket.js"],
    }

    prompt, call_arguments, run_dir = capture_groq_diagnosis_prompt(
        monkeypatch,
        tmp_path,
        repository_context,
        reproduction_result,
        bug_report,
    )
    model_call = json.loads(
        (run_dir / "diagnosis_model_call.json").read_text(encoding="utf-8")
    )

    assert len(prompt) <= 8500
    assert call_arguments["max_completion_tokens"] == 1536
    assert model_call["prompt_character_count"] == len(prompt)
    assert model_call["prompt_character_limit"] == 8500
    assert "REPRODUCED" in prompt
    assert "Expected: 2" in prompt
    assert "Received: 1" in prompt
    assert "backend/src/websocket.js:88:11" in prompt
    assert "affected handler excerpt" in prompt
    assert "generated/irrelevant_3999.js" not in prompt
    assert "ignore-me-content" not in prompt


def test_groq_api_status_error_metadata_is_sanitized(monkeypatch):
    diagnosis_agent, _groq_client, _chat_groq = load_bug_diagnosis_config(
        monkeypatch
    )
    api_key = "offline-test-key"
    bearer_token = "gsk_test_secret_token_value"
    raw_request = "raw-user-prompt-payload"
    error = diagnosis_agent.APIStatusError(
        (
            f"Rate limit reached; api_key={api_key}; "
            f"Authorization: Bearer {bearer_token}; prompt={raw_request}"
        ),
        429,
        request={"headers": {"Authorization": bearer_token}},
        body={"request": raw_request, "key": api_key},
    )

    metadata = diagnosis_agent.sanitized_groq_status_error(error, api_key)
    serialized = json.dumps(metadata)

    assert metadata["http_status_code"] == 429
    assert "Rate limit reached" in metadata["provider_error_message"]
    assert api_key not in serialized
    assert bearer_token not in serialized
    assert raw_request not in serialized
    assert "Authorization" not in metadata["provider_error_message"]
    assert "prompt=" not in metadata["provider_error_message"]
    assert "raw-auth-header" not in serialized


def test_groq_api_status_error_is_persisted_safely_in_run_metadata(
    monkeypatch, tmp_path
):
    diagnosis_agent, _groq_client, _chat_groq = load_bug_diagnosis_config(
        monkeypatch
    )
    metadata_path = tmp_path / "diagnosis_model_call.json"
    monkeypatch.setattr(
        diagnosis_agent,
        "MODEL_CALL_RESULT_FILE",
        os.fspath(metadata_path),
    )
    api_key = "offline-test-key"
    bearer_token = "gsk_another_sensitive_token"
    request_payload = "untrusted-diagnostic-request"
    error = diagnosis_agent.APIStatusError(
        f"Provider rejected key={api_key}; Bearer {bearer_token}; request={request_payload}",
        503,
        request={"Authorization": bearer_token},
        body={"prompt": request_payload, "api_key": api_key},
    )
    model_call = {
        "attempted": True,
        "succeeded": False,
        "provider": "Groq",
        "model": "openai/gpt-oss-20b",
        "outcome": "in_progress",
    }

    diagnosis_agent.write_model_call_result(model_call, error, api_key)

    saved_text = metadata_path.read_text(encoding="utf-8")
    saved = json.loads(saved_text)
    assert saved["http_status_code"] == 503
    assert "Provider rejected" in saved["provider_error_message"]
    assert saved["outcome"] == "failed"
    assert api_key not in saved_text
    assert bearer_token not in saved_text
    assert request_payload not in saved_text


def test_non_status_groq_errors_do_not_record_untrusted_messages(monkeypatch):
    diagnosis_agent, _groq_client, _chat_groq = load_bug_diagnosis_config(
        monkeypatch
    )
    error = RuntimeError("authorization: should-not-be-recorded")

    assert diagnosis_agent.sanitized_groq_status_error(
        error,
        "offline-test-key",
    ) == {}


def load_code_fix_config(monkeypatch, diagnosis_path=None):
    class StopBeforeFixExecution(Exception):
        pass

    dotenv_module = ModuleType("dotenv")
    dotenv_module.load_dotenv = Mock()
    langchain_groq_module = ModuleType("langchain_groq")
    chat_groq = Mock(return_value=object())
    langchain_groq_module.ChatGroq = chat_groq
    groq_module = ModuleType("groq")
    groq_client = Mock(return_value=object())
    groq_module.Groq = groq_client
    langchain_module = ModuleType("langchain")
    langchain_module.__path__ = []
    langchain_agents_module = ModuleType("langchain.agents")
    create_agent = Mock(side_effect=StopBeforeFixExecution)
    langchain_agents_module.create_agent = create_agent
    langchain_core_module = ModuleType("langchain_core")
    langchain_core_module.__path__ = []
    langchain_tools_module = ModuleType("langchain_core.tools")
    langchain_tools_module.tool = lambda function: function

    monkeypatch.setitem(sys.modules, "dotenv", dotenv_module)
    monkeypatch.setitem(sys.modules, "langchain_groq", langchain_groq_module)
    monkeypatch.setitem(sys.modules, "groq", groq_module)
    monkeypatch.setitem(sys.modules, "langchain", langchain_module)
    monkeypatch.setitem(sys.modules, "langchain.agents", langchain_agents_module)
    monkeypatch.setitem(sys.modules, "langchain_core", langchain_core_module)
    monkeypatch.setitem(sys.modules, "langchain_core.tools", langchain_tools_module)
    monkeypatch.setenv("GROQ_API_KEY", "offline-test-key")
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(
        sys,
        "argv",
        ["code_fix_agent.py"]
        + ([os.fspath(diagnosis_path)] if diagnosis_path is not None else []),
    )

    agent_path = Path(__file__).resolve().parents[1] / "code_fix_agent.py"
    spec = importlib.util.spec_from_file_location(
        "code_fix_agent_path_test",
        agent_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    with pytest.raises(StopBeforeFixExecution):
        spec.loader.exec_module(module)

    dotenv_module.load_dotenv.assert_called_once_with()
    groq_client.assert_called_once_with(api_key="offline-test-key")
    chat_groq.assert_called_once_with(
        model="openai/gpt-oss-20b",
        temperature=0,
    )
    create_agent.assert_called_once()
    return module, create_agent


def test_resolves_artifact_inside_configured_run_directory(tmp_path, monkeypatch):
    base_dir = tmp_path / "project"
    run_dir = tmp_path / "runs" / "run-1"
    monkeypatch.setenv(ARTIFACT_DIR_ENV, os.fspath(run_dir))

    result = resolve_artifact_path("test_result.json", base_dir)

    assert result == os.fspath(run_dir / "test_result.json")
    assert not run_dir.exists()


def test_preserves_standalone_base_directory_without_run_configuration(
    tmp_path, monkeypatch
):
    base_dir = tmp_path / "project"
    monkeypatch.delenv(ARTIFACT_DIR_ENV, raising=False)

    result = resolve_artifact_path("diagnosis.json", base_dir)

    assert result == os.fspath(base_dir / "diagnosis.json")
    assert not base_dir.exists()
    assert not (base_dir / "diagnosis.json").exists()


def test_import_does_not_create_directories_or_modify_existing_files(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "runs" / "imported-run"
    existing_file = tmp_path / "existing.txt"
    existing_file.write_text("keep this content", encoding="utf-8")
    original_content = existing_file.read_bytes()
    original_modified_time = existing_file.stat().st_mtime_ns
    monkeypatch.setenv(ARTIFACT_DIR_ENV, os.fspath(run_dir))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)

    helper_path = Path(__file__).resolve().parents[1] / "workflow_artifacts.py"
    spec = importlib.util.spec_from_file_location(
        "workflow_artifacts_import_probe",
        helper_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)

    assert not run_dir.exists()
    assert existing_file.read_bytes() == original_content
    assert existing_file.stat().st_mtime_ns == original_modified_time


def test_configured_resolution_does_not_create_run_directory_or_modify_files(
    tmp_path, monkeypatch
):
    run_parent = tmp_path / "runs"
    run_parent.mkdir()
    existing_file = run_parent / "existing.txt"
    existing_file.write_text("unchanged", encoding="utf-8")
    original_content = existing_file.read_bytes()
    original_modified_time = existing_file.stat().st_mtime_ns
    run_dir = run_parent / "configured-run"
    monkeypatch.setenv(ARTIFACT_DIR_ENV, os.fspath(run_dir))

    result = resolve_artifact_path("test_result.json", tmp_path / "project")

    assert result == os.fspath(run_dir / "test_result.json")
    assert not run_dir.exists()
    assert existing_file.read_bytes() == original_content
    assert existing_file.stat().st_mtime_ns == original_modified_time


def test_run_directories_resolve_to_isolated_paths(tmp_path, monkeypatch):
    base_dir = tmp_path / "project"
    first_run = tmp_path / "runs" / "run-1"
    second_run = tmp_path / "runs" / "run-2"

    monkeypatch.setenv(ARTIFACT_DIR_ENV, os.fspath(first_run))
    first_path = resolve_artifact_path("workflow_report.json", base_dir)
    monkeypatch.setenv(ARTIFACT_DIR_ENV, os.fspath(second_run))
    second_path = resolve_artifact_path("workflow_report.json", base_dir)

    assert first_path == os.fspath(first_run / "workflow_report.json")
    assert second_path == os.fspath(second_run / "workflow_report.json")
    assert first_path != second_path
    assert not first_run.exists()
    assert not second_run.exists()


def test_repository_agent_uses_configured_run_directory(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "repository-analysis"
    monkeypatch.setenv(ARTIFACT_DIR_ENV, os.fspath(run_dir))

    repository_agent = load_repository_agent_module(monkeypatch)

    assert repository_agent.OUTPUT_FILE == os.fspath(
        run_dir / "repository_context.json"
    )
    assert not run_dir.exists()


def test_repository_agent_preserves_standalone_output_path(monkeypatch):
    monkeypatch.delenv(ARTIFACT_DIR_ENV, raising=False)

    repository_agent = load_repository_agent_module(monkeypatch)

    assert repository_agent.OUTPUT_FILE == os.path.join(
        repository_agent.BASE_DIR,
        "repository_context.json",
    )


def test_repository_agent_import_does_not_scan_or_clone(monkeypatch):
    walk = Mock()
    run = Mock()
    monkeypatch.setattr(os, "walk", walk)
    monkeypatch.setattr(subprocess, "run", run)

    load_repository_agent_module(monkeypatch)

    walk.assert_not_called()
    run.assert_not_called()


def test_test_generation_agent_import_does_not_execute_runtime(monkeypatch):
    dotenv_module = ModuleType("dotenv")
    load_dotenv = Mock()
    dotenv_module.load_dotenv = load_dotenv

    langchain_module = ModuleType("langchain")
    langchain_module.__path__ = []
    langchain_agents_module = ModuleType("langchain.agents")
    create_agent = Mock()
    langchain_agents_module.create_agent = create_agent
    langchain_tools_module = ModuleType("langchain.tools")
    langchain_tools_module.tool = lambda function: function
    langchain_groq_module = ModuleType("langchain_groq")
    chat_groq = Mock()
    langchain_groq_module.ChatGroq = chat_groq

    monkeypatch.setitem(sys.modules, "dotenv", dotenv_module)
    monkeypatch.setitem(sys.modules, "langchain", langchain_module)
    monkeypatch.setitem(sys.modules, "langchain.agents", langchain_agents_module)
    monkeypatch.setitem(sys.modules, "langchain.tools", langchain_tools_module)
    monkeypatch.setitem(sys.modules, "langchain_groq", langchain_groq_module)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    exit_mock = Mock(side_effect=AssertionError("sys.exit called during import"))
    monkeypatch.setattr(sys, "exit", exit_mock)

    agent_path = Path(__file__).resolve().parents[1] / "test_generation_agent.py"
    spec = importlib.util.spec_from_file_location(
        "test_generation_agent_import_test",
        agent_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert callable(module.main)
    load_dotenv.assert_not_called()
    chat_groq.assert_not_called()
    create_agent.assert_not_called()
    exit_mock.assert_not_called()


def test_test_result_checker_import_does_not_run_or_write(monkeypatch):
    run = Mock(side_effect=AssertionError("subprocess.run called during import"))
    open_file = Mock(side_effect=AssertionError("file opened during import"))
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(builtins, "open", open_file)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)

    checker_path = Path(__file__).resolve().parents[1] / "test_result_checker.py"
    spec = importlib.util.spec_from_file_location(
        "test_result_checker_import_test",
        checker_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert callable(module.main)
    assert callable(module.load_json)
    assert callable(module.write_result)
    assert callable(module.failed_result)
    assert callable(module.parse_node_test_summary)
    run.assert_not_called()
    open_file.assert_not_called()


@pytest.mark.parametrize(
    ("executed", "bug_observed", "evidence_complete", "execution_error", "expected"),
    [
        (True, True, True, False, "REPRODUCED"),
        (True, False, True, False, "NOT_REPRODUCED"),
        (True, False, False, False, "INCONCLUSIVE"),
        (False, False, False, True, "EXECUTION_ERROR"),
    ],
)
def test_reproduction_classifier_covers_all_contract_states(
    monkeypatch, executed, bug_observed, evidence_complete, execution_error, expected
):
    reproduction_agent = load_bug_reproduction_agent_module(monkeypatch)

    assert reproduction_agent.classify_reproduction_outcome(
        executed,
        bug_observed=bug_observed,
        evidence_complete=evidence_complete,
        execution_error=execution_error,
    ) == expected


@pytest.mark.parametrize(
    ("action_response", "after_response", "expected_status", "expected_executed"),
    [
        (
            {"success": True, "status_code": 200, "body": "", "error": None},
            {
                "success": True,
                "status_code": 200,
                "body": '[{"name":"","position":"","level":""}]',
                "error": None,
            },
            "REPRODUCED",
            True,
        ),
        (
            {"success": False, "status_code": 400, "body": "", "error": "rejected"},
            {"success": True, "status_code": 200, "body": "[]", "error": None},
            "NOT_REPRODUCED",
            True,
        ),
        (
            {"success": True, "status_code": 200, "body": "", "error": None},
            {"success": False, "status_code": None, "body": "", "error": "unavailable"},
            "INCONCLUSIVE",
            True,
        ),
        (
            {"success": False, "status_code": None, "body": "", "error": "offline"},
            {"success": True, "status_code": 200, "body": "[]", "error": None},
            "EXECUTION_ERROR",
            True,
        ),
    ],
)
def test_http_reproduction_outcomes_use_mocked_request_evidence(
    monkeypatch,
    tmp_path,
    action_response,
    after_response,
    expected_status,
    expected_executed,
):
    reproduction_agent = load_bug_reproduction_agent_module(monkeypatch)
    before_response = {
        "success": True,
        "status_code": 200,
        "body": "[]",
        "error": None,
    }
    request = Mock(side_effect=[before_response, action_response, after_response])
    monkeypatch.setattr(reproduction_agent, "http_request", request)
    bug_definition = {
        "bug": "Invalid record is accepted",
        "action": {
            "method": "POST",
            "path": "/record",
            "data": {"name": "", "position": "", "level": ""},
            "body_type": "json",
        },
        "state_check": {"method": "GET", "path": "/record"},
    }

    bug_result = reproduction_agent.reproduce_http_bug(
        os.fspath(tmp_path),
        object(),
        "http://example.invalid",
        bug_definition,
    )
    result = reproduction_agent.aggregate_reproduction_results([bug_result])

    assert result["status"] == expected_status
    assert result["executed"] is expected_executed
    assert set(result["evidence"]) == {"attempted", "observed", "errors"}
    assert result["evidence"]["observed"][0]["status"] == expected_status
    if expected_executed:
        assert result["evidence"]["attempted"][0]["steps"]
    if expected_status in ("INCONCLUSIVE", "EXECUTION_ERROR"):
        assert result["evidence"]["errors"]
    assert request.call_count == 3


def test_missing_application_is_execution_error_without_claiming_execution(
    monkeypatch, tmp_path
):
    reproduction_agent = load_bug_reproduction_agent_module(monkeypatch)
    request = Mock()
    monkeypatch.setattr(reproduction_agent, "http_request", request)

    result = reproduction_agent.reproduce_http_bug(
        os.fspath(tmp_path),
        None,
        "http://example.invalid",
        {
            "bug": "Example",
            "action": {"method": "POST", "path": "/", "data": {}},
            "state_check": {"method": "GET", "path": "/"},
        },
    )

    assert result["status"] == "EXECUTION_ERROR"
    assert result["executed"] is False
    request.assert_not_called()


def test_build_failure_result_has_structured_execution_error_evidence(
    monkeypatch,
):
    reproduction_agent = load_bug_reproduction_agent_module(monkeypatch)

    result = reproduction_agent.build_failure_result(
        "Application dependency installation failed.",
        attempted=[{"operation": "start_application"}],
    )

    assert result["status"] == "EXECUTION_ERROR"
    assert result["executed"] is False
    assert result["evidence"] == {
        "attempted": [{"operation": "start_application"}],
        "observed": [],
        "errors": [{
            "stage": "reproduction_setup",
            "details": "Application dependency installation failed.",
        }],
    }


def test_final_reproduction_json_serializes_contract_and_legacy_bug_evidence(
    monkeypatch, tmp_path
):
    reproduction_agent = load_bug_reproduction_agent_module(monkeypatch)
    legacy_evidence = ["Submitted POST /addtask with newtask=\"\"."]
    sensitive_values = (
        "Ada Lovelace",
        "password-value-secret",
        "api-token-value-secret",
        "header-token-value-secret",
        "query-token-value-secret",
        "unrecognized-secret-value",
    )
    raw_attempt = {
        "method": "post",
        "path": "/record?token=query-token-value-secret",
        "body_type": "json",
        "data": {
            "name": "Ada Lovelace",
            "password": "password-value-secret",
            "api_token": "api-token-value-secret",
        },
        "token": "header-token-value-secret",
        "headers": {"Authorization": "Bearer header-token-value-secret"},
        "unrecognized": "unrecognized-secret-value",
    }
    bug_result = reproduction_agent.apply_reproduction_contract(
        {
            "status": "REPRODUCED",
            "bug": "Empty task can be added",
            "observed_behavior": "An empty task appeared.",
            "expected_behavior": "Empty tasks are rejected.",
            "evidence": legacy_evidence,
            "application_status": 200,
        },
        executed=True,
        attempted=[raw_attempt],
    )
    assert bug_result["attempted"] == [{
        "method": "POST",
        "path": "/record",
        "body_type": "json",
        "data_fields": ["name", "password", "api_token"],
        "values_redacted": True,
    }]
    result = reproduction_agent.aggregate_reproduction_results([bug_result])
    result["evidence"]["attempted"] = [raw_attempt]
    final_result = reproduction_agent.build_final_reproduction_result(
        os.fspath(tmp_path / "project"),
        "Node.js",
        "Empty task can be added",
        result,
    )
    output_path = tmp_path / "reproduction_result.json"
    output_path.write_text(json.dumps(final_result), encoding="utf-8")
    saved_text = output_path.read_text(encoding="utf-8")
    saved_result = json.loads(saved_text)

    assert saved_result["status"] == "REPRODUCED"
    assert saved_result["executed"] is True
    assert set(saved_result["evidence"]) == {"attempted", "observed", "errors"}
    safe_attempt = saved_result["evidence"]["attempted"][0]
    assert safe_attempt["path"] == "/record"
    assert safe_attempt["method"] == "POST"
    assert safe_attempt["body_type"] == "json"
    assert safe_attempt["data_fields"] == ["name", "password", "api_token"]
    assert safe_attempt["values_redacted"] is True
    assert all(value not in saved_text for value in sensitive_values)
    assert saved_result["evidence"]["observed"][0]["status"] == "REPRODUCED"
    assert saved_result["bugs"][0]["evidence"] == legacy_evidence
    assert "data" not in saved_result["bugs"][0]["attempted"][0]


def test_empty_task_reproduction_records_invoked_action_with_mocked_http(
    monkeypatch, tmp_path
):
    reproduction_agent = load_bug_reproduction_agent_module(monkeypatch)
    monkeypatch.setattr(
        reproduction_agent,
        "read_file",
        Mock(side_effect=["app.post('/addtask')", '<form action="/addtask"><input name="newtask">']),
    )
    responses = [
        {
            "success": True,
            "status_code": 200,
            "body": "<h2>Added Task</h2><li>Existing</li><h2>Completed Tasks</h2>",
            "error": None,
        },
        {"success": True, "status_code": 200, "body": "", "error": None},
        {
            "success": True,
            "status_code": 200,
            "body": "<h2>Added Task</h2><li></li><h2>Completed Tasks</h2>",
            "error": None,
        },
    ]
    request = Mock(side_effect=responses)
    monkeypatch.setattr(reproduction_agent, "http_request", request)

    result = reproduction_agent.reproduce_empty_task_bug(
        os.fspath(tmp_path), "unused", "HTTP", object()
    )

    assert result["status"] == "REPRODUCED"
    assert result["executed"] is True
    assert result["evidence"][-1].startswith("After submitting")
    assert result["attempted"] == [{
        "method": "POST",
        "path": "/addtask",
        "data_fields": ["newtask"],
        "body_type": "form",
        "values_redacted": True,
    }]
    assert request.call_count == 3


def test_remove_task_pre_action_failures_do_not_claim_execution(
    monkeypatch, tmp_path
):
    reproduction_agent = load_bug_reproduction_agent_module(monkeypatch)
    monkeypatch.setattr(
        reproduction_agent,
        "read_file",
        Mock(side_effect=["/removetask", '<input name="check">']),
    )
    request = Mock()
    monkeypatch.setattr(reproduction_agent, "http_request", request)

    result = reproduction_agent.reproduce_remove_task_bug(
        os.fspath(tmp_path), "unused", "HTTP", None
    )

    assert result["status"] == "EXECUTION_ERROR"
    assert result["executed"] is False
    assert result["attempted"] == []
    request.assert_not_called()


def test_remove_task_request_error_records_attempt_without_claiming_success(
    monkeypatch, tmp_path
):
    reproduction_agent = load_bug_reproduction_agent_module(monkeypatch)
    monkeypatch.setattr(
        reproduction_agent,
        "read_file",
        Mock(side_effect=["/removetask", '<input name="check">']),
    )
    monkeypatch.setattr(reproduction_agent.time, "time", lambda: 123)
    active_page = (
        "<h2>Added Task</h2><li>AI_REPRODUCTION_TEST_123</li>"
        "<h2>Completed Tasks</h2>"
    )
    request = Mock(side_effect=[
        {"success": True, "status_code": 200, "body": "", "error": None},
        {"success": True, "status_code": 200, "body": active_page, "error": None},
        {"success": False, "status_code": None, "body": "", "error": "offline"},
        {"success": True, "status_code": 200, "body": active_page, "error": None},
    ])
    monkeypatch.setattr(reproduction_agent, "http_request", request)

    result = reproduction_agent.reproduce_remove_task_bug(
        os.fspath(tmp_path), "unused", "HTTP", object()
    )

    assert result["status"] == "EXECUTION_ERROR"
    assert result["executed"] is True
    assert [item["path"] for item in result["attempted"]] == [
        "/addtask",
        "/removetask",
    ]
    assert request.call_count == 4


def test_bug_diagnosis_agent_uses_configured_run_directory(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "diagnosis"
    bug_report_path = os.fspath(tmp_path / "user-bug-report.json")
    monkeypatch.setenv(ARTIFACT_DIR_ENV, os.fspath(run_dir))
    monkeypatch.setenv("BUG_REPORT_FILE", bug_report_path)

    diagnosis_agent, groq_client, chat_groq = load_bug_diagnosis_config(monkeypatch)

    assert diagnosis_agent.REPOSITORY_CONTEXT_FILE == os.fspath(
        run_dir / "repository_context.json"
    )
    assert diagnosis_agent.REPRODUCTION_RESULT_FILE == os.fspath(
        run_dir / "reproduction_result.json"
    )
    assert diagnosis_agent.OUTPUT_FILE == os.fspath(run_dir / "diagnosis.json")
    assert diagnosis_agent.DEFAULT_BUG_REPORT_FILE == os.path.join(
        diagnosis_agent.BASE_DIR,
        "bug_report.json",
    )
    assert diagnosis_agent.BUG_REPORT_PATH == bug_report_path
    assert not run_dir.exists()
    groq_client.assert_not_called()
    chat_groq.assert_not_called()


def test_bug_diagnosis_agent_preserves_standalone_artifact_paths(monkeypatch):
    monkeypatch.delenv(ARTIFACT_DIR_ENV, raising=False)
    monkeypatch.delenv("BUG_REPORT_FILE", raising=False)

    diagnosis_agent, groq_client, chat_groq = load_bug_diagnosis_config(monkeypatch)

    assert diagnosis_agent.REPOSITORY_CONTEXT_FILE == os.path.join(
        diagnosis_agent.BASE_DIR,
        "repository_context.json",
    )
    assert diagnosis_agent.REPRODUCTION_RESULT_FILE == os.path.join(
        diagnosis_agent.BASE_DIR,
        "reproduction_result.json",
    )
    assert diagnosis_agent.OUTPUT_FILE == os.path.join(
        diagnosis_agent.BASE_DIR,
        "diagnosis.json",
    )
    assert diagnosis_agent.BUG_REPORT_PATH == diagnosis_agent.DEFAULT_BUG_REPORT_FILE
    groq_client.assert_not_called()
    chat_groq.assert_not_called()


def test_bug_diagnosis_agent_import_stops_before_external_initialization(
    monkeypatch,
):
    monkeypatch.delenv(ARTIFACT_DIR_ENV, raising=False)

    diagnosis_agent, groq_client, chat_groq = load_bug_diagnosis_config(monkeypatch)

    assert diagnosis_agent.OUTPUT_FILE.endswith(os.path.join("ai-agent-learning", "diagnosis.json"))
    groq_client.assert_not_called()
    chat_groq.assert_not_called()


def test_bug_diagnosis_agent_cli_bug_report_overrides_environment(
    tmp_path, monkeypatch
):
    cli_report = tmp_path / "cli-bug-report.json"
    environment_report = tmp_path / "environment-bug-report.json"
    cli_report.write_text("{}", encoding="utf-8")
    environment_report.write_text("{}", encoding="utf-8")
    cli_path = os.fspath(cli_report)
    environment_path = os.fspath(environment_report)
    monkeypatch.setenv("BUG_REPORT_FILE", environment_path)
    monkeypatch.setenv("GROQ_API_KEY", "offline-test-key")
    monkeypatch.delenv(ARTIFACT_DIR_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", ["bug_diagnosis_agent.py", cli_path])
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    dotenv_module = ModuleType("dotenv")
    dotenv_module.load_dotenv = Mock()
    langchain_groq_module = ModuleType("langchain_groq")
    langchain_groq_module.ChatGroq = Mock()
    groq_module = ModuleType("groq")
    groq_client = Mock(return_value=object())
    groq_module.Groq = groq_client
    groq_module.APIStatusError = type("APIStatusError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "dotenv", dotenv_module)
    monkeypatch.setitem(sys.modules, "langchain_groq", langchain_groq_module)
    monkeypatch.setitem(sys.modules, "groq", groq_module)

    loaded_report_paths = []

    class StopAfterReportSelected(Exception):
        pass

    def stop_after_report_load(file):
        path = os.fspath(file.name)
        if path in {cli_path, environment_path}:
            loaded_report_paths.append(path)
            raise StopAfterReportSelected(path)
        raise AssertionError(f"Unexpected JSON input before report selection: {path}")

    monkeypatch.setattr(json, "load", stop_after_report_load)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)

    agent_path = Path(__file__).resolve().parents[1] / "bug_diagnosis_agent.py"
    spec = importlib.util.spec_from_file_location(
        "bug_diagnosis_agent_cli_precedence_test",
        agent_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    with pytest.raises(StopAfterReportSelected) as error:
        spec.loader.exec_module(module)

    assert error.value.args == (cli_path,)
    assert loaded_report_paths == [cli_path]
    groq_client.assert_called_once_with(api_key="offline-test-key")


def test_code_fix_agent_uses_configured_workflow_artifact_paths(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "runs" / "code-fix"
    run_dir.mkdir(parents=True)
    (run_dir / "repository_context.json").write_text(
        json.dumps({"repository_path": os.fspath(tmp_path / "target")}),
        encoding="utf-8",
    )
    (run_dir / "diagnosis.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv(ARTIFACT_DIR_ENV, os.fspath(run_dir))

    code_fix_agent, _create_agent = load_code_fix_config(monkeypatch)

    assert code_fix_agent.REPOSITORY_CONTEXT_FILE == os.fspath(
        run_dir / "repository_context.json"
    )
    assert code_fix_agent.DIAGNOSIS_FILE == os.fspath(run_dir / "diagnosis.json")
    assert code_fix_agent.CODE_FIX_RESULT_FILE == os.fspath(
        run_dir / "code_fix_result.json"
    )
    assert not (run_dir / "code_fix_result.json").exists()


def test_code_fix_agent_preserves_standalone_workflow_artifact_paths(
    monkeypatch, tmp_path
):
    monkeypatch.delenv(ARTIFACT_DIR_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "diagnosis.json").write_text("{}", encoding="utf-8")
    expected_base = os.fspath(Path(__file__).resolve().parents[1])
    context_path = os.path.join(expected_base, "repository_context.json")
    original_open = builtins.open

    def open_test_context(file, *args, **kwargs):
        if os.path.abspath(os.fspath(file)) == context_path:
            return io.StringIO(
                json.dumps({"repository_path": os.fspath(tmp_path / "target")})
            )
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_test_context)

    code_fix_agent, _create_agent = load_code_fix_config(monkeypatch)

    assert code_fix_agent.REPOSITORY_CONTEXT_FILE == context_path
    assert code_fix_agent.DIAGNOSIS_FILE == "diagnosis.json"
    assert code_fix_agent.CODE_FIX_RESULT_FILE == os.path.join(
        expected_base,
        "code_fix_result.json",
    )


def test_code_fix_agent_preserves_explicit_diagnosis_input(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "runs" / "code-fix-cli"
    run_dir.mkdir(parents=True)
    (run_dir / "repository_context.json").write_text(
        json.dumps({"repository_path": os.fspath(tmp_path / "target")}),
        encoding="utf-8",
    )
    cli_diagnosis = tmp_path / "user-diagnosis.json"
    original_diagnosis = b'{"affected_files": []}'
    cli_diagnosis.write_bytes(original_diagnosis)
    monkeypatch.setenv(ARTIFACT_DIR_ENV, os.fspath(run_dir))

    code_fix_agent, _create_agent = load_code_fix_config(
        monkeypatch,
        cli_diagnosis,
    )

    assert code_fix_agent.DIAGNOSIS_FILE == os.fspath(cli_diagnosis)
    assert cli_diagnosis.read_bytes() == original_diagnosis
    assert not (run_dir / "code_fix_result.json").exists()


def test_bug_reproduction_agent_uses_configured_run_directory(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "runs" / "reproduction"
    monkeypatch.setenv(ARTIFACT_DIR_ENV, os.fspath(run_dir))

    reproduction_agent = load_bug_reproduction_agent_module(monkeypatch)

    assert reproduction_agent.REPOSITORY_CONTEXT_FILE == os.fspath(
        run_dir / "repository_context.json"
    )
    assert reproduction_agent.REPRODUCTION_RESULT_FILE == os.fspath(
        run_dir / "reproduction_result.json"
    )
    assert reproduction_agent.BUG_REPORT_FILE == os.path.join(
        reproduction_agent.BASE_DIR,
        "bug_report_level2.json",
    )
    assert not run_dir.exists()


def test_bug_reproduction_agent_preserves_standalone_artifact_paths(monkeypatch):
    monkeypatch.delenv(ARTIFACT_DIR_ENV, raising=False)

    reproduction_agent = load_bug_reproduction_agent_module(monkeypatch)

    assert reproduction_agent.REPOSITORY_CONTEXT_FILE == os.path.join(
        reproduction_agent.BASE_DIR,
        "repository_context.json",
    )
    assert reproduction_agent.REPRODUCTION_RESULT_FILE == os.path.join(
        reproduction_agent.BASE_DIR,
        "reproduction_result.json",
    )
    assert reproduction_agent.BUG_REPORT_FILE == os.path.join(
        reproduction_agent.BASE_DIR,
        "bug_report_level2.json",
    )


def test_bug_reproduction_agent_import_does_not_scan_or_run_subprocess(
    monkeypatch,
):
    walk = Mock()
    run = Mock()
    monkeypatch.setattr(os, "walk", walk)
    monkeypatch.setattr(subprocess, "run", run)

    load_bug_reproduction_agent_module(monkeypatch)

    walk.assert_not_called()
    run.assert_not_called()


def test_bug_reproduction_agent_cli_bug_report_overrides_environment(
    tmp_path, monkeypatch
):
    cli_bug_report = tmp_path / "cli-bug-report.json"
    environment_bug_report = tmp_path / "environment-bug-report.json"
    cli_bug_report.write_text("{}", encoding="utf-8")
    environment_bug_report.write_text("{}", encoding="utf-8")
    cli_path = os.fspath(cli_bug_report)
    environment_path = os.fspath(environment_bug_report)
    monkeypatch.setenv("BUG_REPORT_FILE", environment_path)
    monkeypatch.setattr(sys, "argv", ["bug_reproduction_agent.py", cli_path])

    reproduction_agent = load_bug_reproduction_agent_module(monkeypatch)
    loaded_paths = []

    class StopAfterCliBugReportRead(Exception):
        pass

    def fake_load_json(path):
        loaded_paths.append(path)
        if path == reproduction_agent.REPOSITORY_CONTEXT_FILE:
            return {}
        if path == cli_path:
            raise StopAfterCliBugReportRead
        raise AssertionError(f"Unexpected JSON input: {path}")

    monkeypatch.setattr(reproduction_agent.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(reproduction_agent, "load_json", fake_load_json)

    with pytest.raises(StopAfterCliBugReportRead):
        reproduction_agent.main()

    assert loaded_paths == [reproduction_agent.REPOSITORY_CONTEXT_FILE, cli_path]
    assert environment_path not in loaded_paths


def test_orchestrator_creates_unique_run_directories(tmp_path):
    first_run = workflow_orchestrator.create_run_artifact_dir(tmp_path)
    second_run = workflow_orchestrator.create_run_artifact_dir(tmp_path)

    assert first_run != second_run
    assert Path(first_run).parent == tmp_path / "workflow_runs"
    assert Path(second_run).parent == tmp_path / "workflow_runs"
    assert Path(first_run).is_dir()
    assert Path(second_run).is_dir()


def test_orchestrator_uses_current_run_diagnosis_and_fix_result_paths(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(workflow_orchestrator, "BASE_DIR", os.fspath(tmp_path))
    monkeypatch.setattr(
        workflow_orchestrator,
        "DIAGNOSIS_FILE",
        workflow_orchestrator.DIAGNOSIS_FILE,
    )
    monkeypatch.setattr(
        workflow_orchestrator,
        "CODE_FIX_RESULT_FILE",
        workflow_orchestrator.CODE_FIX_RESULT_FILE,
    )
    run_dir = tmp_path / "workflow_runs" / "current-run"
    run_dir.mkdir(parents=True)
    diagnosis = {
        "bug_title": "Example",
        "project_type": "Node.js",
        "failure_analysis": "Failure",
        "root_cause": "Cause",
        "affected_files": ["app.js"],
        "evidence": ["Observed"],
        "proposed_fix": "Fix",
        "verification_plan": ["Test"],
    }
    diagnosis_path = run_dir / "diagnosis.json"
    diagnosis_path.write_text(json.dumps(diagnosis), encoding="utf-8")
    root_fix_result = tmp_path / "code_fix_result.json"
    root_fix_result.write_bytes(b"historical-root-result")
    original_root_result = root_fix_result.read_bytes()

    workflow_orchestrator.configure_run_artifact_paths(os.fspath(run_dir))

    assert workflow_orchestrator.DIAGNOSIS_FILE == os.fspath(diagnosis_path)
    assert workflow_orchestrator.CODE_FIX_RESULT_FILE == os.fspath(
        run_dir / "code_fix_result.json"
    )
    assert workflow_orchestrator.validate_diagnosis() is True

    calls = []

    def fake_run_agent(script_name, arguments):
        calls.append((script_name, arguments))
        Path(workflow_orchestrator.CODE_FIX_RESULT_FILE).write_text(
            json.dumps({"status": "SUCCESS", "modified_files": []}),
            encoding="utf-8",
        )
        return {"success": True, "return_code": 0}

    monkeypatch.setattr(workflow_orchestrator, "run_agent", fake_run_agent)
    result, fix_result = workflow_orchestrator.run_code_fix_stage()

    assert calls == [
        (workflow_orchestrator.AGENTS["code_fix"], [os.fspath(diagnosis_path)])
    ]
    assert result["success"] is True
    assert fix_result["status"] == "SUCCESS"
    assert root_fix_result.read_bytes() == original_root_result


def test_orchestrator_preserves_standalone_diagnosis_and_fix_result_paths(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(workflow_orchestrator, "BASE_DIR", os.fspath(tmp_path))
    monkeypatch.setattr(
        workflow_orchestrator,
        "DIAGNOSIS_FILE",
        workflow_orchestrator.DIAGNOSIS_FILE,
    )
    monkeypatch.setattr(
        workflow_orchestrator,
        "CODE_FIX_RESULT_FILE",
        workflow_orchestrator.CODE_FIX_RESULT_FILE,
    )
    monkeypatch.delenv(ARTIFACT_DIR_ENV, raising=False)

    workflow_orchestrator.configure_run_artifact_paths()

    assert workflow_orchestrator.ACTIVE_RUN_ARTIFACT_DIR is None
    assert workflow_orchestrator.DIAGNOSIS_FILE == os.path.join(
        os.fspath(tmp_path),
        "diagnosis.json",
    )
    assert workflow_orchestrator.CODE_FIX_RESULT_FILE == os.path.join(
        os.fspath(tmp_path),
        "code_fix_result.json",
    )


def test_retry_context_reads_diagnosis_from_current_run_directory(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(workflow_orchestrator, "BASE_DIR", os.fspath(tmp_path))
    monkeypatch.setattr(
        workflow_orchestrator,
        "DIAGNOSIS_FILE",
        workflow_orchestrator.DIAGNOSIS_FILE,
    )
    monkeypatch.setattr(
        workflow_orchestrator,
        "CODE_FIX_RESULT_FILE",
        workflow_orchestrator.CODE_FIX_RESULT_FILE,
    )
    run_dir = tmp_path / "workflow_runs" / "retry-run"
    run_dir.mkdir(parents=True)
    current_diagnosis = {"root_cause": "current-run diagnosis"}
    (run_dir / "diagnosis.json").write_text(
        json.dumps(current_diagnosis),
        encoding="utf-8",
    )
    root_diagnosis = tmp_path / "diagnosis.json"
    root_diagnosis.write_bytes(b"historical-root-diagnosis")
    original_root_diagnosis = root_diagnosis.read_bytes()

    workflow_orchestrator.configure_run_artifact_paths(os.fspath(run_dir))
    retry_context = workflow_orchestrator.load_retry_failure_context()

    assert retry_context["previous_diagnosis"] == current_diagnosis
    assert root_diagnosis.read_bytes() == original_root_diagnosis


def test_diagnosis_archives_are_isolated_between_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow_orchestrator, "BASE_DIR", os.fspath(tmp_path))
    monkeypatch.setattr(
        workflow_orchestrator,
        "DIAGNOSIS_FILE",
        workflow_orchestrator.DIAGNOSIS_FILE,
    )
    monkeypatch.setattr(
        workflow_orchestrator,
        "CODE_FIX_RESULT_FILE",
        workflow_orchestrator.CODE_FIX_RESULT_FILE,
    )
    root_archive = tmp_path / "diagnosis_attempt_1.json"
    root_archive.write_bytes(b"historical-archive")

    archive_paths = []
    for run_name, diagnosis in (
        ("run-one", {"root_cause": "first"}),
        ("run-two", {"root_cause": "second"}),
    ):
        run_dir = tmp_path / "workflow_runs" / run_name
        run_dir.mkdir(parents=True)
        (run_dir / "diagnosis.json").write_text(
            json.dumps(diagnosis),
            encoding="utf-8",
        )
        workflow_orchestrator.configure_run_artifact_paths(os.fspath(run_dir))
        archive_paths.append(workflow_orchestrator.archive_diagnosis(1))

    assert archive_paths == [
        os.fspath(tmp_path / "workflow_runs" / "run-one" / "diagnosis_attempt_1.json"),
        os.fspath(tmp_path / "workflow_runs" / "run-two" / "diagnosis_attempt_1.json"),
    ]
    assert json.loads(Path(archive_paths[0]).read_text(encoding="utf-8")) == {
        "root_cause": "first"
    }
    assert json.loads(Path(archive_paths[1]).read_text(encoding="utf-8")) == {
        "root_cause": "second"
    }
    assert root_archive.read_bytes() == b"historical-archive"


def test_retry_reports_are_isolated_between_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow_orchestrator, "BASE_DIR", os.fspath(tmp_path))
    monkeypatch.setattr(
        workflow_orchestrator,
        "ACTIVE_BUG_REPORT_FILE",
        os.fspath(tmp_path / "bug_report.json"),
    )
    monkeypatch.setattr(
        workflow_orchestrator,
        "DIAGNOSIS_FILE",
        workflow_orchestrator.DIAGNOSIS_FILE,
    )
    monkeypatch.setattr(
        workflow_orchestrator,
        "CODE_FIX_RESULT_FILE",
        workflow_orchestrator.CODE_FIX_RESULT_FILE,
    )
    (tmp_path / "bug_report.json").write_text(
        json.dumps({"bug_title": "Original bug"}),
        encoding="utf-8",
    )
    root_retry_report = tmp_path / "bug_report_retry_2.json"
    root_retry_report.write_bytes(b"historical-retry-report")

    retry_paths = []
    for run_name, failure_context in (
        ("run-one", {"failure": "first"}),
        ("run-two", {"failure": "second"}),
    ):
        run_dir = tmp_path / "workflow_runs" / run_name
        run_dir.mkdir(parents=True)
        workflow_orchestrator.configure_run_artifact_paths(os.fspath(run_dir))
        retry_paths.append(
            workflow_orchestrator.create_retry_bug_report(2, failure_context)
        )

    assert retry_paths == [
        os.fspath(tmp_path / "workflow_runs" / "run-one" / "bug_report_retry_2.json"),
        os.fspath(tmp_path / "workflow_runs" / "run-two" / "bug_report_retry_2.json"),
    ]
    assert json.loads(Path(retry_paths[0]).read_text(encoding="utf-8"))["failure_context"] == {
        "failure": "first"
    }
    assert json.loads(Path(retry_paths[1]).read_text(encoding="utf-8"))["failure_context"] == {
        "failure": "second"
    }
    assert root_retry_report.read_bytes() == b"historical-retry-report"


def test_archive_and_retry_reports_keep_standalone_root_paths(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(workflow_orchestrator, "BASE_DIR", os.fspath(tmp_path))
    monkeypatch.setattr(
        workflow_orchestrator,
        "ACTIVE_BUG_REPORT_FILE",
        os.fspath(tmp_path / "bug_report.json"),
    )
    monkeypatch.setattr(
        workflow_orchestrator,
        "DIAGNOSIS_FILE",
        workflow_orchestrator.DIAGNOSIS_FILE,
    )
    monkeypatch.setattr(
        workflow_orchestrator,
        "CODE_FIX_RESULT_FILE",
        workflow_orchestrator.CODE_FIX_RESULT_FILE,
    )
    monkeypatch.delenv(ARTIFACT_DIR_ENV, raising=False)
    (tmp_path / "bug_report.json").write_text("{}", encoding="utf-8")
    (tmp_path / "diagnosis.json").write_text(
        json.dumps({"root_cause": "standalone"}),
        encoding="utf-8",
    )

    workflow_orchestrator.configure_run_artifact_paths()
    archive_path = workflow_orchestrator.archive_diagnosis(1)
    retry_path = workflow_orchestrator.create_retry_bug_report(2, {})

    assert archive_path == os.fspath(tmp_path / "diagnosis_attempt_1.json")
    assert retry_path == os.fspath(tmp_path / "bug_report_retry_2.json")


def test_run_agent_passes_child_environment_without_mutating_parent(
    monkeypatch, tmp_path
):
    run_dir = os.fspath(tmp_path / "run-current")
    monkeypatch.setattr(workflow_orchestrator, "ACTIVE_RUN_ARTIFACT_DIR", run_dir)
    monkeypatch.setattr(
        workflow_orchestrator,
        "CHILD_ENV_OVERRIDES",
        {"BUG_REPORT_FILE": "selected-bug.json"},
    )
    monkeypatch.setenv(ARTIFACT_DIR_ENV, "parent-artifact-directory")
    monkeypatch.setenv("WORKFLOW_TEST_SENTINEL", "keep-parent-value")
    original_environment = os.environ.copy()
    captured = {}

    def fake_subprocess_run(command, **kwargs):
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(workflow_orchestrator.subprocess, "run", fake_subprocess_run)

    result = workflow_orchestrator.run_agent("mock_agent.py", ["input.json"])

    assert result["success"] is True
    assert captured["command"][-2:] == [
        os.path.join(workflow_orchestrator.BASE_DIR, "mock_agent.py"),
        "input.json",
    ]
    assert captured["environment"] is not os.environ
    assert captured["environment"][ARTIFACT_DIR_ENV] == run_dir
    assert captured["environment"]["BUG_REPORT_FILE"] == "selected-bug.json"
    assert captured["environment"]["WORKFLOW_TEST_SENTINEL"] == "keep-parent-value"
    assert os.environ[ARTIFACT_DIR_ENV] == "parent-artifact-directory"
    assert os.environ == original_environment


def test_main_preserves_existing_root_artifacts_at_run_start(
    monkeypatch, tmp_path
):
    artifact_names = (
        "repository_context.json",
        "reproduction_result.json",
        "diagnosis.json",
        "test_result.json",
        "verification_report.json",
        "workflow_report.json",
        "build_result.json",
        "validation_result.json",
        "code_fix_result.json",
    )
    existing_artifacts = []
    for name in artifact_names:
        artifact = tmp_path / name
        artifact.write_bytes(f"historical:{name}".encode("utf-8"))
        existing_artifacts.append((artifact, artifact.read_bytes()))

    monkeypatch.setattr(workflow_orchestrator, "BASE_DIR", os.fspath(tmp_path))
    monkeypatch.setattr(
        workflow_orchestrator,
        "ACTIVE_BUG_REPORT_FILE",
        workflow_orchestrator.BUG_REPORT_FILE,
    )
    monkeypatch.setattr(workflow_orchestrator, "ACTIVE_RUN_ARTIFACT_DIR", None)
    monkeypatch.setattr(workflow_orchestrator, "CHILD_ENV_OVERRIDES", {})
    monkeypatch.setattr(
        workflow_orchestrator,
        "DIAGNOSIS_FILE",
        workflow_orchestrator.DIAGNOSIS_FILE,
    )
    monkeypatch.setattr(
        workflow_orchestrator,
        "CODE_FIX_RESULT_FILE",
        workflow_orchestrator.CODE_FIX_RESULT_FILE,
    )
    monkeypatch.setattr(
        workflow_orchestrator,
        "STALE_ARTIFACTS",
        tuple(os.fspath(path) for path, _ in existing_artifacts),
    )
    create_run_dir = workflow_orchestrator.create_run_artifact_dir
    monkeypatch.setattr(
        workflow_orchestrator,
        "create_run_artifact_dir",
        lambda: create_run_dir(tmp_path),
    )
    monkeypatch.setattr(
        workflow_orchestrator,
        "run_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stop after startup")),
    )
    monkeypatch.setattr(sys, "argv", ["workflow_orchestrator.py", "", "bug_report.json"])

    try:
        workflow_orchestrator.main()
    except RuntimeError as error:
        assert str(error) == "stop after startup"
    else:
        raise AssertionError("The mocked first stage should stop the workflow.")

    for artifact, original_bytes in existing_artifacts:
        assert artifact.read_bytes() == original_bytes

    assert not (tmp_path / "workflow_artifact_archive").exists()
    assert len(list((tmp_path / "workflow_runs").iterdir())) == 1


def create_validation_repository(repo_path):
    frontend_path = repo_path / "frontend"
    backend_path = repo_path / "backend"
    frontend_path.mkdir(parents=True)
    backend_path.mkdir(parents=True)
    jest_runner = backend_path / "node_modules" / "jest" / "bin" / "jest.js"
    jest_runner.parent.mkdir(parents=True)
    jest_runner.write_text("", encoding="utf-8")
    return {
        "repository_path": os.fspath(repo_path),
        "execution_strategy": {
            "technology": "Node.js",
            "test_command": "npm test",
        },
        "architecture": {
            "components": [
                {
                    "name": "frontend",
                    "type": "frontend",
                    "path": "frontend",
                    "build_command": "npm run build",
                    "test_command": "npm run lint",
                },
                {
                    "name": "backend",
                    "type": "backend",
                    "path": "backend",
                    "test_framework": "Jest",
                    "test_command": [
                        "node",
                        os.path.join("node_modules", "jest", "bin", "jest.js"),
                        "--runInBand",
                    ],
                },
            ]
        },
    }


def test_workflow_argument_parser_preserves_github_url_and_bug_report():
    repository_url = "https://example.invalid/org/project.git"
    bug_report = "user-report.json"

    arguments = workflow_orchestrator.parse_workflow_arguments(
        [repository_url, bug_report]
    )

    assert arguments.repository_url == repository_url
    assert arguments.bug_report == bug_report
    assert arguments.local_repository is None
    assert arguments.validation_only is False


def test_workflow_argument_parser_accepts_local_path_and_positional_report(
    tmp_path,
):
    repository = tmp_path / "local-project"
    repository.mkdir()

    arguments = workflow_orchestrator.parse_workflow_arguments([
        "--local-repository",
        os.fspath(repository),
        "--validation-only",
        "user-report.json",
    ])

    assert arguments.repository_url is None
    assert arguments.local_repository == os.path.abspath(repository)
    assert arguments.bug_report == "user-report.json"
    assert arguments.validation_only is True


def test_workflow_argument_parser_rejects_missing_local_directory(tmp_path):
    with pytest.raises(SystemExit):
        workflow_orchestrator.parse_workflow_arguments([
            "--local-repository",
            os.fspath(tmp_path / "missing"),
        ])

    with pytest.raises(SystemExit):
        workflow_orchestrator.parse_workflow_arguments(["--validation-only"])


def test_workflow_argument_parser_accepts_local_websocket_demo(tmp_path):
    repository = tmp_path / "local-project"
    repository.mkdir()

    arguments = workflow_orchestrator.parse_workflow_arguments([
        "--local-repository",
        os.fspath(repository),
        "--local-websocket-demo",
    ])

    assert arguments.local_repository == os.path.abspath(repository)
    assert arguments.local_websocket_demo is True
    assert arguments.validation_only is False


def test_ai_diagnosis_flag_requires_local_websocket_demo(tmp_path):
    repository = tmp_path / "local-project"
    repository.mkdir()

    arguments = workflow_orchestrator.parse_workflow_arguments([
        "--local-repository",
        os.fspath(repository),
        "--local-websocket-demo",
        "--ai-diagnosis",
    ])
    assert arguments.ai_diagnosis is True

    with pytest.raises(SystemExit):
        workflow_orchestrator.parse_workflow_arguments([
            "--local-repository",
            os.fspath(repository),
            "--ai-diagnosis",
        ])


def test_local_repository_scan_is_read_only_and_skips_generated_directories(
    tmp_path, monkeypatch
):
    repository_agent = load_repository_agent_module(monkeypatch)
    repository = tmp_path / "repository"
    repository.mkdir()
    source_file = repository / "README.md"
    source_file.write_text("local project", encoding="utf-8")
    generated_files = []
    for directory in ("node_modules", "dist", ".serverless"):
        generated_file = repository / directory / "generated.txt"
        generated_file.parent.mkdir()
        generated_file.write_text("keep", encoding="utf-8")
        generated_files.append(generated_file)
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in [source_file, *generated_files]
    }

    files = repository_agent.scan_repository(os.fspath(repository))

    assert files == ["README.md"]
    assert all(path.read_bytes() == content for path, (content, _) in before.items())
    assert all(
        path.stat().st_mtime_ns == modified
        for path, (_, modified) in before.items()
    )
    assert repository_agent.resolve_local_repository_path(os.fspath(repository)) == os.fspath(repository)
    with pytest.raises(ValueError):
        repository_agent.resolve_local_repository_path(os.fspath(tmp_path / "missing"))


def test_local_component_detection_finds_frontend_and_serverless_backend(
    tmp_path, monkeypatch
):
    repository_agent = load_repository_agent_module(monkeypatch)
    repository = tmp_path / "level3"
    frontend = repository / "frontend"
    backend = repository / "backend"
    (frontend / "src").mkdir(parents=True)
    (backend / "src").mkdir(parents=True)
    (frontend / "src" / "App.tsx").write_text("export {}", encoding="utf-8")
    (backend / "src" / "websocket.js").write_text("", encoding="utf-8")
    (backend / "serverless.yml").write_text(
        "service: test\n"
        "provider:\n"
        "  name: aws\n"
        "  runtime: nodejs22.x\n"
        "functions:\n"
        "  message:\n"
        "    handler: src/websocket.message\n"
        "    events:\n"
        "      - websocket:\n"
        "          route: message\n"
        "resources:\n"
        "  Resources:\n"
        "    MessagesTable:\n"
        "      Type: AWS::DynamoDB::Table\n",
        encoding="utf-8",
    )
    (repository / "package.json").write_text(json.dumps({
        "devDependencies": {"serverless": "1"},
    }), encoding="utf-8")
    (frontend / "package.json").write_text(json.dumps({
        "dependencies": {"react": "1", "vite": "1"},
        "scripts": {"build": "vite build", "lint": "eslint ."},
    }), encoding="utf-8")
    (backend / "package.json").write_text(json.dumps({
        "dependencies": {
            "@aws-sdk/client-apigatewaymanagementapi": "1",
            "@aws-sdk/client-dynamodb": "1",
        },
        "devDependencies": {"jest": "1", "serverless": "1"},
        "scripts": {"test": "jest"},
    }), encoding="utf-8")

    context = repository_agent.build_repository_context(
        os.fspath(repository),
        repository.as_uri(),
    )
    components = context["architecture"]["components"]
    detected = {component["path"]: component for component in components}

    assert detected["."]["type"] == "application"
    assert detected["frontend"]["type"] == "frontend"
    assert detected["frontend"]["build_command"] == "npm run build"
    assert detected["frontend"]["test_command"] == "npm run lint"
    assert detected["backend"]["type"] == "backend"
    assert detected["backend"]["technology"] == "AWS Lambda"
    assert detected["backend"]["runtime"] == "nodejs22.x"
    assert detected["backend"]["test_framework"] == "Jest"
    assert detected["backend"]["test_command"] == [
        "node",
        os.path.join("node_modules", "jest", "bin", "jest.js"),
        "--runInBand",
    ]
    technologies = {project["technology"] for project in context["project_types"]}
    assert {
        "AWS Lambda",
        "API Gateway WebSocket",
        "DynamoDB",
    }.issubset(technologies)
    assert any(
        component["technology"] == "API Gateway WebSocket"
        for component in components
    )
    assert any(
        component["technology"] == "DynamoDB"
        for component in components
    )
    strategy = context["execution_strategy"]
    assert strategy["technology"] == "React/Vite + AWS Serverless"
    assert strategy["backend"] == "AWS Lambda"
    assert strategy["backend_runtime"] == "nodejs22.x"
    assert strategy["api_gateway"] == "WebSocket"
    assert strategy["database"] == "DynamoDB"
    assert strategy["start_command"] is None
    assert strategy["test_command"] == [
        "node",
        os.path.join("backend", "node_modules", "jest", "bin", "jest.js"),
        "--runInBand",
    ]
    handler_path = os.path.join("backend", "src", "websocket.js")
    assert handler_path in context["important_files"]
    assert context["file_contents"][handler_path] == ""


def test_execution_strategy_preserves_generic_node_and_mern_defaults(monkeypatch):
    repository_agent = load_repository_agent_module(monkeypatch)

    node_strategy = repository_agent.detect_execution_strategy([
        {"technology": "Node.js"},
    ])
    mern_strategy = repository_agent.detect_execution_strategy([
        {"technology": "Node.js"},
        {"technology": "React"},
        {"technology": "Express"},
        {"technology": "MongoDB"},
    ])

    assert node_strategy["technology"] == "Node.js"
    assert node_strategy["start_command"] == "node index.js"
    assert node_strategy["test_command"] == "node --test"
    assert mern_strategy["technology"] == "MERN"
    assert mern_strategy["backend_base_url"] == "http://localhost:5050"
    assert mern_strategy["test_command"] == "npm test"


def test_repository_agent_keeps_positional_github_url_compatibility(monkeypatch):
    repository_agent = load_repository_agent_module(monkeypatch)
    github_url = "https://github.com/example/legacy-project.git"
    monkeypatch.setattr(sys, "argv", ["repository_agent.py", github_url])
    monkeypatch.setenv("GITHUB_REPOSITORY_URL", "https://example.invalid/ignored.git")

    assert repository_agent.get_repository_url() == github_url


def test_validation_only_runs_only_offline_local_commands(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    context = create_validation_repository(repository)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    workflow_orchestrator.configure_run_artifact_paths(os.fspath(run_dir))
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="passed", stderr="")

    monkeypatch.setattr(workflow_orchestrator.subprocess, "run", fake_run)

    result = workflow_orchestrator.run_validation_only(context)

    assert result["status"] == "PASSED"
    assert result["application_started"] is False
    assert result["dependency_installation"] is False
    assert result["aws_accessed"] is False
    assert [command for command, _ in calls] == [
        workflow_orchestrator.command_parts(["npm", "run", "build"]),
        workflow_orchestrator.command_parts(["npm", "run", "lint"]),
        ["node", os.path.join("node_modules", "jest", "bin", "jest.js"), "--runInBand"],
    ]
    assert all(kwargs["env"]["npm_config_offline"] == "true" for _, kwargs in calls)
    assert all("npx" not in command[0].lower() for command, _ in calls)
    assert all(kwargs["cwd"].startswith(os.fspath(repository)) for _, kwargs in calls)


def test_validation_only_requires_installed_local_jest_runner(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    context = create_validation_repository(repository)
    runner = repository / "backend" / "node_modules" / "jest" / "bin" / "jest.js"
    runner.unlink()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    workflow_orchestrator.configure_run_artifact_paths(os.fspath(run_dir))
    calls = []
    monkeypatch.setattr(
        workflow_orchestrator.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command)
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = workflow_orchestrator.run_validation_only(context)

    assert result["status"] == "FAILED"
    assert len(calls) == 2
    assert all("node" not in command[0].lower() for command in calls)
    assert any(
        item.get("error") == "Installed local Jest runner was not detected."
        for item in result["checks"]
    )


def test_validation_only_main_stops_after_analysis_and_isolates_artifacts(
    tmp_path, monkeypatch
):
    repository = tmp_path / "local-repository"
    context = create_validation_repository(repository)
    user_bug_report = tmp_path / "user-report.json"
    user_bug_report.write_bytes(b"user supplied report")
    original_bug_report = user_bug_report.read_bytes()
    historical_artifacts = {}
    for name in (
        "repository_context.json",
        "reproduction_result.json",
        "diagnosis.json",
        "code_fix_result.json",
        "test_generation_result.json",
        "test_result.json",
        "build_result.json",
        "validation_result.json",
        "verification_report.json",
        "workflow_report.json",
    ):
        artifact = tmp_path / name
        artifact.write_bytes(f"historical:{name}".encode("utf-8"))
        historical_artifacts[artifact] = artifact.read_bytes()
    run_dir = tmp_path / "workflow_runs" / "validation"
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(workflow_orchestrator, "BASE_DIR", os.fspath(tmp_path))
    monkeypatch.setattr(
        workflow_orchestrator,
        "create_run_artifact_dir",
        lambda: os.fspath(run_dir),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "workflow_orchestrator.py",
            "--local-repository",
            os.fspath(repository),
            "--validation-only",
            os.fspath(user_bug_report),
        ],
    )
    agent_calls = []

    def fake_run_agent(script_name, _arguments=None):
        agent_calls.append(script_name)
        Path(workflow_orchestrator.REPOSITORY_CONTEXT_FILE).write_text(
            json.dumps(context),
            encoding="utf-8",
        )
        return {"success": True, "return_code": 0}

    monkeypatch.setattr(workflow_orchestrator, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        workflow_orchestrator.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="passed",
            stderr="",
        ),
    )

    return_code = workflow_orchestrator.main()

    assert return_code == 0
    assert agent_calls == [workflow_orchestrator.AGENTS["repository_analysis"]]
    assert workflow_orchestrator.CHILD_ENV_OVERRIDES["LOCAL_REPOSITORY_PATH"] == os.fspath(repository)
    assert workflow_orchestrator.CHILD_ENV_OVERRIDES["BUG_REPORT_FILE"] == os.fspath(user_bug_report)
    assert json.loads((run_dir / "workflow_report.json").read_text(encoding="utf-8"))["final_status"] == "PASS"
    assert (run_dir / "repository_context.json").exists()
    assert (run_dir / "test_result.json").exists()
    assert (run_dir / "verification_report.json").exists()
    assert user_bug_report.read_bytes() == original_bug_report
    assert all(path.read_bytes() == content for path, content in historical_artifacts.items())


def test_all_json_artifact_names_resolve_under_each_active_run(tmp_path, monkeypatch):
    artifact_names = (
        "repository_context.json",
        "reproduction_result.json",
        "diagnosis.json",
        "code_fix_result.json",
        "test_generation_result.json",
        "test_result.json",
        "build_result.json",
        "validation_result.json",
        "verification_report.json",
        "workflow_report.json",
    )
    resolved_runs = []

    for run_name in ("first", "second"):
        run_dir = tmp_path / "workflow_runs" / run_name
        monkeypatch.setenv(ARTIFACT_DIR_ENV, os.fspath(run_dir))
        resolved_runs.append([
            resolve_artifact_path(name, tmp_path)
            for name in artifact_names
        ])

    assert resolved_runs[0] == [
        os.fspath(tmp_path / "workflow_runs" / "first" / name)
        for name in artifact_names
    ]
    assert resolved_runs[1] == [
        os.fspath(tmp_path / "workflow_runs" / "second" / name)
        for name in artifact_names
    ]
    assert not (tmp_path / "workflow_runs" / "first").exists()
    assert not (tmp_path / "workflow_runs" / "second").exists()


def build_level3_repository_context(monkeypatch):
    repository_agent = load_repository_agent_module(monkeypatch)
    repository = Path(__file__).resolve().parents[1] / "level3-serverless-app"
    return repository_agent.build_repository_context(
        os.fspath(repository),
        repository.as_uri(),
    )


def test_level3_demo_reproduces_fixes_and_verifies_disposable_copy(
    tmp_path, monkeypatch
):
    context = build_level3_repository_context(monkeypatch)
    source_repository = Path(context["repository_path"])
    source_handler = source_repository / "backend" / "src" / "websocket.js"
    source_schema = source_repository / "backend" / "serverless.yml"
    source_before = {
        source_handler: source_handler.read_bytes(),
        source_schema: source_schema.read_bytes(),
    }
    run_dir = tmp_path / "workflow_runs" / "demo-run"
    run_dir.mkdir(parents=True)
    historical_artifact = tmp_path / "test_result.json"
    historical_artifact.write_bytes(b"historical root result")
    historical_bug_report = tmp_path / local_websocket_demo.DEMO_BUG_REPORT_NAME
    historical_bug_report.write_bytes(b"user-owned report")

    result = local_websocket_demo.run_level3_websocket_demo(
        context,
        os.fspath(run_dir),
    )

    demo_repository = Path(result["disposable_project"])
    demo_handler = demo_repository / "backend" / "src" / "websocket.js"
    demo_schema = demo_repository / "backend" / "serverless.yml"
    assert result["final_status"] == "PASS"
    assert result["diagnosis_mode"] == "deterministic"
    assert result["model_call"]["attempted"] is False
    assert result["ai_powered_stages"] == []
    assert "Deterministic evidence diagnosis" in result["workflow_logic"]
    assert not any("AI Code Fix" in item for item in result["workflow_logic"])
    assert result["baseline_reproduced"] is True
    assert result["root_cause_identified"] is True
    assert result["fix_applied"] is True
    assert result["post_fix_verified"] is True
    assert result["aws_accessed"] is False
    assert result["network_calls"] is False
    baseline_test_result = result["stages"][0]["details"]["test_result"]
    baseline_output = baseline_test_result["stdout"] + baseline_test_result["stderr"]
    assert baseline_test_result["tests"] == 12
    assert baseline_test_result["passed"] == 11
    assert baseline_test_result["failed"] == 1
    assert "Expected: 2" in baseline_output
    assert "Received: 1" in baseline_output
    assert result["attempts"][0]["verified"] is True
    assert "messageId" in demo_handler.read_text(encoding="utf-8")
    assert "messageId" in demo_schema.read_text(encoding="utf-8")
    assert all(path.read_bytes() == content for path, content in source_before.items())
    assert historical_artifact.read_bytes() == b"historical root result"
    assert historical_bug_report.read_bytes() == b"user-owned report"
    assert (run_dir / local_websocket_demo.DEMO_BUG_REPORT_NAME).is_file()
    assert (run_dir / "reproduction_result.json").is_file()
    assert (run_dir / "diagnosis.json").is_file()
    assert (run_dir / "code_fix_result.json").is_file()
    assert (run_dir / "test_result.json").is_file()
    assert (run_dir / "verification_report.json").is_file()


def test_level3_demo_retries_after_post_fix_verification_failure(
    tmp_path, monkeypatch
):
    context = build_level3_repository_context(monkeypatch)
    run_dir = tmp_path / "workflow_runs" / "retry-run"
    run_dir.mkdir(parents=True)
    outputs = [
        (1, "FAIL Level 3 demo retains each accepted message\nExpected: 2\nReceived: 1\nTests: 1 failed, 12 total"),
        (1, "Tests: 1 failed, 12 total"),
        (0, "Tests: 12 passed, 12 total"),
    ]
    calls = []

    def fake_command_runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=outputs[len(calls) - 1][0],
            stdout=outputs[len(calls) - 1][1],
            stderr="",
        )

    result = local_websocket_demo.run_level3_websocket_demo(
        context,
        os.fspath(run_dir),
        max_retries=1,
        command_runner=fake_command_runner,
    )

    assert result["final_status"] == "PASS"
    assert len(calls) == 3
    assert [attempt["attempt"] for attempt in result["attempts"]] == [1, 2]
    assert result["attempts"][0]["verified"] is False
    assert result["attempts"][1]["verified"] is True
    assert (run_dir / "diagnosis_retry_2.json").is_file()


def test_level3_demo_does_not_accept_passing_command_without_semantic_fix(
    tmp_path, monkeypatch
):
    context = build_level3_repository_context(monkeypatch)
    run_dir = tmp_path / "workflow_runs" / "unverified-run"
    run_dir.mkdir(parents=True)
    outputs = [
        (1, "FAIL Level 3 demo retains each accepted message\nExpected: 2\nReceived: 1\nTests: 1 failed, 12 total"),
        (0, "Tests: 12 passed, 12 total"),
        (0, "Tests: 12 passed, 12 total"),
        (0, "Tests: 12 passed, 12 total"),
    ]
    call_count = 0

    def fake_command_runner(_command, **_kwargs):
        nonlocal call_count
        output = outputs[call_count]
        call_count += 1
        return SimpleNamespace(returncode=output[0], stdout=output[1], stderr="")

    def no_op_fixer(_demo_repository, _attempt):
        return {
            "status": "SUCCESS",
            "modified_files": ["backend/src/websocket.js"],
        }

    result = local_websocket_demo.run_level3_websocket_demo(
        context,
        os.fspath(run_dir),
        max_retries=2,
        command_runner=fake_command_runner,
        fixer=no_op_fixer,
    )

    assert result["final_status"] == "FAIL"
    assert result["post_fix_verified"] is False
    assert len(result["attempts"]) == 3
    assert all(attempt["test_result"]["status"] == "PASSED" for attempt in result["attempts"])
    assert all(not attempt["semantic_fix_present"] for attempt in result["attempts"])
    assert all(not attempt["verified"] for attempt in result["attempts"])
    fix_result = json.loads(
        (run_dir / "code_fix_result.json").read_text(encoding="utf-8")
    )
    verification = json.loads(
        (run_dir / "verification_report.json").read_text(encoding="utf-8")
    )
    assert fix_result["status"] == "FAILED"
    assert fix_result["modified_files"] == []
    assert verification["status"] == "FAIL"


def groq_diagnosis_fixture():
    return {
        "bug_title": "Messages overwrite",
        "project_type": "AWS Lambda WebSocket",
        "failure_analysis": "Two writes use the same key.",
        "root_cause": "The partition key does not distinguish messages.",
        "affected_files": ["backend/src/websocket.js"],
        "evidence": ["The local repro observed one row instead of two."],
        "proposed_fix": "Add a unique message sort key.",
        "verification_plan": ["Run the same local regression test."],
    }


def test_groq_diagnosis_schema_requires_typed_nonempty_fields():
    valid_diagnosis = groq_diagnosis_fixture()

    assert local_websocket_demo.validate_groq_diagnosis(valid_diagnosis) == {
        "valid": True,
        "errors": [],
    }

    invalid_diagnosis = {
        **valid_diagnosis,
        "affected_files": "backend/src/websocket.js",
        "root_cause": "",
    }
    validation = local_websocket_demo.validate_groq_diagnosis(invalid_diagnosis)

    assert validation["valid"] is False
    assert any("affected_files must be list" in error for error in validation["errors"])
    assert any("root_cause must not be empty" in error for error in validation["errors"])


def test_run_groq_diagnosis_uses_disposable_context_and_safe_metadata(
    tmp_path, monkeypatch
):
    source_repository = tmp_path / "source"
    disposable_repository = tmp_path / "run" / "disposable"
    source_repository.mkdir()
    disposable_repository.mkdir(parents=True)
    run_dir = tmp_path / "run"
    report_path = run_dir / "bug_report_level3_demo.json"
    report_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GROQ_API_KEY", "do-not-record-this-test-secret")
    captured = {}

    def fake_subprocess_run(command, **kwargs):
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        captured["cwd"] = kwargs["cwd"]
        diagnosis_dir = Path(kwargs["env"]["WORKFLOW_ARTIFACT_DIR"])
        local_websocket_demo.save_json(
            diagnosis_dir / "diagnosis_model_call.json",
            {
                "attempted": True,
                "succeeded": True,
                "provider": "Groq",
                "model": "openai/gpt-oss-20b",
                "outcome": "succeeded",
            },
        )
        local_websocket_demo.save_json(
            diagnosis_dir / "diagnosis.json",
            groq_diagnosis_fixture(),
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = local_websocket_demo.run_groq_diagnosis(
        {"repository_path": os.fspath(source_repository)},
        os.fspath(disposable_repository),
        {"status": "REPRODUCED", "evidence": ["one stored row"]},
        os.fspath(report_path),
        os.fspath(run_dir),
        command_runner=fake_subprocess_run,
    )

    diagnosis_dir = run_dir / "ai_diagnosis"
    saved_context = json.loads(
        (diagnosis_dir / "repository_context.json").read_text(encoding="utf-8")
    )
    saved_metadata = (run_dir / "diagnosis_model_call.json").read_text(encoding="utf-8")
    assert result["succeeded"] is True
    assert result["model_call"]["attempted"] is True
    assert result["model_call"]["succeeded"] is True
    assert result["diagnosis_validation"]["valid"] is True
    assert saved_context["repository_path"] == os.fspath(disposable_repository)
    assert captured["environment"]["BUG_REPORT_FILE"] == os.fspath(report_path)
    assert captured["environment"]["GROQ_API_KEY"] == "do-not-record-this-test-secret"
    assert os.fspath(source_repository) != saved_context["repository_path"]
    assert "do-not-record-this-test-secret" not in saved_metadata


def test_run_groq_diagnosis_reports_failed_request_without_fallback(
    tmp_path,
):
    source_repository = tmp_path / "source"
    disposable_repository = tmp_path / "run" / "disposable"
    source_repository.mkdir()
    disposable_repository.mkdir(parents=True)
    run_dir = tmp_path / "run"
    report_path = run_dir / "bug_report_level3_demo.json"
    report_path.write_text("{}", encoding="utf-8")

    def failed_subprocess(command, **kwargs):
        diagnosis_dir = Path(kwargs["env"]["WORKFLOW_ARTIFACT_DIR"])
        local_websocket_demo.save_json(
            diagnosis_dir / "diagnosis_model_call.json",
            {
                "attempted": True,
                "succeeded": False,
                "provider": "Groq",
                "model": "openai/gpt-oss-20b",
                "outcome": "failed",
                "error_type": "APIConnectionError",
            },
        )
        return SimpleNamespace(returncode=1, stdout="", stderr="redacted")

    result = local_websocket_demo.run_groq_diagnosis(
        {"repository_path": os.fspath(source_repository)},
        os.fspath(disposable_repository),
        {"status": "REPRODUCED"},
        os.fspath(report_path),
        os.fspath(run_dir),
        command_runner=failed_subprocess,
    )

    assert result["succeeded"] is False
    assert result["diagnosis"] is None
    assert result["model_call"]["attempted"] is True
    assert result["model_call"]["succeeded"] is False
    assert result["model_call"]["outcome"] == "failed"
    assert result["diagnosis_validation"]["valid"] is False
    assert not (run_dir / "diagnosis.json").exists()


def test_run_groq_diagnosis_rejects_invalid_schema_after_successful_call(
    tmp_path,
):
    source_repository = tmp_path / "source"
    disposable_repository = tmp_path / "run" / "disposable"
    source_repository.mkdir()
    disposable_repository.mkdir(parents=True)
    run_dir = tmp_path / "run"
    report_path = run_dir / "bug_report_level3_demo.json"
    report_path.write_text("{}", encoding="utf-8")

    def invalid_schema_subprocess(command, **kwargs):
        diagnosis_dir = Path(kwargs["env"]["WORKFLOW_ARTIFACT_DIR"])
        local_websocket_demo.save_json(
            diagnosis_dir / "diagnosis_model_call.json",
            {
                "attempted": True,
                "succeeded": True,
                "provider": "Groq",
                "model": "openai/gpt-oss-20b",
                "outcome": "succeeded",
            },
        )
        local_websocket_demo.save_json(
            diagnosis_dir / "diagnosis.json",
            {"root_cause": "Incomplete response"},
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = local_websocket_demo.run_groq_diagnosis(
        {"repository_path": os.fspath(source_repository)},
        os.fspath(disposable_repository),
        {"status": "REPRODUCED"},
        os.fspath(report_path),
        os.fspath(run_dir),
        command_runner=invalid_schema_subprocess,
    )

    assert result["succeeded"] is False
    assert result["diagnosis"] is None
    assert result["model_call"]["succeeded"] is True
    assert result["diagnosis_validation"]["valid"] is False
    assert len(result["diagnosis_validation"]["errors"]) == 7


def test_groq_diagnosis_failure_is_not_replaced_by_deterministic_fix(
    tmp_path, monkeypatch
):
    context = build_level3_repository_context(monkeypatch)
    run_dir = tmp_path / "workflow_runs" / "ai-failure"
    run_dir.mkdir(parents=True)
    command_outputs = [
        (1, "FAIL Level 3 demo retains each accepted message\nExpected: 2\nReceived: 1\nTests: 1 failed, 12 total"),
    ]
    command_calls = []
    fixer = Mock()

    def fake_command_runner(command, **kwargs):
        command_calls.append(command)
        return SimpleNamespace(
            returncode=command_outputs[len(command_calls) - 1][0],
            stdout=command_outputs[len(command_calls) - 1][1],
            stderr="",
        )

    failed_diagnosis = {
        "succeeded": False,
        "diagnosis": None,
        "diagnosis_validation": {
            "valid": False,
            "errors": ["Groq diagnosis agent failed."],
        },
        "model_call": {
            "attempted": True,
            "succeeded": False,
            "provider": "Groq",
            "model": "openai/gpt-oss-20b",
            "outcome": "failed",
            "error_type": "APIConnectionError",
        },
        "agent_error_type": "CalledProcessError",
    }
    result = local_websocket_demo.run_level3_websocket_demo(
        context,
        os.fspath(run_dir),
        command_runner=fake_command_runner,
        fixer=fixer,
        diagnosis_mode="groq_ai",
        diagnosis_runner=Mock(return_value=failed_diagnosis),
    )

    assert result["final_status"] == "FAIL"
    assert result["diagnosis_mode"] == "groq_ai"
    assert result["model_call"]["attempted"] is True
    assert result["model_call"]["succeeded"] is False
    assert result["ai_powered_stages"] == []
    assert result["fix_applied"] is False
    fixer.assert_not_called()
    assert not (run_dir / "diagnosis.json").exists()
    attempt = json.loads(
        (run_dir / "diagnosis_attempt.json").read_text(encoding="utf-8")
    )
    assert attempt["model_call"]["outcome"] == "failed"
    reproduction = json.loads(
        (run_dir / "reproduction_result.json").read_text(encoding="utf-8")
    )
    final_report = json.loads(
        (run_dir / "validation_result.json").read_text(encoding="utf-8")
    )
    assert reproduction["status"] == "REPRODUCED"
    assert final_report["final_status"] == "FAIL"
    assert final_report["model_call"]["outcome"] == "failed"


def test_groq_diagnosis_opt_in_records_only_diagnosis_as_ai_powered(
    tmp_path, monkeypatch
):
    context = build_level3_repository_context(monkeypatch)
    run_dir = tmp_path / "workflow_runs" / "ai-success"
    run_dir.mkdir(parents=True)
    command_outputs = [
        (1, "FAIL Level 3 demo retains each accepted message\nExpected: 2\nReceived: 1\nTests: 1 failed, 12 total"),
        (0, "Tests: 12 passed, 12 total"),
    ]
    command_calls = []

    def fake_command_runner(command, **kwargs):
        command_calls.append(command)
        output = command_outputs[len(command_calls) - 1]
        return SimpleNamespace(returncode=output[0], stdout=output[1], stderr="")

    successful_diagnosis = {
        "succeeded": True,
        "diagnosis": groq_diagnosis_fixture(),
        "diagnosis_validation": {"valid": True, "errors": []},
        "model_call": {
            "attempted": True,
            "succeeded": True,
            "provider": "Groq",
            "model": "openai/gpt-oss-20b",
            "outcome": "succeeded",
        },
    }
    result = local_websocket_demo.run_level3_websocket_demo(
        context,
        os.fspath(run_dir),
        command_runner=fake_command_runner,
        diagnosis_mode="groq_ai",
        diagnosis_runner=Mock(return_value=successful_diagnosis),
    )

    assert result["final_status"] == "PASS"
    assert result["diagnosis_mode"] == "groq_ai"
    assert result["ai_powered_stages"] == ["diagnosis"]
    assert result["code_fix_mode"] == "deterministic_demo"
    assert result["test_generation_mode"] == "deterministic_demo"
    assert "Groq-backed root-cause diagnosis" in result["workflow_logic"]
    assert result["model_call"]["succeeded"] is True


def test_orchestrator_persists_ai_diagnosis_metadata_in_workflow_report(
    tmp_path, monkeypatch
):
    repository = tmp_path / "local-repository"
    repository.mkdir()
    run_dir = tmp_path / "workflow_runs" / "ai-report"
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(workflow_orchestrator, "BASE_DIR", os.fspath(tmp_path))
    for name in (
        "ACTIVE_RUN_ARTIFACT_DIR",
        "ACTIVE_BUG_REPORT_FILE",
        "CHILD_ENV_OVERRIDES",
        "REPOSITORY_CONTEXT_FILE",
        "REPRODUCTION_RESULT_FILE",
        "DIAGNOSIS_FILE",
        "CODE_FIX_RESULT_FILE",
        "TEST_GENERATION_RESULT_FILE",
        "TEST_RESULT_FILE",
        "BUILD_RESULT_FILE",
        "VALIDATION_RESULT_FILE",
        "VERIFICATION_REPORT_FILE",
        "WORKFLOW_REPORT_FILE",
    ):
        monkeypatch.setattr(
            workflow_orchestrator,
            name,
            getattr(workflow_orchestrator, name),
        )
    monkeypatch.setattr(
        workflow_orchestrator,
        "create_run_artifact_dir",
        lambda: os.fspath(run_dir),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "workflow_orchestrator.py",
            "--local-repository",
            os.fspath(repository),
            "--local-websocket-demo",
            "--ai-diagnosis",
        ],
    )
    context = {
        "repository_path": os.fspath(repository),
        "execution_strategy": {
            "technology": "AWS Lambda",
            "test_command": ["node", "jest.js"],
        },
        "architecture": {"components": []},
    }

    def fake_run_agent(_script_name, _arguments=None):
        Path(workflow_orchestrator.REPOSITORY_CONTEXT_FILE).write_text(
            json.dumps(context),
            encoding="utf-8",
        )
        return {"success": True, "return_code": 0}

    demo_result = {
        "status": "PASSED",
        "final_status": "PASS",
        "return_code": 0,
        "diagnosis_mode": "groq_ai",
        "model_call": {
            "attempted": True,
            "succeeded": True,
            "provider": "Groq",
            "model": "openai/gpt-oss-20b",
            "outcome": "succeeded",
        },
        "diagnosis_validation": {"valid": True, "errors": []},
        "ai_powered_stages": ["diagnosis"],
        "code_fix_mode": "deterministic_demo",
        "test_generation_mode": "deterministic_demo",
        "workflow_logic": ["Groq-backed root-cause diagnosis"],
        "attempts": [{
            "attempt": 1,
            "test_result": {"status": "PASSED"},
            "fix_result": {"modified_files": ["backend/src/websocket.js"]},
            "semantic_fix_present": True,
            "verified": True,
        }],
        "stages": [],
    }
    monkeypatch.setattr(workflow_orchestrator, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        workflow_orchestrator,
        "run_validation_only",
        lambda _context: {"status": "PASSED", "checks": []},
    )
    monkeypatch.setattr(
        workflow_orchestrator,
        "run_level3_websocket_demo",
        lambda *_args, **kwargs: demo_result
        if kwargs["diagnosis_mode"] == "groq_ai"
        else {},
    )

    assert workflow_orchestrator.main() == 0
    report = json.loads(
        (run_dir / "workflow_report.json").read_text(encoding="utf-8")
    )
    assert report["workflow"] == "Level 3 Local WebSocket Bug-Fix Demo"
    assert report["diagnosis_mode"] == "groq_ai"
    assert report["model_call"]["attempted"] is True
    assert report["model_call"]["succeeded"] is True
    assert report["ai_powered_stages"] == ["diagnosis"]
    assert report["workflow_logic"] == ["Groq-backed root-cause diagnosis"]
    assert report["code_fix_mode"] == "deterministic_demo"
    assert report["test_generation_mode"] == "deterministic_demo"
    assert report["retry_history"] == [{
        "attempt": 1,
        "test_result": "PASS",
        "verification": "PASS",
        "overall": "PASS",
        "modified_files": ["backend/src/websocket.js"],
        "semantic_fix_present": True,
    }]


def test_retry_failure_context_includes_all_latest_stage_artifacts(
    tmp_path, monkeypatch
):
    artifact_paths = {
        "REPRODUCTION_RESULT_FILE": "reproduction_result.json",
        "TEST_GENERATION_RESULT_FILE": "test_generation_result.json",
        "CODE_FIX_RESULT_FILE": "code_fix_result.json",
        "TEST_RESULT_FILE": "test_result.json",
        "BUILD_RESULT_FILE": "build_result.json",
        "VALIDATION_RESULT_FILE": "validation_result.json",
        "VERIFICATION_REPORT_FILE": "verification_report.json",
        "DIAGNOSIS_FILE": "diagnosis.json",
    }
    expected = {}
    for variable, filename in artifact_paths.items():
        artifact_path = tmp_path / filename
        value = {"artifact": filename}
        artifact_path.write_text(json.dumps(value), encoding="utf-8")
        expected[filename] = value
        monkeypatch.setattr(
            workflow_orchestrator,
            variable,
            os.fspath(artifact_path),
        )

    context = workflow_orchestrator.load_retry_failure_context()

    assert context == {
        "previous_reproduction_result": expected["reproduction_result.json"],
        "previous_test_generation_result": expected["test_generation_result.json"],
        "previous_code_fix_result": expected["code_fix_result.json"],
        "previous_test_result": expected["test_result.json"],
        "previous_build_result": expected["build_result.json"],
        "previous_validation_result": expected["validation_result.json"],
        "previous_verification_result": expected["verification_report.json"],
        "previous_diagnosis": expected["diagnosis.json"],
    }


def test_execution_strategy_prioritizes_react_and_angular_over_node(tmp_path, monkeypatch):
    repository_agent = load_repository_agent_module(monkeypatch)

    react_root = tmp_path / "react"
    (react_root / "src").mkdir(parents=True)
    (react_root / "src" / "App.tsx").write_text("export {}", encoding="utf-8")
    (react_root / "vite.config.ts").write_text("", encoding="utf-8")
    (react_root / "package.json").write_text(json.dumps({
        "dependencies": {"react": "1", "vite": "1"},
        "scripts": {"build": "vite build", "test": "vitest run", "dev": "vite"},
    }), encoding="utf-8")
    react_context = repository_agent.build_repository_context(
        os.fspath(react_root), react_root.as_uri()
    )
    assert react_context["execution_strategy"]["technology"] == "React"
    assert react_context["execution_strategy"]["test_command"] == "npm run test"
    assert react_context["execution_strategy"]["build_command"] == "npm run build"

    angular_root = tmp_path / "angular"
    angular_root.mkdir()
    (angular_root / "angular.json").write_text("{}", encoding="utf-8")
    (angular_root / "package.json").write_text(json.dumps({
        "dependencies": {"@angular/core": "1"},
        "scripts": {"build": "ng build", "test": "ng test --watch=false"},
    }), encoding="utf-8")
    angular_context = repository_agent.build_repository_context(
        os.fspath(angular_root), angular_root.as_uri()
    )
    angular_components = angular_context["architecture"]["components"]
    assert any(component["type"] == "frontend" for component in angular_components)
    assert angular_context["execution_strategy"]["technology"] == "Angular"
    assert angular_context["execution_strategy"]["test_command"] == "npm run test"
    assert angular_context["execution_strategy"]["build_command"] == "npm run build"


@pytest.mark.parametrize(
    ("project_type", "expected_technology", "expected_test", "expected_build"),
    [
        ({"technology": "Python"}, "Python", "pytest", None),
        ({"technology": "Java", "package_manager": "Maven"}, "Java", "mvn test", "mvn package"),
        ({"technology": "Java", "package_manager": "Gradle"}, "Java", "gradle test", "gradle build"),
        ({"technology": ".NET"}, ".NET", "dotnet test", "dotnet build"),
    ],
)
def test_language_execution_strategies_select_expected_commands(
    monkeypatch,
    project_type,
    expected_technology,
    expected_test,
    expected_build,
):
    repository_agent = load_repository_agent_module(monkeypatch)
    strategy = repository_agent.detect_execution_strategy([project_type])

    assert strategy["technology"] == expected_technology
    assert strategy["test_command"] == expected_test
    assert strategy["build_command"] == expected_build


def run_mocked_retry_workflow(
    tmp_path,
    monkeypatch,
    failure_stage=None,
    failed_attempts=(),
    analysis_success=True,
):
    base_dir = tmp_path / "workflow-root"
    base_dir.mkdir()
    repository = base_dir / "target-project"
    repository.mkdir()
    bug_report_path = base_dir / "demo-bug-report.json"
    bug_report_path.write_text(json.dumps({"bug_title": "Retry demo"}), encoding="utf-8")
    run_dir = base_dir / "workflow_runs" / "run-1"
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(workflow_orchestrator, "BASE_DIR", os.fspath(base_dir))
    monkeypatch.setattr(
        workflow_orchestrator,
        "create_run_artifact_dir",
        lambda: os.fspath(run_dir),
    )
    monkeypatch.setattr(workflow_orchestrator, "MAX_RETRIES", 2)
    monkeypatch.setenv("BUG_REPORT_FILE", os.fspath(bug_report_path))
    monkeypatch.setattr(
        sys,
        "argv",
        ["workflow_orchestrator.py", "--local-repository", os.fspath(repository)],
    )
    context = {
        "repository_path": os.fspath(repository),
        "execution_strategy": {"technology": "Python", "test_command": "pytest"},
        "architecture": {"components": []},
    }
    diagnosis = {
        "bug_title": "Retry demo",
        "project_type": "Python",
        "failure_analysis": "Mocked failure evidence.",
        "root_cause": "Mocked root cause.",
        "affected_files": ["app.py"],
        "evidence": ["Mocked deterministic evidence."],
        "proposed_fix": "Apply a mocked fix.",
        "verification_plan": ["Run mocked tests."],
    }
    calls = {name: 0 for name in (
        "repository_analysis", "bug_reproduction", "bug_diagnosis",
        "test_generation", "test_execution", "verification", "code_fix",
        "build", "runtime",
    )}
    retry_failure_contexts = []
    failed_attempts = set(failed_attempts)

    def fake_run_agent(script_name, arguments=None):
        name = os.path.splitext(os.path.basename(script_name))[0]
        if name == "repository_agent":
            calls["repository_analysis"] += 1
            workflow_orchestrator.save_json(
                workflow_orchestrator.REPOSITORY_CONTEXT_FILE,
                context,
            )
            return {"success": analysis_success, "return_code": 0 if analysis_success else 1}
        elif name == "bug_reproduction_agent":
            calls["bug_reproduction"] += 1
            workflow_orchestrator.save_json(
                workflow_orchestrator.REPRODUCTION_RESULT_FILE,
                {"status": "REPRODUCED", "executed": True, "evidence": ["observed"]},
            )
        elif name == "bug_diagnosis_agent":
            calls["bug_diagnosis"] += 1
            if arguments:
                retry_report = workflow_orchestrator.load_json(arguments[0])
                retry_failure_contexts.append(retry_report.get("failure_context", {}))
            workflow_orchestrator.save_json(
                workflow_orchestrator.DIAGNOSIS_FILE,
                diagnosis,
            )
        elif name == "test_generation_agent":
            calls["test_generation"] += 1
            failed = failure_stage == "test_generation" and calls[name.replace("_agent", "")] in failed_attempts
            workflow_orchestrator.save_json(
                workflow_orchestrator.TEST_GENERATION_RESULT_FILE,
                {"status": "FAILED" if failed else "SUCCESS", "message": "mock"},
            )
            return {"success": not failed, "return_code": 1 if failed else 0}
        elif name == "test_result_checker":
            calls["test_execution"] += 1
            failed = failure_stage == "test_execution" and calls["test_execution"] in failed_attempts
            test_result = {
                "status": "FAILED" if failed else "PASSED",
                "return_code": 1 if failed else 0,
                "tests": 1,
                "passed": 0 if failed else 1,
                "failed": 1 if failed else 0,
            }
            workflow_orchestrator.save_json(workflow_orchestrator.TEST_RESULT_FILE, test_result)
            return {"success": not failed, "return_code": test_result["return_code"]}
        elif name == "verification_agent":
            calls["verification"] += 1
            failed = failure_stage == "verification" and calls["verification"] in failed_attempts
            workflow_orchestrator.save_json(
                workflow_orchestrator.VERIFICATION_REPORT_FILE,
                {"status": "FAIL" if failed else "PASS", "reason": ["mock"]},
            )
        return {"success": True, "return_code": 0}

    def fake_code_fix_stage():
        calls["code_fix"] += 1
        failed = failure_stage == "code_fix" and calls["code_fix"] in failed_attempts
        fix_result = {
            "status": "FAILED" if failed else "SUCCESS",
            "modified_files": [] if failed else [{"file": "app.py", "verified": True}],
        }
        workflow_orchestrator.save_json(workflow_orchestrator.CODE_FIX_RESULT_FILE, fix_result)
        return {"success": not failed, "return_code": 1 if failed else 0}, fix_result

    def fake_build(_context):
        calls["build"] += 1
        failed = failure_stage == "build" and calls["build"] in failed_attempts
        result = {"status": "FAILED" if failed else "PASSED", "return_code": 1 if failed else 0}
        workflow_orchestrator.save_json(workflow_orchestrator.BUILD_RESULT_FILE, result)
        return result

    def fake_runtime(_initial_reproduction):
        calls["runtime"] += 1
        failed = failure_stage == "runtime_validation" and calls["runtime"] in failed_attempts
        result = {"status": "FAILED" if failed else "PASSED", "return_code": 1 if failed else 0}
        workflow_orchestrator.save_json(workflow_orchestrator.VALIDATION_RESULT_FILE, result)
        return result

    monkeypatch.setattr(workflow_orchestrator, "run_agent", fake_run_agent)
    monkeypatch.setattr(workflow_orchestrator, "run_code_fix_stage", fake_code_fix_stage)
    monkeypatch.setattr(workflow_orchestrator, "run_component_builds", fake_build)
    monkeypatch.setattr(workflow_orchestrator, "run_post_fix_runtime_validation", fake_runtime)
    return workflow_orchestrator.main(), run_dir, calls, retry_failure_contexts


def test_orchestrator_retry_loop_successful_run_and_recovery(tmp_path, monkeypatch):
    result, run_dir, calls, _retry_contexts = run_mocked_retry_workflow(
        tmp_path,
        monkeypatch,
    )
    report = json.loads((run_dir / "workflow_report.json").read_text(encoding="utf-8"))
    assert result == 0
    assert report["final_status"] == "PASS"
    assert [attempt["overall"] for attempt in report["retry_history"]] == ["PASS"]
    assert calls["code_fix"] == 1
    assert calls["bug_diagnosis"] == 1


def test_orchestrator_reports_repository_analysis_failure_without_crashing(
    tmp_path, monkeypatch
):
    result, run_dir, _calls, _retry_contexts = run_mocked_retry_workflow(
        tmp_path,
        monkeypatch,
        analysis_success=False,
    )
    report = json.loads((run_dir / "workflow_report.json").read_text(encoding="utf-8"))

    assert result == 1
    assert report["final_status"] == "FAIL"
    assert report["retry_history"] == []


@pytest.mark.parametrize(
    ("failure_stage", "failure_context_key"),
    [
        ("code_fix", "previous_code_fix_result"),
        ("test_generation", "previous_test_generation_result"),
        ("test_execution", "previous_test_result"),
        ("build", "previous_build_result"),
        ("runtime_validation", "previous_validation_result"),
        ("verification", "previous_verification_result"),
    ],
)
def test_orchestrator_retries_each_failure_with_latest_evidence(
    tmp_path, monkeypatch, failure_stage, failure_context_key
):
    result, run_dir, calls, retry_contexts = run_mocked_retry_workflow(
        tmp_path,
        monkeypatch,
        failure_stage=failure_stage,
        failed_attempts={1},
    )
    report = json.loads((run_dir / "workflow_report.json").read_text(encoding="utf-8"))

    assert result == 0
    assert report["final_status"] == "PASS"
    assert [attempt["overall"] for attempt in report["retry_history"]] == ["FAIL", "PASS"]
    assert calls["bug_diagnosis"] == 2
    assert len(retry_contexts) == 1
    assert retry_contexts[0][failure_context_key] is not None


def test_orchestrator_exhausts_bounded_retries_and_reports_failure(tmp_path, monkeypatch):
    result, run_dir, calls, retry_contexts = run_mocked_retry_workflow(
        tmp_path,
        monkeypatch,
        failure_stage="verification",
        failed_attempts={1, 2, 3},
    )
    report = json.loads((run_dir / "workflow_report.json").read_text(encoding="utf-8"))

    assert result == 1
    assert report["final_status"] == "FAIL"
    assert report["max_retries"] == 2
    assert [attempt["attempt"] for attempt in report["retry_history"]] == [1, 2, 3]
    assert [attempt["overall"] for attempt in report["retry_history"]] == [
        "FAIL", "FAIL", "FAIL"
    ]
    assert calls["code_fix"] == 3
    assert calls["bug_diagnosis"] == 3
    assert len(retry_contexts) == 2


def test_optional_langfuse_trace_wraps_workflow_with_safe_summary(tmp_path, monkeypatch):
    report_path = tmp_path / "workflow_report.json"
    report_path.write_text(json.dumps({
        "final_status": "PASS",
        "stages": [
            {"stage": 1, "name": "Repository Analysis", "status": "SUCCESS", "details": {"secret": "do-not-trace"}},
            {"stage": 2, "name": "Code Fix", "status": "SUCCESS", "details": {"stdout": "private output"}},
        ],
        "retry_history": [
            {"attempt": 1, "overall": "FAIL", "failure_context": "do-not-trace"},
            {"attempt": 2, "overall": "PASS"},
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(workflow_orchestrator, "WORKFLOW_REPORT_FILE", os.fspath(report_path))

    observation = Mock()
    observation_context = Mock()
    observation_context.__enter__ = Mock(return_value=observation)
    observation_context.__exit__ = Mock(return_value=False)
    client = Mock()
    client.start_as_current_observation.return_value = observation_context
    runner = Mock(return_value=0)

    result = workflow_orchestrator.run_with_optional_langfuse(
        runner,
        client_factory=lambda: client,
    )

    assert result == 0
    runner.assert_called_once_with()
    client.start_as_current_observation.assert_called_once_with(
        as_type="agent",
        name="ai-agentic-debugging-workflow",
        input={"workflow": "repository-debugging", "mode": "orchestrated"},
    )
    trace_output = observation.update.call_args.kwargs["output"]
    assert trace_output["status"] == "PASS"
    assert trace_output["stages"] == [
        {"stage": 1, "name": "Repository Analysis", "status": "SUCCESS"},
        {"stage": 2, "name": "Code Fix", "status": "SUCCESS"},
    ]
    assert trace_output["attempts"] == [
        {"attempt": 1, "overall": "FAIL"},
        {"attempt": 2, "overall": "PASS"},
    ]
    assert "do-not-trace" not in json.dumps(trace_output)
    assert "private output" not in json.dumps(trace_output)
    client.flush.assert_called_once_with()


def test_optional_langfuse_disabled_path_runs_workflow_without_client():
    runner = Mock(return_value=1)
    client_factory = Mock(return_value=None)

    result = workflow_orchestrator.run_with_optional_langfuse(
        runner,
        client_factory=client_factory,
    )

    assert result == 1
    client_factory.assert_called_once_with()
    runner.assert_called_once_with()


def test_langfuse_missing_credentials_disables_tracing_gracefully(monkeypatch):
    dotenv_module = ModuleType("dotenv")
    dotenv_module.load_dotenv = Mock()
    monkeypatch.setitem(sys.modules, "dotenv", dotenv_module)
    monkeypatch.setenv("WORKFLOW_TRACE_PROVIDER", "langfuse")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    assert workflow_orchestrator.get_optional_langfuse_client() is None
    dotenv_module.load_dotenv.assert_called_once_with()