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

The workflow continues only when the reported failure can be reproduced. When automatic reproduction does not confirm the bug, the workflow asks a human, in this order:

1. **AI reproduction test** (`reproduction_test_agent.py`, Jest and pytest projects). The AI writes one test file for the bug and the workflow runs it. A test only counts as reproducing the bug when it fails on an assertion (`expect(...)` / `assert`). A crash, a missing module or a syntax error is reported as "could not run correctly". The human then chooses **Confirm**, **Revise** (with feedback, up to 3 rounds) or **Use manual steps**. A confirmed test is saved as `confirmed_reproduction_test.json`. It must fail before the fix and pass after it.
2. **Manual reproduction**, in one of two ways:
   * **Playwright recording (web console).** The human records the bug in a browser with `playwright codegen` and uploads the script. See "Playwright recording" below.
   * **Typed steps.** The human enters steps, expected and observed behaviour, and evidence.

#### Playwright recording

**Files:** `browser_recording.py` (parser and safety check), `browser_replay_agent.py` (starts the app and runs the replay), `browser_replay/replay.mjs` (fixed replay runner)

1. Record the bug against the local app:

   ```powershell
   cd C:\Users\SINGER\OneDrive\Desktop\ai-agent-learning\browser_replay
   npx playwright codegen http://localhost:3000
   ```

   Reproduce the bug in the browser, then add by hand an assertion for what the correct app should show (for example `await expect(page.locator('input[name="check"]')).toHaveCount(2);`, see the note below on toolbar assertions). Save the script (`.js`, `.ts` or `.py`).
2. Stop your own copy of the app, then upload the script in the console's **Upload a Playwright recording** panel.
3. The workflow turns the script into a step list. **The uploaded script is never run.** Only these parts are kept:
   * `goto` to `localhost` / `127.0.0.1`, one app only
   * locators: `getByRole`, `getByText`, `getByLabel`, `getByPlaceholder`, `getByTestId`, `getByTitle`, `getByAltText`, `locator`, `filter`, `nth`, `first`, `last`
   * actions: `click`, `dblclick`, `fill`, `press`, `check`, `uncheck`, `selectOption`, `hover`
   * assertions (also with `not`): `toBeVisible`, `toBeHidden`, `toBeChecked`, `toBeEnabled`, `toBeDisabled`, `toContainText`, `toHaveText`, `toHaveValue`, `toHaveCount`

   Any other line (`require`, `evaluate`, template strings, other URLs, file uploads, …) rejects the recording with its line number. Limits: 64 KB, 400 lines, 80 steps, at least one assertion.
4. The workflow starts the app itself, replays the steps in headless Chromium (requests to other hosts are blocked), and stops the app.
   * A recorded **assertion fails**: the bug is shown, and the recording is confirmed (`confirmed_browser_recording.json`).
   * **Everything passes**: the recording does not show the bug, and the user can upload another one or type steps.
   * A **step cannot run** (element not found, app did not start): it is reported as an error, not as a reproduction.
5. After each fix attempt the same steps are replayed on the fixed app, and every step must pass. Otherwise the attempt fails and is retried.
6. Every replay saves a Playwright trace (`browser_replay_<name>_trace.zip`) and, when it stops at a failing step, a screenshot (`browser_replay_<name>_failure.png`) in the run folder. The console shows the screenshots and links to download the traces under **Browser replay evidence**. Open a trace with `npx playwright show-trace <file>` in `browser_replay/` to see each step with the page before and after it. The API serves only these replay files from the run folder, nothing else.

The replay needs Playwright and Chromium, installed once in `browser_replay/`:

```powershell
cd C:\Users\SINGER\OneDrive\Desktop\ai-agent-learning\browser_replay
npm install
npx playwright install chromium
```

Without them the console reports "Playwright is not installed" and typed steps still work.

**Supported apps**

* **Single-server apps** (Level 1 Todo): the workflow starts the app with its start command and replays against it.
* **Full-stack apps** (Level 2 MERN): the workflow starts a **temporary MongoDB** (temporary data folder, free port), the backend on its configured port and the Vite frontend on a free port, replays against the frontend, then stops everything and deletes the temporary database. The database setting name (for example `ATLAS_URI`) is read from `config.env.example`; the real `config.env` is never read. If the database cannot be isolated, the replay is refused, so the user's own data is never changed. Every replay starts from the same empty database.
* **Level 3 (AWS WebSocket)** is not replayed: its frontend talks to the deployed AWS API, and the workflow must not use AWS. Level 3 uses the AI reproduction test instead.

**Record mode for full-stack apps.** Recording against your own running copy would write test data into your real database. Instead start the app isolated, record against the URL it prints, then press Enter to stop it:

```powershell
cd C:\Users\SINGER\OneDrive\Desktop\ai-agent-learning
.\venv\Scripts\python.exe browser_replay_agent.py --serve <path-to-repository>
# in another terminal:
cd browser_replay
npx playwright codegen --output <recording.spec.ts> <printed URL>
```

