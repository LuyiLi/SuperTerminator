from __future__ import annotations

from nicegui import ui


def empty_state(message: str) -> None:
    """Render a simple empty-state card."""

    with ui.card().classes("w-full items-center p-8 text-grey-7"):
        ui.label(message)


def error_label(message: str) -> None:
    """Render a negative label for errors."""

    ui.label(message).classes("text-negative")
