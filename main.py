import os
import json
from dotenv import load_dotenv
from google import genai

# Load environment variables from .env
load_dotenv()

# Check that the API key exists
api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("GEMINI_API_KEY was not found.")

# Create Gemini client
client = genai.Client(api_key=api_key)

# Send a request using the Interactions API
interaction = client.interactions.create(
    model="gemini-3.6-flash",
    input="""
You are a senior Python code reviewer.

Analyze the following Python code:

def divide(a, b):
    return a / b

Identify actual problems in the code.

Rules:
- Severity must be exactly one of: Low, Medium, High.
- Do not rewrite the code.
- Do not invent problems.
- Focus only on the provided function.

Return ONLY valid JSON.

Use exactly this structure:

{
  "issues": [
    {
      "problem": "string",
      "severity": "Low | Medium | High",
      "explanation": "string",
      "recommended_fix": "string"
    }
  ]
}
"""
)

# Print the response
result = json.loads(interaction.output_text)
issues = result["issues"]

for issue in issues:
    print("Problem:", issue["problem"])
    print("Severity:", issue["severity"])
    print("Explanation:", issue["explanation"])
    print("Recommended Fix:", issue["recommended_fix"])
