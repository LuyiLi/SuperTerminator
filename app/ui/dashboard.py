from __future__ import annotations

import asyncio
from typing import Any

from nicegui import ui
from sqlalchemy import Engine

from app.config import load_settings
from app.db import engine, session_scope
from app.models import Server
from app.schemas import ServerStatus
from app.ssh_client import SSHClient
from app.visual_actions import collect_server_status

settings = load_settings()


class DashboardRefreshGuard:
    """Coordinate refreshes so older results cannot overwrite newer requests."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self._generation = 0

    def next_generation(self) -> int:
        self._generation += 1
        return self._generation

    def is_current(self, generation: int) -> bool:
        return generation == self._generation


def load_enabled_server_aliases(*, target_engine: Engine = engine) -> list[str]:
    """Return enabled server aliases in stable display order."""

    with session_scope(target_engine) as session:
        return [
            alias
            for (alias,) in session.query(Server.alias)
            .filter(Server.enabled.is_(True))
            .order_by(Server.alias)
            .all()
        ]


def _mapping_value(mapping: Any, key: str, default: Any = "") -> Any:
    if mapping is None:
        return default
    return mapping.get(key, default)


def _memory_used_text(status: ServerStatus) -> str:
    memory = status.memory
    if memory is None:
        return "RAM: unavailable"
    total_kib = float(_mapping_value(memory, "total_kib", 0) or 0)
    available_kib = float(_mapping_value(memory, "available_kib", 0) or 0)
    used_percent = _mapping_value(memory, "used_percent", None)
    if total_kib <= 0:
        return f"RAM: {used_percent}% used" if used_percent is not None else "RAM: unavailable"
    used_gib = (total_kib - available_kib) / 1024 / 1024
    total_gib = total_kib / 1024 / 1024
    return f"RAM: {used_gib:.1f}/{total_gib:.1f} GiB used ({used_percent}%)"


def _render_status_card(status: ServerStatus) -> None:
    state = "online" if status.online else "offline"
    badge_class = "text-positive" if status.online else "text-negative"
    if status.error:
        state = "error"

    with ui.card().classes("w-full"):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label(status.alias).classes("text-lg font-semibold")
            ui.label(state).classes(badge_class)
        if status.error:
            ui.label(status.error).classes("text-negative")
        if status.hostname:
            ui.label(f"Hostname: {status.hostname}")
        if status.cpu_percent is not None:
            ui.label(f"CPU: {status.cpu_percent}%")
        ui.label(_memory_used_text(status))

        ui.label("GPUs").classes("font-semibold")
        if status.gpu:
            for gpu in status.gpu:
                name = _mapping_value(gpu, "name", "GPU")
                used = _mapping_value(gpu, "memory_used_mib", "?")
                total = _mapping_value(gpu, "memory_total_mib", "?")
                util = _mapping_value(gpu, "utilization_gpu_percent", "?")
                ui.label(f"{name}: {used}/{total} MiB, {util}% util")
        else:
            ui.label("No GPU metrics.").classes("text-grey-7")

        ui.label("Disks").classes("font-semibold")
        if status.disks:
            for disk in status.disks[:5]:
                mount = _mapping_value(disk, "mount", "")
                used = _mapping_value(disk, "used", "?")
                size = _mapping_value(disk, "size", "?")
                avail = _mapping_value(disk, "avail", "?")
                use_percent = _mapping_value(disk, "use_percent", "?")
                ui.label(f"{mount}: {used}/{size} used, {avail} avail ({use_percent})")
        else:
            ui.label("No disk metrics.").classes("text-grey-7")


def render_dashboard_page() -> None:
    """Render the home dashboard with periodically refreshed server status."""

    ui.label("Home Dashboard").classes("text-2xl font-bold")
    ui.label(f"Auto-refreshes every {settings.refresh_seconds}s.").classes("text-grey-7")

    container = ui.column().classes("w-full gap-4")
    refresh_guard = DashboardRefreshGuard()

    def render_empty_state() -> None:
        container.clear()
        with container:
            with ui.card().classes("w-full"):
                ui.label("No enabled servers. Add one on the Servers page.").classes(
                    "text-grey-7"
                )

    def render_results(aliases: list[str], results: list[ServerStatus | BaseException]) -> None:
        container.clear()
        with container:
            for alias, result in zip(aliases, results, strict=True):
                if isinstance(result, BaseException):
                    status = ServerStatus(alias=alias, online=False, error=str(result))
                else:
                    status = result
                _render_status_card(status)

    async def refresh() -> None:
        generation = refresh_guard.next_generation()
        async with refresh_guard.lock:
            aliases = load_enabled_server_aliases()
            if not aliases:
                if refresh_guard.is_current(generation):
                    render_empty_state()
                return

            results = await asyncio.gather(
                *(collect_server_status(alias, SSHClient()) for alias in aliases),
                return_exceptions=True,
            )
            if refresh_guard.is_current(generation):
                render_results(aliases, results)

    ui.button("Manual Refresh", on_click=refresh)
    ui.timer(settings.refresh_seconds, refresh)
    ui.timer(0.1, refresh, once=True)
