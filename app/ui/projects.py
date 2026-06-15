from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from nicegui import ui
from sqlalchemy import Engine, desc
from sqlalchemy.orm import joinedload

from app.db import engine, session_scope
from app.models import Preset, Project, ProjectServer, ProjectWorkdir, Run, Server, Template
from app.schemas import ServerStatus
from app.ssh_client import SSHClient
from app.templates import build_variables_schema, merge_template_values, render_template
from app.visual_actions import collect_server_status, launch_direct_command, launch_run
from app.ui.dashboard import render_pending_server_card, render_server_card


_project_status_cache: dict[str, ServerStatus] = {}


def _notify(message: str, *, type: str = "info") -> None:
    ui.notify(message, type=type)


def _reload() -> None:
    ui.navigate.reload()


def create_project(
    name: str,
    git_url: str = "",
    default_workdir: str = "",
    *,
    target_engine: Engine = engine,
) -> int | None:
    clean_name = name.strip()
    if not clean_name:
        _notify("Project name is required.", type="negative")
        return None

    with session_scope(target_engine) as session:
        project = Project(
            name=clean_name,
            git_url=git_url.strip(),
            default_workdir=default_workdir.strip(),
        )
        session.add(project)
        session.flush()
        project_id = project.id

    _notify(f"Project '{clean_name}' created.", type="positive")
    _reload()
    return project_id


def list_projects(*, target_engine: Engine = engine) -> list[Project]:
    with session_scope(target_engine) as session:
        projects = list(session.query(Project).order_by(Project.name).all())
        for project in projects:
            session.expunge(project)
        return projects


def get_project(project_id: int, *, target_engine: Engine = engine) -> Project | None:
    with session_scope(target_engine) as session:
        project = session.get(Project, project_id)
        if project is not None:
            session.expunge(project)
        return project


def list_unlinked_enabled_servers(
    project_id: int, *, target_engine: Engine = engine
) -> list[tuple[int, str]]:
    with session_scope(target_engine) as session:
        linked_ids = {
            row[0]
            for row in session.query(ProjectServer.server_id).filter_by(project_id=project_id).all()
        }
        servers = (
            session.query(Server)
            .filter(Server.enabled.is_(True))
            .order_by(Server.alias)
            .all()
        )
        return [(server.id, server.alias) for server in servers if server.id not in linked_ids]


def link_server_to_project(
    project_id: int,
    server_id: int,
    default_workdir: str = "",
    *,
    target_engine: Engine = engine,
) -> int:
    with session_scope(target_engine) as session:
        link = (
            session.query(ProjectServer)
            .filter_by(project_id=project_id, server_id=server_id)
            .one_or_none()
        )
        if link is None:
            link = ProjectServer(project_id=project_id, server_id=server_id, enabled=True)
            session.add(link)
            session.flush()
        else:
            link.enabled = True

        clean_workdir = default_workdir.strip()
        if clean_workdir:
            existing = (
                session.query(ProjectWorkdir)
                .filter_by(project_server_id=link.id, path=clean_workdir)
                .one_or_none()
            )
            for workdir in session.query(ProjectWorkdir).filter_by(project_server_id=link.id):
                workdir.is_default = False
            session.flush()
            if existing is None:
                session.add(
                    ProjectWorkdir(
                        project_server_id=link.id,
                        path=clean_workdir,
                        label="default",
                        is_default=True,
                    )
                )
            else:
                existing.label = existing.label or "default"
                existing.is_default = True
        link_id = link.id

    _notify("Server linked to project.", type="positive")
    _reload()
    return link_id


def add_project_workdir(
    project_server_id: int,
    path: str,
    label: str = "",
    *,
    is_default: bool = False,
    target_engine: Engine = engine,
) -> int | None:
    clean_path = path.strip()
    if not clean_path:
        _notify("Workdir path is required.", type="negative")
        return None
    clean_label = label.strip() or "workdir"

    with session_scope(target_engine) as session:
        if is_default:
            for workdir in session.query(ProjectWorkdir).filter_by(project_server_id=project_server_id):
                workdir.is_default = False
            session.flush()
        workdir = ProjectWorkdir(
            project_server_id=project_server_id,
            path=clean_path,
            label=clean_label,
            is_default=is_default,
        )
        session.add(workdir)
        session.flush()
        workdir_id = workdir.id

    _notify("Workdir added.", type="positive")
    _reload()
    return workdir_id


