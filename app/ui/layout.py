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

    ui.page_title(title)

    with ui.header().classes("items-center gap-4"):
        ui.label("gpu-ssh-panel").classes("text-h6 font-bold")
        ui.label(title).classes("text-subtitle1")

    with ui.left_drawer().classes("bg-grey-1"):
        with ui.column().classes("w-full gap-1 p-2"):
            for label, target in NAV_ITEMS:
                ui.link(label, target).classes("w-full p-2 rounded hover:bg-grey-3")

    with ui.column().classes("w-full max-w-screen-lg gap-4 p-6"):
        content()
