from __future__ import annotations

import asyncio
import json
import math
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
from app.ui.scoped_timer import create_scoped_timer

settings = load_settings()
_expanded_servers: set[str] = set()
_status_cache: dict[str, ServerStatus] = {}


class DashboardRefreshGuard:
    """Coordinate refreshes so older results cannot overwrite newer requests."""

    def __init__(self) -> None:
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


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def metric_percent(value: Any) -> float | None:
    """Unlike the legacy formatters, keep missing readings distinct from zero."""
    if isinstance(value, str):
        value = value.strip().removesuffix("%").strip()
    number = _finite_number(value)
    return None if number is None else max(0.0, min(number, 100.0))


def metric_ratio(used: Any, total: Any) -> float | None:
    numerator, denominator = _finite_number(used), _finite_number(total)
    if numerator is None or denominator is None or denominator <= 0 or numerator < 0:
        return None
    return metric_percent(numerator / denominator * 100)


def gpu_metric_values(gpu: Any) -> tuple[float | None, float | None]:
    return (
        metric_percent(_mapping_value(gpu, "utilization_gpu_percent", None)),
        metric_ratio(
            _mapping_value(gpu, "memory_used_mib", None),
            _mapping_value(gpu, "memory_total_mib", None),
        ),
    )


def _metric_text(value: float | None) -> str:
    return "未知" if value is None else f"{value:g}%"


def _gpu_memory_text(gpu: Any) -> str:
    used = _mapping_value(gpu, "memory_used_mib", None)
    total = _mapping_value(gpu, "memory_total_mib", None)
    if metric_ratio(used, total) is None:
        return "未知"
    return f"{format_mib(used)} / {format_mib(total)}"


def gpu_spec(status: ServerStatus) -> str:
    if not status.online:
        return "离线 · 连接失败" if status.error else "离线"
    if not status.gpu:
        return "暂无 GPU 指标"
    names = {str(_mapping_value(gpu, "name", "GPU")) for gpu in status.gpu}
    capacities = {_mapping_value(gpu, "memory_total_mib", None) for gpu in status.gpu}
    if len(names) == 1:
        model = next(iter(names)).removeprefix("NVIDIA ").removeprefix("GeForce ")
        spec = f"{len(status.gpu)} × {model}"
    else:
        spec = f"{len(status.gpu)} 张 GPU · 混合型号"
    if len(capacities) == 1:
        capacity = _finite_number(next(iter(capacities)))
        if capacity is not None and capacity > 0:
            spec += f" · {format_mib(capacity)} / 卡"
    return spec


def system_metrics(status: ServerStatus) -> list[tuple[str, float | None, str]]:
    if not status.online:
        return [(label, None, "指标未知") for label in ("CPU", "RAM", "磁盘")]
    cpu = metric_percent(status.cpu_percent)
    memory = status.memory
    ram = metric_percent(_mapping_value(memory, "used_percent", None))
    total = _finite_number(_mapping_value(memory, "total_kib", None))
    available = _finite_number(_mapping_value(memory, "available_kib", None))
    ram_detail = "未知"
    if total is not None and total > 0 and available is not None:
        used = max(0.0, total - available)
        if ram is None:
            ram = metric_ratio(used, total)
        ram_detail = f"{format_kib(used)} / {format_kib(total)} · {_metric_text(ram)}"
    elif ram is not None:
        ram_detail = _metric_text(ram)
    disk_readings = [
        (metric_percent(_mapping_value(disk, "use_percent", None)), disk)
        for disk in status.disks
    ]
    known_disks = [(value, disk) for value, disk in disk_readings if value is not None]
    if known_disks:
        disk_percent, fullest = max(known_disks, key=lambda reading: reading[0])
        disk_detail = (
            f"最高占用 {_mapping_value(fullest, 'mount', 'disk')} · "
            f"{_mapping_value(fullest, 'used', '?')} / "
            f"{_mapping_value(fullest, 'size', '?')} · {_metric_text(disk_percent)}"
        )
    else:
        disk_percent, disk_detail = None, "未知"
    return [("CPU", cpu, _metric_text(cpu)), ("RAM", ram, ram_detail),
            ("磁盘", disk_percent, disk_detail)]


