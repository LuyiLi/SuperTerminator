"""Application workflow helpers for SSH-backed visual actions."""

from __future__ import annotations

from datetime import datetime
from types import MappingProxyType
from typing import Any

from sqlalchemy import Engine

from app import metrics
from app.db import session_scope
from app.models import Preset, ProjectServer, ProjectWorkdir, Run, Server, Template
from app.run_config import extract_training_run_name, training_display_name
from app.run_runtime import (
    build_managed_run_script,
    build_runtime_output_command,
    run_id_from_session_name,
    runtime_dir,
    wrap_stop_command,
)
from app.runs import (
    build_tmux_capture_command,
    build_tmux_kill_command,
    build_tmux_start_command,
    make_tmux_session_name,
)
from app.security import quote_shell
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


def _make_nohup_session_name(run_id: int) -> str:
    return f"nohup-{run_id}"


def _nohup_paths(run_id_tag: str) -> tuple[str, str]:
    base = "$HOME/.gpu-ssh-panel/runs"
    return (f"{base}/{run_id_tag}.log", f"{base}/{run_id_tag}.pid")


def _is_nohup_session(session_name: str) -> bool:
    return run_id_from_session_name(session_name) is not None and session_name.startswith("nohup-")


def _validated_nohup_tag(session_name: str) -> str:
    run_id = run_id_from_session_name(session_name)
    if run_id is None or not session_name.startswith("nohup-"):
        raise ValueError(f"Invalid nohup session name: {session_name}")
    return f"nohup-{run_id}"


def _build_nohup_start_command(
    session_name: str,
    *,
    workdir: str,
    rendered_command: str,
    run_id: int | None = None,
) -> str:
    resolved_run_id = run_id if run_id is not None else run_id_from_session_name(session_name)
    if resolved_run_id is None:
        raise ValueError("run_id is required for a managed nohup run")
    script = build_managed_run_script(
        resolved_run_id,
        workdir=workdir,
        rendered_command=rendered_command,
        keepalive_on_error_seconds=0,
    )
    return (
        "mkdir -p $HOME/.gpu-ssh-panel/runs && "
        f"nohup bash -lc {quote_shell(script)} > /dev/null 2>&1 < /dev/null &"
    )


def _build_nohup_capture_command(session_name: str, lines: int) -> str:
    log_path, _ = _nohup_paths(_validated_nohup_tag(session_name))
    return (
        f"if [ -f {log_path} ]; then "
        f"tail -n {lines} {log_path}; "
        "else echo 'nohup output file not ready'; fi"
    )


def _build_nohup_stop_command(session_name: str) -> str:
    run_id = run_id_from_session_name(session_name)
    if run_id is None or not session_name.startswith("nohup-"):
        raise ValueError(f"Invalid nohup session name: {session_name}")
    _log_path, legacy_pid_path = _nohup_paths(_validated_nohup_tag(session_name))
    managed_pid_path = f"{runtime_dir(run_id)}/pid"
    return (
        f'if [ -f "{managed_pid_path}" ]; then pid=$(cat "{managed_pid_path}"); '
        f"elif [ -f {legacy_pid_path} ]; then pid=$(cat {legacy_pid_path}); "
        "else echo 'nohup pid file not found'; exit 1; fi; "
        'if [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1; then '
        'pkill -TERM -P "$pid" >/dev/null 2>&1 || true; '
        'kill -TERM "$pid" >/dev/null 2>&1 || true; fi; '
        f"rm -f {legacy_pid_path}; exit 0"
    )


