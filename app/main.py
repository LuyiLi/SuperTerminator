from __future__ import annotations

from nicegui import ui

from app.config import load_settings
from app.db import init_db
from app.ui.components import empty_state
from app.ui.layout import app_frame


settings = load_settings()


@ui.page("/")
def home_page() -> None:
    app_frame("Home", lambda: empty_state("Home page placeholder."))


@ui.page("/projects")
def projects_page() -> None:
    app_frame("Projects", lambda: empty_state("Projects page placeholder."))


@ui.page("/runs")
def runs_page() -> None:
    app_frame("Runs", lambda: empty_state("Runs page placeholder."))


@ui.page("/servers")
def servers_page() -> None:
    app_frame("Servers", lambda: empty_state("Servers page placeholder."))


@ui.page("/settings")
def settings_page() -> None:
    def content() -> None:
        ui.label(f"Database path: {settings.db_path}")
        ui.label(f"Refresh interval: {settings.refresh_seconds} seconds")
        debug_terminal = "enabled" if settings.show_debug_terminal else "disabled"
        ui.label(f"Debug terminal: {debug_terminal}")

    app_frame("Settings", content)


def main() -> None:
    init_db()
    ui.run(host=settings.host, port=settings.port, reload=settings.reload, title="gpu-ssh-panel")


if __name__ in {"__main__", "__mp_main__"}:
    main()
