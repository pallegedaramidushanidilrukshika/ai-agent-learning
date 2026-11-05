"""AI reproduction test: runner selection, result classification and the agent.

Mocked unit tests: no real model call and no real test runner.
"""

import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

import reproduction_test_agent as agent
import reproduction_tests as rt


JEST_CONTEXT = {
    "execution_strategy": {
        "technology": "React/Vite + AWS Serverless",
        "test_framework": "Jest",
        "backend_path": "backend",
        "test_command": ["node", "backend\\node_modules\\jest\\bin\\jest.js", "--runInBand"],
    }
}


def test_jest_runner_targets_a_single_backend_test_file():
    runner = rt.select_runner(JEST_CONTEXT)
    assert runner["framework"] == "jest"
    assert runner["test_path"] == "backend/test/ai-reproduction.test.js"
    assert rt.single_test_command(runner)[-2:] == ["--runTestsByPath", "backend/test/ai-reproduction.test.js"]


def test_pytest_runner_and_unsupported_projects():
    pytest_runner = rt.select_runner({"execution_strategy": {"technology": "Python", "test_framework": "pytest", "test_command": "python -m pytest"}})
    assert pytest_runner["test_path"] == "tests/test_ai_reproduction.py"
    assert rt.single_test_command(pytest_runner)[-2:] == ["tests/test_ai_reproduction.py", "-q"]
    assert rt.select_runner({"execution_strategy": {"technology": ".NET", "test_command": "dotnet test"}}) is None
    assert rt.select_runner({}) is None


def fake_process(stdout="", stderr="", returncode=0):
    return lambda *args, **kwargs: SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


@pytest.mark.parametrize(
    ("stderr", "returncode", "expected"),
    [
        ("FAIL backend/test/ai-reproduction.test.js\n  ● keeps both\n    expect(received).toEqual(expected)\nTests:       1 failed, 1 total\n", 1, rt.FAILS_ON_CURRENT_CODE),
        # Real case from the 27 Oct rehearsal: the test crashed reading a wrong path.
        ("FAIL x\n  ● keeps both\n    ENOENT: no such file or directory, open 'serverless.yml'\nTests:       1 failed, 1 total\n", 1, rt.RUN_ERROR),
        ("FAIL x\n  ● keeps both\n    ReferenceError: rows is not defined\nTests:       1 failed, 1 total\n", 1, rt.RUN_ERROR),
        ("console.error\n  Error: DynamoDB unavailable\nFAIL x\n  ● keeps both\n    expect(received).toEqual(expected)\nTests:       1 failed, 1 total\n", 1, rt.FAILS_ON_CURRENT_CODE),
        ("PASS backend/test/ai-reproduction.test.js\nTests:       1 passed, 1 total\n", 0, rt.PASSES_ON_CURRENT_CODE),
        ("FAIL x\n  ● Test suite failed to run\n  SyntaxError: Unexpected token\nTests:       0 total\n", 1, rt.RUN_ERROR),
        ("FAIL x\n  Cannot find module 'uuid'\nTests:       1 failed, 1 total\n", 1, rt.RUN_ERROR),
        ("no summary at all", 1, rt.RUN_ERROR),
    ],
)
def test_classification_only_accepts_real_assertion_failures(stderr, returncode, expected):
    runner = rt.select_runner(JEST_CONTEXT)
    run = rt.run_single_test(".", runner, runner_function=fake_process(stderr=stderr, returncode=returncode))
    assert rt.classify_run(run) == expected


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("E       AssertionError: assert ['second'] == ['first', 'second']\n1 failed in 0.1s", rt.FAILS_ON_CURRENT_CODE),
        ("E       assert 1 == 2\n1 failed in 0.1s", rt.FAILS_ON_CURRENT_CODE),
        ("E       FileNotFoundError: [Errno 2] No such file\n1 failed in 0.1s", rt.RUN_ERROR),
    ],
)
def test_pytest_only_assertion_failures_reproduce(stdout, expected):
    runner = rt.select_runner({"execution_strategy": {"technology": "Python", "test_framework": "pytest", "test_command": "pytest"}})
    run = rt.run_single_test(".", runner, runner_function=fake_process(stdout=stdout, returncode=1))
    assert rt.classify_run(run) == expected


