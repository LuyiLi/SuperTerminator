"""Unified run identity, live-state reconciliation, progress, and output service."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
import re
from typing import Any

from sqlalchemy import Engine, desc
from sqlalchemy.orm import joinedload

from app.db import engine, session_scope
from app.models import Run
from app.run_config import extract_training_config, training_display_name
from app.run_runtime import TERMINAL_STATES, runtime_dir, validate_run_id
from app.runs import build_tmux_has_session_command
from app.schemas import CommandResult
from app.ssh_client import SSHClient
from app.visual_actions import capture_run_output


RUN_STATES = frozenset(
    {
        "created",
        "preparing",
        "starting",
        "running",
        "succeeded",
        "failed",
        "stopped",
        "lost",
        "finished_unknown",
        "unknown",
    }
)
ATTENTION_STATES = frozenset({"failed", "lost", "unknown"})
FINISHED_STATES = frozenset({"succeeded", "stopped", "finished_unknown"})
_INSPECTION_BATCH_SIZE = 20

_PROGRESS_PATTERNS: dict[str, re.Pattern[str]] = {
    "iteration": re.compile(r"Learning iteration\s+(\d+)\s*/\s*(\d+)"),
    "run_name": re.compile(r"Run name:\s*(.+?)\s*$", re.MULTILINE),
    "elapsed": re.compile(r"Time elapsed:\s*(.+?)\s*$", re.MULTILINE),
    "eta": re.compile(r"ETA:\s*(.+?)\s*$", re.MULTILINE),
    "mean_reward": re.compile(r"Mean reward:\s*([-+]?\d+(?:\.\d+)?)"),
    "steps_per_second": re.compile(r"Steps per second:\s*(\d+)"),
}


def _last_match(pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
    matches = list(pattern.finditer(text))
    return matches[-1] if matches else None


def parse_training_progress(output: str) -> dict[str, Any]:
    """Parse optional RSL-RL progress fields from recent console output."""

    text = str(output or "")
    progress: dict[str, Any] = {}

    iteration = _last_match(_PROGRESS_PATTERNS["iteration"], text)
    if iteration:
        current = int(iteration.group(1))
        total = int(iteration.group(2))
        progress.update(
            {
                "iteration": current,
                "max_iterations": total,
                "percent": round((current / total) * 100, 2) if total else None,
            }
        )

    for key in ("run_name", "elapsed", "eta"):
        match = _last_match(_PROGRESS_PATTERNS[key], text)
        if match:
            progress[key] = match.group(1).strip()

    reward = _last_match(_PROGRESS_PATTERNS["mean_reward"], text)
    if reward:
        progress["mean_reward"] = float(reward.group(1))
    steps = _last_match(_PROGRESS_PATTERNS["steps_per_second"], text)
    if steps:
        progress["steps_per_second"] = int(steps.group(1))
    return progress


def _serialize_run(run: Run) -> dict[str, Any]:
    config = extract_training_config(run.rendered_command)
    training_name = (run.training_run_name or config.get("training_run_name") or "").strip()
    display_name = training_display_name(
        run_id=run.id,
        panel_name=run.name,
        training_run_name=training_name,
        command=run.rendered_command,
    )
    return {
        "id": int(run.id),
        "run_id": int(run.id),
        "name": display_name,
        "training_run_name": training_name or None,
        "panel_name": run.name,
        "project_id": int(run.project_id),
        "project_name": run.project.name,
        "server_id": int(run.server_id),
        "server_alias": run.server.alias,
        "workdir": run.workdir,
        "tmux_session": run.tmux_session,
        "rendered_command": run.rendered_command,
        "db_status": run.status,
        "status": run.status,
        "state": run.status,
        "status_source": run.status_source or "database",
        "status_detail": run.status_detail or "",
        "launch_source": run.launch_source or "ui",
        "request_key": run.request_key,
        "sync_stage": run.sync_stage or "",
        "sync_metadata": dict(run.sync_metadata or {}),
        "source_run_id": run.source_run_id,
        "launch_preflight_status": run.launch_preflight_status or None,
        "launch_preflight_observed_at": (
            run.launch_preflight_observed_at.isoformat()
            if run.launch_preflight_observed_at
            else None
        ),
        "launch_preflight_detail": run.launch_preflight_detail or None,
        "exit_code": run.exit_code,
        "session_alive": None,
        "is_favorite": bool(run.is_favorite),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "last_observed_at": (
            run.last_observed_at.isoformat() if run.last_observed_at else None
        ),
        "observed_at": None,
        "live_verified": False,
        "config": config,
    }


def list_run_records(
    *,
    limit: int = 100,
    project_id: int | None = None,
    project: str | None = None,
    server: str | None = None,
    query: str | None = None,
    favorite_only: bool = False,
    target_engine: Engine = engine,
) -> list[dict[str, Any]]:
    """Load detached database snapshots; no SSH is performed here."""

    bounded_limit = max(1, min(int(limit), 500))
    with session_scope(target_engine) as session:
        database_query = session.query(Run).options(
            joinedload(Run.project), joinedload(Run.server)
        )
        if project_id is not None:
            database_query = database_query.filter(Run.project_id == project_id)
        if favorite_only:
            database_query = database_query.filter(Run.is_favorite.is_(True))
        runs = database_query.order_by(desc(Run.id)).limit(bounded_limit).all()
        records = [_serialize_run(run) for run in runs]

    project_filter = str(project or "").strip().lower()
    server_filter = str(server or "").strip().lower()
    text_filter = str(query or "").strip().lower()
    filtered: list[dict[str, Any]] = []
    for record in records:
        if project_filter and project_filter not in record["project_name"].lower():
            continue
        if server_filter and server_filter not in record["server_alias"].lower():
            continue
        if text_filter:
            haystack = " ".join(
                [
                    str(record["run_id"]),
                    str(record["training_run_name"] or ""),
                    str(record["panel_name"] or ""),
                    str(record["name"] or ""),
                ]
            ).lower()
            if text_filter not in haystack:
                continue
        filtered.append(record)
    return filtered


def get_run_record(run_id: int, *, target_engine: Engine = engine) -> dict[str, Any] | None:
    validate_run_id(run_id)
    with session_scope(target_engine) as session:
        run = (
            session.query(Run)
            .options(joinedload(Run.project), joinedload(Run.server))
            .filter(Run.id == run_id)
            .one_or_none()
        )
        return _serialize_run(run) if run is not None else None


def _read_field_command(path: str, field: str) -> str:
    return (
        f"printf '{field}='; "
        f'if [ -f "{path}" ]; then tr \'\\n\' \' \' < "{path}"; fi; '
        "printf '\\n'"
    )


def _inspection_block(record: dict[str, Any]) -> str:
    run_id = validate_run_id(int(record["run_id"]))
    run_dir = runtime_dir(run_id)
    session_name = str(record["tmux_session"])
    parts = [f"printf '__ST_BEGIN__={run_id}\\n'"]
    for field in ("state", "exit_code", "reason", "started_at", "ended_at", "pid"):
        parts.append(_read_field_command(f"{run_dir}/{field}", field))

    if session_name.startswith("nohup-"):
        legacy_pid = f"$HOME/.gpu-ssh-panel/runs/nohup-{run_id}.pid"
        supervisor = (
            f'__st_pid="$(cat "{run_dir}/pid" 2>/dev/null || '
            f'cat "{legacy_pid}" 2>/dev/null || true)"; '
            'if [ -n "$__st_pid" ] && kill -0 "$__st_pid" 2>/dev/null; '
            "then printf 'supervisor_alive=1\\n'; "
            "else printf 'supervisor_alive=0\\n'; fi"
        )
        parts.append("printf 'supervisor_kind=nohup\\n'")
    else:
        has_session = build_tmux_has_session_command(session_name)
        supervisor = (
            f"if {has_session} >/dev/null 2>&1; "
            "then printf 'supervisor_alive=1\\n'; "
            "else printf 'supervisor_alive=0\\n'; fi"
        )
        parts.append("printf 'supervisor_kind=tmux\\n'")
        parts.append(
            '__st_legacy_exit="$(tmux capture-pane -t '
            f"{session_name} -p -S -300 2>/dev/null | "
            "sed -n 's/.*\\[SuperTerminator\\] command exited with code "
            "\\([0-9][0-9]*\\).*/\\1/p' | tail -n 1)\"; "
            "printf 'legacy_exit_code=%s\\n' \"$__st_legacy_exit\""
        )
    parts.append(supervisor)
    parts.append(f"printf '__ST_END__={run_id}\\n'")
    return "; ".join(parts)


def _parse_inspection_output(output: str) -> dict[int, dict[str, str]]:
    parsed: dict[int, dict[str, str]] = {}
    current_id: int | None = None
    for raw_line in str(output or "").splitlines():
        if raw_line.startswith("__ST_BEGIN__="):
            try:
                current_id = int(raw_line.split("=", 1)[1])
            except ValueError:
                current_id = None
                continue
            parsed[current_id] = {}
            continue
        if raw_line.startswith("__ST_END__="):
            current_id = None
            continue
        if current_id is None or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        parsed[current_id][key] = value.strip()
    return parsed


def _parse_exit_code(value: str | None) -> int | None:
    try:
        return int(value) if value not in {None, ""} else None
    except ValueError:
        return None


def _resolve_observation(
    record: dict[str, Any],
    raw: dict[str, str] | None,
    *,
    observed_at: datetime,
    error: str = "",
) -> dict[str, Any]:
    result = dict(record)
    result["observed_at"] = observed_at.isoformat()
    result["last_observed_at"] = observed_at.isoformat()
    result["live_verified"] = not bool(error)

    if error:
        result.update(
            {
                "state": "unknown",
                "status": "unknown",
                "status_source": "ssh_error",
                "status_detail": error,
                "session_alive": None,
            }
        )
        return result

    raw = raw or {}
    manifest_state = raw.get("state", "").strip()
    alive = raw.get("supervisor_alive") == "1"
    exit_code = _parse_exit_code(raw.get("exit_code"))
    legacy_exit_code = _parse_exit_code(raw.get("legacy_exit_code"))
    reason = raw.get("reason", "").strip()

    if manifest_state in TERMINAL_STATES:
        state = manifest_state
        source = "remote_manifest"
        detail = reason
    elif manifest_state in {"starting", "running"}:
        if alive:
            state = manifest_state
            source = "remote_manifest"
            detail = reason
        else:
            state = "lost"
            source = "runtime_reconcile"
            detail = "remote state is running but its supervisor is missing"
    elif alive:
        if legacy_exit_code is not None:
            state = "failed"
            source = "legacy_output_marker"
            detail = f"legacy command exited with code {legacy_exit_code}"
            exit_code = legacy_exit_code
        else:
            state = "running"
            source = "legacy_session"
            detail = "legacy run has a live supervisor but no managed state manifest"
    else:
        db_status = str(record.get("db_status") or "unknown")
        if db_status in TERMINAL_STATES:
            state = db_status
            source = str(record.get("status_source") or "database")
            detail = str(record.get("status_detail") or "")
        else:
            state = "finished_unknown"
            source = "legacy_missing"
            detail = "legacy supervisor is missing; no exit code was recorded"

    result.update(
        {
            "state": state,
            "status": state,
            "status_source": source,
            "status_detail": detail,
            "session_alive": alive,
            "exit_code": exit_code if exit_code is not None else record.get("exit_code"),
            "ended_at": raw.get("ended_at") or record.get("ended_at"),
            "remote_started_at": raw.get("started_at") or None,
            "supervisor_kind": raw.get("supervisor_kind") or None,
        }
    )
    return result


def _persist_observations(
    observations: Iterable[dict[str, Any]], *, target_engine: Engine
) -> None:
    with session_scope(target_engine) as session:
        for observation in observations:
            run = session.get(Run, int(observation["run_id"]))
            if run is None:
                continue
            state = str(observation["state"])
            run.last_observed_at = datetime.fromisoformat(observation["observed_at"])
            run.status_source = str(observation.get("status_source") or "database")
            run.status_detail = str(observation.get("status_detail") or "")
            if not run.training_run_name and observation.get("training_run_name"):
                run.training_run_name = str(observation["training_run_name"])
            if state == "unknown":
                if run.status not in TERMINAL_STATES:
                    run.status = "unknown"
            else:
                run.status = state
                run.exit_code = observation.get("exit_code")
                if state in TERMINAL_STATES | {"lost", "finished_unknown"}:
                    ended_at = observation.get("ended_at")
                    if ended_at:
                        try:
                            run.ended_at = datetime.fromisoformat(str(ended_at))
                        except ValueError:
                            pass
                    if run.ended_at is None:
                        run.ended_at = run.last_observed_at


async def observe_run_records(
    records: list[dict[str, Any]],
    *,
    ssh_client_factory: Callable[[], Any] = SSHClient,
    persist: bool = True,
    target_engine: Engine = engine,
) -> list[dict[str, Any]]:
    """Batch live checks into one SSH command per server."""

    if not records:
        return []
    sync_records = [record for record in records if record.get("launch_source") == "sync"]
    if sync_records:
        from app.sync_launch import persist_sync_observation, preparation_interrupted
        from app.sync_remote import SyncRemote

        async def inspect_sync(record: dict[str, Any]) -> dict[str, Any]:
            result = dict(record)
            metadata = record.get("sync_metadata", {})
            result["observed_at"] = datetime.now().isoformat()
            if not metadata.get("launch_attempted") or metadata.get("launch_rejected"):
                if preparation_interrupted(record):
                    result.update(state="failed", status="failed", status_source="sync_launch",
                                  status_detail="准备过程中服务退出；没有自动启动训练。请检查后点击“新训练”。")
                    if persist:
                        _persist_observations([result], target_engine=target_engine)
                return result
            try:
                observed = await SyncRemote(ssh_client_factory()).inspect(
                    record["server_alias"], record["run_id"], metadata["profile"]
                )
                result.update(state=observed.get("state", "unknown"),
                              status=observed.get("state", "unknown"),
                              status_detail=observed.get("status_detail", ""),
                              status_source="sync_remote", live_verified=observed.get("live_verified", False),
                              session_alive=observed.get("process_alive", False),
                              exit_code=observed.get("exit_code"))
                result["sync_metadata"] = {**metadata, **{k: observed[k] for k in
                    ("wandb_url", "log_path", "process_alive") if observed.get(k) is not None}}
                if result["state"] == "running":
                    result["sync_stage"] = "training"
                if persist:
                    canonical = persist_sync_observation(record["run_id"], observed, target_engine)
                    result.update(canonical)
                    result["observed_at"] = datetime.now().isoformat()
                    result["live_verified"] = observed.get("live_verified", False)
                    result["session_alive"] = (observed.get("process_alive", False)
                                               if canonical["state"] not in TERMINAL_STATES else False)
            except Exception as exc:
                result.update(state="unknown", status="unknown", live_verified=False,
                              status_detail=f"状态待确认：{exc}；请刷新原任务核实")
                if persist:
                    result.update(persist_sync_observation(record["run_id"], result, target_engine))
                    result["observed_at"] = datetime.now().isoformat()
            return result

        sync_results = await asyncio.gather(*(inspect_sync(record) for record in sync_records))
        legacy_results = await observe_run_records(
            [record for record in records if record.get("launch_source") != "sync"],
            ssh_client_factory=ssh_client_factory, persist=persist, target_engine=target_engine,
        )
        by_id = {record["run_id"]: record for record in sync_results + legacy_results}
        return [by_id[record["run_id"]] for record in records]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record["server_alias"])].append(record)

    async def inspect_host(
        alias: str, host_records: list[dict[str, Any]]
    ) -> tuple[str, list[int], dict[int, dict[str, str]] | None, str]:
        run_ids = [int(record["run_id"]) for record in host_records]
        command = "\n".join(_inspection_block(record) for record in host_records)
        try:
            response: CommandResult = await ssh_client_factory().run(alias, command, timeout=15)
        except Exception as exc:  # pragma: no cover - real network boundary
            return alias, run_ids, None, str(exc)
        if response.exit_status != 0:
            error = response.stderr.strip() or response.stdout.strip() or (
                f"SSH inspection exited {response.exit_status}"
            )
            return alias, run_ids, None, error
        return alias, run_ids, _parse_inspection_output(response.stdout), ""

    batches = [
        (alias, host_records[index : index + _INSPECTION_BATCH_SIZE])
        for alias, host_records in grouped.items()
        for index in range(0, len(host_records), _INSPECTION_BATCH_SIZE)
    ]
    host_results = await asyncio.gather(
        *(inspect_host(alias, host_records) for alias, host_records in batches)
    )
    raw_by_run_id: dict[int, dict[str, str]] = {}
    error_by_run_id: dict[int, str] = {}
    for _alias, run_ids, raw, error in host_results:
        if error:
            error_by_run_id.update({run_id: error for run_id in run_ids})
        elif raw is not None:
            raw_by_run_id.update(raw)
    observed_at = datetime.now()
    observations: list[dict[str, Any]] = []
    for record in records:
        run_id = int(record["run_id"])
        observations.append(
            _resolve_observation(
                record,
                raw_by_run_id.get(run_id),
                observed_at=observed_at,
                error=error_by_run_id.get(run_id, ""),
            )
        )
    if persist:
        _persist_observations(observations, target_engine=target_engine)
    return observations


async def list_training_runs(
    *,
    limit: int = 20,
    query: str | None = None,
    project: str | None = None,
    server: str | None = None,
    states: list[str] | None = None,
    refresh_live: bool = True,
    ssh_client_factory: Callable[[], Any] = SSHClient,
    target_engine: Engine = engine,
) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(int(limit), 100))
    candidate_limit = (
        500
        if query or project or server or states
        else max(bounded_limit * 5, bounded_limit)
    )
    records = list_run_records(
        limit=candidate_limit,
        query=query,
        project=project,
        server=server,
        target_engine=target_engine,
    )
    if refresh_live:
        records = await observe_run_records(
            records,
            ssh_client_factory=ssh_client_factory,
            target_engine=target_engine,
        )
    state_filter = {str(state) for state in states or [] if state}
    if state_filter:
        records = [record for record in records if record["state"] in state_filter]
    return records[:bounded_limit]


async def tail_training_run(
    run_id: int,
    *,
    lines: int = 100,
    capture: Callable[..., Awaitable[str]] = capture_run_output,
    ssh_client_factory: Callable[[], Any] = SSHClient,
    target_engine: Engine = engine,
) -> dict[str, Any]:
    bounded_lines = max(1, min(int(lines), 500))
    record = get_run_record(run_id, target_engine=target_engine)
    if record is None:
        raise ValueError(f"Run not found: {run_id}")
    try:
        output = await capture(
            record["server_alias"],
            record["tmux_session"],
            ssh_client_factory(),
            lines=bounded_lines,
            run_id=run_id,
        )
        error = ""
    except Exception as exc:  # pragma: no cover - real network boundary
        output = ""
        error = str(exc)
    progress = parse_training_progress(output)
    configured_name = record.get("training_run_name")
    observed_name = progress.get("run_name")
    warning = None
    if configured_name and observed_name and configured_name != observed_name:
        warning = (
            f"Configured run name {configured_name!r} does not match "
            f"observed log name {observed_name!r}"
        )
    return {
        "run_id": run_id,
        "training_run_name": configured_name,
        "lines": bounded_lines,
        "output": output,
        "output_error": error or None,
        "progress": progress,
        "warning": warning,
    }


async def get_training_run(
    run_id: int,
    *,
    tail_lines: int = 100,
    refresh_live: bool = True,
    ssh_client_factory: Callable[[], Any] = SSHClient,
    target_engine: Engine = engine,
) -> dict[str, Any]:
    record = get_run_record(run_id, target_engine=target_engine)
    if record is None:
        raise ValueError(f"Run not found: {run_id}")
    if refresh_live:
        record = (
            await observe_run_records(
                [record],
                ssh_client_factory=ssh_client_factory,
                target_engine=target_engine,
            )
        )[0]
    tail = await tail_training_run(
        run_id,
        lines=tail_lines,
        ssh_client_factory=ssh_client_factory,
        target_engine=target_engine,
    )
    record.update(
        {
            "progress": tail["progress"],
            "output_tail": tail["output"],
            "output_error": tail["output_error"],
            "warning": tail["warning"],
        }
    )
    return record
