from __future__ import annotations

from collections.abc import Callable

from nicegui import ui


NAV_ITEMS = [
    ("Home", "/", "home"),
    ("Projects", "/projects", "folder_open"),
    ("Runs", "/runs", "terminal"),
    ("Servers", "/servers", "dns"),
    ("Settings", "/settings", "settings"),
]


def _current_path() -> str:
    try:
        return ui.context.client.page.path
    except Exception:
        return "/"


def _is_active(target: str, current_path: str) -> bool:
    if target == "/":
        return current_path == "/"
    return current_path == target or current_path.startswith(f"{target}/")


def _nav_link_classes(active: bool) -> str:
    base = "flex items-center gap-3 px-3 py-2 rounded-xl no-underline transition-all"
    if active:
        return f"{base} bg-primary text-white shadow-sm"
    return f"{base} text-grey-8 hover:bg-blue-1 hover:text-primary"


def app_frame(title: str, content: Callable[[], None]) -> None:
    """Render the shared application frame around page content."""

    ui.page_title("gpu-ssh-panel")

    with ui.header().classes("items-center bg-white text-grey-9 shadow-sm border-b border-grey-3"):
        ui.label("gpu-ssh-panel").classes("text-lg font-bold")
        ui.label(title).classes("text-sm opacity-70")

    current_path = _current_path()
    with ui.left_drawer(value=True).classes("bg-grey-1 border-r border-grey-3"):
        with ui.column().classes("w-full h-full gap-4 p-3"):
            with ui.card().classes("w-full bg-grey-10 text-white shadow-none rounded-2xl p-4"):
                with ui.row().classes("items-center gap-3"):
                    ui.icon("memory").classes("text-cyan-3 text-3xl")
                    with ui.column().classes("gap-0"):
                        ui.label("GPU SSH Panel").classes("text-base font-bold")
                        ui.label("Local SuperTerminal").classes("text-xs text-grey-4")

            with ui.column().classes("w-full gap-1"):
                for label, target, icon in NAV_ITEMS:
                    active = _is_active(target, current_path)
                    with ui.link(target=target).classes(_nav_link_classes(active)):
                        ui.icon(icon).classes("text-lg")
                        ui.label(label).classes("font-medium")

    with ui.column().classes("w-full p-4 gap-4"):
        content()
