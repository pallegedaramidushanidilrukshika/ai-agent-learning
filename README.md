# AI Agent Learning Project

## 1. Project Overview

This project demonstrates the development of an agentic software engineering workflow using Python, LLMs, LangChain, LangGraph, LangSmith, and LangFuse.

The main workflow focuses on automated test failure detection, failure analysis, code fixing, and verification using pytest.

## 2. Core Agentic Workflow

```text
Test Project
     ↓
Pytest
     ↓
Failure Detection
     ↓
Failure Analysis Agent
     ↓
read_file Tool
     ↓
Failure Analysis
     ↓
Code Fix Agent
     ↓
read_file + write_file Tools
     ↓
Code Modification
     ↓
Pytest Again
     ↓
Tests Passed
```

## 3. Main Components

### Failure Pipeline

`failure_pipeline.py`

Runs the pytest test suite and extracts information about failed tests.

### Failure Analysis Agent

`failure_analysis_agent.py`

Analyzes the detected failure using an LLM agent and the `read_file` tool.

### Code Fix Agent

`code_fix_agent.py`

Analyzes the failed test, reads the relevant file, determines a fix, and updates the file using a file-writing tool.

### Test Project

The demo test project contains:

* `calculator.py`
* `test_calculator.py`

## 4. Verified Workflow

The workflow was tested using a deliberately failing test:

```python
def test_intentional_failure():
    assert 1 == 2
```

The Code Fix Agent identified the incorrect assertion and updated the test.

The test suite was then executed again.

Result:

```text
10 passed
```

## 5. LangGraph

The project also contains LangGraph demonstrations and workflow implementations for building stateful agentic workflows.

Relevant files include:

* `langgraph_demo.py`
* `langgraph_tool_demo.py`
* `langgraph_multi_tool.py`
* `langgraph_workflow.py`
* `langgraph_full_workflow.py`

## 6. LangSmith

LangSmith is used for LLM and agent observability/tracing.

Relevant file:

`langsmith_test.py`

The LangSmith tracing configuration has been tested successfully.

## 7. LangFuse

LangFuse is included for LLM/agent observability and workflow tracing.

Relevant files include:

* `langfuse_test.py`
* `langfuse_workflow.py`

## 8. n8n

n8n connectivity has been tested as part of the agentic workflow learning.

Full end-to-end orchestration through n8n is planned as a further integration step.

## 9. Setup

Create a Python virtual environment:

```bash
python -m venv venv
```

Activate the environment on Windows:

```powershell
venv\Scripts\activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Create a `.env` file using `.env.example` as a reference and add the required API keys.

## 10. Running the Core Workflow

Run the failure pipeline:

```bash
python failure_pipeline.py
```

Run the failure analysis agent:

```bash
python failure_analysis_agent.py
```

Run the code fix agent:

```bash
python code_fix_agent.py
```

After the fix, verify the tests using pytest.

## 11. Observability

The project uses:

* LangSmith for tracing and observability.
* LangFuse for tracing and observability.

API keys and secrets must be stored in `.env` and must not be committed to the repository.

## 12. Current Status

* Test generation: Completed
* Pytest execution: Completed
* Failure detection: Completed
* Failure analysis agent: Completed
* Code fix agent: Completed
* Pytest verification after fix: Completed
* LangGraph learning/workflows: Completed
* LangSmith tracing test: Completed
* LangFuse workflow/testing: Completed
* n8n connectivity testing: Completed
* Full GitHub → n8n → Agent → pytest → fix → verification integration: Next integration step