Codegen's toolbar assertions record what the page shows **now**, which on the buggy app is the wrong behaviour. Add the expected-behaviour assertion by hand, for example `await expect(page.locator('tbody tr')).toHaveCount(1);`. When the list loads after navigation, first wait for something that must be there (for example `await expect(page.getByText('Alice')).toBeVisible();`) so a count is not checked before the data arrives.

**Resetting a demo copy between runs.** A run leaves the AI fix and the generated test in the project copy. To run the same demo again, put the copy back to its committed (buggy) state. The script shows what it would change; add `--yes` to apply it. It keeps commits, `node_modules` and `.env` / `config.env` files, and refuses to touch this workflow project:

```powershell
.\venv\Scripts\python.exe reset_demo_copy.py <path-to-demo-copy> [<another-copy> ...]
.\venv\Scripts\python.exe reset_demo_copy.py <path-to-demo-copy> --yes
```

Use it only on copies made for demos, never on a repository with your own unsaved work.

Either way, the human can attach evidence files instead of describing the bug in text: HAR recordings, JSON message logs, `.log`/`.txt` files and recorded test scripts (max 5 files, 256 KB each, 1 MB total). Files are read as text and never executed. Tokens, cookies, keys and passwords are redacted before the AI sees them.

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
├── workflow_ui_api.py            (local API for the web console)
├── workflow_control.py           (Retry / Continue / Stop Safely, human input)
├── workflow_validation.py        (bounded commands, test output parsing)
├── browser_recording.py          (Playwright recording parser)
├── browser_replay_agent.py       (starts the app and replays a recording)
├── browser_replay/replay.mjs     (fixed Playwright replay runner)
├── reset_demo_copy.py            (puts a demo copy back to its buggy state)
├── level3-serverless-app/frontend/src/WorkflowConsole.tsx (web console)
├── tests/                        (offline regression tests)
│
├── workflow_runs/<run-id>/       (artifacts of each console run)
├── repository_context.json       (terminal-mode artifacts below)
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
Sensitive configuration such as API keys must be stored in a local .env file.

The repository includes .env.example as a template for the required environment variables.

API keys and other secrets must never be committed to Git.

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

When the workflow needs a human (reproduction review, manual steps, fix review), it prompts in the terminal. Without an interactive terminal it never assumes an answer: an unreproduced bug is not fixed, and an unreviewed fix ends as `UNCONFIRMED`.

### Human-in-the-Loop Web Console

The same workflow can be driven from a local React console. It needs two terminals.

Terminal 1, the local workflow API (listens only on `127.0.0.1:8765`):

```powershell
cd C:\Users\SINGER\OneDrive\Desktop\ai-agent-learning
.\venv\Scripts\python.exe workflow_ui_api.py
```

Terminal 2, the console (Vite proxies `/api` to the workflow API):

```powershell
cd C:\Users\SINGER\OneDrive\Desktop\ai-agent-learning\level3-serverless-app\frontend
npm run dev
```

Open `http://localhost:5173/`. The Level 3 WebSocket demo page is still available at `/websocket-demo`.

The console lets a human:

* Enter a repository path and a bug report, optionally attaching evidence files
* Watch the current status, stage and blocker, and read the final report
* Review an AI reproduction test: Confirm, Revise with feedback, or switch to manual steps
* Enter manual reproduction steps when automation cannot confirm the bug, or upload a Playwright recording that the workflow replays before and after the fix
* Approve or reject the fix when the bug was never confirmed by a test
* Use **Retry**, **Continue** and **Stop Safely**. These change the real workflow state.

Repository paths must be inside this project folder, or inside a folder listed in `WORKFLOW_ALLOWED_REPOSITORY_ROOTS` (separated by `;`). The console never runs commands typed by the user.

Final statuses:

| Status | Meaning |
|---|---|
| `PASS` | Fix verified. If the bug was confirmed only by manual steps, a human also approved the fix. |
| `FAIL` | Validation failed, retries ran out, or a human rejected the fix |
| `UNCONFIRMED` | Every check passed, but nobody confirmed the bug with a test or reviewed the fix |
| `NEEDS_REVIEW` | Every check passed, but the fix looks wider than the bug needs and nobody approved it |
| `STOPPED` | A human chose Stop Safely. No new stage starts and all artifacts are kept. |

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

Human-in-the-loop runs also save, under `workflow_runs/<run-id>/`:

* `reproduction_test.json` / `confirmed_reproduction_test.json`: the AI reproduction test and the human's decision
* `manual_reproduction.json`: manual steps entered by the human
* `browser_recording.json` / `confirmed_browser_recording.json`: the validated step list of an uploaded Playwright recording, and the confirmed one
* `browser_replay_before_fix.json`, `browser_replay_after_fix_attempt_<n>.json` and `*_failure.png`: each replay result, with a screenshot of the step that failed
* `evidence/` and `evidence_index.json`: uploaded evidence after redaction
* `fix_baseline.json` and `fix_review.json`: the original files, the fix diff and the human fix decision
* `.control/state.json`: the live state shown in the web console

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

