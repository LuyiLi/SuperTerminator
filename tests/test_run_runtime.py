from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

from app.run_runtime import (
    build_managed_run_script,
    build_runtime_output_command,
    run_id_from_session_name,
    wrap_stop_command,
)


def _wait_for(path: Path, value: str, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip() == value:
            return
        time.sleep(0.02)
    observed = path.read_text().strip() if path.exists() else "<missing>"
    raise AssertionError(f"expected {path}={value!r}, observed {observed!r}")


def test_managed_run_script_persists_success_and_output(tmp_path: Path):
    home = tmp_path / "home"
    workdir = tmp_path / "work"
    home.mkdir()
    workdir.mkdir()
    script = build_managed_run_script(
        42,
        workdir=str(workdir),
        rendered_command="echo training-output",
        keepalive_on_error_seconds=0,
    )

    result = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "HOME": str(home)},
        text=True,
        capture_output=True,
        check=False,
    )

    run_dir = home / ".gpu-ssh-panel" / "runs" / "42"
    assert result.returncode == 0
    assert (run_dir / "state").read_text().strip() == "succeeded"
    assert (run_dir / "exit_code").read_text().strip() == "0"
    assert "training-output" in (run_dir / "output.log").read_text()


def test_failed_state_is_visible_while_debug_supervisor_is_still_alive(tmp_path: Path):
    home = tmp_path / "home"
    workdir = tmp_path / "work"
    home.mkdir()
    workdir.mkdir()
    script = build_managed_run_script(
        43,
        workdir=str(workdir),
        rendered_command="echo boom; exit 7",
        keepalive_on_error_seconds=1,
    )
    process = subprocess.Popen(
        ["bash", "-c", script],
        env={**os.environ, "HOME": str(home)},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    run_dir = home / ".gpu-ssh-panel" / "runs" / "43"
    _wait_for(run_dir / "state", "failed")
    assert process.poll() is None
    assert (run_dir / "exit_code").read_text().strip() == "7"
    assert (run_dir / "reason").read_text().strip() == "exit_code:7"
    assert process.wait(timeout=3) == 7


def test_runtime_command_helpers_and_session_id_parsing():
    assert run_id_from_session_name("gpu-panel-20260613-223000-42") == 42
    assert run_id_from_session_name("nohup-9") == 9
    assert run_id_from_session_name("other") is None

    capture = build_runtime_output_command(42, lines=10, fallback_command="legacy-capture")
    assert "$HOME/.gpu-ssh-panel/runs/42/output.log" in capture
    assert "tail -n 10" in capture
    assert "legacy-capture" in capture

    stop = wrap_stop_command(42, "tmux kill-session -t safe")
    assert "tmux kill-session -t safe" in stop
    assert '"stopped"' in stop
    assert '"user_requested"' in stop
