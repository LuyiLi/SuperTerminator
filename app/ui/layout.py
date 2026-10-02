from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from nicegui import ui

from app.db import engine, session_scope
from app.models import Project


NAV_ITEMS = [
    ("Home", "/", "home"),
    ("Projects", "/projects", "folder_open"),
    ("Runs", "/runs", "terminal"),
    ("Servers", "/servers", "dns"),
    ("Settings", "/settings", "settings"),
]

NAV_LABELS = {
    "Home": "总览",
    "Projects": "项目",
    "Project": "项目",
    "Runs": "运行",
    "Run": "运行详情",
    "Servers": "机器",
    "Settings": "设置",
}

# Shared tokens also style the dashboard and the server cards inside projects.
SHELL_CSS = """
:root {
    --st-bg: #f7f8fa;
    --st-panel: #ffffff;
    --st-sidebar: #f0f2f5;
    --st-ink: #1d2534;
    --st-muted: #687385;
    --st-line: #e4e8ee;
    --st-accent: #4964ce;
    --st-soft: #edf0fc;
    --st-ok: #237b61;
    --st-ok-bg: #ebf6f0;
    --st-warn: #9b6b23;
    --st-warn-bg: #fbf3e6;
    --st-danger: #b54747;
    --st-radius: 12px;
    --st-sidebar-width: 176px;
    --q-primary: var(--st-accent);
    --q-secondary: #62758e;
}
body {
    background: var(--st-bg);
    color: var(--st-ink);
    font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans SC', sans-serif;
    -webkit-font-smoothing: antialiased;
}
.nicegui-content:has(> .st-main) {
    padding: 0;
    gap: 0;
}
.st-main {
    width: 100%;
    min-width: 0;
    padding: 24px;
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 16px;
    font-size: 13px;
    line-height: 1.5;
}
.st-main > .text-2xl {
    color: var(--st-ink);
    font-size: 25px;
    font-weight: 600;
    letter-spacing: -.6px;
}
.st-main .q-card {
    background: var(--st-panel);
    border: 1px solid var(--st-line);
    border-radius: var(--st-radius);
    box-shadow: none;
}
.st-main .q-btn {
    border-radius: 7px;
    font-size: 12px;
    font-weight: 500;
    text-transform: none;
}
.st-main .q-field {
    font-size: 13px;
}
.st-main .q-expansion-item__container > .q-item {
    border-radius: 7px;
}
.st-sidebar {
    padding: 0;
    background: var(--st-sidebar);
    border-right: 1px solid var(--st-line);
    color: var(--st-ink);
}
.st-sidebar-inner {
    width: 100%;
    min-height: 100%;
    padding: 24px 11px 16px;
    display: flex;
    flex-direction: column;
    gap: 25px;
}
.st-brand {
    display: flex;
    align-items: center;
    flex-wrap: nowrap;
    gap: 8px;
    padding: 0 1px;
    min-width: 0;
}
.st-brand-mark {
    width: 25px;
    height: 25px;
    flex-shrink: 0;
    display: grid;
    place-items: center;
    background: var(--st-ink);
    color: white;
    border-radius: 7px;
}
.st-brand-mark .q-icon { font-size: 16px; }
.st-brand-name {
    font-size: 13px;
    font-weight: 650;
    letter-spacing: -.5px;
    white-space: nowrap;
}
.st-nav {
    display: flex;
    flex-direction: column;
    gap: 5px;
    width: 100%;
}
.st-nav-link {
    display: flex;
    align-items: center;
    gap: 10px;
    min-height: 38px;
    min-width: 0;
    padding: 9px 10px;
    border-radius: 7px;
    color: var(--st-muted);
    text-decoration: none;
    font-size: 12px;
    line-height: 1.45;
}
.st-nav-link > .q-icon { font-size: 17px; flex-shrink: 0; }
.st-nav-link:hover {
    color: var(--st-accent);
    background: var(--st-soft);
}
.st-nav-link.st-nav-active {
    background: var(--st-panel);
    color: var(--st-ink);
    font-weight: 600;
    box-shadow: 0 1px 3px #1c29470a;
}
.st-nav-link.st-nav-active > .q-icon { color: var(--st-accent); }
.st-nav-link.st-nav-parent-active { color: var(--st-ink); }
.st-project-section { width: 100%; gap: 8px; }
.st-nav-label {
    padding: 0 10px;
    color: var(--st-muted);
    font-size: 10px;
    line-height: 1.5;
    letter-spacing: 1.5px;
}
.st-project-links {
    width: 100%;
    display: flex;
    flex-direction: column;
    gap: 3px;
}
.st-project-link { gap: 10px; padding: 7px 10px; min-height: 34px; font-size: 11px; }
.st-project-link > .q-icon { font-size: 14px; }
.st-project-name { min-width: 0; overflow-wrap: anywhere; }
.st-project-empty { padding: 7px 8px; color: var(--st-muted); font-size: 11px; }
.st-sidebar-footer {
    margin-top: auto;
    padding: 12px 9px 0;
    color: var(--st-muted);
    font-size: 11px;
    display: flex;
    align-items: center;
    gap: 7px;
}
.st-sidebar-footer .q-icon { font-size: 14px; }
.st-mobile-header, .st-mobile-only { display: none; }
.st-skip-link {
    position: fixed;
    z-index: 10000;
    top: 8px;
    left: 8px;
    transform: translateY(-200%);
    padding: 10px 14px;
    background: var(--st-panel);
    color: var(--st-ink);
    border: 1px solid var(--st-line);
    border-radius: 7px;
}
.st-skip-link:focus { transform: translateY(0); }
.st-nav-link:focus-visible, .st-skip-link:focus-visible,
.st-mobile-header .q-btn:focus-visible, .st-sidebar .q-btn:focus-visible {
    outline: 2px solid var(--st-accent);
    outline-offset: 2px;
}
@media (max-width: 1100px) {
    .st-main { padding: 20px; }
}
@media (max-width: 767px) {
    .st-mobile-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        min-height: 52px;
        padding: 4px 12px;
        background: var(--st-panel);
        color: var(--st-ink);
        border-bottom: 1px solid var(--st-line);
        box-shadow: none;
    }
    .st-mobile-heading { display: flex; align-items: center; gap: 7px; }
    .st-mobile-page { color: var(--st-muted); font-size: 12px; }
    .st-mobile-only { display: inline-flex; }
    .st-sidebar-inner { padding-top: 18px; gap: 18px; }
    .st-sidebar-close { align-self: flex-end; margin: -8px -2px -12px 0; }
    .st-main { padding: 18px 14px; gap: 14px; }
    .st-nav-link { min-height: 44px; }
    .st-project-link { min-height: 40px; }
}
"""


