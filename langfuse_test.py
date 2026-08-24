import os
from dotenv import load_dotenv
from langfuse import get_client

# -----------------------------
# LOAD ENVIRONMENT VARIABLES
# -----------------------------

load_dotenv()

public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
secret_key = os.getenv("LANGFUSE_SECRET_KEY")
base_url = os.getenv("LANGFUSE_BASE_URL")

print("=============================")
print("LANGFUSE TRACE TEST")
print("=============================")

print("LANGFUSE_PUBLIC_KEY =", "SET" if public_key else "NOT SET")
print("LANGFUSE_SECRET_KEY =", "SET" if secret_key else "NOT SET")
print("LANGFUSE_BASE_URL =", base_url)

if not public_key:
    raise ValueError("LANGFUSE_PUBLIC_KEY was not found.")

if not secret_key:
    raise ValueError("LANGFUSE_SECRET_KEY was not found.")


# -----------------------------
# CREATE LANGFUSE CLIENT
# -----------------------------

langfuse = get_client()


# -----------------------------
# CREATE TEST TRACE
# -----------------------------

with langfuse.start_as_current_observation(
    as_type="span",
    name="ai-agent-learning-test"
) as span:

    span.update(
        input={
            "task": "Test Langfuse tracing"
        },
        output={
            "status": "Langfuse trace created successfully"
        }
    )

    print("\nTrace created successfully.")


# -----------------------------
# FLUSH TRACE
# -----------------------------

langfuse.flush()

print("\n=============================")
print("LANGFUSE TRACE TEST COMPLETED")
print("=============================")