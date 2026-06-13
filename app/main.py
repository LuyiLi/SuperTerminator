from __future__ import annotations

from nicegui import ui

from app.config import load_settings
from app.db import init_db
from app.ui.dashboard import render_dashboard_page
from app.ui.projects import render_project_detail, render_projects_page
from app.ui.layout import app_frame
from app.ui.runs import render_run_detail, render_runs_page
from app.ui.servers import render_servers_page


settings = load_settings()


@ui.page("/")
def home_page() -> None:
    app_frame("Home", render_dashboard_page)


@ui.page("/projects")
def projects_page() -> None:
    app_frame("Projects", render_projects_page)


@ui.page("/projects/{project_id}")
def project_detail_page(project_id: int) -> None:
    app_frame("Project", lambda: render_project_detail(int(project_id)))


@ui.page("/runs")
def runs_page() -> None:
    app_frame("Runs", render_runs_page)


@ui.page("/runs/{run_id}")
def run_detail_page(run_id: int) -> None:
    app_frame("Run", lambda: render_run_detail(int(run_id)))


@ui.page("/servers")
def servers_page() -> None:
    app_frame("Servers", render_servers_page)


@ui.page("/settings")
def settings_page() -> None:
    def content() -> None:
        ui.label(f"Database: {settings.db_path}")
        ui.label(f"Refresh interval: {settings.refresh_seconds}s")
        ui.label(f"Show debug terminal: {settings.show_debug_terminal}")

    app_frame("Settings", content)


def main() -> None:
    init_db()
    ui.run(host=settings.host, port=settings.port, reload=settings.reload, title="gpu-ssh-panel")


if __name__ in {"__main__", "__mp_main__"}:
    main()
