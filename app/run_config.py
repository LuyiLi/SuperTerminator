"""Extract stable training identity and useful fields from rendered shell commands.

The panel stores the exact rendered launch command for every run.  For the
training commands used by this project, ``--run_name`` is the canonical human
name while ``Run.name`` historically contained UI-only labels such as
``copy of ...``.  This module keeps command parsing independent from NiceGUI,
the training repository, and Isaac Sim imports.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from app.command_parser import parse_command


_RUN_NAME_KEYS = {"--run_name", "--run-name"}
_OPTION_KEYS = {
    "task": {"--task"},
    "registry_name": {"--registry_name", "--registry-name"},
    "num_envs": {"--num_envs", "--num-envs"},
    "max_iterations": {"--max_iterations", "--max-iterations"},
    "nproc_per_node": {"--nproc_per_node", "--nproc-per-node"},
    "nnodes": {"--nnodes"},
    "wandb_project": {"--wandb_project", "--wandb-project", "--log_project_name"},
    "experiment_name": {"--experiment_name", "--experiment-name"},
}

# Keep replacements deliberately narrow.  The value alternatives cover the
# forms present in historical commands while preserving every other byte of
# the user's shell command.
_SHELL_VALUE = r'(?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\s\\;&|]+)'
_RUN_NAME_ARGUMENT_RE = re.compile(
    rf"(?P<key>(?<![\w-])--run[-_]name)(?P<sep>\s*=\s*|\s+)(?P<value>{_SHELL_VALUE})"
)


def sanitize_rendered_command(command: str) -> str:
    """Remove database control-byte damage without otherwise rewriting shell text."""

    return str(command or "").replace("\x00", "")


def _coerce_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_training_config(command: str) -> dict[str, Any]:
    """Return a structured, non-executing summary of a rendered command."""

    clean_command = sanitize_rendered_command(command)
    parsed = parse_command(clean_command)

    values: dict[str, str] = {}
    cuda_visible_devices: str | None = None
    scripts: list[str] = []
    distributed = False

    for line in parsed.lines:
        for token in line.tokens:
            if token.endswith(".py"):
                scripts.append(token)

        for param in line.params:
            if param.kind == "env" and param.key == "CUDA_VISIBLE_DEVICES":
                cuda_visible_devices = param.value
            if param.key in _RUN_NAME_KEYS and param.value:
                values["training_run_name"] = param.value
            for field, keys in _OPTION_KEYS.items():
                if param.key in keys and param.value:
                    values[field] = param.value
            if param.key == "--distributed":
                distributed = True

    script = scripts[-1] if scripts else None
    run_name = values.get("training_run_name")
    return {
        "training_run_name": run_name,
        "training_run_name_source": "command:--run_name" if run_name else None,
        "script": script,
        "task": values.get("task"),
        "registry_name": values.get("registry_name"),
        "num_envs": _coerce_int(values.get("num_envs")),
        "max_iterations": _coerce_int(values.get("max_iterations")),
        "nproc_per_node": _coerce_int(values.get("nproc_per_node")),
        "nnodes": _coerce_int(values.get("nnodes")),
        "cuda_visible_devices": cuda_visible_devices,
        "wandb_project": values.get("wandb_project"),
        "experiment_name": values.get("experiment_name"),
        "distributed": distributed,
    }


def extract_training_run_name(command: str) -> str | None:
    """Return the canonical ``--run_name`` value, if one is configured."""

    value = extract_training_config(command).get("training_run_name")
    return str(value) if value else None


def training_display_name(
    *,
    run_id: int | None,
    panel_name: str,
    training_run_name: str | None = None,
    command: str = "",
) -> str:
    """Choose a useful display name without leaking legacy ``copy of`` labels."""

    canonical = (training_run_name or extract_training_run_name(command) or "").strip()
    if canonical:
        return canonical

    clean_panel_name = str(panel_name or "").strip()
    if (
        clean_panel_name
        and clean_panel_name.lower() != "manual command"
        and not clean_panel_name.lower().startswith("copy of ")
    ):
        return clean_panel_name

    config = extract_training_config(command)
    script = config.get("script")
    if script:
        stem = str(script).rsplit("/", 1)[-1].removesuffix(".py")
        return f"{stem} · #{run_id}" if run_id is not None else stem

    if clean_panel_name and not clean_panel_name.lower().startswith("copy of "):
        return clean_panel_name
    return f"Run #{run_id}" if run_id is not None else "Unnamed run"


def set_training_run_name(command: str, run_name: str) -> str:
    """Update or insert ``--run_name`` while preserving the rest of the command.

    Insertion is limited to commands that contain a Python training entrypoint;
    arbitrary commands such as ``echo hi`` are never rewritten into invalid CLI
    invocations merely because the panel has a display name.
    """

    clean_command = sanitize_rendered_command(command)
    clean_name = str(run_name or "").strip()
    if not clean_name:
        return clean_command

    quoted_name = shlex.quote(clean_name)
    match = _RUN_NAME_ARGUMENT_RE.search(clean_command)
    if match is not None:
        return (
            clean_command[: match.start()]
            + f"{match.group('key')}={quoted_name}"
            + clean_command[match.end() :]
        )

    if extract_training_config(clean_command).get("script") is None:
        return clean_command

    lines = clean_command.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if not lines[index].strip():
            continue
        lines[index] = f"{lines[index].rstrip()} --run_name={quoted_name}"
        break
    return "\n".join(lines)
