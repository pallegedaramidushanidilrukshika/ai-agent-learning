"""Guard against AI-written changes that cannot be correct.

The rejected examples mirror what the model produced in the 23 Oct 2026
rehearsal run: serverless.yml overwritten with JavaScript, and a handler
that imports the undeclared `uuid` package.
"""

import json
import shutil

import pytest

from workflow_validation import source_change_errors


ORIGINAL_YAML = """service: level3-serverless-backend
provider:
  name: aws
  environment:
    TABLE_NAME:
      Ref: MessagesTable
resources:
  Resources:
    MessagesTable:
      Type: AWS::DynamoDB::Table
      Properties:
        AttributeDefinitions:
          - AttributeName: connectionId
            AttributeType: S
        KeySchema:
          - AttributeName: connectionId
            KeyType: HASH
plugins:
  - serverless-offline
"""

FIXED_YAML = ORIGINAL_YAML.replace(
    "          - AttributeName: connectionId\n            AttributeType: S\n",
    "          - AttributeName: connectionId\n            AttributeType: S\n"
    "          - AttributeName: messageId\n            AttributeType: S\n",
).replace(
    "            KeyType: HASH\n",
    "            KeyType: HASH\n          - AttributeName: messageId\n            KeyType: RANGE\n",
)

JS_WRITTEN_INTO_YAML = """// backend/src/websocket.js
const { DynamoDBClient, PutItemCommand } = require("@aws-sdk/client-dynamodb");
exports.message = async (event) => {
  return { statusCode: 200 };
};
"""

ORIGINAL_JS = """const { DynamoDBClient, PutItemCommand } = require("@aws-sdk/client-dynamodb");
const path = require("path");
exports.message = async (event) => ({ statusCode: 200 });
"""


@pytest.fixture
def repo(tmp_path):
    backend = tmp_path / "backend"
    (backend / "src").mkdir(parents=True)
    (backend / "package.json").write_text(
        json.dumps({"dependencies": {"@aws-sdk/client-dynamodb": "^3.0.0"}}),
        encoding="utf-8",
    )
    return tmp_path


def no_syntax_error(_source, _suffix):
    return None


def test_correct_schema_fix_is_accepted(repo):
    assert source_change_errors("backend/serverless.yml", ORIGINAL_YAML, FIXED_YAML, str(repo)) == []


def test_yaml_replaced_by_javascript_is_rejected(repo):
    errors = source_change_errors(
        "backend/serverless.yml", ORIGINAL_YAML, JS_WRITTEN_INTO_YAML, str(repo)
    )
    assert errors


def test_yaml_with_removed_sections_is_rejected(repo):
    trimmed = "service: level3-serverless-backend\nprovider:\n  name: aws\n"
    errors = source_change_errors("backend/serverless.yml", ORIGINAL_YAML, trimmed, str(repo))
    assert any("resources" in error for error in errors)


def test_cloudformation_tags_are_allowed(repo):
    tagged = ORIGINAL_YAML.replace("Ref: MessagesTable", "Value: !Ref MessagesTable")
    assert source_change_errors("backend/serverless.yml", ORIGINAL_YAML, tagged, str(repo)) == []


def test_invalid_json_is_rejected(repo):
    errors = source_change_errors("backend/config.json", '{"a": 1}', "{ not json", str(repo))
    assert errors


def test_undeclared_package_import_is_rejected(repo):
    replacement = 'const { v4: uuidv4 } = require("uuid");\n' + ORIGINAL_JS
    errors = source_change_errors(
        "backend/src/websocket.js", ORIGINAL_JS, replacement, str(repo), node_check=no_syntax_error
    )
    assert any("uuid" in error for error in errors)


def test_declared_builtin_and_relative_imports_are_accepted(repo):
    replacement = (
        ORIGINAL_JS
        + 'const crypto = require("node:crypto");\n'
        + 'const os = require("os");\n'
        + 'const helper = require("./helper");\n'
        + 'const { GetItemCommand } = require("@aws-sdk/client-dynamodb");\n'
    )
    assert source_change_errors(
        "backend/src/websocket.js", ORIGINAL_JS, replacement, str(repo), node_check=no_syntax_error
    ) == []


def test_empty_replacement_is_rejected(repo):
    assert source_change_errors("backend/src/websocket.js", ORIGINAL_JS, "  \n", str(repo))


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_javascript_syntax_error_is_rejected_with_real_node(repo):
    errors = source_change_errors(
        "backend/src/websocket.js", ORIGINAL_JS, "exports.message = async (event => {", str(repo)
    )
    assert any("not valid JavaScript" in error for error in errors)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_valid_javascript_passes_real_node_check(repo):
    assert source_change_errors(
        "backend/src/websocket.js", ORIGINAL_JS, ORIGINAL_JS + "\n// fixed\n", str(repo)
    ) == []
