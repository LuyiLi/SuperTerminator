from __future__ import annotations

import asyncio
import re
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


def parse_percent_value(value: Any) -> float:
    if isinstance(value, int | float):
        return max(0.0, min(float(value), 100.0))
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    if not match:
        return 0.0
    return max(0.0, min(float(match.group(0)), 100.0))


def ratio_percent(used: Any, total: Any) -> float:
    try:
        used_float = float(used)
        total_float = float(total)
    except (TypeError, ValueError):
        return 0.0
    if total_float <= 0:
        return 0.0
    return round(max(0.0, min((used_float / total_float) * 100.0, 100.0)), 1)


def percent_color(percent: Any) -> str:
    value = parse_percent_value(percent)
    if value >= 85:
        return "negative"
    if value >= 60:
        return "warning"
    return "positive"


def format_mib(value: Any) -> str:
    try:
        mib = float(value)
    except (TypeError, ValueError):
        return "?"
    if mib >= 1024:
        return f"{mib / 1024:.1f} GiB"
    return f"{mib:.0f} MiB"


def format_kib(value: Any) -> str:
    try:
        kib = float(value)
    except (TypeError, ValueError):
        return "?"
    gib = kib / 1024 / 1024
    if gib >= 1:
        return f"{gib:.1f} GiB"
    return f"{kib / 1024:.0f} MiB"


def _progress_value(percent: Any) -> float:
    return parse_percent_value(percent) / 100.0


def render_metric_bar(label: str, detail: str, percent: Any) -> None:
    value = parse_percent_value(percent)
    with ui.column().classes("w-full gap-1"):
        with ui.row().classes("items-center justify-between w-full text-sm"):
            ui.label(label).classes("font-medium")
            ui.label(f"{detail} · {value:.0f}%").classes("text-grey-7")
        ui.linear_progress(_progress_value(value), color=percent_color(value)).classes("w-full")


def render_gpu_panel(status: ServerStatus) -> None:
    with ui.column().classes("w-full gap-3"):
        ui.label("GPU").classes("text-lg font-semibold")
        if not status.gpu:
            ui.label("No GPU metrics available.").classes("text-grey-7")
            return
        for index, gpu in enumerate(status.gpu):
            name = _mapping_value(gpu, "name", f"GPU {index}")
            util = parse_percent_value(_mapping_value(gpu, "utilization_gpu_percent", 0))
            used = _mapping_value(gpu, "memory_used_mib", 0)
            total = _mapping_value(gpu, "memory_total_mib", 0)
            mem_percent = ratio_percent(used, total)
            with ui.card().classes("w-full bg-grey-1 shadow-none border border-grey-3"):
                ui.label(f"GPU {index} · {name}").classes("font-medium")
                render_metric_bar("Util", "active", util)
                render_metric_bar("Memory", f"{format_mib(used)} / {format_mib(total)}", mem_percent)


def _render_cpu(status: ServerStatus) -> None:
    with ui.card().classes("w-full bg-grey-1 shadow-none border border-grey-3"):
        ui.label("CPU").classes("font-medium")
        if status.cpu_percent is None:
            ui.label("Unavailable").classes("text-grey-7")
            return
        value = parse_percent_value(status.cpu_percent)
        with ui.row().classes("items-center gap-3"):
            ui.circular_progress(_progress_value(value), color=percent_color(value), show_value=True).props(
                "size=72px"
            )
            ui.label(f"{value:.0f}% used").classes("text-lg font-semibold")


def _render_memory(status: ServerStatus) -> None:
    memory = status.memory
    if memory is None:
        render_metric_bar("RAM", "unavailable", 0)
        return
    total = float(_mapping_value(memory, "total_kib", 0) or 0)
    available = float(_mapping_value(memory, "available_kib", 0) or 0)
    used = max(total - available, 0)
    percent = parse_percent_value(_mapping_value(memory, "used_percent", ratio_percent(used, total)))
    render_metric_bar("RAM", f"{format_kib(used)} / {format_kib(total)}", percent)


def _render_disks(status: ServerStatus) -> None:
    ui.label("Disk").classes("font-medium")
    if not status.disks:
        ui.label("No disk metrics available.").classes("text-grey-7")
        return
    for disk in status.disks[:5]:
        mount = _mapping_value(disk, "mount", "disk")
        used = _mapping_value(disk, "used", "?")
        size = _mapping_value(disk, "size", "?")
        percent = parse_percent_value(_mapping_value(disk, "use_percent", 0))
        render_metric_bar(str(mount), f"{used} / {size}", percent)


def render_system_panel(status: ServerStatus) -> None:
    with ui.column().classes("w-full gap-3"):
        ui.label("System").classes("text-lg font-semibold")
        _render_cpu(status)
        with ui.card().classes("w-full bg-grey-1 shadow-none border border-grey-3"):
            _render_memory(status)
        with ui.card().classes("w-full bg-grey-1 shadow-none border border-grey-3"):
            _render_disks(status)


def render_server_card(status: ServerStatus) -> None:
    state = "online" if status.online else "offline"
    badge_color = "positive" if status.online else "negative"
    card_classes = "w-full border border-grey-3 shadow-sm"
    if status.error:
        state = "error"
        badge_color = "negative"
        card_classes += " bg-red-1"

    with ui.card().classes(card_classes):
        with ui.row().classes("items-start justify-between w-full gap-3"):
            with ui.column().classes("gap-1"):
                ui.label(status.alias).classes("text-xl font-bold")
                if status.hostname:
                    ui.label(f"hostname: {status.hostname}").classes("text-grey-7")
            ui.badge(state, color=badge_color).classes("uppercase")

        if status.error:
            ui.label(status.error).classes("text-negative font-medium")

        with ui.grid(columns=2).classes("w-full grid-cols-1 xl:grid-cols-2 gap-4"):
            render_gpu_panel(status)
            render_system_panel(status)


def render_dashboard_page() -> None:
    """Render the home dashboard with periodically refreshed server status."""

    with ui.row().classes("items-center justify-between w-full"):
        with ui.column().classes("gap-1"):
            ui.label("Home Dashboard").classes("text-2xl font-bold")
            ui.label(f"Auto-refreshes every {settings.refresh_seconds}s.").classes("text-grey-7")

    container = ui.column().classes("w-full grid grid-cols-1 lg:grid-cols-2 gap-4")
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
                render_server_card(status)

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

    ui.button("Refresh now", on_click=refresh).props("icon=refresh")
    ui.timer(settings.refresh_seconds, refresh)
    ui.timer(0.1, refresh, once=True)