def _label_props(text: str) -> str:
    return f"aria-label={json.dumps(text, ensure_ascii=False)}"


def _render_chart_slot(value: float | None, series: str) -> None:
    classes = f"st-gpu-slot st-series-{series}"
    if value is None:
        classes += " st-metric-unknown"
    with ui.element("span").classes(classes).props('aria-hidden="true"'):
        if value is not None:
            ui.element("i").style(f"height:{value:.3f}%")


def _render_system_summary(status: ServerStatus) -> None:
    with ui.element("div").classes("st-system-summary"):
        for label, percent, detail in system_metrics(status):
            with ui.element("button").classes("st-system-metric").props(
                f'type="button" {_label_props(f"{label}：{detail}，查看系统详情")}'
            ).on("click", lambda: open_server_details(status)):
                ui.label(label)
                if percent is None:
                    ui.label("—").classes("st-metric-unavailable")
                else:
                    with ui.element("span").classes("st-system-track").props(
                        'aria-hidden="true"'
                    ):
                        ui.element("i").style(f"width:{percent:.3f}%")
                ui.tooltip(f"{label} · {detail}").classes("st-resource-tooltip")


def render_collapsed_summary(status: ServerStatus) -> None:
    if not status.online or not status.gpu:
        with ui.element("div").classes("st-resource-unavailable"):
            ui.icon("cloud_off" if not status.online else "memory")
            ui.label("暂未获取指标" if not status.online else "暂无 GPU 指标")
    else:
        # Each native button is one device, with both measurements aligned vertically.
        # Additional GPUs wrap in groups of eight; neither count nor capacity is assumed.
        with ui.element("div").classes("st-gpu-charts"):
            for start in range(0, len(status.gpu), 8):
                group = status.gpu[start:start + 8]
                with ui.element("div").classes("st-gpu-chart-row"):
                    with ui.element("div").classes("st-gpu-axis").props('aria-hidden="true"'):
                        ui.label("计算")
                        ui.label("显存")
                    with ui.element("div").classes("st-gpu-columns").style(
                        f"--st-gpu-count:{len(group)}"
                    ):
                        for index, gpu in enumerate(group, start):
                            util, memory = gpu_metric_values(gpu)
                            description = (
                                f"GPU {index} · 计算 {_metric_text(util)} · "
                                f"显存 {_gpu_memory_text(gpu)}"
                            )
                            with ui.element("button").classes("st-gpu-glyph").props(
                                f'type="button" {_label_props(description)}'
                            ).on("click", lambda i=index: open_server_details(status, i)):
                                _render_chart_slot(util, "compute")
                                _render_chart_slot(memory, "memory")
                                ui.tooltip(description).classes("st-resource-tooltip")
    _render_system_summary(status)


def _render_detail_reading(text: str, percent: float | None, series: str) -> None:
    with ui.element("div").classes(f"st-detail-reading st-series-{series}"):
        ui.label(text)
        if percent is not None:
            with ui.element("span").classes("st-system-track").props('aria-hidden="true"'):
                ui.element("i").style(f"width:{percent:.3f}%")


