"""Application workflow helpers for SSH-backed visual actions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Engine

from app import metrics
from app.db import session_scope
from app.models import Preset, Run, Server, Template
from app.runs import (
    build_tmux_capture_command,
    build_tmux_kill_command,
    build_tmux_start_command,
    make_tmux_session_name,
)
from app.schemas import ServerStatus
from app.templates import merge_template_values, render_template


def _result_error(result: Any) -> str:
    return (
        getattr(result, "stderr", "").strip()
        or getattr(result, "stdout", "").strip()
        or f"exit {getattr(result, 'exit_status', 'unknown')}"
    )


async def test_connection(alias: str, ssh_client) -> tuple[bool, str]:
    """Check whether *alias* can execute a trivial SSH command."""
    try:
        result = await ssh_client.run(alias, "echo ok", timeout=10)
    except Exception as exc:  # pragma: no cover - defensive around real SSH implementations
        return False, str(exc)
    if result.exit_status == 0 and result.stdout.strip() == "ok":
        return True, "ok"
    return False, _result_error(result)


test_connection.__test__ = False


async def collect_server_status(alias: str, ssh_client) -> ServerStatus:
    """Collect parsed hostname and resource metrics for an SSH host alias."""
    ok, error = await test_connection(alias, ssh_client)
    if not ok:
        return ServerStatus(alias=alias, online=False, error=error)

    hostname = await ssh_client.run(alias, "hostname", timeout=10)
    gpu = await ssh_client.run(alias, metrics.GPU_QUERY_COMMAND, timeout=10)
    cpu = await ssh_client.run(alias, metrics.CPU_COMMAND, timeout=10)
    memory = await ssh_client.run(alias, metrics.MEMORY_COMMAND, timeout=10)
    disks = await ssh_client.run(alias, metrics.DISK_COMMAND, timeout=10)

    return ServerStatus(
        alias=alias,
        online=True,
        hostname=hostname.stdout.strip() if hostname.exit_status == 0 else "",
        gpu=metrics.parse_gpu_csv(gpu.stdout) if gpu.exit_status == 0 else [],
        cpu_percent=metrics.parse_cpu_percent(cpu.stdout) if cpu.exit_status == 0 else None,
        memory=metrics.parse_memory_line(memory.stdout) if memory.exit_status == 0 else None,
        disks=metrics.parse_disk_lines(disks.stdout) if disks.exit_status == 0 else [],
    )


async def launch_run(
    *,
    engine: Engine,
    ssh_client,
    project_id: int,
    server_id: int,
    template_id: int,
    preset_id: int | None,
    workdir: str,
    run_name: str,
    form_values: dict[str, str],
) -> Run:
    """Create a run, start it in a detached tmux session over SSH, and return it detached."""
    with session_scope(engine) as session:
        server = session.get(Server, server_id)
        if server is None:
            raise ValueError(f"Server not found: {server_id}")
        template = session.get(Template, template_id)
        if template is None:
            raise ValueError(f"Template not found: {template_id}")

        preset_values: dict[str, Any] = {}
        if preset_id is not None:
            preset = session.get(Preset, preset_id)
            if preset is None:
                raise ValueError(f"Preset not found: {preset_id}")
            preset_values = dict(preset.values_json or {})

        values = merge_template_values(template.variables_schema, preset_values, form_values)
        rendered_command = render_template(template.command_template, template.variables_schema, values)
        run = Run(
            project_id=project_id,
            server_id=server_id,
            template_id=template_id,
            preset_id=preset_id,
            workdir=workdir,
            name=run_name,
            rendered_command=rendered_command,
            status="created",
            started_at=datetime.now(),
        )
        session.add(run)
        session.flush()
        run.tmux_session = make_tmux_session_name(run.id, now=datetime.now())
        session.flush()

        alias = server.alias
        command = build_tmux_start_command(
            run.tmux_session,
            workdir=workdir,
            rendered_command=rendered_command,
        )
        run_id = run.id

    result = await ssh_client.run(alias, command, timeout=15)

    with session_scope(engine) as session:
        stored = session.get(Run, run_id)
        if stored is None:
            raise ValueError(f"Run disappeared: {run_id}")
        stored.status = "running" if result.exit_status == 0 else "unknown"
        session.flush()
        session.expunge(stored)
        return stored


async def capture_run_output(alias: str, session_name: str, ssh_client, lines: int = 300) -> str:
    """Capture recent output from a tmux run session."""
    result = await ssh_client.run(
        alias,
        build_tmux_capture_command(session_name, lines=lines),
        timeout=10,
    )
    if result.exit_status != 0:
        return result.stderr.strip() or "tmux session not found"
    return result.stdout


async def stop_run(alias: str, session_name: str, ssh_client) -> tuple[bool, str]:
    """Stop a tmux run session."""
    result = await ssh_client.run(alias, build_tmux_kill_command(session_name), timeout=10)
    if result.exit_status == 0:
        return True, "stopped"
    return False, _result_error(result)
