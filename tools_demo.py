import os
from dotenv import load_dotenv
from google import genai
import subprocess

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
    result = subprocess.run(
        ["python", file_path],
        capture_output=True,
        text=True
    )

    if result.returncode == 0:
        return result.stdout

    return f"ERROR:\n{result.stderr}"

# -----------------------------
# Ask Gemini
# -----------------------------
response = client.models.generate_content(
    model="gemini-3.6-flash",
    contents="""
Inspect the current project.

Your goal is to understand and validate the Python code.

Follow these steps:

1. Find the available files.
2. Identify the Python source files.
3. Read sample_code.py.
4. Analyze the function.
5. Run sample_code.py to see whether it executes successfully.
6. Report your findings.

Use the available tools when necessary.

Do not modify any files.
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