Enforced checks:

* AI file edits are rejected if they leave a file empty, break YAML/JSON, remove top-level config sections, fail `node --check`, or import packages that are not declared
* A failed or incomplete validation can never become `PASS`
* The confirmed reproduction test is restored if the fix tries to change it
* Uploaded evidence is size-limited, text-only, never executed and redacted
* Uploaded Playwright recordings are never executed: only an allowlisted step list is replayed, against the local app only, with requests to other hosts blocked
* The web API validates repository paths, request sizes, workflow IDs, state transitions and retry limits

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
* Human-in-the-loop web console (status, stage, blocker, report, Retry / Continue / Stop Safely): Implemented
* AI reproduction test with human confirmation (fail before fix, pass after fix): Implemented
* Evidence upload (HAR, logs, recorded scripts) with secret redaction: Implemented
* Human fix review for bugs not confirmed by a test (`UNCONFIRMED` status): Implemented
* AI limit messages: when an agent fails on the Groq free-tier limit (daily tokens, per-minute tokens or HTTP 413 request too large) or cannot reach Groq at all (connection error), the console and report say what happened and how long to wait, without showing the provider's raw error: Implemented
* Fix scope check: a fix for a confirmed bug that changes files the diagnosis did not name, more than 3 separate places or more than 60 lines also needs human approval (`NEEDS_REVIEW` when not approved): Implemented
* Playwright recording replay for full-stack apps (temporary MongoDB, backend and Vite frontend) and record mode: Implemented. Full console run on the Level 2 MERN app ended in `PASS` on the first attempt, with the user's own database untouched.
* Playwright codegen recording as reproduction (safe step list, replay before and after the fix): Implemented and verified. Full console run on the Level 1 Todo app with a recording made in `playwright codegen` ended in `PASS` on the first attempt: the recording failed before the fix (3 tasks instead of 2) and passed after it.
* Full real-AI (Groq) runs of the new flow: Verified. Level 1 and Level 2 console runs with Groq diagnosis, fix and test generation ended in `PASS` on the first attempt, and a later Level 1 regression run passed again after the reliability fixes.
* Playwright recording replay for Level 3: Not supported (frontend uses the AWS WebSocket API). Planned as future work; Level 3 uses the AI reproduction test.
* AWS deployment: Not done. Waiting for supervisor approval.

### AI / LLM Learning

* LLM fundamentals: Completed
* Prompt engineering: Completed
* Tool/function calling: Completed
* AI Agent concepts: Completed
* LangChain learning: Completed
* LangGraph learning/workflows: Completed
* LangSmith: Environment-based tracing is available to LangChain model calls when configured; main-workflow tracing was not independently verified against LangSmith.
* LangFuse: Standalone examples exist; the main orchestrator has an optional workflow trace, with remote delivery not verified against live credentials.
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
* Human approval before applying code changes for every bug, not only unconfirmed ones
* AI reproduction tests for stacks other than Jest and pytest
* More advanced retry and recovery strategies
* Full external workflow automation using n8n
* Playwright recording replay for Level 3, using a local stand-in for the AWS WebSocket API so no AWS resource is used

## Supervisor Demo and Verified Support

Run the offline regression suite from the repository root:

```powershell
venv\Scripts\python.exe -m pytest tests -q
```

Run the deterministic Level 3 WebSocket/Lambda demo. It uses a disposable copy, mocked AWS SDK clients, and no live AWS or WebSocket connection:

```powershell
venv\Scripts\python.exe workflow_orchestrator.py --local-repository .\level3-serverless-app --local-websocket-demo
```

The demo reproduces the message-retention failure, applies a deterministic fix only in the run directory, reruns backend Jest, and performs frontend build/lint preflight. Run artifacts, the generated report, and the disposable project are saved under `workflow_runs/<run-id>/`.

Groq diagnosis is opt-in and makes a live Groq API request. The demo fails closed if the request or diagnosis validation fails; the existing demo fixer and regression-test generation remain deterministic:

```powershell
venv\Scripts\python.exe workflow_orchestrator.py --local-repository .\level3-serverless-app --local-websocket-demo --ai-diagnosis
```

Optional Langfuse workflow tracing is enabled only when requested. Set `WORKFLOW_TRACE_PROVIDER=langfuse` and configure `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` in the local environment or `.env`. Without the opt-in or usable credentials, the workflow runs without tracing. The trace contains workflow stage names/statuses and retry summaries, not prompts or subprocess logs. Automated coverage uses a mocked client; remote trace delivery has not been verified.

Technology strategy branches and command selection exist for Python, Node.js, .NET, Java, React, and Angular. This does not mean every stack has passed a full reproduce/fix/verify cycle: Level 1/2 are existing completed projects, while the verified disposable end-to-end demonstration is the Level 3 Node.js Lambda/WebSocket backend. Other stacks should be considered strategy-supported, not end-to-end certified. A deployed schema or production message-retention requirement is not verified by the local demo.

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
