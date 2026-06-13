"""Shell quoting and tmux session-name validation helpers."""

from __future__ import annotations

import re
import shlex

TMUX_SESSION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def quote_shell(value: str) -> str:
    """Return *value* quoted for safe use as one POSIX shell token."""
    return shlex.quote(value)


def validate_tmux_session_name(session_name: str) -> str:
    """Validate and return a tmux session name containing only safe characters."""
    if not TMUX_SESSION_NAME_PATTERN.fullmatch(session_name):
        raise ValueError(f"Invalid tmux session name: {session_name}")
    return session_name
