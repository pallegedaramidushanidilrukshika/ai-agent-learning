# AI Agentic Software Debugging Workflow

## 1. Project Overview

This project demonstrates the development of a general, technology-aware AI agentic software debugging and code fixing workflow using Python and Large Language Models (LLMs).

The workflow is designed to analyze software projects, detect the technology stack, reproduce software failures, identify root causes, modify the relevant source code, execute appropriate tests or validation commands, and verify whether the issue has been successfully resolved.

The workflow is designed to be technology-agnostic and can be extended to different software technologies such as:

- Node.js
- JavaScript
- TypeScript
- React
- Angular
- Java
- .NET
- Python
- Other supported software projects

The execution strategy is dynamically determined based on the detected project technology and repository context.

---

## 2. Core Agentic Workflow

The main debugging workflow follows this process:

```text
GitHub / Software Repository
            ↓
Repository Analysis
            ↓
Technology / Project Detection
            ↓
Bug Reproduction
            ↓
Failure Analysis
            ↓
Root Cause Identification
            ↓
Relevant Code Identification
            ↓
AI Code Fix
            ↓
Test Generation / Existing Tests
            ↓
Appropriate Test / Build / Validation
            ↓
Fix Verification
            ↓
        Fix Failed?
          ↙       ↘
        Yes        No
         ↓         ↓
   Re-analyze    Success
    and Fix
         ↓
     Retry Loop
````

The workflow supports bounded retry cycles so that if the generated fix does not successfully resolve the issue, the failure information can be analyzed again and another fix can be attempted.

---

## 3. Agentic Workflow Stages

### Stage 1 - Repository Analysis & Technology Detection

**File:**

```text
repository_agent.py
```

This agent analyzes the target software repository and identifies important project information such as:

* Project type
* Programming technology
* Package manager
* Test framework
* Test command
* Build command
* Start command
* Validation method
* Execution strategy

The detected repository context is stored in:

```text
repository_context.json
```

The workflow uses this context to determine how subsequent agents should interact with the project.

---

### Stage 2 - Bug Reproduction

**File:**

```text
bug_reproduction_agent.py
```

This agent attempts to reproduce the reported software issue using the execution strategy detected during repository analysis.

The reproduction result is stored in:

```text
reproduction_result.json
```

The workflow continues only when the reported failure can be reproduced successfully.

---

### Stage 3 - Failure Analysis & Root Cause Identification

**File:**

```text
bug_diagnosis_agent.py
```

This agent uses an LLM to analyze the reproduced failure.

The diagnosis identifies:

* Bug title
* Project type
* Failure analysis
* Root cause
* Affected files
* Evidence
* Proposed fix
* Verification plan

The diagnosis is stored in:

```text
diagnosis.json
```

Previous diagnoses can also be archived during retry attempts for debugging and comparison.

---

### Stage 4 - AI Code Fix

**File:**

```text
code_fix_agent.py
```

The Code Fix Agent uses the diagnosis produced by the Failure Analysis Agent to determine the relevant source code changes.

The agent:

1. Reads the relevant source files.
2. Analyzes the existing implementation.
3. Determines the required modification.
4. Applies the fix to the relevant source code.
5. Preserves unrelated project files and existing tests.

The purpose of this stage is to modify only the code necessary to resolve the identified root cause.

---

### Stage 5 - AI Test Generation

**File:**

```text
test_generation_agent.py
```

This agent generates or updates appropriate tests based on the detected project technology, diagnosis, and verification requirements.

The test generation process is designed to be technology-aware rather than being limited to a single testing framework.

Depending on the detected project, the workflow can use appropriate testing approaches such as:

* Node.js test runner
* Jest
* Pytest
* JUnit
* .NET testing frameworks
* Other project-specific testing frameworks

The workflow uses the repository context to determine the appropriate test execution strategy.

---

### Stage 6 - Test Execution & Validation

**File:**

```text
test_result_checker.py
```

This stage executes the appropriate test, build, or validation command determined from the repository context.

The execution result is analyzed to determine:

* Whether execution succeeded
* Number of tests
* Number of passed tests
* Number of failed tests
* Return code
* Validation result

The result is stored in:

```text
test_result.json
```

The workflow does not assume a single testing framework or hardcoded test command.

---

### Stage 7 - Fix Verification

**File:**

```text
verification_agent.py
```

The Verification Agent evaluates whether the applied code modification successfully resolves the original issue.

Verification considers:

* Original failure
* Root cause
* Applied code changes
* Test results
* Repository execution strategy
* Verification requirements

The verification result is stored in:

```text
verification_report.json
```

---

### Stage 8 - Retry and Re-analysis

If the tests or verification fail, the workflow does not immediately terminate.

Instead, the failure information is collected and provided to the diagnosis agent for another analysis cycle.

```text
Test / Verification Failed
          ↓
