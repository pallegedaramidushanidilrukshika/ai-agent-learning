import os
import subprocess
from dotenv import load_dotenv
from google import genai
import sys
import shutil
from langsmith import traceable

@traceable(name="Gemini Code Analysis")
def analyze_code_with_gemini(prompt):
    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=prompt
    )
    return response.text

def clone_repository(repo_url, destination):
    """
    Clone a GitHub repository into the specified destination folder.
    """

    if os.path.exists(destination):
        print(f"Destination already exists: {destination}")
        return False

    print(f"Cloning repository from: {repo_url}")

    result = subprocess.run(
        ["git", "clone", repo_url, destination],
        capture_output=True,
        text=True
    )

    if result.returncode == 0:
        print("Repository cloned successfully.")
        return True

    print("Failed to clone repository.")
    print(result.stderr)

    return False

repo_url = "https://github.com/pallegedaramidushanidilrukshika/ai-agent-demo-project.git"
destination = r"C:\Users\SINGER\OneDrive\Desktop\agent_workspace\ai-agent-demo-project"

clone_repository(repo_url, destination)

# -----------------------------
# TOOL: List files
# -----------------------------

def list_files(directory):
    """
    Return the files inside a directory.
    """

    if not os.path.exists(directory):
        return f"Directory does not exist: {directory}"

    files = []

    for item in os.listdir(directory):
        full_path = os.path.join(directory, item)

        if os.path.isfile(full_path):
            files.append(item)

    return files


# -----------------------------
# Test the tool
# -----------------------------

files = list_files(destination)

print("\nFiles found in repository:")

for file in files:
    print("-", file)

# -----------------------------
# TOOL: Read file
# -----------------------------

def read_file(file_path):
    """
    Read and return the contents of a file.
    """

    if not os.path.exists(file_path):
        return f"File does not exist: {file_path}"

    try:
        with open(file_path, "r", encoding="utf-8") as file:
            return file.read()

    except Exception as e:
        return f"Failed to read file: {e}"


# -----------------------------
# Test read_file tool
# -----------------------------

calculator_path = os.path.join(destination, "calculator.py")

code = read_file(calculator_path)

print("\nContents of calculator.py:")
print(code)

# -----------------------------
# GEMINI CLIENT
# -----------------------------

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("GEMINI_API_KEY was not found.")

client = genai.Client(api_key=api_key)

# -----------------------------
# GEMINI CODE ANALYSIS
# -----------------------------

prompt = f"""
You are a senior Python code reviewer.

Analyze the following Python code:

```python
{code}

Identify actual problems in the code.

Rules:

Focus only on the provided code.
Do not invent problems.
Identify runtime errors and important code quality issues.
For each issue, provide:
problem
severity
explanation
recommended_fix

Return the analysis clearly.
"""

# -----------------------------
# GEMINI CODE ANALYSIS
# -----------------------------

try:
    analysis_text = analyze_code_with_gemini(prompt)

    print("\nGemini Code Analysis:")
    print(analysis_text)

except Exception as e:
    print("\nGemini Code Analysis:")
    print("Gemini API is currently unavailable.")
    print("Reason:", e)

# -----------------------------
# TOOL: Run Python file
# -----------------------------

def run_python_file(file_path):
    """
    Run a Python file and return the execution result.
    """

    if not os.path.exists(file_path):
        return f"File does not exist: {file_path}"

    try:
        result = subprocess.run(
            ["python", file_path],
            capture_output=True,
            text=True,
            timeout=10
        )

        return {
            "return_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr
        }

    except subprocess.TimeoutExpired:
        return {
            "return_code": -1,
            "stdout": "",
            "stderr": "Execution timed out."
        }

    except Exception as e:
        return {
            "return_code": -1,
            "stdout": "",
            "stderr": str(e)
        }

# -----------------------------
# Test Code Execution
# -----------------------------

calculator_path = os.path.join(destination, "calculator.py")

execution_result = run_python_file(calculator_path)

print("\nCode Execution Result:")
print(execution_result)


# -----------------------------
# GENERATE TESTS WITH GEMINI
# -----------------------------

