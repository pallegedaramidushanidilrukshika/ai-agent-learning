import os
import sys
import subprocess
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain.tools import tool
from langchain.agents import create_agent

load_dotenv()

# =============================
# CONFIGURATION
# =============================

TARGET_REPOSITORY = r"C:\Users\SINGER\OneDrive\Desktop\target-projects\nodejs-todo"

DIAGNOSIS_FILE = (
    sys.argv[1] if len(sys.argv) > 1 else "diagnosis_2.json"
)

TEST_FILE = os.path.join(
    TARGET_REPOSITORY,
    "test",
    "todo.test.js"
)


# =============================
# LLM
# =============================

llm = ChatGroq(
    model="openai/gpt-oss-20b",
    temperature=0
)


# =============================
# TOOLS
# =============================

@tool
def read_file(file_path: str) -> str:
    """Read a file from the target repository."""

    full_path = os.path.join(TARGET_REPOSITORY, file_path)

    if not os.path.isfile(full_path):
        return f"ERROR: File not found: {file_path}"

    try:
        with open(full_path, "r", encoding="utf-8") as f:
            return f.read()

    except Exception as e:
        return f"ERROR reading {file_path}: {str(e)}"


@tool
def create_test_file(content: str) -> str:
    """Create or replace the generated test file."""

    try:
        test_directory = os.path.dirname(TEST_FILE)

        os.makedirs(
            test_directory,
            exist_ok=True
        )

        with open(TEST_FILE, "w", encoding="utf-8") as f:
            f.write(content)

        return f"SUCCESS: Test file created at {TEST_FILE}"

    except Exception as e:
        return f"ERROR creating test file: {str(e)}"


@tool
def run_tests() -> str:
    """Run the generated Node.js test file using Node.js built-in test runner."""

    try:

        result = subprocess.run(
            [
                "node",
                "--test",
                "test/todo.test.js"
            ],
            cwd=TARGET_REPOSITORY,
            capture_output=True,
            text=True
        )

        output = result.stdout + "\n" + result.stderr

        return (
            f"RETURN CODE: {result.returncode}\n"
            f"TEST OUTPUT:\n{output}"
        )

    except Exception as e:
        return f"ERROR running tests: {str(e)}"


# =============================
# AGENT SYSTEM PROMPT
# =============================

system_prompt = """
You are an AI Test Generation Agent.

Your job is to generate automated regression tests
for a REAL application.

The target repository is a Node.js Todo application.

You must:

1. Read the diagnosis provided by the user.

2. Read all relevant source files required to
   understand the identified bug.

3. Understand the existing application behavior.

4. Generate automated regression tests for the
   identified bug.

5. Include important related edge cases.

6. Use the existing application structure.

7. Do NOT modify application source files.

8. Only create or replace:

   test/todo.test.js


TEST FRAMEWORK REQUIREMENT
==========================

The generated test file MUST use Node.js built-in testing:

const { test } = require('node:test');
const assert = require('assert');

Every individual test case MUST be registered
using test().

Example:

test('description of test', async () => {
    // test logic
});


DO NOT:

- create a custom test runner
- create runTest()
- create runAllTests()
- use passedTests
- use failedTests
- manually count tests


The test file MUST be executable with:

node --test test/todo.test.js


The Node.js test runner must count every
test() case individually.


REAL APPLICATION TESTING
========================

The tests must test REAL application behavior.

Do not create fake tests that only inspect
source-code strings unless source inspection is
genuinely necessary.

For HTTP behavior, test the actual application
behavior where appropriate.


TEST COVERAGE
=============

Generate meaningful tests for:

- the identified removal bug
- successful task removal
- task movement to Completed Tasks
- multiple task removal
- no-selection behavior
- valid task addition
- empty task rejection
- whitespace-only task rejection
- trimming whitespace
- regression prevention


TOOLS
=====

You MUST use the available tools.

First use read_file to inspect:

- index.js
- views/index.ejs
- package.json


Then generate:

test/todo.test.js


Use create_test_file to actually save the
generated test file.

Then use run_tests to execute:

node --test test/todo.test.js


If the generated tests fail because of an error
in the generated test code:

1. Analyze the failure.
2. Correct the test file.
3. Use create_test_file again.
4. Run the tests again.


APPLICATION SOURCE PROTECTION
=============================

NEVER modify:

- index.js
- views/index.ejs
- package.json
- package-lock.json
- node_modules
- public files


ONLY modify:

test/todo.test.js


IMPORTANT:

Do NOT finish the task before actually using:

1. read_file
2. create_test_file
3. run_tests


At the end report:

- test file created
- number of tests
- number passed
- number failed
- final test status
"""


# =============================
# LOAD DIAGNOSIS
# =============================

try:

    with open(
        DIAGNOSIS_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        diagnosis = f.read()

except Exception as e:

    print(
        f"ERROR: Could not load diagnosis file: {e}"
    )

    sys.exit(1)


# =============================
# CREATE AGENT
# =============================

agent = create_agent(
    model=llm,
    tools=[
        read_file,
        create_test_file,
        run_tests
    ],
    system_prompt=system_prompt
)


# =============================
# AGENT MESSAGE
# =============================

message = f"""
You MUST complete the entire test-generation workflow.

TARGET REPOSITORY:

{TARGET_REPOSITORY}


DIAGNOSIS:

{diagnosis}


FOLLOW THESE STEPS IN ORDER:

STEP 1
------
Use read_file to read:

index.js
views/index.ejs
package.json


STEP 2
------
Analyze the diagnosis and understand the
actual application behavior.


STEP 3
------
Generate:

test/todo.test.js

The file MUST use:

const {{ test }} = require('node:test');
const assert = require('assert');


Every test case must use test().


STEP 4
------
Use create_test_file to save the generated
test/todo.test.js file.


STEP 5
------
Use run_tests to execute:

node --test test/todo.test.js


STEP 6
------
If tests fail because the generated test code
is incorrect, fix the test file and repeat:

create_test_file
then
run_tests


IMPORTANT:

Do NOT modify application source code.

Only test/todo.test.js may be created or replaced.

DO NOT finish until create_test_file and
run_tests have actually been used.

Finally report the test count, passed count,
failed count and final status.
"""


# =============================
# RUN AGENT
# =============================

print("=============================")
print("AI TEST GENERATION AGENT")
print("=============================")

print("\nTarget Repository:")
print(TARGET_REPOSITORY)

print("\nDiagnosis:")
print(diagnosis)


try:

    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": message
                }
            ]
        }
    )

    print("\n=============================")
    print("TEST GENERATION RESULT")
    print("=============================")

    messages = result.get("messages", [])

    print(f"\nTotal messages: {len(messages)}")

    if messages:

        final_message = messages[-1]

        if hasattr(final_message, "content"):

            print(final_message.content)

        else:

            print(final_message)

    else:

        print(result)

except Exception as e:

    print("\n=============================")
    print("TEST GENERATION ERROR")
    print("=============================")

    print(str(e))