def test_pytest_collection_error_is_not_a_reproduction():
    runner = rt.select_runner({"execution_strategy": {"technology": "Python", "test_framework": "pytest", "test_command": "pytest"}})
    run = rt.run_single_test(".", runner, runner_function=fake_process(stdout="1 error in 0.1s\nERROR collecting tests/test_ai_reproduction.py", returncode=2))
    assert rt.classify_run(run) == rt.RUN_ERROR


# ------------------------------------------------------------
# Agent
# ------------------------------------------------------------

BUG_TEST = """const { message } = require("../src/handler");
test("keeps both messages", async () => {
  expect(await message()).toEqual(["first", "second"]);
});
"""


@pytest.fixture
def jest_repository(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    (repository / "backend" / "src").mkdir(parents=True)
    (repository / "backend" / "tests").mkdir()
    (repository / "backend" / "package.json").write_text(json.dumps({"devDependencies": {"jest": "30"}}), encoding="utf-8")
    (repository / "backend" / "src" / "handler.js").write_text("exports.message = async () => ['second'];\n", encoding="utf-8")
    (repository / "backend" / "tests" / "handler.test.js").write_text("test('x', () => {});\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    context = dict(JEST_CONTEXT, repository_path=str(repository), file_contents={"backend\\src\\handler.js": "..."})
    (run_dir / "repository_context.json").write_text(json.dumps(context), encoding="utf-8")
    bug_path = tmp_path / "bug.json"
    bug_path.write_text(json.dumps({"bug_title": "Second message overwrites the first"}), encoding="utf-8")
    monkeypatch.setattr(agent, "REPOSITORY_CONTEXT_FILE", str(run_dir / "repository_context.json"))
    monkeypatch.setattr(agent, "OUTPUT_FILE", str(run_dir / "reproduction_test.json"))
    monkeypatch.setenv("GROQ_API_KEY", "mock-key")
    dotenv_module = ModuleType("dotenv")
    dotenv_module.load_dotenv = Mock()
    monkeypatch.setitem(sys.modules, "dotenv", dotenv_module)
    return repository, run_dir, bug_path


def install_model(monkeypatch, contents):
    client = Mock()
    client.chat.completions.create.side_effect = [
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))]) for text in contents
    ]
    groq_module = ModuleType("groq")
    groq_module.Groq = Mock(return_value=client)
    monkeypatch.setitem(sys.modules, "groq", groq_module)
    return client


def test_agent_writes_test_runs_it_and_records_classification(jest_repository, monkeypatch):
    repository, run_dir, bug_path = jest_repository
    client = install_model(monkeypatch, ["```js\n" + BUG_TEST + "```"])
    monkeypatch.setattr(agent, "source_change_errors", lambda *args, **kwargs: [])
    monkeypatch.setattr(rt, "run_single_test", lambda repo, runner: {"return_code": 1, "tests": 1, "passed": 0, "failed": 1, "suite_error": False, "timed_out": False, "output_tail": "expected 2 received 1"})

    assert agent.main([str(bug_path)]) == 0

    result = json.loads((run_dir / "reproduction_test.json").read_text(encoding="utf-8"))
    assert result["status"] == "GENERATED"
    assert result["classification"] == rt.FAILS_ON_CURRENT_CODE
    assert result["test_file"] == "backend/test/ai-reproduction.test.js"
    assert (repository / "backend" / "test" / "ai-reproduction.test.js").read_text(encoding="utf-8") == BUG_TEST.rstrip("\n")
    assert result["content_sha256"] == rt.content_sha256(result["content"])
    prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert "FAIL with an assertion" in prompt
    assert "jest" in prompt.lower()
    assert "handler.js" in prompt


