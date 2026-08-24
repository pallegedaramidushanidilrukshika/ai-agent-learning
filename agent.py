import os
from dotenv import load_dotenv
from google import genai

# Load environment variables
load_dotenv()

# Get API key
api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("GEMINI_API_KEY was not found.")

# Create Gemini client
client = genai.Client(api_key=api_key)


# -----------------------------
# Tools
# -----------------------------

def read_file(file_path: str) -> str:
    """Read and return the contents of a file."""
    with open(file_path, "r", encoding="utf-8") as file:
        return file.read()


def list_files(directory: str) -> list[str]:
    """Return the files in a directory."""
    return os.listdir(directory)


def run_python_file(file_path: str) -> str:
    """Run a Python file and return its output or error."""

    import subprocess

    result = subprocess.run(
        ["python", file_path],
        capture_output=True,
        text=True
    )

    if result.returncode == 0:
        return result.stdout

    return f"ERROR:\n{result.stderr}"


# -----------------------------
# Agent
# -----------------------------

response = client.models.generate_content(
    model="gemini-3.6-flash",

    contents="""
You are a Python code analysis agent.

Your goal is:

Inspect the current project and determine whether the Python code has obvious runtime problems.

You have access to tools that allow you to:
- list files
- read files
- run Python files

Use the tools when necessary.

Focus on sample_code.py.

Do not modify any files.

After your investigation, provide a concise final report.
""",

    config={
        "tools": [
            read_file,
            list_files,
            run_python_file
        ]
    }
)

print(response.text)