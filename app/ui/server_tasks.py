"""Small, on-demand task list for a machine's resource details."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from nicegui import ui

from app.config import load_settings
from app.run_status import SERVER_ACTIVE_STATES, list_server_active_runs, observe_run_records
from app.ui.runs import format_run_time, run_gpu_label, run_state_view
from app.ui.scoped_timer import _polling_is_active, create_scoped_timer


class ServerTasksPanel:
    def __init__(self, alias: str) -> None:
        self.alias = alias
        self.refreshing = False
        self.rows_snapshot: list[tuple[Any, ...]] | None = None
        interval = max(30, load_settings().refresh_seconds)
        with ui.element("section").classes("st-server-tasks").props(
            'aria-label="本机任务"'
        ) as self.container:
            with ui.element("header").classes("st-server-tasks-heading"):
                ui.label("本机任务").classes("st-server-tasks-title")
                self.count = ui.label("").classes("st-server-tasks-count")
            ui.label(f"面板记录的任务 · 每 {interval} 秒核实").classes("st-server-tasks-note")
            self.rows = ui.element("div").classes("st-server-task-list")
            self.freshness = ui.label("").classes("st-server-tasks-note")

        # The dialog opens immediately, even when the remote host is unreachable.
        records = list_server_active_runs(alias)
        self.render(records)
        self.freshness.set_text("已载入记录 · 正在核实运行状态…" if records else "")
        # One scoped loop: no log downloads or polling for closed machine dialogs.
        create_scoped_timer(self.container, interval, self.refresh)

    def render(self, records: list[dict[str, Any]]) -> None:
        rows = []
        active = uncertain = 0
        for run in records:
            state = str(run.get("state") or run.get("status") or "unknown")
            if state not in SERVER_ACTIVE_STATES | {"lost"}:
                continue
            label, tone, detail = run_state_view(run)
            if state in {"unknown", "lost"}:
                uncertain += 1
            else:
                active += 1
            if state == "running" and not run.get("live_verified"):
                label = "运行记录"
            config = run.get("config") or {}
            rows.append((
                int(run["id"]), run.get("name") or f"任务 #{run['id']}",
                run.get("project_name") or "未记录项目", run_gpu_label(run),
                config.get("task") or "", format_run_time(run.get("started_at")),
                label, tone, detail,
            ))
        if rows == self.rows_snapshot:
            return
        self.rows_snapshot = rows
        self.count.set_text(" · ".join(part for part in (
            f"{active} 个进行中" if active else "",
            f"{uncertain} 个待核实" if uncertain else "",
        ) if part) or "0 个")
        self.rows.clear()
        with self.rows:
            if not rows:
                ui.label("暂无运行中的面板任务").classes("st-server-tasks-empty")
            for run_id, name, project, gpus, task, started, label, tone, detail in rows:
                with ui.element("article").classes("st-server-task"):
                    with ui.element("div").classes("st-server-task-heading"):
                        ui.link(name, f"/runs/{run_id}").classes("st-server-task-name").tooltip(
                            "查看任务进度与输出"
                        )
                        ui.label(label).classes(f"st-server-task-state st-task-{tone}").tooltip(detail)
                    with ui.element("div").classes("st-server-task-meta"):
                        ui.label(f"#{run_id} · {project}")
                        ui.label(f"GPU {gpus}").classes("st-server-task-gpus").tooltip(
                            "启动配置指定的 GPU"
                        )
                    if task:
                        ui.label(f"Task · {task}").classes("st-server-task-config")
                    ui.label(f"启动于 {started}").classes("st-server-task-started")

    async def refresh(self) -> None:
        if self.refreshing or not _polling_is_active(self.container):
            return
        self.refreshing = True
        try:
            records = list_server_active_runs(self.alias)
            try:
                observed = await observe_run_records(records)
            except Exception as exc:
                # Keep identities visible; a network failure is not an empty machine.
                observed = [dict(run, state="unknown", live_verified=False,
                                 status_detail=str(exc)) for run in records]
            if not _polling_is_active(self.container):
                return
            self.render(observed)
            uncertain = any(run.get("state") in {"unknown", "lost"} for run in observed)
            stamp = datetime.now().strftime("%H:%M:%S")
            self.freshness.set_text(
                f"{stamp} · 部分任务状态待核实，下次自动重试" if uncertain
                else f"{stamp} · 状态已更新" if observed else ""
            )
        finally:
            self.refreshing = False