def test_agent_retries_once_when_output_is_rejected_then_fails(jest_repository, monkeypatch):
    _repository, run_dir, bug_path = jest_repository
    client = install_model(monkeypatch, ["no code block here", "still no code"])

    assert agent.main([str(bug_path)]) == 1
    assert client.chat.completions.create.call_count == 2
    result = json.loads((run_dir / "reproduction_test.json").read_text(encoding="utf-8"))
    assert result["status"] == "FAILED"


def test_agent_feedback_is_included_in_revision_prompt(jest_repository, monkeypatch, tmp_path):
    _repository, _run_dir, bug_path = jest_repository
    client = install_model(monkeypatch, ["```js\n" + BUG_TEST + "```"])
    monkeypatch.setattr(agent, "source_change_errors", lambda *args, **kwargs: [])
    monkeypatch.setattr(rt, "run_single_test", lambda repo, runner: {"return_code": 0, "tests": 1, "passed": 1, "failed": 0, "suite_error": False, "timed_out": False, "output_tail": ""})
    feedback = tmp_path / "feedback.json"
    feedback.write_text(json.dumps({"previous_content": "old test", "previous_classification": "PASSES_ON_CURRENT_CODE", "feedback": "Send both messages on the same connection"}), encoding="utf-8")

    assert agent.main([str(bug_path), "--feedback", str(feedback), "--round", "2"]) == 0
    prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert "Send both messages on the same connection" in prompt
    assert "old test" in prompt


def test_agent_reports_unsupported_projects_without_calling_the_model(jest_repository, monkeypatch):
    _repository, run_dir, bug_path = jest_repository
    context_path = run_dir / "repository_context.json"
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["execution_strategy"] = {"technology": ".NET", "test_command": "dotnet test"}
    context_path.write_text(json.dumps(context), encoding="utf-8")
    client = install_model(monkeypatch, [])

    assert agent.main([str(bug_path)]) == 0
    assert json.loads((run_dir / "reproduction_test.json").read_text(encoding="utf-8"))["status"] == "UNSUPPORTED"
    client.chat.completions.create.assert_not_called()


def test_path_hints_cover_source_and_config_files():
    runner = {"framework": "jest", "test_path": "backend/test/ai-reproduction.test.js"}
    hints = agent.import_path_hints(runner, ["backend/src/websocket.js", "backend/serverless.yml", "README.md"])
    assert hints == [
        "- backend/src/websocket.js: require('../src/websocket')",
        "- backend/serverless.yml: fs.readFileSync(path.join(__dirname, '../serverless.yml'), 'utf8')",
    ]


def test_instanceof_on_a_mocked_command_is_rejected():
    # Real case from the 29 Oct rehearsal: the check was always false, so nothing was stored.
    bad = (
        "const mockSend = jest.fn((command) => { if (command instanceof PutItemCommand) {} });\n"
        "jest.mock('@aws-sdk/client-dynamodb', () => ({\n"
        "  PutItemCommand: jest.fn((input) => ({ input })),\n"
        "}));\n"
    )
    assert "PutItemCommand" in agent.jest_test_problems(bad)[0]
    assert agent.jest_test_problems(bad.replace("command instanceof PutItemCommand", "command.input.Item")) == []
    assert agent.jest_test_problems("if (error instanceof Error) {}") == []


def test_jest_mock_factory_using_a_non_mock_variable_is_rejected():
    # Real case from the 29 Oct rehearsal: Jest refused to load the file.
    bad = (
        "const PutItemCommand = jest.fn((input) => ({ input }));\n"
        "const mockSend = jest.fn();\n"
        "jest.mock('@aws-sdk/client-dynamodb', () => ({\n"
        "  DynamoDBClient: jest.fn().mockImplementation(() => ({ send: mockSend })),\n"
        "  PutItemCommand,\n"
        "}));\n"
    )
    problems = agent.jest_test_problems(bad)
    assert len(problems) == 1 and "PutItemCommand" in problems[0]
    good = bad.replace("const PutItemCommand", "const mockPutItemCommand").replace("  PutItemCommand,", "  PutItemCommand: mockPutItemCommand,")
    assert agent.jest_test_problems(good) == []
