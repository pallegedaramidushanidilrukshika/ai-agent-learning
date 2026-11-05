import os
import subprocess
import sys
import time

from workflow_validation import TIMEOUT_RETURN_CODE, run_bounded


def process_alive(pid):
    if os.name == "nt":
        output = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False
        ).stdout
        return str(pid) in output
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_normal_command_returns_like_subprocess_run():
    result = run_bounded(
        [sys.executable, "-c", "import sys; print('ok'); print('warn', file=sys.stderr); sys.exit(3)"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 3
    assert result.stdout.strip() == "ok"
    assert result.stderr.strip() == "warn"
    assert result.timed_out is False


def test_hanging_command_and_its_child_are_stopped(tmp_path):
    # Like an AI-written test that starts the app server and never stops it.
    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open(r'{pid_file}', 'w').write(str(child.pid))\n"
        "print('# tests 1', flush=True)\n"
        "time.sleep(120)\n"
    )
    started = time.monotonic()
    result = run_bounded([sys.executable, "-c", script], timeout=3, capture_output=True, text=True, encoding="utf-8")

    assert time.monotonic() - started < 30
    assert result.returncode == TIMEOUT_RETURN_CODE
    assert result.timed_out is True
    assert "did not finish within 3 seconds" in result.stderr
    child_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while process_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not process_alive(child_pid)
