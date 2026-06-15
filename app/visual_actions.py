"""Application workflow helpers for SSH-backed visual actions."""

from __future__ import annotations

from datetime import datetime
from types import MappingProxyType
from typing import Any

from sqlalchemy import Engine

from app import metrics
from app.db import session_scope
from app.models import Preset, ProjectServer, ProjectWorkdir, Run, Server, Template
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


def _immutable_mapping(value: dict[str, Any]) -> MappingProxyType:
    return MappingProxyType(dict(value))


def _immutable_mapping_tuple(values: list[dict[str, Any]]) -> tuple[MappingProxyType, ...]:
    return tuple(_immutable_mapping(value) for value in values)


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
        gpu=_immutable_mapping_tuple(metrics.parse_gpu_csv(gpu.stdout))
        if gpu.exit_status == 0
        else (),
        cpu_percent=metrics.parse_cpu_percent(cpu.stdout) if cpu.exit_status == 0 else None,
        memory=_immutable_mapping(metrics.parse_memory_line(memory.stdout))
        if memory.exit_status == 0
        else None,
        disks=_immutable_mapping_tuple(metrics.parse_disk_lines(disks.stdout))
        if disks.exit_status == 0
        else (),
    )


def _mark_run_unknown(engine: Engine, run_id: int) -> None:
    with session_scope(engine) as session:
        stored = session.get(Run, run_id)
        if stored is not None:
            stored.status = "unknown"


def mark_run_exited(engine: Engine, run_id: int) -> bool:
    """Persist that a run's tmux session has ended or disappeared."""

    with session_scope(engine) as session:
        stored = session.get(Run, run_id)
        if stored is None:
            return False
        stored.status = "exited"
        stored.ended_at = datetime.now()
        return True


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
        if template.project_id != project_id:
            raise ValueError(f"Template {template_id} does not belong to project {project_id}")

        project_server = (
            session.query(ProjectServer)
            .filter_by(project_id=project_id, server_id=server_id, enabled=True)
            .one_or_none()
        )
        if project_server is None:
            raise ValueError(f"Server {server_id} is not linked to project {project_id}")
        project_workdir = (
            session.query(ProjectWorkdir)
            .filter_by(project_server_id=project_server.id, path=workdir)
            .one_or_none()
        )
        if project_workdir is None:
            raise ValueError(f"Workdir {workdir} is not configured for server {server_id}")

        preset_values: dict[str, Any] = {}
        if preset_id is not None:
            preset = session.get(Preset, preset_id)
            if preset is None:
                raise ValueError(f"Preset not found: {preset_id}")
            if preset.project_id != project_id:
                raise ValueError(f"Preset {preset_id} does not belong to project {project_id}")
            if preset.template_id != template_id:
                raise ValueError(f"Preset {preset_id} does not belong to template {template_id}")
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

    try:
        result = await ssh_client.run(alias, command, timeout=15)
    except Exception:
        _mark_run_unknown(engine, run_id)
        raise

    with session_scope(engine) as session:
        stored = session.get(Run, run_id)
        if stored is None:
            raise ValueError(f"Run disappeared: {run_id}")
        stored.status = "running" if result.exit_status == 0 else "unknown"
        session.flush()
        session.expunge(stored)
        return stored


def _get_or_create_direct_command_template(session, project_id: int) -> Template:
    template = (
        session.query(Template)
        .filter_by(project_id=project_id, name="Direct command")
        .one_or_none()
    )
    if template is None:
        template = Template(
            project_id=project_id,
            name="Direct command",
            command_template="<direct command>",
            variables_schema=[],
        )
        session.add(template)
        session.flush()
    return template


async def launch_direct_command(
    *,
    engine: Engine,
    ssh_client,
    project_id: int,
    server_id: int,
    workdir: str,
    run_name: str,
    command: str,
) -> Run:
    clean_command = command.strip()
    if not clean_command:
        raise ValueError("Command is required")

    with session_scope(engine) as session:
        server = session.get(Server, server_id)
        if server is None:
            raise ValueError(f"Server not found: {server_id}")

        project_server = (
            session.query(ProjectServer)
            .filter_by(project_id=project_id, server_id=server_id, enabled=True)
            .one_or_none()
        )
        if project_server is None:
            raise ValueError(f"Server {server_id} is not linked to project {project_id}")
        project_workdir = (
            session.query(ProjectWorkdir)
            .filter_by(project_server_id=project_server.id, path=workdir)
            .one_or_none()
        )
        if project_workdir is None:
            raise ValueError(f"Workdir {workdir} is not configured for server {server_id}")

        template = _get_or_create_direct_command_template(session, project_id)
        run = Run(
            project_id=project_id,
            server_id=server_id,
            template_id=template.id,
            preset_id=None,
            workdir=workdir,
            name=run_name.strip() or "manual command",
            rendered_command=clean_command,
            status="created",
            started_at=datetime.now(),
        )
        session.add(run)
        session.flush()
        run.tmux_session = make_tmux_session_name(run.id, now=datetime.now())
        session.flush()

        alias = server.alias
        tmux_command = build_tmux_start_command(
            run.tmux_session,
            workdir=workdir,
            rendered_command=clean_command,
        )
        run_id = run.id

    try:
        result = await ssh_client.run(alias, tmux_command, timeout=15)
    except Exception:
        _mark_run_unknown(engine, run_id)
        raise

    with session_scope(engine) as session:
        stored = session.get(Run, run_id)
        if stored is None:
            raise ValueError(f"Run disappeared: {run_id}")
        stored.status = "running" if result.exit_status == 0 else "unknown"
        session.flush()
        session.expunge(stored)
        return stored


async def capture_run_output(
    alias: str,
    session_name: str,
    ssh_client,
    lines: int = 300,
    *,
    engine: Engine | None = None,
    run_id: int | None = None,
) -> str:
    """Capture recent output from a tmux run session."""
    result = await ssh_client.run(
        alias,
        build_tmux_capture_command(session_name, lines=lines),
        timeout=10,
    )
    if result.exit_status != 0:
        if engine is not None and run_id is not None:
            mark_run_exited(engine, run_id)
        return result.stderr.strip() or "tmux session not found"
    return result.stdout


async def stop_run(alias: str, session_name: str, ssh_client) -> tuple[bool, str]:
    """Stop a tmux run session."""
    result = await ssh_client.run(alias, build_tmux_kill_command(session_name), timeout=10)
    if result.exit_status == 0:
        return True, "stopped"
    return False, _result_error(result)