def list_project_servers(project_id: int, *, target_engine: Engine = engine) -> list[dict[str, Any]]:
    with session_scope(target_engine) as session:
        links = (
            session.query(ProjectServer)
            .filter_by(project_id=project_id, enabled=True)
            .join(Server)
            .order_by(Server.alias)
            .all()
        )
        return [
            {
                "id": link.id,
                "server_id": link.server_id,
                "server_alias": link.server.alias,
                "workdirs": [
                    {
                        "id": workdir.id,
                        "path": workdir.path,
                        "label": workdir.label,
                        "is_default": workdir.is_default,
                    }
                    for workdir in sorted(link.workdirs, key=lambda item: (not item.is_default, item.label, item.path))
                ],
            }
            for link in links
        ]


def create_project_template(
    project_id: int,
    name: str,
    command_template: str,
    *,
    target_engine: Engine = engine,
) -> int | None:
    clean_name = name.strip()
    clean_command = command_template.strip()
    if not clean_name:
        _notify("Template name is required.", type="negative")
        return None
    if not clean_command:
        _notify("Command template is required.", type="negative")
        return None

    schema = build_variables_schema(clean_command)
    with session_scope(target_engine) as session:
        template = Template(
            project_id=project_id,
            name=clean_name,
            command_template=clean_command,
            variables_schema=schema,
        )
        session.add(template)
        session.flush()
        template_id = template.id

    _notify("Template created.", type="positive")
    _reload()
    return template_id


def list_project_templates(project_id: int, *, target_engine: Engine = engine) -> list[Template]:
    with session_scope(target_engine) as session:
        templates = list(
            session.query(Template).filter_by(project_id=project_id).order_by(Template.name).all()
        )
        for template in templates:
            session.expunge(template)
        return templates


def _stringify_values(values: dict[str, Any]) -> dict[str, str]:
    return {str(key): "" if value is None else str(value) for key, value in values.items()}


def create_project_preset(
    project_id: int,
    template_id: int,
    name: str,
    values: dict[str, Any],
    *,
    target_engine: Engine = engine,
) -> int | None:
    clean_name = name.strip()
    if not clean_name:
        _notify("Preset name is required.", type="negative")
        return None

    with session_scope(target_engine) as session:
        preset = Preset(
            project_id=project_id,
            template_id=template_id,
            name=clean_name,
            values_json=_stringify_values(values),
        )
        session.add(preset)
        session.flush()
        preset_id = preset.id

    _notify("Preset created.", type="positive")
    _reload()
    return preset_id


def list_project_presets(project_id: int, *, target_engine: Engine = engine) -> list[Preset]:
    with session_scope(target_engine) as session:
        presets = list(session.query(Preset).filter_by(project_id=project_id).order_by(Preset.name).all())
        for preset in presets:
            session.expunge(preset)
        return presets


@dataclass(frozen=True)
class LaunchOptions:
    template_options: dict[int, str]
    preset_options: dict[int | None, str]
    server_options: dict[int, str]
    workdir_options: dict[str, str]
    templates: dict[int, Template]
    presets: dict[int, Preset]


def build_launch_options(project_id: int, *, target_engine: Engine = engine) -> LaunchOptions:
    with session_scope(target_engine) as session:
        templates = list(
            session.query(Template).filter_by(project_id=project_id).order_by(Template.name).all()
        )
        presets = list(session.query(Preset).filter_by(project_id=project_id).order_by(Preset.name).all())
        links = (
            session.query(ProjectServer)
            .filter_by(project_id=project_id, enabled=True)
            .join(Server)
            .order_by(Server.alias)
            .all()
        )

        template_options = {template.id: template.name for template in templates}
        preset_options: dict[int | None, str] = {None: "None"}
        preset_options.update({preset.id: preset.name for preset in presets})
        server_options = {link.server_id: link.server.alias for link in links}
        workdir_options = {
            workdir.path: f"{link.server.alias}: {workdir.label} ({workdir.path})"
            for link in links
            for workdir in sorted(link.workdirs, key=lambda item: (not item.is_default, item.label, item.path))
        }

        for obj in [*templates, *presets]:
            session.expunge(obj)

    return LaunchOptions(
        template_options=template_options,
        preset_options=preset_options,
        server_options=server_options,
        workdir_options=workdir_options,
        templates={template.id: template for template in templates},
        presets={preset.id: preset for preset in presets},
    )


