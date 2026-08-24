import os

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI


# -----------------------------
# LOAD ENVIRONMENT VARIABLES
# -----------------------------

load_dotenv()

print("=============================")
print("LANGSMITH TRACE TEST")
print("=============================")

print("LANGSMITH_TRACING =", os.getenv("LANGSMITH_TRACING"))
print("LANGSMITH_PROJECT =", os.getenv("LANGSMITH_PROJECT"))
print(
    "LANGSMITH_API_KEY =",
    "SET" if os.getenv("LANGSMITH_API_KEY") else "NOT SET"
)


# -----------------------------
# CREATE GEMINI MODEL
# -----------------------------

llm = ChatGoogleGenerativeAI(
    model="gemini-3.5-flash",
    temperature=0
)


# -----------------------------
# MAKE LLM CALL
# -----------------------------

response = llm.invoke(
    "Explain in one sentence what an AI agent is."
)


# -----------------------------
# PRINT RESULT
# -----------------------------

print("\n=============================")
print("LLM RESPONSE")
print("=============================")

print(response.content)

print("\n=============================")
print("TRACE TEST COMPLETED")
print("=============================")
