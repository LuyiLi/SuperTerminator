from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import inspect
import json
from pathlib import Path
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from nicegui import ui
from sqlalchemy import Engine

from app.config import load_settings
from app.db import engine, session_scope
from app.models import Run
from app.run_status import (
    ATTENTION_STATES,
    FINISHED_STATES,
    get_run_record,
    get_training_run as fetch_training_run,
    list_run_records,
    list_training_runs as fetch_training_runs,
    parse_training_progress,
    observe_run_records,
)
from app.ssh_client import SSHClient
from app.visual_actions import capture_run_output, stop_run
from app.ui.scoped_timer import create_scoped_timer
from app.ui.sync_launch import render_sync_summary


settings = load_settings()


class RunOutputRefreshGuard:
    """Coordinate output refreshes so stale captures cannot overwrite newer requests."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self._generation = 0

    def next_generation(self) -> int:
        self._generation += 1
        return self._generation

    def is_current(self, generation: int) -> bool:
        return generation == self._generation


def _notify(message: str, *, type: str = "info") -> None:
    ui.notify(message, type=type)


def _run_summary(run: Run) -> dict[str, Any]:
    return {
        "id": run.id,
        "name": run.name,
        "project_name": run.project.name,
        "server_alias": run.server.alias,
        "status": run.status,
        "tmux_session": run.tmux_session,
    }


def _run_detail(run: Run) -> dict[str, Any]:
    summary = _run_summary(run)
    summary.update(
        {
            "workdir": run.workdir,
            "rendered_command": run.rendered_command,
        }
    )
    return summary


def launch_provenance_label(run: dict[str, Any]) -> str:
    """Return a stable, human-readable origin label for run cards/details."""

    source = str(run.get("launch_source") or "ui").strip().lower()
    source_label = "Codex MCP" if source == "mcp" else "Web UI" if source == "ui" else source
    label = f"Launch source: {source_label}"
    if run.get("source_run_id") is not None:
        label += f" · based on run #{run['source_run_id']}"
    return label


def launch_preflight_label(run: dict[str, Any]) -> str:
    status = str(run.get("launch_preflight_status") or "not recorded").strip()
    label = f"GPU preflight: {status}"
    if run.get("launch_preflight_observed_at"):
        label += f" · {run['launch_preflight_observed_at']}"
    if run.get("launch_preflight_detail"):
        label += f" · {run['launch_preflight_detail']}"
    return label


def list_recent_runs(*, limit: int = 100, target_engine: Engine = engine) -> list[dict[str, Any]]:
    """Load recent runs with project and server labels for the runs overview."""

    return list_run_records(limit=limit, target_engine=target_engine)


def get_run_detail(run_id: int, *, target_engine: Engine = engine) -> dict[str, Any] | None:
    """Load one run with project and server metadata for the detail page."""

    return get_run_record(run_id, target_engine=target_engine)


def mark_run_exited(run_id: int, *, target_engine: Engine = engine) -> bool:
    """Persist that a run was explicitly stopped (legacy public function name)."""

    with session_scope(target_engine) as session:
        run = session.get(Run, run_id)
        if run is None:
            return False
        run.status = "stopped"
        run.status_source = "user_stop"
        run.status_detail = "user_requested"
        run.ended_at = datetime.now()
        return True


async def refresh_run_output(
    output: Any,
    run: dict[str, Any],
    guard: RunOutputRefreshGuard,
    *,
    capture: Callable[..., Awaitable[str]] = capture_run_output,
    ssh_client_factory: Callable[[], Any] = SSHClient,
    progress_target: Any | None = None,
) -> None:
    """Refresh tmux output, ignoring results made stale by newer refresh requests."""

    generation = guard.next_generation()
    async with guard.lock:
        try:
            captured = await capture(
                run["server_alias"],
                run["tmux_session"],
                ssh_client_factory(),
                lines=300,
                engine=engine,
                run_id=run["id"],
            )
        except Exception as exc:  # pragma: no cover - defensive around real SSH callbacks
            if guard.is_current(generation):
                _notify(str(exc), type="negative")
            return
        if guard.is_current(generation):
            output.value = captured
            if progress_target is not None:
                progress = parse_training_progress(captured)
                if progress.get("epoch") is not None:
                    text = progress_summary(progress)
                    if progress.get("eta"):
                        text += f" · ETA {progress['eta']}"
                elif progress.get("iteration") is not None:
                    text = (
                        f"Iteration {progress['iteration']} / {progress['max_iterations']} "
                        f"({progress.get('percent', 0):.2f}%)"
                    )
                    if progress.get("eta"):
                        text += f" · ETA {progress['eta']}"
                else:
                    text = "Progress unavailable in the current output tail"
                if hasattr(progress_target, "set_text"):
                    progress_target.set_text(text)
                elif hasattr(progress_target, "text"):
                    progress_target.text = text
                else:
                    progress_target.value = text


async def stop_run_session(
    run_id: int,
    run: dict[str, Any],
    *,
    stop: Callable[..., Awaitable[tuple[bool, str]]] = stop_run,
    mark_exited: Callable[[int], bool] = mark_run_exited,
    ssh_client_factory: Callable[[], Any] = SSHClient,
) -> None:
    """Stop a remote tmux session and persist the local exited status."""

    if run.get("request_key") or run.get("sync_metadata"):
        from app.sync_launch import stop_sync_run

        try:
            result = await stop_sync_run(run_id)
        except Exception as exc:
            _notify(str(exc), type="negative")
            return
        status = result.get("status") or result.get("state")
        _notify(
            "任务已停止。" if status == "stopped" else str(result.get("status_detail") or "停止状态待确认，请刷新原任务。"),
            type="positive" if status == "stopped" else "warning",
        )
        return

    try:
        ok, message = await stop(run["server_alias"], run["tmux_session"], ssh_client_factory())
    except Exception as exc:  # pragma: no cover - defensive around real SSH callbacks
        _notify(str(exc), type="negative")
        return
    if not ok:
        _notify(message, type="negative")
        return
    if not mark_exited(run_id):
        _notify("Run stopped but could not update local status.", type="negative")
        return
    _notify("Run stopped.", type="positive")


ACTIVE_RUN_STATES = frozenset({"created", "preparing", "starting", "running"})
UNCERTAIN_RUN_STATES = frozenset({"unknown", "lost"})
RUN_FILTERS = (("active", "运行中"), ("attention", "待处理"), ("finished", "已结束"), ("all", "全部"))


def run_state_view(run: dict[str, Any]) -> tuple[str, str, str]:
    """Use reported lifecycle evidence; an observation failure is never a failed run."""
    state = str(run.get("state") or run.get("status") or "unknown")
    detail = str(run.get("status_detail") or "")
    if state == "failed":
        exit_code = run.get("exit_code")
        reason = f"退出码 {exit_code}" if exit_code is not None else "启动或运行失败已记录"
        return "失败", "failed", detail or reason
    if state in UNCERTAIN_RUN_STATES:
        return "待核实", "unknown", detail or "当前结果尚未确认，请核实状态；连接中断不代表训练失败。"
    if state in {"finished_unknown", "exited"}:
        return "结果未知", "unknown", detail or "进程已结束，尚未记录可确认的退出结果。"
    if state == "succeeded":
        return "已完成", "done", detail or "进程已正常退出。"
    if state == "stopped":
        return "已停止", "done", detail or "已记录停止请求。"
    if state in ACTIVE_RUN_STATES:
        label = {"created": "已创建", "preparing": "准备中", "starting": "启动中", "running": "运行中"}[state]
        return label, "running", detail or "以最近一次状态观测为准。"
    return "待核实", "unknown", detail or f"尚未识别的运行状态：{state}"


def run_gpu_label(run: dict[str, Any]) -> str:
    ids = (run.get("sync_metadata") or {}).get("gpu_ids")
    if ids is not None:
        return ", ".join(str(item) for item in ids) or "未指定"
    return str((run.get("config") or {}).get("cuda_visible_devices") or "未记录")


def progress_summary(progress: dict[str, Any]) -> str:
    if progress.get("epoch") is not None:
        current, total, unit = progress["epoch"], progress.get("max_epochs"), "epochs"
    elif progress.get("iteration") is not None:
        current, total, unit = progress["iteration"], progress.get("max_iterations"), "iterations"
    else:
        return "尚未取得进度"
    text = f"{current:,} / {total:,} {unit}" if isinstance(total, int) else f"{current:,} {unit}"
    percent = progress.get("percent")
    if isinstance(percent, (int, float)):
        text += f" · {percent:g}%"
    return text


def format_run_time(value: Any) -> str:
    if not value:
        return "未记录"
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class RunOutputSnapshot:
    value: str = ""
    error: str = ""
    collected_at: str | None = None
    progress: dict[str, Any] = field(default_factory=dict)

    def accept(self, output: str) -> None:
        self.value = output
        self.error = ""
        self.collected_at = datetime.now().strftime("%H:%M:%S")
        # A short tail may no longer contain the last progress line. Keep that evidence.
        parsed = parse_training_progress(output)
        if "iteration" in parsed or "epoch" in parsed:
            for key in ("iteration", "max_iterations", "epoch", "max_epochs"):
                self.progress.pop(key, None)
        self.progress.update(parsed)


def parse_workbench_context(query: Any) -> dict[str, Any]:
    context: dict[str, Any] = {}
    for key in ("run", "project"):
        value = str(query.get(key, ""))
        if value.isascii() and value.isdecimal() and 0 < len(value) < 12 and int(value) > 0:
            context[key] = int(value)
    if query.get("filter") in dict(RUN_FILTERS):
        context["filter"] = query["filter"]
    if query.get("tab") in {"overview", "logs", "config"}:
        context["tab"] = query["tab"]
    if query.get("q"):
        context["q"] = str(query["q"])[:256]
    return context


def reuse_project_url(run: dict[str, Any], state: RunWorkbenchState) -> str:
    context = {"run": run["id"], "filter": state.filter_key, "tab": state.detail_tab}
    if state.project_filter is not None:
        context["project"] = state.project_filter
    if state.query:
        context["q"] = state.query
    return_to = "/runs?" + urlencode(context)
    return f"/projects/{run['project_id']}?" + urlencode({"reuse_run": run["id"], "return_to": return_to})


@dataclass
class RunWorkbenchState:
    records: list[dict[str, Any]] = field(default_factory=list)
    selected_id: int | None = None
    filter_key: str = "active"
    project_filter: int | None = None
    query: str = ""
    detail_tab: str = "overview"
    outputs: dict[int, RunOutputSnapshot] = field(default_factory=dict)

    def filtered(self, filter_key: str | None = None) -> list[dict[str, Any]]:
        selected_filter = filter_key or self.filter_key
        query = self.query.strip().casefold()
        result = []
        for run in self.records:
            state = str(run.get("state") or run.get("status") or "unknown")
            if self.project_filter is not None and run.get("project_id") != self.project_filter:
                continue
            if selected_filter == "active" and state not in ACTIVE_RUN_STATES:
                continue
            if selected_filter == "attention" and state not in (ATTENTION_STATES | UNCERTAIN_RUN_STATES):
                continue
            if selected_filter == "finished" and state not in (FINISHED_STATES | {"exited"}):
                continue
            if query and query not in " ".join(str(run.get(key) or "") for key in (
                "id", "name", "training_run_name", "panel_name", "project_name", "server_alias"
            )).casefold():
                continue
            result.append(run)
        return result

    def selected(self) -> dict[str, Any] | None:
        return next((run for run in self.records if run["id"] == self.selected_id), None)

    def choose_visible(self) -> None:
        visible = self.filtered()
        if not any(run["id"] == self.selected_id for run in visible):
            self.selected_id = visible[0]["id"] if visible else None

    def replace_records(self, records: list[dict[str, Any]]) -> None:
        """Automatic observations preserve selection even when its state changes groups."""
        selected = self.selected()
        self.records = records
        if selected and not any(run["id"] == self.selected_id for run in records):
            self.records.append(selected)
        if self.selected_id is None:
            self.choose_visible()


class _CheckedOutputSSHClient:
    """Keep capture command failures out of the displayed training output."""

    def __init__(self) -> None:
        self.client = SSHClient()

    async def run(self, *args, **kwargs):
        result = await self.client.run(*args, **kwargs)
        if result.exit_status != 0:
            raise RuntimeError(result.stderr.strip() or f"日志采集命令退出码 {result.exit_status}")
        return result


async def capture_workbench_output(
    run: dict[str, Any], snapshot: RunOutputSnapshot, guard: RunOutputRefreshGuard,
    *, capture: Callable[..., Awaitable[str]] | None = None,
    ssh_client_factory: Callable[[], Any] = _CheckedOutputSSHClient,
) -> None:
    generation = guard.next_generation()
    async with guard.lock:
        try:
            output = await (capture or capture_run_output)(
                run["server_alias"], run["tmux_session"], ssh_client_factory(),
                lines=300, engine=engine, run_id=run["id"],
            )
        except Exception as exc:
            if guard.is_current(generation):
                snapshot.error = str(exc)
            return
        if guard.is_current(generation):
            snapshot.accept(output)


def _set_text(target: Any, text: str) -> None:
    target.set_text(text)


def _run_key_values(items: list[tuple[str, Any]]) -> None:
    with ui.element("dl").classes("st-run-kv"):
        for label, value in items:
            with ui.element("dt"):
                ui.label(label)
            with ui.element("dd"):
                ui.label(str(value) if value is not None and value != "" else "未记录")


def _run_dialog(title: str):
    """Keep dialogs outside the refreshed inspector and dispose NiceGUI's canary too."""
    with ui.context.client.content:
        owner = ui.element("div").style("display:none")
    with owner:
        dialog = ui.dialog().props(f'aria-label={json.dumps(title, ensure_ascii=False)}')
    def dispose() -> None:
        dialog.delete()
        owner.delete()
    dialog.on("hide", dispose)
    return dialog