test_prompt = f"""
You are a senior Python test engineer.

Generate pytest tests for the following Python source code.

Source code:

```python
{code}

Requirements:

Generate valid executable Python code only.

Import all functions that are being tested from the source module.

The source file is named calculator.py, so use:
from calculator import add, divide

Do not include Markdown code fences.
Do not include explanations outside the Python code.

Test normal inputs and important edge cases.

IMPORTANT:
Generate tests based on the EXPECTED CORRECT BEHAVIOR of the functions,
not simply the current implementation behavior.

For add(a, b):

Verify that two numbers are added correctly.
Include positive, negative, zero, and floating-point values.

For divide(a, b):

Verify that the result is a / b.
Include positive, negative, zero numerator, and floating-point values.
Include a test for division by zero.
The division-by-zero test should expect ZeroDivisionError for the CURRENT implementation.

The tests must be strong enough to detect an incorrect implementation such as:
return a * b

Do not change the source code.
Do not make the tests match an incorrect implementation.

Return only the complete pytest code.

Generate the tests now.
"""


# -----------------------------
# GENERATE TESTS WITH GEMINI
# -----------------------------


@traceable(name="Gemini Test Generation")
def generate_tests_with_gemini(prompt):
    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=prompt
    )
    return response.text


test_code = None

try:

    test_code = generate_tests_with_gemini(test_prompt).strip()

    # Remove Markdown code fences if Gemini returns them
    if test_code.startswith("```"):
        lines = test_code.splitlines()

        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        test_code = "\n".join(lines).strip()

    print("\nGenerated Tests:")
    print(test_code)

except Exception as e:
    print("\nGemini Test Generation:")
    print("Gemini API is currently unavailable.")
    print("Reason:", e)


# -----------------------------
# SAVE / USE GENERATED TESTS
# -----------------------------

test_file = os.path.join(destination, "test_calculator.py")

if test_code:
    with open(test_file, "w", encoding="utf-8") as file:
        file.write(test_code)

    print("\nGenerated tests saved to:")
    print(test_file)

else:
    if os.path.exists(test_file):
        print("\nGemini test generation was skipped.")
        print("Using existing test file:")
        print(test_file)

    else:
        print("\nNo test file available.")
        test_file = None

# -----------------------------
# TOOL: Run Pytest
# -----------------------------

def run_pytest(directory):
    """
    Run pytest inside the repository directory
    and return the test execution result.
    """

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-v"
            ],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=30
        )

        return {
            "return_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr
        }

    except subprocess.TimeoutExpired:
        return {
            "return_code": -1,
            "stdout": "",
            "stderr": "Pytest execution timed out."
        }

    except Exception as e:
        return {
            "return_code": -1,
            "stdout": "",
            "stderr": str(e)
        }


# -----------------------------
# TOOL: Git Commit and Push
# -----------------------------

def git_commit_and_push(directory):
    """
    Commit and push changes to the GitHub repository.
    """

    try:
        # Check Git status
        status_result = subprocess.run(
            ["git", "status", "--short"],
            cwd=directory,
            capture_output=True,
            text=True
        )

        print("\nGit Status:")
        print(status_result.stdout)

        # No changes
        if not status_result.stdout.strip():
            print("\nNo Git changes to commit.")
            return {
                "success": True,
                "message": "No changes to commit."
            }

        # Add changes
        subprocess.run(
            ["git", "add", "."],
            cwd=directory,
            check=True
        )

        # Commit
        commit_result = subprocess.run(
            [
                "git",
                "commit",
                "-m",
                "AI agent: apply code fix and update tests"
            ],
            cwd=directory,
            capture_output=True,
            text=True
        )

        print("\nGit Commit:")
        print(commit_result.stdout)

        if commit_result.returncode != 0:
            print(commit_result.stderr)

        # Push
        push_result = subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=directory,
            capture_output=True,
            text=True
        )

        print("\nGit Push:")
        print(push_result.stdout)

        if push_result.stderr:
            print(push_result.stderr)

        return {
            "success": push_result.returncode == 0,
            "message": push_result.stdout
        }

    except Exception as e:
        print("\nGit automation failed:")
        print(e)

        return {
            "success": False,
            "message": str(e)
        }
    
# -----------------------------
# RUN GENERATED TESTS
# -----------------------------

print("\nRunning pytest...")

pytest_result = run_pytest(destination)