def _sidebar_projects() -> list[tuple[int, str]]:
    """Return project links for the sidebar.

    Keep the frame resilient if DB is unavailable.
    """

    try:
        with session_scope(engine) as session:
            projects = session.query(Project.id, Project.name).order_by(Project.name).all()
            return [(int(project_id), str(name)) for project_id, name in projects]
    except Exception:
        return []


def _current_path() -> str:
    try:
        client = ui.context.client
        if client.request is not None:
            return client.request.url.path
        return client.page.path
    except Exception:
        return "/"


def _is_active(target: str, current_path: str) -> bool:
    if target == "/":
        return current_path == "/"
    return current_path == target or current_path.startswith(f"{target}/")


def _nav_link_classes(active: bool) -> str:
    return "st-nav-link st-nav-active" if active else "st-nav-link"


def _nav_child_link_classes(active: bool) -> str:
    return f"{_nav_link_classes(active)} st-project-link"


def _render_project_shortcuts(current_path: str) -> None:
    with ui.element("nav").props('aria-label="项目快捷入口"').classes("st-project-links"):
        projects = _sidebar_projects()
        if not projects:
            ui.label("暂无项目").classes("st-project-empty")
            return

        for project_id, name in projects:
            target = f"/projects/{project_id}"
            active = current_path == target
            with ui.link(target=target).classes(_nav_child_link_classes(active)) as link:
                if active:
                    link.props("aria-current=page")
                ui.icon("folder")
                ui.label(name).classes("st-project-name")


def _render_nav_item(label: str, target: str, icon: str, current_path: str) -> None:
    active = _is_active(target, current_path)
    project_parent = target == "/projects" and current_path != target and active
    classes = _nav_link_classes(active and not project_parent)
    if project_parent:
        classes += " st-nav-parent-active"
    with ui.link(target=target).classes(classes) as link:
        if active and not project_parent:
            link.props("aria-current=page")
        ui.icon(icon)
        ui.label(NAV_LABELS.get(label, label))


def app_frame(title: str, content: Callable[[], None]) -> None:
    """Render the shared shell, with an automatic drawer and a mobile menu."""

    ui.page_title("SuperTerminator")
    ui.add_css(SHELL_CSS)
    ui.add_css(Path(__file__).with_name("dashboard.css"))
    ui.add_css(Path(__file__).with_name("operations.css"))

    with ui.element("a").props("href=#st-main").classes("st-skip-link"):
        ui.label("跳转到内容")

    current_path = _current_path()
    with ui.left_drawer(value=None).props(
        "width=176 breakpoint=767 no-swipe-open no-swipe-close no-swipe-backdrop"
    ).classes("st-sidebar") as drawer:
        with ui.column().classes("st-sidebar-inner"):
            ui.button(icon="close", on_click=drawer.hide, color=None).props(
                'flat round dense aria-label="关闭导航"'
            ).classes("st-mobile-only st-sidebar-close")
            with ui.row().classes("st-brand"):
                with ui.element("div").classes("st-brand-mark"):
                    ui.icon("terminal")
                ui.label("SuperTerminator").classes("st-brand-name")

            with ui.element("nav").props('id=st-navigation aria-label="主导航"').classes("st-nav"):
                for label, target, icon in NAV_ITEMS:
                    _render_nav_item(label, target, icon, current_path)

            with ui.column().classes("st-project-section"):
                ui.label("PROJECTS").classes("st-nav-label")
                _render_project_shortcuts(current_path)

            with ui.row().classes("st-sidebar-footer"):
                ui.icon("computer")
                ui.label("本地工作区")

    with ui.header().classes("st-mobile-header"):
        with ui.row().classes("st-mobile-heading"):
            ui.button(icon="menu", on_click=drawer.toggle, color=None).props(
                'flat round dense aria-label="打开导航" aria-controls=st-navigation'
            )
            ui.label("SuperTerminator").classes("st-brand-name")
        ui.label(NAV_LABELS.get(title, title)).classes("st-mobile-page")

    with ui.element("main").props("id=st-main tabindex=-1").classes("st-shell-page st-main"):
        content()