Failure Context
          ↓
Re-diagnosis
          ↓
Code Fix
          ↓
Test / Validation
          ↓
Verification
```

The workflow uses a bounded retry mechanism to prevent infinite debugging loops.

---

## 4. Workflow Orchestrator

**File:**

```text
workflow_orchestrator.py
```

The Workflow Orchestrator coordinates the complete agentic debugging process.

It controls the execution order of the agents:

```text
Repository Analysis
        ↓
Bug Reproduction
        ↓
Bug Diagnosis
        ↓
Code Fix
        ↓
Test Generation
        ↓
Test Execution
        ↓
Verification
        ↓
Retry if Required
        ↓
Final Workflow Report
```

The orchestrator also:

* Validates intermediate results.
* Removes stale workflow artifacts at the beginning of a new run.
* Maintains retry history.
* Archives previous diagnoses.
* Prevents the workflow from continuing when critical stages fail.
* Produces a final workflow report.

The final workflow information is stored in:

```text
workflow_report.json
```

---

## 5. Project Structure

The main project contains the following workflow components:

```text
ai-agent-learning/
│
├── repository_agent.py
├── bug_reproduction_agent.py
├── bug_diagnosis_agent.py
├── code_fix_agent.py
├── test_generation_agent.py
├── test_result_checker.py
├── verification_agent.py
├── workflow_orchestrator.py
│
├── repository_context.json
├── reproduction_result.json
├── diagnosis.json
├── test_result.json
├── verification_report.json
└── workflow_report.json
```

Additional learning and experimentation files related to LLMs, LangChain, LangGraph, LangSmith, LangFuse, and n8n are also included in the project.

---

## 6. Demonstration Project

The current demonstration uses a Node.js Todo application as the target software repository.

The project is located separately from the AI agent learning project and is used as a target repository for testing the debugging workflow.

The demonstration validates that the agentic workflow can:

1. Analyze the repository.
2. Detect the Node.js technology stack.
3. Reproduce the reported issue.
4. Analyze the failure.
5. Identify the root cause.
6. Modify the relevant source code.
7. Generate or use appropriate tests.
8. Execute the tests.
9. Verify the applied fix.
10. Produce a final workflow report.

The architecture itself is designed to support other technologies without changing the overall debugging workflow.

---

## 7. Technology-Aware Execution

A key objective of this project is to avoid hardcoding a single technology or testing framework.

Instead, the workflow detects the project characteristics and determines the appropriate execution strategy.

For example:

| Technology | Possible Test / Validation    |
| ---------- | ----------------------------- |
| Node.js    | `node --test`, Jest, npm test |
| React      | npm test, build, lint         |
| Angular    | Angular test/build commands   |
| Python     | pytest, unittest              |
| Java       | Maven / Gradle test           |
| .NET       | dotnet test / build           |

The actual command is determined from the detected repository context rather than being permanently hardcoded into the workflow orchestrator.

---

## 8. LLM and Agent Technologies

The project includes practical learning and implementation of modern LLM and agentic technologies.

### LLM

Large Language Models are used for:

* Failure analysis
* Root cause identification
* Code reasoning
* Code modification decisions
* Test generation
* Verification

### LangChain

LangChain concepts and tools are used to build LLM-powered agents and tool-based workflows.

### LangGraph

LangGraph is used to explore stateful and multi-step agentic workflows.

Relevant learning files include:

```text
langgraph_demo.py
langgraph_tool_demo.py
langgraph_multi_tool.py
langgraph_workflow.py
langgraph_full_workflow.py
```

### LangSmith

LangSmith is used for LLM and agent observability and tracing.

Relevant file:

```text
langsmith_test.py
```

### LangFuse

LangFuse is included for LLM and agent observability and workflow tracing.

Relevant files include:

```text
langfuse_test.py
langfuse_workflow.py
```

### n8n

n8n has been explored for workflow automation and integration with agentic systems.

---

## 9. Configuration and Environment Variables

Sensitive configuration such as API keys must be stored in a `.env` file.

Example:

```text
.env
.env.example
```

API keys and other secrets must never be committed to Git.

The `.gitignore` file is configured to prevent sensitive and generated files from being committed where appropriate.

---

## 10. Setup

Create a Python virtual environment:

```bash
python -m venv venv
```

Activate the environment on Windows:

```powershell
venv\Scripts\activate
```

Install the required dependencies:

```bash
pip install -r requirements.txt
```

Create a `.env` file using `.env.example` as a reference and configure the required API credentials.

---

## 11. Running the Complete Agentic Workflow

From the project directory:

```powershell
cd C:\Users\SINGER\OneDrive\Desktop\ai-agent-learning
```

Run the complete workflow:

```powershell
python workflow_orchestrator.py
```

The orchestrator will execute the agents in sequence and generate the relevant JSON result files.

The final workflow result is written to:

```text
workflow_report.json
```

---

## 12. Individual Agent Execution

Agents can also be executed individually when debugging or testing the workflow.

Repository analysis:

```powershell
python repository_agent.py
```

Bug reproduction:

```powershell
python bug_reproduction_agent.py
```

Bug diagnosis:

```powershell
python bug_diagnosis_agent.py
```

Code fixing:

```powershell
python code_fix_agent.py
```

Test generation:

```powershell
python test_generation_agent.py
```

Test execution:

```powershell
python test_result_checker.py
```

Verification:

```powershell
python verification_agent.py
```

For normal end-to-end execution, use:

```powershell
python workflow_orchestrator.py
```

---

## 13. Workflow Outputs

The workflow produces structured JSON artifacts for each major stage.

```text
repository_context.json
        ↓