async def _has_tmux(ssh_client, host_alias: str) -> bool:
    result = await ssh_client.run(host_alias, "command -v tmux", timeout=10)
    return result.exit_status == 0


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
            stored.status_source = "ssh_error"
            stored.status_detail = "SSH transport failed while starting the supervisor"


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
    launch_source: str = "ui",
    source_run_id: int | None = None,
    launch_preflight_status: str = "",
    launch_preflight_observed_at: datetime | None = None,
    launch_preflight_detail: str = "",
) -> Run:
    """Create a run, start it in a detached tmux session over SSH, and return it detached."""
    with session_scope(engine) as session:
        server = session.get(Server, server_id)
        if server is None:
            raise ValueError(f"Server not found: {server_id}")
        if not server.enabled:
            raise ValueError(f"Server {server_id} is disabled")
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
        training_run_name = extract_training_run_name(rendered_command) or ""
        canonical_name = training_display_name(
            run_id=None,
            panel_name=run_name,
            training_run_name=training_run_name,
            command=rendered_command,
        )
        run = Run(
            project_id=project_id,
            server_id=server_id,
            template_id=template_id,
            preset_id=preset_id,
            workdir=workdir,
            name=canonical_name,
            rendered_command=rendered_command,
            status="created",
            training_run_name=training_run_name,
            status_source="launch",
            launch_source=launch_source,
            source_run_id=source_run_id,
            launch_preflight_status=launch_preflight_status,
            launch_preflight_observed_at=launch_preflight_observed_at,
            launch_preflight_detail=launch_preflight_detail,
            started_at=datetime.now(),
        )
        session.add(run)
        session.flush()
        run.tmux_session = make_tmux_session_name(run.id, now=datetime.now())
        session.flush()

        alias = server.alias
        if await _has_tmux(ssh_client, alias):
            run.tmux_session = make_tmux_session_name(run.id, now=datetime.now())
            command = build_tmux_start_command(
                run.tmux_session,
                workdir=workdir,
                rendered_command=rendered_command,
                run_id=run.id,
            )
        else:
            run.tmux_session = _make_nohup_session_name(run.id)
            command = _build_nohup_start_command(
                run.tmux_session,
                workdir=workdir,
                rendered_command=rendered_command,
                run_id=run.id,
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
        if result.exit_status == 0:
            stored.status = "running"
            stored.status_source = "supervisor_start"
            stored.status_detail = ""
        else:
            stored.status = "failed"
            stored.status_source = "supervisor_start"
            stored.status_detail = _result_error(result)
            stored.exit_code = result.exit_status
            stored.ended_at = datetime.now()
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
    launch_source: str = "ui",
    source_run_id: int | None = None,
    launch_preflight_status: str = "",
    launch_preflight_observed_at: datetime | None = None,
    launch_preflight_detail: str = "",
) -> Run:
    clean_command = command.strip()
    if not clean_command:
        raise ValueError("Command is required")

    with session_scope(engine) as session:
        server = session.get(Server, server_id)
        if server is None:
            raise ValueError(f"Server not found: {server_id}")
        if not server.enabled:
            raise ValueError(f"Server {server_id} is disabled")

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
        training_run_name = extract_training_run_name(clean_command) or ""
        canonical_name = training_display_name(
            run_id=None,
            panel_name=run_name,
            training_run_name=training_run_name,
            command=clean_command,
        )
        run = Run(
            project_id=project_id,
            server_id=server_id,
            template_id=template.id,
            preset_id=None,
            workdir=workdir,
            name=canonical_name,
            rendered_command=clean_command,
            status="created",
            training_run_name=training_run_name,
            status_source="launch",
            launch_source=launch_source,
            source_run_id=source_run_id,
            launch_preflight_status=launch_preflight_status,
            launch_preflight_observed_at=launch_preflight_observed_at,
            launch_preflight_detail=launch_preflight_detail,
            started_at=datetime.now(),
        )
        session.add(run)
        session.flush()
        run.tmux_session = make_tmux_session_name(run.id, now=datetime.now())
        session.flush()

        alias = server.alias
        if await _has_tmux(ssh_client, alias):
            run.tmux_session = make_tmux_session_name(run.id, now=datetime.now())
            command = build_tmux_start_command(
                run.tmux_session,
                workdir=workdir,
                rendered_command=clean_command,
                run_id=run.id,
            )
        else:
            run.tmux_session = _make_nohup_session_name(run.id)
            command = _build_nohup_start_command(
                run.tmux_session,
                workdir=workdir,
                rendered_command=clean_command,
                run_id=run.id,
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
        if result.exit_status == 0:
            stored.status = "running"
            stored.status_source = "supervisor_start"
            stored.status_detail = ""
        else:
            stored.status = "failed"
            stored.status_source = "supervisor_start"
            stored.status_detail = _result_error(result)
            stored.exit_code = result.exit_status
            stored.ended_at = datetime.now()
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
    """Capture persistent managed output, falling back to legacy tmux/nohup output."""
    resolved_run_id = run_id if run_id is not None else run_id_from_session_name(session_name)
    if _is_nohup_session(session_name):
        fallback_command = _build_nohup_capture_command(session_name, lines=lines)
    else:
        fallback_command = build_tmux_capture_command(session_name, lines=lines)
    command = (
        build_runtime_output_command(
            resolved_run_id,
            lines=lines,
            fallback_command=fallback_command,
        )
        if resolved_run_id is not None
        else fallback_command
    )
    result = await ssh_client.run(
        alias,
        command,
        timeout=10,
    )
    if result.exit_status != 0:
        return result.stderr.strip() or "tmux session not found"
    return result.stdout


async def stop_run(alias: str, session_name: str, ssh_client) -> tuple[bool, str]:
    """Stop a tmux run session."""
    command = (
        _build_nohup_stop_command(session_name)
        if _is_nohup_session(session_name)
        else build_tmux_kill_command(session_name)
    )
    run_id = run_id_from_session_name(session_name)
    if run_id is not None:
        command = wrap_stop_command(run_id, command)
    result = await ssh_client.run(alias, command, timeout=10)
    if result.exit_status == 0:
        return True, "stopped"
    return False, _result_error(result)
