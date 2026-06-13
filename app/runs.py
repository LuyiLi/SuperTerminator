"""Helpers for constructing tmux commands for run sessions."""

from __future__ import annotations

from datetime import datetime

from app.security import quote_shell, validate_tmux_session_name


def make_tmux_session_name(run_id: int, now: datetime | None = None) -> str:
    """Build a deterministic, safe tmux session name for a run."""
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return validate_tmux_session_name(f"gpu-panel-{timestamp}-{run_id}")


def build_tmux_start_command(session_name: str, *, workdir: str, rendered_command: str) -> str:
    """Build a command that starts *rendered_command* in a detached tmux session."""
    session_name = validate_tmux_session_name(session_name)
    inner_command = f"cd {quote_shell(workdir)} && {rendered_command}"
    return f"tmux new-session -d -s {session_name} {quote_shell(inner_command)}"


def build_tmux_capture_command(session_name: str, *, lines: int = 300) -> str:
    """Build a command that captures the last *lines* lines from a tmux session."""
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