def render_server_details(status: ServerStatus, gpu_index: int | None = None) -> None:
    """Keep exact hardware and system readings available without crowding the overview."""
    if status.error:
        ui.label(status.error).classes("st-resource-error")
    if status.online and status.gpu:
        models = {str(_mapping_value(gpu, "name", "GPU")) for gpu in status.gpu}
        mixed_models = len(models) > 1
        with ui.element("div").classes("st-detail-table-wrap"):
            with ui.element("table").classes("st-detail-table"):
                with ui.element("thead"):
                    with ui.element("tr"):
                        for label in ("GPU / 型号" if mixed_models else "GPU", "计算利用率", "显存占用"):
                            with ui.element("th").props('scope="col"'):
                                ui.label(label)
                with ui.element("tbody"):
                    for index, gpu in enumerate(status.gpu):
                        util, memory = gpu_metric_values(gpu)
                        with ui.element("tr").classes(
                            "st-detail-selected" if index == gpu_index else ""
                        ):
                            with ui.element("th").props('scope="row"'):
                                ui.label(f"GPU {index}")
                                if mixed_models:
                                    ui.label(str(_mapping_value(gpu, "name", "GPU"))).classes(
                                        "st-detail-model"
                                    )
                            with ui.element("td"):
                                _render_detail_reading(_metric_text(util), util, "compute")
                            with ui.element("td"):
                                _render_detail_reading(_gpu_memory_text(gpu), memory, "memory")
    else:
        ui.label("当前 GPU 指标未知").classes("st-resource-unavailable")

    metrics = system_metrics(status)
    util_values = [gpu_metric_values(gpu)[0] for gpu in status.gpu]
    # A partial sample is not a trustworthy whole-machine average.
    average = (
        sum(util_values) / len(util_values)
        if status.online and util_values and all(v is not None for v in util_values)
        else None
    )
    fields = [
        ("SSH alias", status.alias),
        ("主机名", status.hostname or "未知"),
        ("连接状态", "在线" if status.online else "离线"),
        ("GPU 型号", " / ".join(dict.fromkeys(
            str(_mapping_value(gpu, "name", "GPU")) for gpu in status.gpu
        )) if status.online and status.gpu else "未知"),
        ("GPU 平均利用率", _metric_text(average)),
        ("CPU", metrics[0][2]),
        ("内存", metrics[1][2]),
    ]
    if status.online and status.disks:
        fields.extend(
            (
                f"磁盘 {_mapping_value(disk, 'mount', 'disk')}",
                f"{_mapping_value(disk, 'used', '?')} / "
                f"{_mapping_value(disk, 'size', '?')} · "
                f"{_metric_text(metric_percent(_mapping_value(disk, 'use_percent', None)))}",
            )
            for disk in status.disks
        )
    else:
        fields.append(("磁盘", "未知"))
    with ui.element("dl").classes("st-detail-fields"):
        for label, text in fields:
            with ui.element("dt"):
                ui.label(label)
            with ui.element("dd"):
                ui.label(text)


def open_server_details(status: ServerStatus, gpu_index: int | None = None) -> None:
    # Attach to the page, not the refreshed card, so polling cannot close a user's dialog.
    with ui.context.client.content:
        owner = ui.element("div").style("display:none")
    # NiceGUI adds a hidden lifetime canary beside each dialog. Give it an owner
    # that is deleted on dismissal rather than leaving it on the page indefinitely.
    with owner:
        dialog = ui.dialog().props(_label_props(f"{status.alias} 完整信息"))
        with dialog, ui.card().classes("st-resource-dialog"):
            with ui.element("header").classes("st-detail-heading"):
                with ui.column().classes("gap-1 min-w-0"):
                    ui.label(status.alias).classes("st-detail-title")
                    ui.label("资源采集快照 · " + gpu_spec(status)).classes("st-detail-subtitle")
                ui.button("关闭", icon="close", on_click=dialog.close).props(
                    "flat no-caps"
                ).classes("st-detail-close")
            render_server_details(status, gpu_index)

    def dispose() -> None:
        dialog.delete()
        owner.delete()

    dialog.on("hide", dispose)
    dialog.open()


def render_server_card(status: ServerStatus) -> None:
    classes = "st-resource-card"
    if not status.online or status.error:
        classes += " st-resource-offline"
    with ui.element("section").classes(classes).props(_label_props(status.alias)):
        with ui.element("div").classes("st-resource-heading"):
            ui.element("span").classes("st-online-dot").props(
                _label_props("在线" if status.online else "离线")
            )
            with ui.element("button").classes("st-resource-title").props(
                f'type="button" {_label_props(f"查看 {status.alias} 完整信息")}'
            ).on("click", lambda: open_server_details(status)):
                ui.label(status.alias)
                ui.icon("chevron_right")
                ui.tooltip(
                    f"{status.alias} · {status.hostname or '主机名未知'} · "
                    f"{'在线' if status.online else '离线'}"
                ).classes("st-resource-tooltip")
        ui.label(gpu_spec(status)).classes("st-resource-spec").tooltip(gpu_spec(status))
        render_collapsed_summary(status)


def render_empty_state(container: Any) -> None:
    container.clear()
    with container:
        with ui.element("div").classes("st-dashboard-empty"):
            ui.icon("dns")
            ui.label("还没有启用的机器")
            ui.link("添加或启用机器", "/servers")


