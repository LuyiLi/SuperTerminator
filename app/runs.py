"""Helpers for constructing tmux commands for run sessions."""

from __future__ import annotations

from datetime import datetime

from app.run_runtime import build_managed_run_script, run_id_from_session_name
from app.security import quote_shell, validate_tmux_session_name


def make_tmux_session_name(run_id: int, now: datetime | None = None) -> str:
    """Build a deterministic, safe tmux session name for a run."""
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return validate_tmux_session_name(f"gpu-panel-{timestamp}-{run_id}")


def build_tmux_start_command(
    session_name: str,
    *,
    workdir: str,
    rendered_command: str,
    keepalive_on_error_seconds: int = 86400,
    run_id: int | None = None,
) -> str:
    """Build a detached tmux start command.

    The user's command is run through ``bash -lc`` rather than the remote account's
    default shell. This keeps common training setup snippets such as ``source`` and
    ``conda activate`` working even when the default shell is ``/bin/sh``.

    If the command exits with a non-zero status, the tmux pane is kept alive for
    ``keepalive_on_error_seconds`` so the UI can still capture the real failure output
    instead of immediately showing only ``can't find pane``.

    ``rendered_command`` is intentionally preserved raw inside the bash script because
    this app executes user-provided training shell commands by design.
    """
    session_name = validate_tmux_session_name(session_name)
    resolved_run_id = run_id if run_id is not None else run_id_from_session_name(session_name)
    if resolved_run_id is None:
        raise ValueError("run_id is required when it cannot be inferred from session_name")
    script = build_managed_run_script(
        resolved_run_id,
        workdir=workdir,
        rendered_command=rendered_command,
        keepalive_on_error_seconds=keepalive_on_error_seconds,
    )
    bash_command = f"bash -lc {quote_shell(script)}"
    return f"tmux new-session -d -s {session_name} {quote_shell(bash_command)}"


def build_tmux_capture_command(session_name: str, *, lines: int = 300) -> str:
    """Build a command that captures the last *lines* lines from a tmux session."""
    if type(lines) is not int:
        raise TypeError("lines must be an integer")
    if lines <= 0:
        raise ValueError("lines must be positive")
    session_name = validate_tmux_session_name(session_name)
    return f"tmux capture-pane -t {session_name} -p -S -{lines}"


def build_tmux_kill_command(session_name: str) -> str:
    """Build a command that kills a tmux session."""
    session_name = validate_tmux_session_name(session_name)
    return f"tmux kill-session -t {session_name}"


def build_tmux_has_session_command(session_name: str) -> str:
    """Build a command that checks whether a tmux session exists."""
    session_name = validate_tmux_session_name(session_name)
    return f"tmux has-session -t {session_name}"
