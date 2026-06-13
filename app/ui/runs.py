from __future__ import annotations

from typing import Any

from nicegui import ui
from sqlalchemy import Engine, desc
from sqlalchemy.orm import joinedload

from app.config import load_settings
from app.db import engine, session_scope
from app.models import Run
from app.ssh_client import SSHClient
from app.visual_actions import capture_run_output, stop_run


settings = load_settings()


def _notify(message: str, *, type: str = "info") -> None:
    ui.notify(message, type=type)


def _run_summary(run: Run) -> dict[str, Any]:
    return {
        "id": run.id,
        "name": run.name,
        "project_name": run.project.name,
        "server_alias": run.server.alias,
        "status": run.status,
        "tmux_session": run.tmux_session,
    }


def _run_detail(run: Run) -> dict[str, Any]:
    summary = _run_summary(run)
    summary.update(
        {
            "workdir": run.workdir,
            "rendered_command": run.rendered_command,
        }
    )
    return summary


def list_recent_runs(*, limit: int = 100, target_engine: Engine = engine) -> list[dict[str, Any]]:
    """Load recent runs with project and server labels for the runs overview."""

    with session_scope(target_engine) as session:
        runs = (
            session.query(Run)
            .options(joinedload(Run.project), joinedload(Run.server))
            .order_by(desc(Run.id))
            .limit(limit)
            .all()
        )
        return [_run_summary(run) for run in runs]


def get_run_detail(run_id: int, *, target_engine: Engine = engine) -> dict[str, Any] | None:
    """Load one run with project and server metadata for the detail page."""

    with session_scope(target_engine) as session:
        run = (
            session.query(Run)
            .options(joinedload(Run.project), joinedload(Run.server))
            .filter(Run.id == run_id)
            .one_or_none()
        )
        return _run_detail(run) if run is not None else None


def mark_run_exited(run_id: int, *, target_engine: Engine = engine) -> bool:
    """Persist that a run has been stopped/exited."""

    with session_scope(target_engine) as session:
        run = session.get(Run, run_id)
        if run is None:
            return False
        run.status = "exited"
        return True


def render_runs_page() -> None:
    """Render the latest runs overview."""

    ui.label("Runs").classes("text-2xl font-bold")
    ui.label("Latest tmux-backed command runs.").classes("text-grey-7")

    runs = list_recent_runs()
    if not runs:
        with ui.card().classes("w-full"):
            ui.label("No runs yet.").classes("text-grey-7")
        return

    for run in runs:
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center justify-between w-full"):
                with ui.column().classes("gap-1"):
                    ui.label(f"#{run['id']} {run['name']}").classes("text-lg font-semibold")
                    ui.label(f"Project: {run['project_name']}").classes("text-grey-7")
                    ui.label(f"Server: {run['server_alias']}").classes("text-grey-7")
                    ui.label(f"Status: {run['status']}").classes("text-grey-7")
                    ui.label(f"tmux: {run['tmux_session']}").classes("font-mono text-grey-8")
                ui.link("Open", f"/runs/{run['id']}")


def render_run_detail(run_id: int) -> None:
    """Render a run detail page with command and live tmux output controls."""

    run = get_run_detail(run_id)
    if run is None:
        ui.label(f"Run not found: {run_id}").classes("text-negative")
        return

    ui.label(f"#{run['id']} {run['name']}").classes("text-2xl font-bold")
    with ui.card().classes("w-full"):
        ui.label(f"Project: {run['project_name']}")
        ui.label(f"Server: {run['server_alias']}")
        ui.label(f"Workdir: {run['workdir']}").classes("font-mono")
        ui.label(f"tmux session: {run['tmux_session']}").classes("font-mono")
        ui.label(f"Status: {run['status']}")

    command = ui.textarea("Rendered command", value=run["rendered_command"]).classes("w-full")
    command.props("readonly autogrow")

    output = ui.textarea("tmux output", value="").classes("w-full")
    output.props("readonly autogrow")

    async def refresh_output() -> None:
        try:
            output.value = await capture_run_output(
                run["server_alias"],
                run["tmux_session"],
                SSHClient(),
                lines=300,
            )
        except Exception as exc:  # pragma: no cover - defensive around real SSH callbacks
            output.value = str(exc)
            _notify(str(exc), type="negative")

    async def do_stop() -> None:
        try:
            ok, message = await stop_run(run["server_alias"], run["tmux_session"], SSHClient())
        except Exception as exc:  # pragma: no cover - defensive around real SSH callbacks
            _notify(str(exc), type="negative")
            return
        if ok:
            mark_run_exited(run_id)
            _notify("Run stopped.", type="positive")
        else:
            _notify(message, type="negative")

    with ui.row().classes("gap-3"):
        ui.button("Refresh output", on_click=refresh_output)
        ui.button("Stop tmux session", on_click=do_stop).props("color=negative")

    ui.timer(settings.run_output_seconds, refresh_output, immediate=False)
    ui.timer(0.1, refresh_output, once=True)

    if settings.show_debug_terminal:
        ui.label("Debug terminal placeholder: interactive terminal will appear here.").classes(
            "text-grey-7"
        )
