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
_expanded_servers: set[str] = set()


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


def percent_track_class(percent: Any) -> str:
    track_classes = {
        "positive": "bg-green-2",
        "warning": "bg-amber-2",
        "negative": "bg-red-2",
    }
    return track_classes[percent_color(percent)]


def format_percent(percent: Any) -> str:
    return f"{parse_percent_value(percent):.0f}%"


def gpu_average_utilization(status: ServerStatus) -> float:
    if not status.gpu:
        return 0.0
    values = [
        parse_percent_value(_mapping_value(gpu, "utilization_gpu_percent", 0))
        for gpu in status.gpu
    ]
    return round(sum(values) / len(values), 1)


def memory_used_percent(status: ServerStatus) -> float:
    memory = status.memory
    if memory is None:
        return 0.0
    total = float(_mapping_value(memory, "total_kib", 0) or 0)
    available = float(_mapping_value(memory, "available_kib", 0) or 0)
    fallback = ratio_percent(max(total - available, 0), total)
    return round(parse_percent_value(_mapping_value(memory, "used_percent", fallback)), 1)


def set_server_expanded(alias: str, expanded: bool) -> None:
    if expanded:
        _expanded_servers.add(alias)
    else:
        _expanded_servers.discard(alias)


def is_server_expanded(alias: str) -> bool:
    return alias in _expanded_servers


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


def render_labeled_bar(text: str, percent: Any) -> None:
    value = parse_percent_value(percent)
    color_classes = {
        "positive": "bg-positive",
        "warning": "bg-warning",
        "negative": "bg-negative",
    }
    color_class = color_classes[percent_color(value)]
    with ui.element("div").classes(
        "relative w-full h-6 overflow-hidden rounded bg-grey-3 border border-grey-4"
    ):
        ui.label(text).classes(
            "absolute inset-0 flex items-center justify-center text-xs font-semibold text-black z-10"
        )
        inner_text_width = 10000 / value if value > 0 else 100
        with ui.element("div").classes(
            f"absolute inset-y-0 left-0 overflow-hidden {color_class} z-20"
        ).style(f"width: {value:.0f}%"):
            ui.label(text).classes(
                "absolute inset-y-0 left-0 flex items-center justify-center text-xs font-semibold text-white"
            ).style(f"width: {inner_text_width:.4f}%")


def render_metric_bar(label: str, detail: str, percent: Any) -> None:
    render_labeled_bar(f"{label} {detail} {format_percent(percent)}", percent)


def render_thin_usage_bar(percent: Any) -> None:
    ui.linear_progress(
        _progress_value(percent),
        color=percent_color(percent),
        show_value=False,
    ).classes(f"w-full h-2 rounded-full {percent_track_class(percent)}")


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
                with ui.grid(columns=2).classes("w-full grid-cols-1 sm:grid-cols-2 gap-3"):
                    render_metric_bar("Util", "active", util)
                    render_metric_bar(
                        "Memory", f"{format_mib(used)} / {format_mib(total)}", mem_percent
                    )


def _render_cpu(status: ServerStatus) -> None:
    with ui.card().classes("w-full bg-grey-1 shadow-none border border-grey-3"):
        ui.label("CPU").classes("font-medium")
        if status.cpu_percent is None:
            ui.label("Unavailable").classes("text-grey-7")
            return
        value = parse_percent_value(status.cpu_percent)
        with ui.row().classes("items-center gap-3"):
            with ui.circular_progress(
                _progress_value(value),
                color=percent_color(value),
                show_value=False,
            ).props("size=72px"):
                ui.label(format_percent(value)).classes("absolute-center text-sm font-bold")


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


def render_collapsed_summary(status: ServerStatus) -> None:
    gpu_average = gpu_average_utilization(status)
    cpu_percent = status.cpu_percent if status.cpu_percent is not None else 0
    ram_percent = memory_used_percent(status)

    with ui.grid(columns=2).classes("w-full grid-cols-2 gap-4"):
        with ui.column().classes("w-full gap-2"):
            with ui.row().classes("items-baseline justify-between w-full"):
                ui.label("GPU").classes("text-xs uppercase text-grey-6")
                ui.label(format_percent(gpu_average)).classes("text-lg font-bold")
            if status.gpu:
                with ui.grid(columns=2).classes("w-full grid-cols-2 gap-1"):
                    for gpu in status.gpu:
                        render_thin_usage_bar(_mapping_value(gpu, "utilization_gpu_percent", 0))
            else:
                render_thin_usage_bar(0)

        with ui.column().classes("w-full gap-2"):
            with ui.row().classes("items-baseline justify-between w-full"):
                ui.label("CPU").classes("text-xs uppercase text-grey-6")
                ui.label(format_percent(cpu_percent)).classes("text-lg font-bold")
            render_thin_usage_bar(cpu_percent)
            with ui.row().classes("items-baseline justify-between w-full"):
                ui.label("RAM").classes("text-xs uppercase text-grey-6")
                ui.label(format_percent(ram_percent)).classes("text-sm font-semibold")
            render_thin_usage_bar(ram_percent)


def render_server_card(status: ServerStatus) -> None:
    state = "online" if status.online else "offline"
    badge_color = "positive" if status.online else "negative"
    card_classes = "w-full border border-grey-3 shadow-sm"
    if status.error:
        state = "error"
        badge_color = "negative"
        card_classes += " bg-red-1"

    with ui.card().classes(card_classes):
        expansion = ui.expansion(value=is_server_expanded(status.alias)).classes("w-full")
        expansion.on(
            "update:model-value",
            lambda event, alias=status.alias: set_server_expanded(alias, bool(event.args)),
        )
        with expansion.add_slot("header"):
            with ui.column().classes("w-full gap-3 cursor-pointer"):
                with ui.row().classes("items-start justify-between w-full gap-3"):
                    with ui.column().classes("gap-1"):
                        ui.label(status.alias).classes("text-xl font-bold")
                        if status.hostname:
                            ui.label(f"hostname: {status.hostname}").classes("text-grey-7")
                    with ui.row().classes("items-center gap-2"):
                        ui.badge(state, color=badge_color).classes("uppercase")
                        ui.icon("expand_more").classes("text-grey-6")
                render_collapsed_summary(status)

        with expansion:
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
