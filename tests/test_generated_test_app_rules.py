"""AI tests for HTTP apps must send requests to the app the workflow runs."""
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest


def load_agent_helpers():
    """Load only the module-level helpers (the agent itself runs at import)."""
    source = (Path(__file__).resolve().parents[1] / "test_generation_agent.py").read_text(encoding="utf-8")
    start = source.index("APP_BASE_URL_VARIABLE = ")
    end = source.index("def build_dynamodb_message_regression_test(")
    namespace = {"re": __import__("re")}
    exec(source[start:end], namespace)
    return namespace


helpers = load_agent_helpers()


GOOD_TEST = """const test = require('node:test');
const assert = require('node:assert');
const base = process.env.REPRODUCTION_BASE_URL;
test('blank task is not added', async () => {
  await fetch(new URL('/addtask', base), { method: 'POST', body: new URLSearchParams({ newtask: '   ' }) });
  const html = await (await fetch(new URL('/', base))).text();
  assert.strictEqual((html.match(/name="check"/g) || []).length, 2);
});
"""


def test_request_only_test_is_accepted():
    assert helpers["app_start_problems"](GOOD_TEST) == []


@pytest.mark.parametrize(
    ("snippet", "reason"),
    [
        ("const { spawn } = require('child_process');", "child_process"),
        ("const server = spawn('node', ['index.js']);", "do not start processes"),
        ("execSync('node index.js');", "do not start processes"),
        ("const app = require('../index');", "do not require or import"),
        ("const app = require('../index.js');", "do not require or import"),
        ("import app from '../app.js';", "do not require or import"),
        ("app.listen(3000);", "listen()"),
    ],
)
def test_tests_that_start_the_app_are_rejected(snippet, reason):
    problems = helpers["app_start_problems"](GOOD_TEST + "\n" + snippet)
    assert any(reason in problem for problem in problems)


def test_test_must_use_the_workflow_url():
    content = GOOD_TEST.replace("process.env.REPRODUCTION_BASE_URL", "'http://localhost:3000'")
    assert any("REPRODUCTION_BASE_URL" in problem for problem in helpers["app_start_problems"](content))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('app.listen(3000, function() {})', "http://localhost:3000"),
        ("const port = process.env.PORT || 5050;\napp.listen(port)", "http://localhost:5050"),
        ("module.exports = app;", None),
    ],
)
def test_app_url_is_read_from_the_entry_file(source, expected):
    assert helpers["app_base_url_from_entry"](source) == expected


def test_rules_tell_the_model_the_app_is_already_running():
    rules = helpers["APP_RUN_RULES"]
    assert "process.env.REPRODUCTION_BASE_URL" in rules
    assert "Do NOT start, spawn" in rules


def test_edge_case_rules_ask_for_whitespace_and_valid_input():
    rules = helpers["EDGE_CASE_RULES"]
    assert 'whitespace-only input ("   ")' in rules
    assert "normal valid value still works" in rules
    assert "keep the check" in rules


def test_edge_case_rules_are_always_in_the_system_prompt():
    source = (Path(__file__).resolve().parents[1] / "test_generation_agent.py").read_text(encoding="utf-8")
    assert '""" + EDGE_CASE_RULES + (APP_RUN_RULES if workflow_runs_app else "")' in source


def test_code_fix_prompt_treats_whitespace_as_empty():
    source = (Path(__file__).resolve().parents[1] / "code_fix_agent.py").read_text(encoding="utf-8")
    assert "also reject\n  whitespace-only input" in source


def test_rules_ask_for_a_short_test_file():
    assert "about 40 lines" in helpers["EDGE_CASE_RULES"]
    assert helpers["MAX_TOOL_CALL_ATTEMPTS"] == 3
    assert "shorter test file" in helpers["SHORTER_TEST_REMINDER"]
