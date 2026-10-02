from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from nicegui import ui
from sqlalchemy import Engine, desc

from app.db import engine, session_scope
from app.models import Preset, Project, ProjectServer, ProjectWorkdir, Run, Server, Template
from app.schemas import ServerStatus
from app.ssh_client import SSHClient
from app.command_parser import (
    collect_line_history,
    collect_param_history,
    collect_param_key_history,
    param_history_key,
    parse_command,
)
from app.run_config import (
    extract_training_run_name,
    set_training_run_name,
    training_display_name,
)
from app.run_status import ATTENTION_STATES, get_run_record, list_run_records, observe_run_records
from app.templates import build_variables_schema
from app.visual_actions import collect_server_status, launch_direct_command
from app.ui.dashboard import render_pending_server_card, render_server_card
from app.ui.scoped_timer import create_scoped_timer
from app.ui.sync_launch import render_sync_launch_panel


_project_status_cache: dict[str, ServerStatus] = {}


def _notify(message: str, *, type: str = "info") -> None:
    ui.notify(message, type=type)


def _reload() -> None:
    ui.navigate.reload()


def _history_hidden_path(target_engine: Engine = engine) -> Path:
    database = target_engine.url.database
    if database:
        return Path(database).parent / "command_history_hidden.json"
    return Path("data/command_history_hidden.json")


def _load_hidden_history(target_engine: Engine = engine) -> set[str]:
    path = _history_hidden_path(target_engine)
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(item) for item in data}


def _save_hidden_history(hidden: set[str], target_engine: Engine = engine) -> None:
    path = _history_hidden_path(target_engine)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(hidden), indent=2))


def _history_id(scope: str, key: str, value: str) -> str:
    return json.dumps([scope, key, value], ensure_ascii=False, separators=(",", ":"))


def hide_command_history_value(
    scope: str,
    key: str,
    value: str,
    *,
    target_engine: Engine = engine,
) -> bool:
    clean_value = str(value)
    if not clean_value:
        return False
    hidden = _load_hidden_history(target_engine)
    hidden.add(_history_id(scope, key, clean_value))
    _save_hidden_history(hidden, target_engine)
    return True


def _visible_history_options(
    options: list[str],
    *,
    scope: str,
    key: str,
    hidden: set[str],
) -> list[str]:
    return [value for value in options if _history_id(scope, key, str(value)) not in hidden]


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


def list_enabled_servers(*, target_engine: Engine = engine) -> list[tuple[int, str]]:
    """Return every enabled server as options for project linking/workdir edits."""

    with session_scope(target_engine) as session:
        servers = (
            session.query(Server)
            .filter(Server.enabled.is_(True))
            .order_by(Server.alias)
            .all()
        )
        return [(server.id, server.alias) for server in servers]