def render_pending_server_card(alias: str) -> None:
    with ui.element("section").classes("st-resource-card st-resource-pending").props(
        f'aria-busy="true" {_label_props(alias + " 正在获取指标")}'
    ):
        with ui.element("div").classes("st-resource-heading"):
            ui.element("span").classes("st-online-dot")
            ui.label(alias).classes("st-resource-title")
        ui.label("正在连接机器").classes("st-resource-spec")
        with ui.element("div").classes("st-resource-unavailable"):
            ui.icon("hourglass_empty")
            ui.label("正在获取指标…")
        with ui.element("div").classes("st-system-summary"):
            for label in ("CPU", "RAM", "磁盘"):
                ui.label(f"{label} —").classes("st-system-metric")


def _render_immediate_statuses(container: Any, aliases: list[str]) -> None:
    container.clear()
    with container:
        for alias in aliases:
            cached = _status_cache.get(alias)
            if cached is None:
                render_pending_server_card(alias)
            else:
                render_server_card(cached)


def render_results(
    container: Any, aliases: list[str], results: list[ServerStatus | BaseException]
) -> None:
    container.clear()
    with container:
        for alias, result in zip(aliases, results, strict=True):
            if isinstance(result, BaseException):
                status = ServerStatus(alias=alias, online=False, error=str(result))
            else:
                status = result
            _status_cache[alias] = status
            render_server_card(status)


def render_dashboard_page() -> None:
    """Render the home dashboard with periodically refreshed server status."""

    refresh_guard = DashboardRefreshGuard()
    has_snapshot = False
    displayed_snapshot: tuple[tuple[str, ...], tuple[ServerStatus, ...]] | None = None
    summary_text = "正在加载机器"

    def set_summary(text: str) -> None:
        nonlocal summary_text
        if text != summary_text:
            fleet_summary.set_text(text)
            summary_text = text

    async def refresh() -> None:
        nonlocal has_snapshot, displayed_snapshot
        generation = refresh_guard.next_generation()
        aliases = load_enabled_server_aliases()
        if not aliases:
            if refresh_guard.is_current(generation):
                if displayed_snapshot != ((), ()):
                    render_empty_state(container)
                    displayed_snapshot = ((), ())
                has_snapshot = True
                set_summary("0 台机器")
            return

        if not has_snapshot and refresh_guard.is_current(generation):
            _render_immediate_statuses(container, aliases)
            has_snapshot = True
            set_summary(f"{len(aliases)} 台机器 · 正在刷新")

        results = await asyncio.gather(
            *(collect_server_status(alias, SSHClient()) for alias in aliases),
            return_exceptions=True,
        )
        if refresh_guard.is_current(generation):
            statuses = tuple(
                ServerStatus(alias=alias, online=False, error=str(result))
                if isinstance(result, BaseException) else result
                for alias, result in zip(aliases, results, strict=True)
            )
            snapshot = (tuple(aliases), statuses)
            if snapshot != displayed_snapshot:
                render_results(container, aliases, list(statuses))
                displayed_snapshot = snapshot
            online = sum(status.online for status in statuses)
            set_summary(
                f"{len(aliases)} 台机器 · {online} 在线 · {len(aliases) - online} 离线"
            )

    with ui.column().classes("st-dashboard"):
        with ui.row().classes("st-dashboard-heading"):
            with ui.column().classes("gap-1"):
                ui.label("资源概览").classes("st-dashboard-title")
                fleet_summary = ui.label("正在加载机器").classes("st-dashboard-subtitle")
            ui.button("刷新", icon="refresh", on_click=refresh).props(
                "outline no-caps"
            ).classes("st-dashboard-refresh")
        container = ui.column().classes("st-fleet-grid")
        with ui.element("footer").classes("st-dashboard-footer"):
            with ui.element("div").classes("st-dashboard-legend"):
                for label, series in (("计算", "compute"), ("显存", "memory")):
                    with ui.element("span").classes("st-legend-item"):
                        ui.element("i").classes(f"st-series-{series}")
                        ui.label(label)
                ui.label("GPU 按序排列 · 悬停或点击查看数值")
            ui.label(f"每 {settings.refresh_seconds} 秒刷新")
    create_scoped_timer(container, settings.refresh_seconds, refresh, immediate=False)
    create_scoped_timer(container, 0, refresh, once=True)