class RunWorkbench:
    def __init__(self, project_id: int | None, selected_run_id: int | None,
                 on_reuse: Callable[[dict[str, Any]], Any] | None) -> None:
        self.project_id = project_id
        self.on_reuse = on_reuse
        context = self._context_query() if project_id is None else {}
        if selected_run_id is None:
            selected_run_id = context.get("run")
        self.state = RunWorkbenchState(
            selected_id=selected_run_id,
            filter_key=context.get("filter", "active"),
            project_filter=context.get("project"),
            query=context.get("q", ""),
            detail_tab=context.get("tab", "overview"),
        )
        self.guards: dict[int, RunOutputRefreshGuard] = {}
        self.status_generation = 0
        self.refs: dict[str, Any] = {}
        self.display_limit = 50
        self.name_expanded = False
        self.config_expanded = False
        self.list_signature = None
        self.status_error = ""
        self.selection_error = ""
        self._status_busy = False
        self._output_busy: set[int] = set()
        self.state.records = self._load_records()
        if selected_run_id is not None:
            selected = get_run_detail(selected_run_id)
            if selected and (project_id is None or selected.get("project_id") == project_id):
                if not any(run["id"] == selected_run_id for run in self.state.records):
                    self.state.records.insert(0, selected)
                if "filter" not in context and selected not in self.state.filtered():
                    self.state.filter_key = "all"
            else:
                self.state.selected_id = None
                self.status_error = f"任务 #{selected_run_id} 不存在或不属于当前项目。"
                self.selection_error = self.status_error
        if "filter" not in context and not self.state.filtered() and self.state.records:
            self.state.filter_key = "all"
        if self.state.selected_id is None:
            self.state.choose_visible()
        self._render()

    @staticmethod
    def _context_query() -> dict[str, Any]:
        try:
            query = ui.context.client.request.query_params
        except (AttributeError, RuntimeError):
            return {}
        return parse_workbench_context(query)

    def _load_records(self) -> list[dict[str, Any]]:
        return list_run_records(limit=500, project_id=self.project_id)

    def _alive(self) -> bool:
        return not self.root.is_deleted

    def _render(self) -> None:
        ui.add_css(Path(__file__).with_name("runs.css"))
        with ui.column().classes("st-run-workspace") as self.root:
            with ui.row().classes("st-toolbar st-run-toolbar"):
                self.filter_bar = ui.row().classes("st-run-filter-tabs")
                with ui.row().classes("st-run-search-tools"):
                    if self.project_id is None:
                        options = {0: "全部项目"}
                        options.update({run["project_id"]: run["project_name"] for run in self.state.records})
                        if self.state.project_filter is not None and self.state.project_filter not in options:
                            options[self.state.project_filter] = f"项目 #{self.state.project_filter}"
                        ui.select(options, value=self.state.project_filter if self.state.project_filter in options else 0, on_change=self._on_project).props(
                            'outlined dense aria-label="按项目筛选"'
                        ).classes("st-run-project-filter")
                    ui.input(value=self.state.query, placeholder="搜索任务、机器或 ID", on_change=self._on_query).props(
                        'outlined dense clearable debounce=250 aria-label="搜索任务"'
                    ).classes("st-run-search")
                    ui.button("核实状态", icon="refresh", on_click=self.refresh_status).props(
                        "outline no-caps"
                    ).classes("st-run-refresh")
            self.status_notice = ui.label(self.status_error).classes("st-run-notice").props("role=status")
            self.status_notice.set_visibility(bool(self.status_error))
            with ui.element("div").classes("st-run-workbench"):
                with ui.element("section").classes("st-panel st-run-list-panel").props('aria-label="任务列表"'):
                    self.list_container = ui.column().classes("st-run-list")
                    self.list_footer = ui.label("").classes("st-run-list-footer")
                self.inspector = ui.column().classes("st-panel st-run-inspector")
            ui.label("待核实表示当前结果未知；状态与输出独立更新。筛选计数仅包含当前加载的最近 500 条记录。").classes("st-run-footnote")
        self.render_filters()
        self.render_list()
        self.render_inspector()
        create_scoped_timer(self.root, settings.refresh_seconds, self.refresh_status, immediate=False)
        create_scoped_timer(self.root, settings.run_output_seconds, self.refresh_selected_output, immediate=False)
        create_scoped_timer(self.root, 0.1, self.refresh_status, once=True)

    def render_filters(self) -> None:
        self.filter_bar.clear()
        with self.filter_bar:
            for key, title in RUN_FILTERS:
                active = key == self.state.filter_key
                with ui.element("button").classes("st-run-filter" + (" is-active" if active else "")).props(
                    f'type=button aria-pressed={str(active).lower()}'
                ).on("click", lambda key=key: self.set_filter(key)):
                    ui.label(title)
                    ui.label(str(len(self.state.filtered(key)))).classes("st-run-filter-count")

    def set_filter(self, key: str) -> None:
        self.state.filter_key = key
        self.state.choose_visible()
        self.render_filters()
        self.render_list()
        self.render_inspector()
        self._schedule_selected_output()

    def _on_query(self, event) -> None:
        self.state.query = str(event.value or "")
        self._filter_changed()

    def _on_project(self, event) -> None:
        self.state.project_filter = int(event.value) if event.value else None
        self._filter_changed()

    def _filter_changed(self) -> None:
        self.state.choose_visible()
        self.render_filters()
        self.render_list()
        self.render_inspector()
        self._schedule_selected_output()

    def select(self, run_id: int) -> None:
        self.state.selected_id = run_id
        self.render_list()
        self.render_inspector()
        self._schedule_selected_output()

    def _schedule_selected_output(self) -> None:
        create_scoped_timer(self.root, 0, self.refresh_selected_output, once=True)

    def set_tab(self, tab: str) -> None:
        self.state.detail_tab = tab
        self.render_inspector()

    def render_list(self) -> None:
        matching = self.state.filtered()
        visible = matching[:self.display_limit]
        selected = self.state.selected()
        if selected and selected in matching and selected not in visible:
            visible.insert(0, selected)
        signature = (
            self.state.selected_id, len(matching), len(self.state.records), self.display_limit,
            tuple((run["id"], run.get("name"), run.get("training_run_name"),
                   run.get("project_name"), run.get("server_alias"), run_gpu_label(run),
                   run.get("exit_code"), run_state_view(run)[:2],
                   progress_summary(self.state.outputs.get(run["id"], RunOutputSnapshot()).progress))
                  for run in visible),
        )
        if signature == self.list_signature:
            return
        self.list_signature = signature
        self.list_container.clear()
        with self.list_container:
            if not visible:
                ui.label("当前筛选下没有任务").classes("st-empty")
            for run in visible:
                run_id = run["id"]
                label, tone, _ = run_state_view(run)
                selected = run_id == self.state.selected_id
                full_name = str(run.get("training_run_name") or run.get("name") or f"Run {run_id}")
                with ui.element("button").classes("st-run-item" + (" is-selected" if selected else "")).props(
                    f'type=button aria-pressed={str(selected).lower()} '
                    f'aria-label={json.dumps(f"选择任务 #{run_id} {full_name}", ensure_ascii=False)}'
                ).on("click", lambda run_id=run_id: self.select(run_id)):
                    with ui.row().classes("st-run-item-title"):
                        ui.label(str(run.get("name") or full_name)).tooltip(full_name)
                        ui.icon("chevron_right")
                    with ui.row().classes("st-run-item-meta"):
                        ui.label(f"#{run_id}")
                        ui.label(label).classes(f"st-run-state st-state-{tone}")
                        if self.project_id is None:
                            ui.label(str(run.get("project_name") or "")).classes("st-run-project-name")
                    with ui.row().classes("st-run-item-tail"):
                        ui.label(str(run.get("server_alias") or ""))
                        ui.label(f"GPU {run_gpu_label(run)}")
                    snapshot = self.state.outputs.get(run_id, RunOutputSnapshot())
                    percent = snapshot.progress.get("percent")
                    if isinstance(percent, (int, float)):
                        ui.linear_progress(max(0, min(percent / 100, 1)), show_value=False, color=None).classes(
                            f"st-run-meter st-state-{tone}"
                        )
                    with ui.row().classes("st-run-item-tail"):
                        ui.label(progress_summary(snapshot.progress))
                        if run.get("exit_code") is not None:
                            ui.label(f"exit {run['exit_code']}")
            if len(matching) > len(visible):
                ui.button("再显示 50 条", on_click=self.show_more).props("flat no-caps")
        self.list_footer.set_text(f"显示 {len(visible)} / {len(matching)} 个匹配任务 · 已加载 {len(self.state.records)} 条")

    def show_more(self) -> None:
        self.display_limit += 50
        self.render_list()

    def render_inspector(self) -> None:
        if "full_name" in self.refs:
            self.name_expanded = self.refs["full_name"].value
        if "config_more" in self.refs:
            self.config_expanded = self.refs["config_more"].value
        self.inspector.clear()
        self.refs = {}
        run = self.state.selected()
        with self.inspector:
            if not run:
                ui.icon("view_sidebar").classes("st-run-empty-icon")
                ui.label("选择任务查看进度与输出").classes("st-empty")
                return
            with ui.element("header").classes("st-run-inspector-head"):
                with ui.row().classes("st-run-title-line"):
                    ui.label(str(run.get("name") or "任务")).classes("st-run-detail-title").tooltip(str(run.get("name") or "任务"))
                    self.refs["state"] = ui.label("").classes("st-run-state")
                full_name = run.get("training_run_name") or run.get("name")
                with ui.expansion("完整实验名", value=self.name_expanded).classes("st-run-full-name") as name_expansion:
                    self.refs["full_name"] = name_expansion
                    ui.label(str(full_name or "未记录")).classes("st-run-mono")
                    if run.get("panel_name") and run["panel_name"] != full_name:
                        ui.label(f"面板名称：{run['panel_name']}")
                ui.label(f"#{run['id']} · {run.get('project_name', '')}").classes("st-run-meta")
                ui.label(f"{run.get('server_alias', '')} · GPU {run_gpu_label(run)}").classes("st-run-meta st-run-mono")
                self.refs["selected_outside_filter"] = ui.label("任务状态已变化，当前选中项保留在详情中。").classes("st-run-notice")
            with ui.row().classes("st-run-detail-tabs"):
                for key, title in (("overview", "概况"), ("logs", "输出"), ("config", "启动信息")):
                    active = key == self.state.detail_tab
                    ui.button(title, color=None, on_click=lambda key=key: self.set_tab(key)).props(
                        f'flat no-caps aria-pressed={str(active).lower()}'
                    ).classes("st-run-detail-tab" + (" is-active" if active else ""))
            with ui.column().classes("st-run-inspector-body"):
                if self.state.detail_tab == "config":
                    self.render_launch_info(run)
                else:
                    if self.state.detail_tab == "overview":
                        with ui.element("div").classes("st-run-outcome") as outcome:
                            self.refs["outcome"] = outcome
                            self.refs["outcome_title"] = ui.label("").classes("st-run-outcome-title")
                            self.refs["outcome_detail"] = ui.label("")
                        with ui.row().classes("st-run-progress-heading"):
                            self.refs["progress_title"] = ui.label("训练进度")
                            self.refs["percent"] = ui.label("—").classes("st-run-progress-number")
                        self.refs["meter"] = ui.linear_progress(0, show_value=False, color=None).classes("st-run-meter")
                        self.refs["progress"] = ui.label("").classes("st-run-progress-meta")
                        self.refs["progress_extra"] = ui.label("").classes("st-run-progress-meta")
                        with ui.row().classes("st-run-section-heading"):
                            ui.label("最新输出")
                            ui.button("完整输出", icon="open_in_full", on_click=lambda: self.set_tab("logs")).props("flat dense no-caps")
                    with ui.row().classes("st-run-log-heading"):
                        self.refs["collected_at"] = ui.label("尚未采集输出")
                        ui.button("刷新输出", icon="refresh", on_click=self.refresh_selected_output).props("flat dense no-caps")
                    self.refs["output_error"] = ui.label("").classes("st-run-notice").props("role=status")
                    self.refs["output"] = ui.label("").classes(
                        "st-run-log" + (" st-run-log-full" if self.state.detail_tab == "logs" else "")
                    ).props('role=log aria-label="训练输出"')
                    if self.state.detail_tab == "logs":
                        ui.label("最近 300 行 · 采集失败时保留最后一次成功输出").classes("st-run-footnote")
                    if settings.show_debug_terminal:
                        ui.label("Debug terminal placeholder: interactive terminal will appear here.").classes("st-run-footnote")
            with ui.element("footer").classes("st-run-inspector-footer"):
                with ui.row().classes("st-actions"):
                    ui.button("复用配置", icon="content_copy", on_click=self.reuse_selected).props("outline no-caps")
                    ui.button("查看资源", icon="dns", on_click=self.show_resources).props("outline no-caps")
                self.refs["stop"] = ui.button("停止…", color=None, on_click=self.confirm_stop).props("flat no-caps").classes("st-run-stop")
            self.update_inspector()

    def render_launch_info(self, run: dict[str, Any]) -> None:
        config, metadata = run.get("config") or {}, run.get("sync_metadata") or {}
        parameters = metadata.get("parameters") or config
        _run_key_values([
            ("完整实验名", run.get("training_run_name") or run.get("name")),
            ("启动来源", launch_provenance_label(run)),
            ("GPU 预检", launch_preflight_label(run)),
            ("任务", parameters.get("task") or config.get("task")),
            ("训练参数", " · ".join(f"{key}: {parameters[key]}" for key in ("num_envs", "seed", "max_iterations") if parameters.get(key) is not None)),
            ("工作目录", run.get("workdir")),
            ("代码来源", metadata.get("source_path")),
            ("分支 / Commit", " · ".join(str(metadata[key]) for key in ("branch", "commit") if metadata.get(key))),
            ("tmux / supervisor", run.get("tmux_session")),
            ("退出码", run.get("exit_code")),
            ("最近训练指标", json.dumps(self.state.outputs.get(run["id"], RunOutputSnapshot()).progress, ensure_ascii=False)),
            ("状态依据", run.get("status_source")),
            ("启动时间", format_run_time(run.get("started_at"))),
            ("结束时间", format_run_time(run.get("ended_at"))),
            ("最近观测", format_run_time(run.get("observed_at") or run.get("last_observed_at"))),
        ])
        wandb_url = metadata.get("wandb_url")
        if isinstance(wandb_url, str) and wandb_url.startswith(("https://", "http://")):
            ui.link("打开 W&B", wandb_url, new_tab=True).classes("st-run-wandb")
        else:
            ui.label("W&B：尚未取得真实 run URL").classes("st-run-footnote")
        with ui.expansion("完整命令、请求与快照", value=self.config_expanded).classes("st-run-config-more") as config_expansion:
            self.refs["config_more"] = config_expansion
            ui.label(str(run.get("rendered_command") or "未记录")).classes("st-run-log st-run-command")
            _run_key_values([
                ("请求 ID", run.get("request_key")), ("启动阶段", run.get("sync_stage")),
                ("快照指纹", metadata.get("fingerprint")),
                ("远端快照", metadata.get("remote_workdir")),
                ("日志路径", metadata.get("log_path")),
                ("状态详情", run.get("status_detail")),
            ])
            ui.label(json.dumps({"config": config, "sync_metadata": metadata}, ensure_ascii=False, indent=2)).classes("st-run-log st-run-command")

    def update_inspector(self) -> None:
        run = self.state.selected()
        if not run or not self.refs:
            return
        label, tone, reason = run_state_view(run)
        self.refs["state"].set_text(label)
        self.refs["state"].classes(replace=f"st-run-state st-state-{tone}")
        self.refs["selected_outside_filter"].set_visibility(not any(item["id"] == run["id"] for item in self.state.filtered()))
        self.refs["stop"].set_visibility(str(run.get("state") or run.get("status")) in (ACTIVE_RUN_STATES | {"unknown", "lost"}))
        if self.state.detail_tab == "config":
            return
        snapshot = self.state.outputs.get(run["id"], RunOutputSnapshot())
        if "outcome" in self.refs:
            self.refs["outcome"].classes(replace=f"st-run-outcome st-state-{tone}")
            self.refs["outcome_title"].set_text("当前运行结果尚未确认" if label == "待核实" else f"任务{label}")
            stamp = run.get("observed_at") or run.get("last_observed_at")
            self.refs["outcome_detail"].set_text(reason + (f" · 观测于 {format_run_time(stamp)}" if stamp else " · 尚无实时核实记录"))
            progress = snapshot.progress
            percent = progress.get("percent")
            known = isinstance(percent, (int, float))
            self.refs["percent"].set_text(f"{percent:g}%" if known else "—")
            self.refs["meter"].set_visibility(known)
            self.refs["meter"].set_value(max(0, min(percent / 100, 1)) if known else 0)
            self.refs["progress_title"].set_text("训练进度" if tone == "running" else "最后记录的进度")
            self.refs["progress"].set_text(progress_summary(progress))
            extras = []
            for key, title in (("eta", "ETA"), ("elapsed", "已运行")):
                if progress.get(key) is not None:
                    extras.append(f"{title} {progress[key]}")
            self.refs["progress_extra"].set_text(" · ".join(extras))
        self.refs["collected_at"].set_text(f"最后成功采集 {snapshot.collected_at}" if snapshot.collected_at else "尚未成功采集输出")
        self.refs["output_error"].set_text("采集未成功，保留最后可用输出。" + snapshot.error if snapshot.error else "")
        self.refs["output_error"].set_visibility(bool(snapshot.error))
        output = snapshot.value
        if self.state.detail_tab == "overview":
            output = "\n".join(output.splitlines()[-6:])
        self.refs["output"].set_text(output or ("输出为空。" if snapshot.collected_at else "等待采集输出…"))

    async def _capture(self, run: dict[str, Any]) -> None:
        run_id = run["id"]
        snapshot = self.state.outputs.setdefault(run_id, RunOutputSnapshot())
        guard = self.guards.setdefault(run_id, RunOutputRefreshGuard())
        await capture_workbench_output(run, snapshot, guard)

    async def refresh_selected_output(self) -> None:
        run = self.state.selected()
        if not run or not self._alive() or run["id"] in self._output_busy:
            return
        self._output_busy.add(run["id"])
        try:
            await self._capture(run)
            if self._alive():
                self.update_inspector()
                self.render_list()
        finally:
            self._output_busy.discard(run["id"])

    async def refresh_status(self) -> None:
        if not self._alive() or self._status_busy:
            return
        self._status_busy = True
        self.status_generation += 1
        generation = self.status_generation
        try:
            records = self._load_records()
            selected = self.state.selected()
            if selected and not any(run["id"] == selected["id"] for run in records):
                records.insert(0, get_run_detail(selected["id"]) or selected)
            inspect_records = [run for run in records if str(run.get("state") or run.get("status")) in (ACTIVE_RUN_STATES | {"unknown", "lost"})]
            if selected and not any(run["id"] == selected["id"] for run in inspect_records):
                inspect_records.append(next((run for run in records if run["id"] == selected["id"]), selected))
            observations = {run["id"]: run for run in await observe_run_records(inspect_records)}
            observed = [observations.get(run["id"], run) for run in records]
            if not self._alive() or generation != self.status_generation:
                return
            self.state.replace_records(observed)
            self.status_error = self.selection_error
            self.status_notice.set_text(self.status_error)
            self.status_notice.set_visibility(bool(self.status_error))
            self.render_filters()
            self.render_list()
            if self.state.selected() and (not self.refs or self.state.detail_tab == "config"):
                self.render_inspector()
            else:
                self.update_inspector()
            # Keep traffic bounded: the selected task and up to twelve visible active tasks.
            targets = [run for run in self.state.filtered() if str(run.get("state") or run.get("status")) in ACTIVE_RUN_STATES][:12]
            selected = self.state.selected()
            if selected and not any(run["id"] == selected["id"] for run in targets):
                targets.insert(0, selected)
            semaphore = asyncio.Semaphore(4)
            async def sample(run):
                async with semaphore:
                    await self._capture(run)
            await asyncio.gather(*(sample(run) for run in targets))
            if self._alive():
                self.render_list()
                self.update_inspector()
        except Exception as exc:
            if self._alive():
                self.status_error = str(exc)
                self.status_notice.set_text("状态核实未完成；已保留当前任务和输出。" + self.status_error)
                self.status_notice.set_visibility(True)
        finally:
            self._status_busy = False

    async def reuse_selected(self) -> None:
        run = self.state.selected()
        if run is None:
            return
        if self.on_reuse is not None:
            result = self.on_reuse(dict(run))
            if inspect.isawaitable(result):
                await result
        elif run.get("project_id") is not None:
            ui.navigate.to(reuse_project_url(run, self.state))

    def confirm_stop(self) -> None:
        selected = self.state.selected()
        if selected is None:
            return
        run = dict(selected)
        dialog = _run_dialog(f"确认停止任务 #{run['id']}")
        with dialog, ui.card().classes("st-run-stop-dialog"):
            ui.label(f"停止任务 #{run['id']}？").classes("st-page-title")
            ui.label(str(run.get("training_run_name") or run.get("name") or "")).classes("st-run-mono")
            _run_key_values([
                ("项目", run.get("project_name")), ("机器", run.get("server_alias")),
                ("GPU", run_gpu_label(run)), ("会话", run.get("tmux_session")),
            ])
            ui.label("将向上述任务发送停止请求。此操作会中断该任务的训练。").classes("st-run-notice")
            async def confirm() -> None:
                confirm_button.disable()
                try:
                    await stop_run_session(run["id"], run)
                    dialog.close()
                    await self.refresh_status()
                finally:
                    if not confirm_button.is_deleted:
                        confirm_button.enable()
            with ui.row().classes("st-actions"):
                ui.button("取消", on_click=dialog.close).props("flat no-caps")
                confirm_button = ui.button(f"确认停止 #{run['id']}", on_click=confirm).props("color=negative no-caps")
        dialog.open()

    def show_resources(self) -> None:
        from app.ui import dashboard
        from app.visual_actions import collect_server_status
        selected = self.state.selected()
        if selected is None:
            return
        alias, run_id = selected["server_alias"], selected["id"]
        dialog = _run_dialog(f"任务 #{run_id} 的机器资源")
        with dialog, ui.card().classes("st-run-resource-dialog"):
            with ui.row().classes("st-page-header"):
                ui.label(f"{alias} · 任务 #{run_id}").classes("st-page-title")
                ui.button("返回任务", icon="close", on_click=dialog.close).props("flat no-caps")
            stamp = ui.label("正在采集资源…").classes("st-page-subtitle")
            resources = ui.column().classes("w-full")
            async def refresh_resource() -> None:
                try:
                    status = await collect_server_status(alias, SSHClient())
                except Exception as exc:
                    if not resources.is_deleted:
                        stamp.set_text(f"资源采集失败：{exc}")
                    return
                if resources.is_deleted:
                    return
                resources.clear()
                with resources:
                    dashboard.render_server_card(status)
                stamp.set_text("资源采集于 " + datetime.now().strftime("%H:%M:%S"))
            ui.button("刷新资源", icon="refresh", on_click=refresh_resource).props("outline no-caps")
        dialog.open()
        create_scoped_timer(resources, 0, refresh_resource, once=True)


def render_run_workbench(project_id: int | None = None, selected_run_id: int | None = None,
                         on_reuse: Callable[[dict[str, Any]], Any] | None = None) -> RunWorkbench:
    """Render a persistent run list beside its selected task; no launch is performed here."""
    return RunWorkbench(project_id, selected_run_id, on_reuse)


def render_runs_page() -> None:
    with ui.row().classes("st-page-header"):
        with ui.column().classes("gap-1"):
            ui.label("运行监控").classes("st-page-title")
            ui.label("查看训练进度、保留输出并核实任务状态。").classes("st-page-subtitle")
        ui.button("新建实验", icon="add", on_click=lambda: ui.navigate.to("/projects")).props("outline no-caps")
    render_run_workbench()


def render_run_detail(run_id: int) -> None:
    with ui.row().classes("st-page-header"):
        with ui.column().classes("gap-1"):
            ui.label("运行监控").classes("st-page-title")
            ui.label(f"任务 #{run_id} · 在列表中切换，保留排查上下文。").classes("st-page-subtitle")
        ui.link("全部运行", "/runs").classes("st-run-back")
    render_run_workbench(selected_run_id=run_id)