def list_unlinked_enabled_servers(
    project_id: int, *, target_engine: Engine = engine
) -> list[tuple[int, str]]:
    with session_scope(target_engine) as session:
        linked_ids = {
            row[0]
            for row in session.query(ProjectServer.server_id)
            .filter_by(project_id=project_id, enabled=True)
            .all()
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


def unlink_server_from_project(
    project_server_id: int,
    *,
    target_engine: Engine = engine,
) -> bool:
    """Remove a server link, including its configured workdirs, from a project."""

    with session_scope(target_engine) as session:
        link = session.get(ProjectServer, project_server_id)
        if link is None:
            _notify("Project server link not found.", type="negative")
            return False
        alias = link.server.alias
        session.delete(link)

    _project_status_cache.pop(alias, None)
    _notify(f"Server '{alias}' removed from project.", type="positive")
    _reload()
    return True


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
    workdirs_by_server: dict[int, dict[str, str]]
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
        workdirs_by_server = {
            link.server_id: {
                workdir.path: f"{workdir.label} ({workdir.path})"
                for workdir in sorted(
                    link.workdirs,
                    key=lambda item: (not item.is_default, item.label, item.path),
                )
            }
            for link in links
        }

        for obj in [*templates, *presets]:
            session.expunge(obj)

    return LaunchOptions(
        template_options=template_options,
        preset_options=preset_options,
        server_options=server_options,
        workdir_options=workdir_options,
        workdirs_by_server=workdirs_by_server,
        templates={template.id: template for template in templates},
        presets={preset.id: preset for preset in presets},
    )



def list_project_historical_commands(project_id: int, *, target_engine: Engine = engine) -> list[str]:
    """Return historical rendered commands for a project, newest first."""

    with session_scope(target_engine) as session:
        return [
            command
            for (command,) in session.query(Run.rendered_command)
            .filter(Run.project_id == project_id)
            .order_by(desc(Run.id))
            .all()
            if command
        ]


def build_project_command_history(project_id: int, *, target_engine: Engine = engine) -> dict[str, list[str]]:
    """Collect parameter value suggestions from historical runs, newest first."""

    return collect_param_history(list_project_historical_commands(project_id, target_engine=target_engine))

def list_project_recent_runs(
    project_id: int, *, limit: int = 10, target_engine: Engine = engine
) -> list[dict[str, Any]]:
    return list_run_records(
        project_id=project_id,
        limit=limit,
        target_engine=target_engine,
    )


def list_project_favorite_runs(
    project_id: int, *, limit: int = 10, target_engine: Engine = engine
) -> list[dict[str, Any]]:
    return list_run_records(
        project_id=project_id,
        limit=limit,
        favorite_only=True,
        target_engine=target_engine,
    )


def set_run_favorite(
    run_id: int, favorite: bool, *, target_engine: Engine = engine, reload_page: bool = True
) -> bool:
    with session_scope(target_engine) as session:
        run = session.get(Run, run_id)
        if run is None:
            _notify("Run not found.", type="negative")
            return False
        run.is_favorite = favorite

    _notify("Run favorited." if favorite else "Run removed from favorites.", type="positive")
    if reload_page:
        _reload()
    return True


async def launch_direct_command_from_project(
    *,
    project_id: int,
    server_id: int,
    workdir: str,
    run_name: str,
    command: str,
    source_run_id: int | None = None,
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
        source_run_id=source_run_id,
    )


def render_projects_page() -> None:
    _load_projects_css()
    with ui.row().classes("st-page-header"):
        with ui.column().classes("gap-1"):
            ui.label("项目").classes("st-page-title")
            ui.label("从项目查看任务进度、排查异常和管理启动配置。").classes("st-page-subtitle")
        ui.button("新建项目", icon="add", color=None, on_click=lambda: create_form.set_visibility(
            not create_form.visible
        )).props("no-caps").classes("st-project-primary")

    with ui.column().classes("st-panel st-project-create") as create_form:
        ui.label("新建项目").classes("st-project-section-title")
        with ui.column().classes("st-project-fields"):
            name_input = ui.input("项目名称").props("outlined dense").classes("w-full")
            git_input = ui.input("Git URL（可选）").props("outlined dense").classes("w-full")
            workdir_input = ui.input("默认工作目录（可选）").props("outlined dense").classes("w-full")
        with ui.row().classes("st-actions"):
            ui.button("创建项目", color=None, on_click=lambda: create_project(
                name_input.value or "", git_input.value or "", workdir_input.value or ""
            )).props("no-caps").classes("st-primary")
            ui.button("取消", on_click=lambda: create_form.set_visibility(False)).props("flat no-caps")
    create_form.set_visibility(False)

    projects = list_projects()
    if not projects:
        with ui.column().classes("st-panel st-empty"):
            ui.icon("folder_open")
            ui.label("还没有项目；创建项目后可关联机器并启动训练。")
        return

    with ui.column().classes("st-project-index"):
        for project in projects:
            with ui.column().classes("st-panel st-project-index-card"):
                ui.icon("folder_open").classes("st-project-index-icon")
                ui.link(project.name, f"/projects/{project.id}").classes("st-project-index-title")
                ui.label(project.git_url or "未设置 Git URL").classes("st-project-path")
                if project.default_workdir:
                    ui.label(project.default_workdir).classes("st-project-path")
                ui.link("查看运行 →", f"/projects/{project.id}").classes("st-project-index-open")


def _load_projects_css() -> None:
    ui.add_css(Path(__file__).with_name("projects.css").read_text())


def _project_query() -> dict[str, str]:
    try:
        request = ui.context.client.request
        return dict(request.query_params) if request is not None else {}
    except (AttributeError, RuntimeError):
        return {}


def _positive_id(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (ValueError, TypeError):
        return None
    return parsed if parsed > 0 else None


def _run_return_path(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.path != "/runs":
        return None
    return value


class _ProjectViews:
    """Build each view once: returning to monitoring never reconstructs a draft."""

    def __init__(self, labels: dict[str, str], *, navigation_class: str = "st-project-tabs") -> None:
        self.builders: dict[str, Callable[[], Any]] = {}
        self.panels: dict[str, Any] = {}
        self.results: dict[str, Any] = {}
        self.buttons: dict[str, Any] = {}
        self.current: str | None = None
        with ui.row().classes(navigation_class).props('role="group" aria-label="项目视图"'):
            for key, label in labels.items():
                self.buttons[key] = ui.button(label, color=None, on_click=lambda k=key: self.show(k)).props(
                    "flat no-caps aria-pressed=false"
                ).classes("st-project-tab")
        self.host = ui.column().classes("st-project-view-host")

    def show(self, name: str) -> Any:
        if name not in self.builders:
            return None
        if name not in self.panels:
            with self.host:
                self.panels[name] = ui.column().classes("st-project-view")
            with self.panels[name]:
                self.results[name] = self.builders[name]()
        for key, panel in self.panels.items():
            panel.set_visibility(key == name)
        for key, button in self.buttons.items():
            selected = key == name
            button.props(f"aria-pressed={'true' if selected else 'false'}")
            button.classes(add="st-project-tab-active" if selected else "",
                           remove="" if selected else "st-project-tab-active")
        self.current = name
        return self.results[name]


def _render_project_launch(project_id: int, on_back: Callable[[], None]) -> Callable[[dict[str, Any]], None]:
    with ui.row().classes("st-project-view-heading"):
        ui.label("草稿在项目内切换时保留").classes("st-page-subtitle")
        ui.button("返回运行监控", icon="arrow_back", color=None, on_click=on_back).props("flat no-caps")
    provenance = ui.label("").classes("st-project-reuse-note")
    provenance.set_visibility(False)
    modes = _ProjectViews({"sync": "同步并启动 · Newton", "command": "命令启动"},
                          navigation_class="st-project-mode-tabs")

    def command_form() -> Callable[[dict[str, Any]], None]:
        with ui.column().classes("st-command-form"):
            return _render_launch_tab(project_id)

    modes.builders = {"sync": lambda: render_sync_launch_panel(project_id),
                      "command": command_form}
    modes.show("sync" if _project_query().get("source") else "command")

    def reuse(run: dict[str, Any]) -> None:
        fill = modes.show("command")
        fill(run)
        provenance.set_text(
            f"复用 #{run['id']} · 已恢复命令与目标，请核对后启动。"
        )
        provenance.set_visibility(True)

    return reuse


def _render_project_configuration(project: Project) -> None:
    ui.label("保存机器、目录、模板或预设会刷新项目；请先完成尚未提交的启动草稿。").classes("st-page-subtitle")
    sections = _ProjectViews(
        {"general": "基本信息", "workdirs": "机器与目录", "templates": "命令模板", "presets": "参数预设"},
        navigation_class="st-project-config-tabs",
    )

    def general() -> None:
        with ui.column().classes("st-panel st-project-settings-panel"):
            ui.label("基本信息").classes("st-project-section-title")
            with ui.element("dl").classes("st-project-metadata"):
                for label, value in (("项目名称", project.name), ("Git URL", project.git_url or "未设置"),
                                     ("默认工作目录", project.default_workdir or "未设置")):
                    with ui.element("dt"):
                        ui.label(label)
                    with ui.element("dd"):
                        ui.label(value)

    def settings_form(callback: Callable[[], None]) -> None:
        with ui.column().classes("st-panel st-project-settings-panel"):
            callback()

    sections.builders = {
        "general": general,
        "workdirs": lambda: settings_form(lambda: _render_servers_workdirs_tab(project.id, project.default_workdir)),
        "templates": lambda: settings_form(lambda: _render_templates_tab(project.id)),
        "presets": lambda: settings_form(lambda: _render_presets_tab(project.id)),
    }
    sections.show("general")


def render_project_detail(project_id: int) -> None:
    from app.ui.runs import render_run_workbench

    _load_projects_css()
    project = get_project(project_id)
    if project is None:
        ui.label(f"Project not found: {project_id}").classes("text-negative")
        return

    query = _project_query()
    return_path = _run_return_path(query.get("return_to"))
    with ui.row().classes("st-page-header"):
        with ui.column().classes("gap-1 min-w-0"):
            ui.label(project.name).classes("st-page-title st-project-name")
            ui.label("项目运行与结果 · 配置集中在独立入口").classes("st-page-subtitle")
        ui.button("启动实验", icon="add", color=None, on_click=lambda: views.show("launch")).props("no-caps").classes("st-primary")

    views = _ProjectViews({"monitor": "运行监控", "launch": "启动实验", "favorites": "收藏配置",
                           "resources": "资源", "config": "项目配置"})

    def reuse(run: dict[str, Any]) -> None:
        fill = views.show("launch")
        fill(run)

    views.builders = {
        "monitor": lambda: render_run_workbench(project_id=project_id,
                    selected_run_id=_positive_id(query.get("run")), on_reuse=reuse),
        "launch": lambda: _render_project_launch(project_id, lambda: (
            ui.navigate.to(return_path) if return_path else views.show("monitor")
        )),
        "favorites": lambda: render_project_favorite_runs_panel(project_id, on_use_command=reuse),
        "resources": lambda: render_project_servers_status_panel(project_id),
        "config": lambda: _render_project_configuration(project),
    }
    initial = query.get("view", "monitor")
    if query.get("source"):
        initial = "launch"
    views.show(initial if initial in views.builders else "monitor")
    if query.get("reuse_run"):
        run_id = _positive_id(query["reuse_run"])
        run = get_run_record(run_id) if run_id is not None else None
        if run is None or run.get("project_id") != project_id:
            _notify("未找到属于当前项目的运行记录，未载入其他任务配置。", type="warning")
        else:
            reuse(run)


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
    ui.label("项目资源").classes("st-project-section-title")
    linked = list_project_servers(project_id)
    aliases = [str(link["server_alias"]) for link in linked]
    if not aliases:
        ui.label("尚未关联机器。请到项目配置 → 机器与目录中添加。").classes("st-empty")
        return

    container = ui.column().classes("st-project-resources")
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

    ui.button("刷新资源", color=None, on_click=refresh_statuses).props("outline icon=refresh no-caps")
    create_scoped_timer(container, 0, refresh_statuses, once=True)


def _render_run_reuse_card(
    run: dict[str, Any],
    *,
    on_use_command: Callable[[dict[str, Any]], None] | None = None,
    show_favorite_button: bool = True,
) -> None:
    with ui.card().classes("w-full st-project-run-card"):
        with ui.row().classes("items-start justify-between w-full gap-3"):
            with ui.column().classes("gap-1"):
                ui.label(str(run["name"])).classes("st-project-run-name")
                ui.label(
                    f"#{run['id']} · {run['server_alias']} · {run['status']}"
                ).classes("text-grey-7")
                if run.get("launch_source") == "mcp":
                    provenance = "Launched via Codex MCP"
                    if run.get("source_run_id") is not None:
                        provenance += f" · based on run #{run['source_run_id']}"
                    ui.label(provenance).classes("text-primary text-sm")
                if run.get("launch_preflight_status"):
                    ui.label(
                        f"GPU preflight: {run['launch_preflight_status']}"
                    ).classes("text-grey-7 text-sm")
                if run.get("exit_code") is not None:
                    ui.label(f"Exit code: {run['exit_code']}").classes("text-negative")
                if run.get("status_detail") and run.get("status") in ATTENTION_STATES:
                    ui.label(str(run["status_detail"])).classes("text-grey-7 text-sm")
                config = run.get("config") or {}
                summary_bits = [
                    f"GPU {config['cuda_visible_devices']}"
                    if config.get("cuda_visible_devices")
                    else "",
                    f"{config['num_envs']} envs" if config.get("num_envs") else "",
                    f"{config['max_iterations']} iterations"
                    if config.get("max_iterations")
                    else "",
                ]
                summary = " · ".join(bit for bit in summary_bits if bit)
                if summary:
                    ui.label(summary).classes("text-grey-7 text-sm")
                else:
                    command_summary = str(run.get("rendered_command", "")).strip().splitlines()
                    if command_summary:
                        ui.label(command_summary[0]).classes("font-mono text-grey-7")
            with ui.column().classes("gap-2"):
                if on_use_command is not None:
                    ui.button("Use config", on_click=lambda run=run: on_use_command(run))
                if show_favorite_button:
                    favorite_state = {"value": bool(run.get("is_favorite", False))}

                    def toggle_favorite() -> None:
                        updated = not favorite_state["value"]
                        if set_run_favorite(int(run["id"]), updated, reload_page=False):
                            favorite_state["value"] = updated
                            favorite_button.set_text("Unfavorite" if updated else "Favorite")
                            favorite_button.props("icon=star" if updated else "icon=star_border")

                    favorite_button = ui.button(
                        "Unfavorite" if favorite_state["value"] else "Favorite", on_click=toggle_favorite,
                    ).props("icon=star" if favorite_state["value"] else "icon=star_border")
                ui.link("Open", f"/runs/{run['id']}")


def render_project_favorite_runs_panel(
    project_id: int, *, on_use_command: Callable[[dict[str, Any]], None] | None = None
) -> None:
    runs = list_project_favorite_runs(project_id)
    if not runs:
        ui.label("还没有收藏配置。可在运行记录中收藏需要再次使用的任务。").classes("st-panel st-empty")
        return

    with ui.column().classes("st-project-favorites"):
        ui.label("收藏配置").classes("st-project-section-title")
        ui.label("复用命令与目标机器；修改后再启动新任务。").classes("st-page-subtitle")
        for run in runs:
            _render_run_reuse_card(run, on_use_command=on_use_command)


def render_project_recent_runs_panel(
    project_id: int, *, on_use_command: Callable[[dict[str, Any]], None] | None = None
) -> None:
    ui.label("Recent Runs").classes("text-xl font-semibold")
    runs = list_project_recent_runs(project_id)
    if not runs:
        ui.label("No runs for this project yet.").classes("text-grey-7")
        return
    async def refresh_live_states() -> None:
        await observe_run_records(list_project_recent_runs(project_id, limit=50))
        _notify("Run states refreshed.", type="positive")
        ui.navigate.reload()

    ui.button("Refresh live states", on_click=refresh_live_states).props("icon=refresh flat")
    for run in runs:
        _render_run_reuse_card(run, on_use_command=on_use_command)


def _render_servers_workdirs_tab(project_id: int, default_workdir: str) -> None:
    ui.label("机器与工作目录").classes("st-project-section-title")
    enabled_servers = list_enabled_servers()
    server_options = {server_id: alias for server_id, alias in enabled_servers}
    with ui.expansion("关联机器 / 设置默认目录", value=False).props("icon=add").classes("w-full"):
        with ui.column().classes("st-project-fields"):
            server_select = ui.select(server_options, label="Server").props("outlined dense").classes("w-full")
            default_workdir_input = ui.input("工作目录（可选）", value=default_workdir).props("outlined dense").classes("w-full")

        def add_server_or_workdir() -> None:
            if server_select.value is None:
                _notify("Choose a server.", type="negative")
                return
            link_server_to_project(project_id, int(server_select.value), default_workdir_input.value or "")

        ui.label("选择已启用的机器。若机器已关联，填写的目录会被添加并设为默认目录。").classes("st-page-subtitle")
        ui.button("保存机器与默认目录", color=None, on_click=add_server_or_workdir).props("no-caps").classes("st-primary")
    linked = list_project_servers(project_id)
    if not linked:
        ui.label("尚未关联机器。展开“关联机器”开始配置。").classes("st-empty")
        return

    for link in linked:
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label(link["server_alias"]).classes("text-lg font-semibold")
                ui.button(
                    "取消关联",
                    on_click=lambda link_id=link["id"]: unlink_server_from_project(int(link_id)),
                ).props("color=negative")
            if link["workdirs"]:
                for workdir in link["workdirs"]:
                    suffix = " · 默认" if workdir["is_default"] else ""
                    ui.label(f"{workdir['label']}: {workdir['path']}{suffix}").classes("font-mono")
            else:
                ui.label("尚未配置工作目录。").classes("st-page-subtitle")
            with ui.expansion("新增目录", value=False).props("icon=add").classes("w-full"):
                with ui.column().classes("st-project-fields"):
                    path_input = ui.input("目录路径").props("outlined dense").classes("w-full")
                    label_input = ui.input("名称", value="main").props("outlined dense").classes("w-full")
                ui.button(
                    "添加工作目录", color=None,
                    on_click=lambda link_id=link["id"], p=path_input, label=label_input: add_project_workdir(
                        int(link_id), p.value or "", label.value or "main"
                    ),
                ).props("no-caps").classes("st-primary")


def _render_templates_tab(project_id: int) -> None:
    ui.label("命令模板").classes("st-project-section-title")
    with ui.expansion("新建模板", value=False).props("icon=add").classes("w-full"):
        with ui.column().classes("w-full gap-4"):
            name_input = ui.input("模板名称").props("outlined dense").classes("w-full")
            command_input = ui.textarea("模板命令", placeholder="python train.py --lr {{ lr }}").props("outlined autogrow").classes("w-full")

            def create_template() -> None:
                try:
                    create_project_template(project_id, name_input.value or "", command_input.value or "")
                except Exception as exc:
                    _notify(str(exc), type="negative")

            ui.button("创建模板", color=None, on_click=create_template).props("no-caps").classes("st-primary")
    templates = list_project_templates(project_id)
    if not templates:
        ui.label("尚无模板。模板使用 {{ variable }} 表示可替换变量。").classes("st-empty")
        return
    for template in templates:
        with ui.card().classes("w-full"):
            ui.label(template.name).classes("text-lg font-semibold")
            ui.label(template.command_template).classes("font-mono")
            ui.label(f"变量：{json.dumps(list(template.variables_schema or []), ensure_ascii=False)}").classes("st-page-subtitle")


def _render_presets_tab(project_id: int) -> None:
    ui.label("参数预设").classes("st-project-section-title")
    templates = list_project_templates(project_id)
    template_options = {template.id: template.name for template in templates}
    with ui.expansion("新建预设", value=False).props("icon=add").classes("w-full"):
        with ui.column().classes("st-project-fields"):
            template_select = ui.select(template_options, label="Template").props("outlined dense").classes("w-full")
            name_input = ui.input("预设名称").props("outlined dense").classes("w-full")
        values_input = ui.textarea("参数 JSON", placeholder='{"lr": "1e-4"}').props("outlined autogrow").classes("w-full")

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

        ui.button("创建预设", color=None, on_click=create_preset).props("no-caps").classes("st-primary")
    presets = list_project_presets(project_id)
    if not presets:
        ui.label("尚无预设。先创建命令模板，再为模板保存一组参数。").classes("st-empty")
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
    ui.label("命令启动").classes("st-project-section-title st-command-heading")
    ui.label("完整命令是实际启动内容；参数编辑后需应用到命令。选定机器与工作目录后，在 tmux 中执行。").classes("st-page-subtitle st-command-description")
    options = build_launch_options(project_id)

    initial_server_id = _first_key(options.server_options)
    server_select = ui.select(
        options.server_options,
        label="目标机器",
        value=initial_server_id,
    ).props("outlined dense").classes("w-full st-command-server")
    per_server_options = getattr(options, "workdirs_by_server", {})
    initial_workdirs = (
        per_server_options.get(initial_server_id, {})
        if per_server_options
        else options.workdir_options
    )
    workdir_select = ui.select(
        initial_workdirs,
        label="工作目录",
        value=_first_key(initial_workdirs),
    ).props("outlined dense").classes("w-full st-command-workdir")

    def update_workdir_options(
        server_id: int | None = None,
        *,
        preferred_path: str | None = None,
    ) -> None:
        resolved_server_id = server_id if server_id is not None else server_select.value
        available = (
            per_server_options.get(int(resolved_server_id), {})
            if per_server_options and resolved_server_id is not None
            else options.workdir_options
        )
        if resolved_server_id is None:
            available = {}
        selected = (preferred_path if preferred_path in available else None) if preferred_path else _first_key(available)
        if hasattr(workdir_select, "set_options"):
            workdir_select.set_options(available, value=selected)
        else:
            workdir_select.options = available
            workdir_select.value = selected

    if hasattr(server_select, "on"):
        server_select.on(
            "update:model-value",
            lambda _event: update_workdir_options(),
        )
    training_name = ui.input("训练名称 (--run_name)", value="").props("outlined dense").classes("w-full st-command-name")
    ui.label(
        "与 --run_name 同步。运行目录包含时间戳，可重复使用训练名称。"
    ).classes("st-page-subtitle st-command-name-note")
    command_input = ui.textarea(
        "完整启动命令",
        placeholder="source ~/miniconda3/etc/profile.d/conda.sh\nconda activate env\ntorchrun ...",
    ).classes("w-full st-command-script")
    command_input.props("outlined rows=12")

    historical_commands = list_project_historical_commands(project_id)
    history = collect_param_history(historical_commands)
    key_history = collect_param_key_history(historical_commands)
    line_history = collect_line_history(historical_commands)
    hidden_history = _load_hidden_history()
    editor_container = ui.column().classes("st-command-editor") if hasattr(ui, "column") else None
    editor_state: list[dict[str, Any]] = []
    editor_baseline: tuple[Any, ...] = ()
    parsed_command = ""
    editor_notice = None
    launch_button = None
    launching = False
    source_run_id: int | None = None
    parameters_expansion = None

    def editor_values() -> tuple[Any, ...]:
        values: list[Any] = []
        for line in editor_state:
            values.extend((line["mode"], line["cmd"].value))
            if line["mode"] == "raw":
                values.append(line["rest"].value)
            else:
                for item in line["params"]:
                    values.extend(item[key].value for key in ("kind", "key", "value", "drop"))
                if line.get("add"):
                    values.extend(line["add"][key].value for key in ("kind", "key", "value"))
        return tuple(values)

    def update_editor_notice() -> None:
        if editor_notice is None:
            return
        dirty = editor_values() != editor_baseline
        if str(command_input.value or "") != parsed_command:
            message = "命令已修改；如需使用参数编辑器，请先重新解析。"
        elif dirty:
            message = "有尚未应用的参数修改。请先应用到命令，再启动训练。"
        else:
            message = "参数编辑器与当前命令一致。"
        editor_notice.set_text(message)
        editor_notice.classes(add="st-command-warning" if dirty else "",
                              remove="" if dirty else "st-command-warning")
        if launch_button is not None:
            launch_button.disable() if dirty or launching else launch_button.enable()

    def sync_training_name_from_command() -> None:
        configured_name = extract_training_run_name(command_input.value or "") or ""
        training_name.value = configured_name

    def sync_command_from_training_name() -> None:
        requested_name = str(training_name.value or "").strip()
        if requested_name:
            command_input.value = set_training_run_name(
                command_input.value or "", requested_name
            )

    if hasattr(training_name, "on"):
        training_name.on("change", lambda _event: sync_command_from_training_name())
    if hasattr(command_input, "on_value_change"):
        def command_changed() -> None:
            sync_training_name_from_command()
            update_editor_notice()
        command_input.on_value_change(lambda _event: command_changed())

    def _quote_token(value: str) -> str:
        return shlex.quote(str(value)) if str(value) else ""

    def _editable_select(
        options: list[str],
        *,
        label: str,
        value: str,
        width: str = "w-64",
        scope: str = "",
        history_key: str = "",
    ):
        visible_options = (
            _visible_history_options(
                [str(option) for option in options],
                scope=scope,
                key=history_key,
                hidden=hidden_history,
            )
            if scope and history_key
            else [str(option) for option in options]
        )
        unique_options = []
        for item in visible_options:
            text = "" if item is None else str(item)
            if text and text not in unique_options:
                unique_options.append(text)
        # A pasted or edited command can contain values absent from saved history.
        if value and value not in unique_options:
            unique_options.append(value)
        select = ui.select(
            unique_options,
            label=label,
            value=value if value else None,
            with_input=True,
            new_value_mode="add-unique",
        ).props("dense outlined options-dense").classes(width)
        select.on_value_change(lambda _event: update_editor_notice())
        if scope and history_key:
            def delete_history_option(event, *, option_scope: str = scope, option_key: str = history_key) -> None:
                value_to_hide = "" if event.args is None else str(event.args)
                if not value_to_hide:
                    return
                hide_command_history_value(option_scope, option_key, value_to_hide)
                _notify("History value hidden.", type="positive")
                refresh_parameter_editor()

            select.on("delete-history-option", delete_history_option)
            select.add_slot(
                "option",
                """
                <q-item v-bind=\"scope.itemProps\" dense>
                  <q-item-section>
                    <q-item-label>{{ scope.opt }}</q-item-label>
                  </q-item-section>
                  <q-item-section side>
                    <q-btn dense flat round size=\"sm\" icon=\"close\" color=\"grey-6\"
                           class=\"bg-white text-grey-6\"
                           @click.stop.prevent=\"$emit('delete-history-option', scope.opt)\" />
                  </q-item-section>
                </q-item>
                """,
            )
        return select

    def _append_editor_line(line_state: dict[str, Any]) -> None:
        editor_state.append(line_state)

    def refresh_parameter_editor(*, expand: bool | None = None) -> None:
        nonlocal hidden_history, editor_baseline, parsed_command, parameters_expansion
        sync_training_name_from_command()
        hidden_history = _load_hidden_history()
        if editor_container is None:
            return
        expanded = bool(parameters_expansion.value) if parameters_expansion is not None else False
        if expand is not None:
            expanded = expand
        editor_container.clear()
        editor_state.clear()
        parsed = parse_command(command_input.value or "")
        parsed_command = str(command_input.value or "")
        with editor_container:
            with ui.expansion("参数编辑器", value=expanded).classes("w-full") as parameters_expansion:
                ui.label("编辑后点击“应用到命令”写回。历史候选旁的 × 可隐藏该候选值。").classes("st-page-subtitle")
                if not parsed.lines:
                    ui.label("先输入命令，再点击“解析 / 编辑参数”。").classes("st-page-subtitle")
                    editor_baseline = ()
                    update_editor_notice()
                    return
                for line_index, line in enumerate(parsed.lines):
                    first_token = line.tokens[0] if line.tokens else ""
                    rest_text = " ".join(line.tokens[1:]) if len(line.tokens) > 1 else ""
                    has_named_params = any(param.kind in {"env", "option", "flag"} for param in line.params)
                    with ui.row().classes("st-command-line"):
                        ui.label(f"{line_index + 1}").classes("text-grey-6 w-6 pt-3")
                        command_select = _editable_select(
                            line_history.get("command", []),
                            label="cmd",
                            value=first_token,
                            width="st-command-executable",
                            scope="line_command",
                            history_key="command",
                        )
                        if not has_named_params:
                            rest_select = _editable_select(
                                line_history.get("rest", []),
                                label="parameters",
                                value=rest_text,
                                width="st-command-raw-args",
                                scope="line_rest",
                                history_key="rest",
                            )
                            _append_editor_line({"mode": "raw", "cmd": command_select, "rest": rest_select})
                            continue

                        line_state: dict[str, Any] = {"mode": "params", "cmd": command_select, "params": []}
                        with ui.column().classes("st-command-params"):
                            for param in line.params:
                                with ui.row().classes("st-command-param"):
                                    kind_select = _editable_select(
                                        ["option", "flag", "env", "positional"],
                                        label="kind",
                                        value=param.kind,
                                        width="st-command-param-kind",
                                    )
                                    key_hist_key = f"{line.role}|{param.kind}"
                                    key_select = _editable_select(
                                        key_history.get(key_hist_key, []),
                                        label="key",
                                        value=param.key,
                                        width="st-command-param-key",
                                        scope="param_key",
                                        history_key=key_hist_key,
                                    )
                                    value_hist_key = param_history_key(line, param)
                                    value_select = _editable_select(
                                        [] if param.kind == "flag" else history.get(value_hist_key, []),
                                        label="value",
                                        value="" if param.kind == "flag" else param.value,
                                        width="st-command-param-value",
                                        scope="param_value",
                                        history_key=value_hist_key,
                                    )
                                    if param.kind == "flag":
                                        value_select.props("disable")
                                    delete_box = ui.checkbox("drop", value=False).props("dense color=negative")
                                    delete_box.on_value_change(lambda _event: update_editor_notice())
                                    line_state["params"].append(
                                        {
                                            "kind": kind_select,
                                            "key": key_select,
                                            "value": value_select,
                                            "drop": delete_box,
                                        }
                                    )
                            with ui.row().classes("st-command-param st-command-new-param"):
                                add_kind = _editable_select(["option", "flag", "env", "positional"], label="kind", value="option", width="st-command-param-kind")
                                add_key = _editable_select(
                                    key_history.get(f"{line.role}|option", []),
                                    label="new key",
                                    value="",
                                    width="st-command-param-key",
                                )
                                add_value = _editable_select([], label="value", value="", width="st-command-param-value")
                                line_state["add"] = {"kind": add_kind, "key": add_key, "value": add_value}
                        _append_editor_line(line_state)
        editor_baseline = editor_values()
        update_editor_notice()

    def apply_editor_changes() -> None:
        if str(command_input.value or "") != parsed_command:
            _notify("命令已修改，请先重新解析参数，避免覆盖新命令。", type="warning")
            return
        rendered_lines: list[str] = []
        for line_state in editor_state:
            cmd = str(line_state["cmd"].value or "").strip()
            if not cmd:
                continue
            if line_state["mode"] == "raw":
                rest = str(line_state["rest"].value or "").strip()
                rendered_lines.append(" ".join(part for part in [_quote_token(cmd), rest] if part))
                continue

            tokens = [_quote_token(cmd)]
            for item in line_state["params"]:
                if bool(item["drop"].value):
                    continue
                kind = str(item["kind"].value or "option")
                key = str(item["key"].value or "").strip()
                value = str(item["value"].value or "").strip()
                if kind == "flag":
                    if key:
                        tokens.append(_quote_token(key))
                elif kind == "env":
                    if key:
                        tokens.append(_quote_token(f"{key}={value}"))
                elif kind == "positional":
                    positional = value or key
                    if positional:
                        tokens.append(_quote_token(positional))
                else:
                    if key and value:
                        if key.startswith("--"):
                            tokens.append(_quote_token(f"{key}={value}"))
                        else:
                            tokens.extend([_quote_token(key), _quote_token(value)])
                    elif key:
                        tokens.append(_quote_token(key))
            add = line_state.get("add")
            if add is not None:
                kind = str(add["kind"].value or "option")
                key = str(add["key"].value or "").strip()
                value = str(add["value"].value or "").strip()
                if key:
                    if kind == "flag":
                        tokens.append(_quote_token(key))
                    elif kind == "env":
                        tokens.append(_quote_token(f"{key}={value}"))
                    elif kind == "positional":
                        tokens.append(_quote_token(value or key))
                    elif key.startswith("--"):
                        tokens.append(_quote_token(f"{key}={value}"))
                    else:
                        tokens.extend([_quote_token(key), _quote_token(value)] if value else [_quote_token(key)])
            rendered_lines.append(" ".join(token for token in tokens if token))
        command_input.value = "\n".join(rendered_lines)
        sync_training_name_from_command()
        refresh_parameter_editor()

    def use_command(run: dict[str, Any]) -> None:
        nonlocal source_run_id
        source_run_id = _positive_id(run.get("id"))
        command_input.value = str(run.get("rendered_command", ""))
        server_select.value = run["server_id"] if run.get("server_id") in options.server_options else None
        update_workdir_options(
            int(run["server_id"]) if run.get("server_id") in options.server_options else None,
            preferred_path=str(run.get("workdir") or "") or None,
        )
        training_name.value = (
            str(run.get("training_run_name") or "").strip()
            or extract_training_run_name(command_input.value or "")
            or ""
        )
        refresh_parameter_editor(expand=False)
        if server_select.value is None or not workdir_select.value:
            _notify("原运行的机器或工作目录已不可用，请明确选择新的目标后启动。", type="warning")

    async def do_launch() -> None:
        nonlocal launching
        if launching:
            return
        if editor_values() != editor_baseline:
            _notify("有尚未应用的参数修改，请先应用到命令再启动。", type="warning")
            return
        if server_select.value is None or not workdir_select.value:
            _notify("请选择目标机器和工作目录。", type="negative")
            return
        if not str(command_input.value or "").strip():
            _notify("请填写完整启动命令。", type="negative")
            return
        sync_command_from_training_name()
        launch_command = str(command_input.value or "")
        configured_name = extract_training_run_name(launch_command) or ""
        display_name = training_display_name(
            run_id=None,
            panel_name="manual command",
            training_run_name=configured_name,
            command=launch_command,
        )
        launching = True
        update_editor_notice()
        try:
            run = await launch_direct_command_from_project(
                project_id=project_id,
                server_id=int(server_select.value),
                workdir=str(workdir_select.value),
                run_name=display_name,
                command=launch_command,
                source_run_id=source_run_id,
            )
        except Exception as exc:
            _notify(str(exc), type="negative")
            return
        finally:
            launching = False
            update_editor_notice()
        _notify(f"Started {run.tmux_session}", type="positive" if run.status == "running" else "warning")
        ui.navigate.to(f"/runs/{run.id}")

    if hasattr(ui, "row"):
        with ui.column().classes("st-command-actions st-panel"):
            ui.label("启动检查").classes("st-project-section-title")
            editor_notice = ui.label("").classes("st-page-subtitle")
            ui.button("解析 / 编辑参数", color=None, on_click=lambda: refresh_parameter_editor(expand=True)).props("outline icon=tune no-caps")
            ui.button("应用到命令", color=None, on_click=apply_editor_changes).props("outline icon=done_all no-caps")
            launch_button = ui.button("启动训练", color=None, on_click=do_launch).props("icon=play_arrow no-caps").classes("st-primary")
    else:  # lightweight test doubles may not implement layout contexts
        ui.button("解析 / 编辑参数", on_click=lambda: refresh_parameter_editor(expand=True)).props("icon=tune")
        ui.button("应用到命令", on_click=apply_editor_changes).props("icon=done_all color=secondary")
        ui.button("启动训练", on_click=do_launch).props("color=primary icon=play_arrow")
    refresh_parameter_editor(expand=False)
    return use_command