reproduction_result.json
        ↓
diagnosis.json
        ↓
test_result.json
        ↓
verification_report.json
        ↓
workflow_report.json
```

These artifacts provide traceability across the debugging process and make it possible to inspect the reasoning and results of each stage.

---

## 14. Safety and Code Protection

The workflow is designed to modify only relevant project source files identified during diagnosis.

Important project files such as:

* Environment configuration
* Existing tests
* Dependency files
* Backup files
* Unrelated source files

should not be modified unnecessarily.

The code fixing process should always be based on the diagnosis and affected files rather than blindly modifying the repository.

---

## 15. Current Status

### Agentic Debugging Workflow

* Repository analysis: Completed
* Technology detection: Implemented
* Repository context generation: Completed
* Bug reproduction: Completed
* Failure analysis: Completed
* Root cause identification: Completed
* AI code fixing: Implemented
* Test generation: Implemented
* Technology-aware test execution: Implemented
* Fix verification: Implemented
* Retry mechanism: Implemented
* Workflow orchestration: Implemented
* Structured workflow reporting: Implemented

### AI / LLM Learning

* LLM fundamentals: Completed
* Prompt engineering: Completed
* Tool/function calling: Completed
* AI Agent concepts: Completed
* LangChain learning: Completed
* LangGraph learning/workflows: Completed
* LangSmith tracing: Completed
* LangFuse workflow/testing: Completed
* n8n connectivity testing: Completed

---

## 16. Future Improvements

Possible future improvements include:

* GitHub repository integration
* Automatic repository cloning
* Support for additional programming languages
* Improved technology detection
* More advanced test framework detection
* Automatic build and lint validation
* Pull Request generation
* GitHub issue integration
* Improved multi-agent coordination
* Human approval before applying code changes
* More advanced retry and recovery strategies
* Full external workflow automation using n8n

---

## 17. Project Goal

The main goal of this project is to demonstrate how AI agents can participate in software engineering tasks by moving beyond simple question-answering and performing a complete debugging workflow.

The target architecture is:

```text
GitHub Project
      ↓
Detect Project / Technology
      ↓
Reproduce Bug
      ↓
Analyze Failure
      ↓
Identify Root Cause
      ↓
Modify Relevant Code
      ↓
Run Appropriate Tests / Build / Validation
      ↓
Verify Fix
      ↓
If Failed → Analyze Again → Fix Again
```

This demonstrates a general AI-powered software engineering workflow that can adapt its execution strategy according to the technology and structure of the target software project.

```