def list_project_recent_runs(
    project_id: int, *, limit: int = 10, target_engine: Engine = engine
) -> list[dict[str, Any]]:
    with session_scope(target_engine) as session:
        runs = (
            session.query(Run)
            .options(joinedload(Run.server))
            .filter(Run.project_id == project_id)
            .order_by(desc(Run.id))
            .limit(limit)
            .all()
        )
        return [
            {
                "id": run.id,
                "name": run.name,
                "server_id": run.server_id,
                "server_alias": run.server.alias,
                "workdir": run.workdir,
                "rendered_command": run.rendered_command,
                "status": run.status,
                "tmux_session": run.tmux_session,
            }
            for run in runs
        ]


async def launch_direct_command_from_project(
    *,
    project_id: int,
    server_id: int,
    workdir: str,
    run_name: str,
    command: str,
    ssh_client: Any | None = None,
    target_engine: Engine = engine,
) -> Run:
    return await launch_direct_command(
        engine=target_engine,
        ssh_client=ssh_client or SSHClient(),
        project_id=project_id,
        server_id=server_id,
        workdir=workdir,
        run_name=run_name,
        command=command,
    )


def render_projects_page() -> None:
    ui.label("Projects").classes("text-2xl font-bold")
    ui.label("Create projects, then open one to configure launch workflows.").classes("text-grey-7")

    name_input = ui.input("Project name").classes("w-full")
    git_input = ui.input("Git URL (optional)").classes("w-full")
    workdir_input = ui.input("Default workdir (optional)").classes("w-full")
    ui.button(
        "Create project",
        on_click=lambda: create_project(
            name_input.value or "", git_input.value or "", workdir_input.value or ""
        ),
    )

    ui.separator()
    projects = list_projects()
    if not projects:
        ui.label("No projects yet.").classes("text-grey-7")
        return

    for project in projects:
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center justify-between w-full"):
                with ui.column().classes("gap-1"):
                    ui.label(project.name).classes("text-lg font-semibold")
                    ui.label(project.git_url or "No git URL").classes("text-grey-7")
                ui.link("Open", f"/projects/{project.id}")


def render_project_detail(project_id: int) -> None:
    project = get_project(project_id)
    if project is None:
        ui.label(f"Project not found: {project_id}").classes("text-negative")
        return

    ui.label(project.name).classes("text-2xl font-bold")
    ui.label(project.git_url or "No git URL").classes("text-grey-7")
    if project.default_workdir:
        ui.label(f"Default workdir: {project.default_workdir}").classes("text-grey-7")

    with ui.grid(columns=3).classes("w-full grid-cols-3 gap-4"):
        with ui.column().classes("w-full gap-3 col-span-2"):
            with ui.expansion("Launch", value=True).classes("w-full"):
                use_command = _render_launch_tab(project_id)
            with ui.expansion("Templates", value=False).classes("w-full"):
                _render_templates_tab(project_id)
            with ui.expansion("Presets", value=False).classes("w-full"):
                _render_presets_tab(project_id)
            with ui.expansion("Servers & Workdirs", value=False).classes("w-full"):
                _render_servers_workdirs_tab(project_id, project.default_workdir)
        with ui.column().classes("w-full gap-4"):
            render_project_servers_status_panel(project_id)
            render_project_recent_runs_panel(project_id, on_use_command=use_command)


def _render_project_server_status_snapshot(container: Any, aliases: list[str]) -> None:
    container.clear()
    with container:
        for alias in aliases:
            cached = _project_status_cache.get(alias)
            if cached is None:
                render_pending_server_card(alias)
            else:
                render_server_card(cached)


