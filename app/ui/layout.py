from __future__ import annotations

from collections.abc import Callable

from nicegui import ui


NAV_ITEMS = [
    ("Home", "/"),
    ("Projects", "/projects"),
    ("Runs", "/runs"),
    ("Servers", "/servers"),
    ("Settings", "/settings"),
]


def app_frame(title: str, content: Callable[[], None]) -> None:
    """Render the shared application frame around page content."""

    ui.page_title("gpu-ssh-panel")

    with ui.header().classes("items-center"):
        ui.label("gpu-ssh-panel").classes("text-lg font-bold")
        ui.label(title).classes("text-sm opacity-70")

    with ui.left_drawer(value=True).classes("bg-grey-1"):
        for label, target in NAV_ITEMS:
            ui.link(label, target).classes("block p-2")

    with ui.column().classes("w-full p-4 gap-4"):
        content()
