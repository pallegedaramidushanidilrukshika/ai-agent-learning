"""Agents must not crash when a Windows cp1252 console receives Jest symbols such as ●."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("module", ["test_generation_agent", "reproduction_test_agent"])
def test_agent_prints_jest_symbols_on_a_cp1252_console(module):
    environment = dict(os.environ, PYTHONIOENCODING="cp1252")
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}; print('Jest \u25cf failed')"],
        cwd=ROOT, env=environment, capture_output=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")[-500:]


def test_orchestrator_prints_jest_symbols_on_a_cp1252_console():
    # Real case from the 29 Oct rehearsal: printing agent output crashed the workflow.
    environment = dict(os.environ, PYTHONIOENCODING="cp1252")
    result = subprocess.run(
        [sys.executable, "-c", "import workflow_orchestrator; print('Jest \u25cf failed')"],
        cwd=ROOT, env=environment, capture_output=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")[-500:]
