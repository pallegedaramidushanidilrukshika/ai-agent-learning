import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse


app = FastAPI(title="AI Agent API")


PROJECT_DIR = Path(__file__).resolve().parent
AGENT_FILE = PROJECT_DIR / "code_fix_agent.py"


@app.get("/")
def home():
    return {
        "status": "running",
        "service": "AI Code Fix Agent API"
    }


@app.post("/run-agent")
def run_agent():

    try:

        result = subprocess.run(
            [
                sys.executable,
                str(AGENT_FILE)
            ],
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            timeout=120
        )

        return JSONResponse(
            content={
                "status": "completed",
                "return_code": result.returncode,
                "output": result.stdout,
                "error": result.stderr
            }
        )

    except subprocess.TimeoutExpired:

        return JSONResponse(
            status_code=500,
            content={
                "status": "timeout",
                "message": "Agent execution timed out."
            }
        )

    except Exception as e:

        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": str(e)
            }
        )