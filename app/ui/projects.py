from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from nicegui import ui
from sqlalchemy import Engine

from app.db import engine, session_scope
from app.models import Preset, Project, ProjectServer, ProjectWorkdir, Server, Template
from app.ssh_client import SSHClient
from app.templates import build_variables_schema, merge_template_values, render_template
from app.visual_actions import launch_run


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
            if existing is None:
                session.add(
                    ProjectWorkdir(
                        project_server_id=link.id,
                        path=clean_workdir,
                        label="default",
                        is_default=True,
                    )
                )
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

    with ui.tabs().classes("w-full") as tabs:
        launch_tab = ui.tab("Launch")
        templates_tab = ui.tab("Templates")
        presets_tab = ui.tab("Presets")
        servers_tab = ui.tab("Servers & Workdirs")

    with ui.tab_panels(tabs, value=launch_tab).classes("w-full"):
        with ui.tab_panel(launch_tab):
            _render_launch_tab(project_id)
        with ui.tab_panel(templates_tab):
            _render_templates_tab(project_id)
        with ui.tab_panel(presets_tab):
            _render_presets_tab(project_id)
        with ui.tab_panel(servers_tab):
            _render_servers_workdirs_tab(project_id, project.default_workdir)


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


def _render_launch_tab(project_id: int) -> None:
    ui.label("Launch").classes("text-xl font-semibold")
    options = build_launch_options(project_id)
    template_select = ui.select(options.template_options, label="Template").classes("w-96")
    preset_select = ui.select(options.preset_options, label="Preset", value=None).classes("w-96")
    server_select = ui.select(options.server_options, label="Server").classes("w-96")
    workdir_select = ui.select(options.workdir_options, label="Workdir").classes("w-full")
    run_name = ui.input("Run name", value="training run").classes("w-96")
    variables_container = ui.column().classes("w-full")
    preview = ui.textarea("Rendered command").classes("w-full")
    variable_inputs: dict[str, Any] = {}

    def selected_template() -> Template | None:
        return options.templates.get(int(template_select.value)) if template_select.value is not None else None

    def selected_preset_values() -> dict[str, Any]:
        if preset_select.value is None:
            return {}
        preset = options.presets.get(int(preset_select.value))
        return dict(preset.values_json or {}) if preset is not None else {}

    def refresh_preview() -> None:
        template = selected_template()
        if template is None:
            preview.value = ""
            return
        form_values = {name: input_.value for name, input_ in variable_inputs.items()}
        try:
            values = merge_template_values(template.variables_schema, selected_preset_values(), form_values)
            preview.value = render_template(template.command_template, template.variables_schema, values)
        except Exception as exc:
            preview.value = str(exc)

    def refresh_variables() -> None:
        variables_container.clear()
        variable_inputs.clear()
        template = selected_template()
        if template is None:
            refresh_preview()
            return
        preset_values = selected_preset_values()
        with variables_container:
            for item in template.variables_schema or []:
                name = str(item["name"])
                default = preset_values.get(name, item.get("default", ""))
                variable_inputs[name] = ui.input(str(item.get("label", name)), value=default).classes("w-96")
        refresh_preview()

    async def do_launch() -> None:
        if template_select.value is None or server_select.value is None or not workdir_select.value:
            _notify("Choose template, server, and workdir.", type="negative")
            return
        try:
            run = await launch_run(
                engine=engine,
                ssh_client=SSHClient(),
                project_id=project_id,
                server_id=int(server_select.value),
                template_id=int(template_select.value),
                preset_id=int(preset_select.value) if preset_select.value else None,
                workdir=str(workdir_select.value),
                run_name=run_name.value or "training run",
                form_values={name: input_.value for name, input_ in variable_inputs.items()},
            )
        except Exception as exc:
            _notify(str(exc), type="negative")
            return
        _notify(f"Started {run.tmux_session}", type="positive" if run.status == "running" else "warning")
        ui.navigate.to(f"/runs/{run.id}")

    template_select.on("update:model-value", lambda _: refresh_variables())
    preset_select.on("update:model-value", lambda _: refresh_variables())
    ui.button("Refresh preview", on_click=refresh_preview)
    ui.button("Launch", on_click=do_launch).props("color=primary")
    refresh_variables()
