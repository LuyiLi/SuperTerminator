"""Remote runtime protocol shared by tmux, nohup, UI, and MCP status reads."""

from __future__ import annotations

import re

from app.security import quote_shell


RUNTIME_ROOT = "$HOME/.gpu-ssh-panel/runs"
TERMINAL_STATES = frozenset({"succeeded", "failed", "stopped"})

_TMUX_RUN_ID_RE = re.compile(r"^gpu-panel-\d{8}-\d{6}-(\d+)$")
_NOHUP_RUN_ID_RE = re.compile(r"^nohup-(\d+)$")


def validate_run_id(run_id: int) -> int:
    if type(run_id) is not int:
        raise TypeError("run_id must be an integer")
    if run_id <= 0:
        raise ValueError("run_id must be positive")
    return run_id


def run_id_from_session_name(session_name: str) -> int | None:
    """Extract a database id from panel-owned tmux/nohup identifiers."""

    value = str(session_name or "")
    match = _TMUX_RUN_ID_RE.fullmatch(value) or _NOHUP_RUN_ID_RE.fullmatch(value)
    return int(match.group(1)) if match else None


def runtime_dir(run_id: int) -> str:
    return f"{RUNTIME_ROOT}/{validate_run_id(run_id)}"


def build_managed_run_script(
    run_id: int,
    *,
    workdir: str,
    rendered_command: str,
    keepalive_on_error_seconds: int = 86400,
) -> str:
    """Wrap a command with persistent output and an atomic state/exit protocol."""

    validate_run_id(run_id)
    if type(keepalive_on_error_seconds) is not int:
        raise TypeError("keepalive_on_error_seconds must be an integer")
    if keepalive_on_error_seconds < 0:
        raise ValueError("keepalive_on_error_seconds must be non-negative")

    run_dir = runtime_dir(run_id)
    script = (
        f'__st_dir="{run_dir}"\n'
        'mkdir -p "$__st_dir"\n'
        "__st_write() {\n"
        '  __st_name="$1"\n'
        '  __st_value="$2"\n'
        '  printf \'%s\\n\' "$__st_value" > "$__st_dir/$__st_name.tmp"\n'
        '  mv -f "$__st_dir/$__st_name.tmp" "$__st_dir/$__st_name"\n'
        "}\n"
        "__st_unexpected_exit() {\n"
        "  __st_rc=$?\n"
        '  __st_state="$(cat "$__st_dir/state" 2>/dev/null || true)"\n'
        '  case "$__st_state" in succeeded|failed|stopped) return ;; esac\n'
        '  __st_write exit_code "$__st_rc"\n'
        '  __st_write reason "wrapper_exit:$__st_rc"\n'
        '  __st_write ended_at "$(date -Iseconds)"\n'
        '  __st_write state "failed"\n'
        "}\n"
        "trap '__st_unexpected_exit' EXIT\n"
        ': > "$__st_dir/output.log"\n'
        'exec > >(tee -a "$__st_dir/output.log") 2>&1\n'
        'printf \'%s\\n\' "$$" > "$__st_dir/pid"\n'
        'rm -f "$__st_dir/exit_code" "$__st_dir/ended_at"\n'
        '__st_write reason ""\n'
        '__st_write started_at "$(date -Iseconds)"\n'
        '__st_write state "running"\n'
        "(\n"
        f"  cd {quote_shell(workdir)} || exit $?\n"
        "  {\n"
        f"{rendered_command}\n"
        "  }\n"
        ")\n"
        "__st_rc=$?\n"
        '__st_write exit_code "$__st_rc"\n'
        '__st_write ended_at "$(date -Iseconds)"\n'
        'if [ "$__st_rc" -eq 0 ]; then\n'
        '  __st_write reason ""\n'
        '  __st_write state "succeeded"\n'
        "else\n"
        '  __st_write reason "exit_code:$__st_rc"\n'
        '  __st_write state "failed"\n'
        "  echo\n"
        '  echo "[SuperTerminator] command exited with code $__st_rc at $(date)"\n'
    )
    if keepalive_on_error_seconds > 0:
        script += (
            '  echo "[SuperTerminator] keeping supervisor alive for '
            f'{keepalive_on_error_seconds} seconds for debugging"\n'
            f"  sleep {keepalive_on_error_seconds}\n"
        )
    script += "fi\ntrap - EXIT\nexit $__st_rc"
    return script


def build_runtime_output_command(
    run_id: int,
    *,
    lines: int,
    fallback_command: str,
) -> str:
    """Prefer the persistent log and fall back to a legacy capture command."""

    validate_run_id(run_id)
    if type(lines) is not int:
        raise TypeError("lines must be an integer")
    if lines <= 0:
        raise ValueError("lines must be positive")
    output_path = f"{runtime_dir(run_id)}/output.log"
    return (
        f'if [ -f "{output_path}" ]; then '
        f'tail -n {lines} "{output_path}"; '
        f"else {fallback_command}; fi"
    )


def wrap_stop_command(run_id: int, stop_command: str) -> str:
    """Mark a successfully stopped supervisor with an explicit terminal state."""

    run_dir = runtime_dir(run_id)
    return (
        f"{stop_command}; __st_stop_rc=$?; "
        'if [ "$__st_stop_rc" -eq 0 ]; then '
        f'mkdir -p "{run_dir}"; '
        f'printf \'%s\\n\' "stopped" > "{run_dir}/state.tmp"; '
        f'mv -f "{run_dir}/state.tmp" "{run_dir}/state"; '
        f'printf \'%s\\n\' "user_requested" > "{run_dir}/reason.tmp"; '
        f'mv -f "{run_dir}/reason.tmp" "{run_dir}/reason"; '
        f'printf \'%s\\n\' "$(date -Iseconds)" > "{run_dir}/ended_at.tmp"; '
        f'mv -f "{run_dir}/ended_at.tmp" "{run_dir}/ended_at"; '
        "fi; exit $__st_stop_rc"
    )