def render_project_servers_status_panel(project_id: int) -> None:
    ui.label("Project Servers").classes("text-xl font-semibold")
    linked = list_project_servers(project_id)
    aliases = [str(link["server_alias"]) for link in linked]
    if not aliases:
        ui.label("No linked servers yet.").classes("text-grey-7")
        return

    container = ui.column().classes("w-full gap-3")
    _render_project_server_status_snapshot(container, aliases)

    async def refresh_statuses() -> None:
        _render_project_server_status_snapshot(container, aliases)
        results = await asyncio.gather(
            *(collect_server_status(alias, SSHClient()) for alias in aliases),
            return_exceptions=True,
        )
        for alias, result in zip(aliases, results, strict=True):
            if isinstance(result, BaseException):
                _project_status_cache[alias] = ServerStatus(
                    alias=alias, online=False, error=str(result)
                )
            else:
                _project_status_cache[alias] = result
        _render_project_server_status_snapshot(container, aliases)

    ui.button("Refresh statuses", on_click=refresh_statuses).props("icon=refresh")
    ui.timer(0, refresh_statuses, once=True)


def render_project_recent_runs_panel(
    project_id: int, *, on_use_command: Callable[[dict[str, Any]], None] | None = None
) -> None:
    ui.label("Recent Runs").classes("text-xl font-semibold")
    runs = list_project_recent_runs(project_id)
    if not runs:
        ui.label("No runs for this project yet.").classes("text-grey-7")
        return
    for run in runs:
        with ui.card().classes("w-full"):
            with ui.row().classes("items-start justify-between w-full gap-3"):
                with ui.column().classes("gap-1"):
                    ui.label(f"#{run['id']} {run['name']}").classes("font-semibold")
                    ui.label(f"{run['server_alias']} · {run['status']}").classes("text-grey-7")
                    ui.label(str(run["tmux_session"])).classes("font-mono text-grey-8")
                    command_summary = str(run.get("rendered_command", "")).strip().splitlines()
                    if command_summary:
                        ui.label(command_summary[0]).classes("font-mono text-grey-7")
                with ui.column().classes("gap-2"):
                    if on_use_command is not None:
                        ui.button("Use command", on_click=lambda run=run: on_use_command(run))
                    ui.link("Open", f"/runs/{run['id']}")


def _render_servers_workdirs_tab(project_id: int, default_workdir: str) -> None:
    ui.label("Servers & Workdirs").classes("text-xl font-semibold")
    unlinked = list_unlinked_enabled_servers(project_id)
    server_options = {server_id: alias for server_id, alias in unlinked}
    server_select = ui.select(server_options, label="Enabled server not linked").classes("w-96")
    default_workdir_input = ui.input("Initial workdir (optional)", value=default_workdir).classes("w-full")

    def add_server() -> None:
        if server_select.value is None:
            _notify("Choose a server to link.", type="negative")
            return
        link_server_to_project(project_id, int(server_select.value), default_workdir_input.value or "")

    ui.button("Add selected server", on_click=add_server)

    ui.separator()
    linked = list_project_servers(project_id)
    if not linked:
        ui.label("No linked servers yet.").classes("text-grey-7")
        return

    for link in linked:
        with ui.card().classes("w-full"):
            ui.label(link["server_alias"]).classes("text-lg font-semibold")
            if link["workdirs"]:
                for workdir in link["workdirs"]:
                    suffix = " (default)" if workdir["is_default"] else ""
                    ui.label(f"{workdir['label']}: {workdir['path']}{suffix}").classes("font-mono")
            else:
                ui.label("No workdirs configured.").classes("text-grey-7")
            with ui.row().classes("items-end gap-3 w-full"):
                path_input = ui.input("Workdir path").classes("w-96")
                label_input = ui.input("Label", value="main").classes("w-48")
                ui.button(
                    "Add workdir",
                    on_click=lambda link_id=link["id"], p=path_input, l=label_input: add_project_workdir(
                        int(link_id), p.value or "", l.value or "main"
                    ),
                )