print("\nPytest Result:")
print(pytest_result)

# -----------------------------
# ANALYZE AND FIX PYTEST FAILURE
# -----------------------------

if pytest_result["return_code"] != 0:

    print("\nTests failed.")
    print("Sending pytest failure to Gemini for analysis...")

    failure_output = (
        pytest_result["stdout"]
        + "\n"
        + pytest_result["stderr"]
    )

    source_file = os.path.join(
        destination,
        "calculator.py"
    )

    # -----------------------------
    # FAILURE PROMPT
    # -----------------------------

    failure_prompt = f"""
You are a senior Python debugging engineer.

The following Python source code was tested using pytest.

SOURCE CODE:

{code}

PYTEST FAILURE OUTPUT:

{failure_output}

Analyze the failure.

Identify:
1. Root cause
2. Responsible file or function
3. Why the failure occurred
4. Recommended fix

Then provide the COMPLETE corrected Python source code.

Rules:
- Focus only on the provided source code and pytest output.
- Do not invent unrelated problems.
- Return the corrected complete source code.
- Do not include Markdown code fences.
- Do not include explanations before or after the corrected code.
- Return Python code only.
"""

    # -----------------------------
    # GEMINI FAILURE ANALYSIS + FIX
    # -----------------------------

    @traceable(name="Gemini Failure Analysis")
    def analyze_failure_with_gemini(prompt):

        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt
        )

        return response.text

    try:

        fix_code = analyze_failure_with_gemini(
            failure_prompt
        ).strip()

        print("\nGemini Generated Fix:")
        print(fix_code)

    except Exception as e:

        print("\nGemini fix process failed.")
        print("Reason:", e)

        fix_code = None

    # -----------------------------
    # APPLY GEMINI FIX
    # -----------------------------

    if fix_code:

        # Remove Markdown code fences if present
        if "```python" in fix_code:

            fix_code = fix_code.split(
                "```python",
                1
            )[1]

            if "```" in fix_code:
                fix_code = fix_code.split(
                    "```",
                    1
                )[0]

        elif "```" in fix_code:

            parts = fix_code.split("```")

            if len(parts) >= 3:
                fix_code = parts[1]

        fix_code = fix_code.strip()

        # -----------------------------
        # BACKUP ORIGINAL SOURCE
        # -----------------------------

        backup_file = os.path.join(
            destination,
            "calculator_backup.py"
        )

        try:

            shutil.copy2(
                source_file,
                backup_file
            )

            print("\nOriginal calculator.py backed up to:")
            print(backup_file)

        except Exception as e:

            print("\nFailed to create backup.")
            print("Reason:", e)

        # -----------------------------
        # APPLY FIX
        # -----------------------------

        try:

            with open(
                source_file,
                "w",
                encoding="utf-8"
            ) as file:

                file.write(fix_code)

            print("\nGemini fix applied to calculator.py.")

        except Exception as e:

            print("\nFailed to apply Gemini fix.")
            print("Reason:", e)

        # -----------------------------
        # RUN PYTEST AGAIN
        # -----------------------------

        print("\nRunning pytest again after applying fix...")

        pytest_result = run_pytest(destination)

        print("\n=============================")
        print("FINAL PYTEST RESULT")
        print("=============================")

        print(
            "Return Code:",
            pytest_result["return_code"]
        )

        print("\nPytest Output:")
        print(pytest_result["stdout"])

        if pytest_result["stderr"]:

            print("\nPytest Errors:")
            print(pytest_result["stderr"])

        # -----------------------------
        # FINAL RESULT
        # -----------------------------

        if pytest_result["return_code"] == 0:

            print("\nFINAL RESULT: SUCCESS")
            print(
                "All tests passed after applying "
                "the Gemini fix."
            )

            print("\n[STEP 7] Git Automation")

            git_result = git_commit_and_push(
                destination
            )

            if git_result.get("success"):

                print(
                    "\nGitHub update completed successfully."
                )

            else:

                print("\nGitHub update failed.")

        else:

            print("\nFINAL RESULT: FAILED")
            print(
                "Tests still fail after applying "
                "the Gemini fix."
            )

    else:

        print("\nNo Gemini fix was generated.")
        print("Automatic fix was skipped.")

else:

    print("\nAll tests passed successfully.")