def _render_templates_tab(project_id: int) -> None:
    ui.label("Templates").classes("text-xl font-semibold")
    name_input = ui.input("Template name").classes("w-96")
    command_input = ui.textarea("Command template", placeholder="python train.py --lr {{ lr }}").classes("w-full")

    def create_template() -> None:
        try:
            create_project_template(project_id, name_input.value or "", command_input.value or "")
        except Exception as exc:
            _notify(str(exc), type="negative")

    ui.button("Create template", on_click=create_template)
    ui.separator()
    templates = list_project_templates(project_id)
    if not templates:
        ui.label("No templates yet.").classes("text-grey-7")
        return
    for template in templates:
        with ui.card().classes("w-full"):
            ui.label(template.name).classes("text-lg font-semibold")
            ui.label(template.command_template).classes("font-mono")
            ui.label(f"Variables: {json.dumps(list(template.variables_schema or []))}").classes("text-grey-7")


def _render_presets_tab(project_id: int) -> None:
    ui.label("Presets").classes("text-xl font-semibold")
    templates = list_project_templates(project_id)
    template_options = {template.id: template.name for template in templates}
    template_select = ui.select(template_options, label="Template").classes("w-96")
    name_input = ui.input("Preset name").classes("w-96")
    values_input = ui.textarea("Values JSON", placeholder='{"lr": "1e-4"}').classes("w-full")

    def create_preset() -> None:
        if template_select.value is None:
            _notify("Choose a template.", type="negative")
            return
        try:
            parsed = json.loads(values_input.value or "{}")
        except json.JSONDecodeError as exc:
            _notify(f"Invalid JSON: {exc.msg}", type="negative")
            return
        if not isinstance(parsed, dict):
            _notify("Values JSON must be an object.", type="negative")
            return
        create_project_preset(project_id, int(template_select.value), name_input.value or "", parsed)

    ui.button("Create preset", on_click=create_preset)
    ui.separator()
    presets = list_project_presets(project_id)
    if not presets:
        ui.label("No presets yet.").classes("text-grey-7")
        return
    template_names = {template.id: template.name for template in templates}
    for preset in presets:
        with ui.card().classes("w-full"):
            ui.label(preset.name).classes("text-lg font-semibold")
            ui.label(f"Template: {template_names.get(preset.template_id, preset.template_id)}")
            ui.label(json.dumps(dict(preset.values_json or {}), sort_keys=True)).classes("font-mono")


def _first_key(options: dict[Any, str]) -> Any | None:
    return next(iter(options), None)


def _render_launch_tab(project_id: int) -> Callable[[dict[str, Any]], None]:
    ui.label("Launch Command").classes("text-xl font-semibold")
    ui.label("Paste a shell command and run it inside tmux on the selected server.").classes("text-grey-7")
    options = build_launch_options(project_id)

    server_select = ui.select(
        options.server_options,
        label="Server",
        value=_first_key(options.server_options),
    ).classes("w-96")
    workdir_select = ui.select(
        options.workdir_options,
        label="Workdir",
        value=_first_key(options.workdir_options),
    ).classes("w-full")
    run_name = ui.input("Run name", value="manual command").classes("w-96")
    command_input = ui.textarea(
        "Command",
        placeholder="source ~/miniconda3/etc/profile.d/conda.sh\nconda activate env\ntorchrun ...",
    ).classes("w-full")
    command_input.props("autogrow")

    def use_command(run: dict[str, Any]) -> None:
        command_input.value = str(run.get("rendered_command", ""))
        if run.get("server_id") in options.server_options:
            server_select.value = run["server_id"]
        if run.get("workdir") in options.workdir_options:
            workdir_select.value = run["workdir"]
        name = str(run.get("name", "manual command")).strip() or "manual command"
        run_name.value = f"copy of {name}"

    async def do_launch() -> None:
        if server_select.value is None or not workdir_select.value:
            _notify("Choose server and workdir.", type="negative")
            return
        if not str(command_input.value or "").strip():
            _notify("Command is required.", type="negative")
            return
        try:
            run = await launch_direct_command_from_project(
                project_id=project_id,
                server_id=int(server_select.value),
                workdir=str(workdir_select.value),
                run_name=run_name.value or "manual command",
                command=command_input.value or "",
            )
        except Exception as exc:
            _notify(str(exc), type="negative")
            return
        _notify(f"Started {run.tmux_session}", type="positive" if run.status == "running" else "warning")
        ui.navigate.to(f"/runs/{run.id}")

    ui.button("Run in tmux", on_click=do_launch).props("color=primary icon=play_arrow")
    return use_command
