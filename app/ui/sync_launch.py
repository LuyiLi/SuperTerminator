"""Small project form for the managed Newton sync-and-launch workflow."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from nicegui import ui

from app.machine_status import collect_machine_gpu_status
from app.run_status import get_run_record, observe_run_records
from app.ui.scoped_timer import create_scoped_timer


STAGES = {
    "snapshot": "准备代码",
    "sync": "同步",
    "environment": "准备环境",
    "launch": "检查并启动",
    "training": "训练中",
}


def source_label(source: dict[str, Any]) -> str:
    branch = source.get("branch") or "detached HEAD / 无分支"
    return f"{source['path']} · {branch}"


def environment_count_label(per_gpu: Any, gpu_ids: list[int]) -> str:
    try:
        count = int(per_gpu)
    except (TypeError, ValueError):
        return "请输入每卡环境数"
    return f"每卡 {count} 个环境 × {len(gpu_ids)} 张 GPU = 总共 {count * len(gpu_ids)} 个环境"


def request_storage_script(
    project_id: int,
    *,
    payload: dict[str, Any] | None = None,
    reset: bool = False,
    run_id: int | None = None,
    request_key: str | None = None,
) -> str:
    """Persist intent before submitting; reloads and concurrent clicks reuse it.

    Browser-local storage needs no NiceGUI session secret. The server remains the
    authority for request-key uniqueness and rejects mutated payloads on replay.
    """
    key = json.dumps(f"superterminator:sync-launch:{project_id}")
    if reset:
        operation = "localStorage.removeItem(key); return true;"
    elif run_id is not None:
        operation = (
            "const stored = JSON.parse(localStorage.getItem(key) || 'null'); "
            f"if (stored && stored.request_key === {json.dumps(request_key)}) {{ "
            f"stored.run_id = {int(run_id)}; localStorage.setItem(key, JSON.stringify(stored)); }} "
            "return stored;"
        )
    else:
        create = ""
        if payload is not None:
            value = json.dumps({"request_key": str(uuid4()), "payload": payload}, ensure_ascii=True)
            create = f"if (!stored) {{ stored = {json.dumps(value)}; localStorage.setItem(key, stored); }}"
        operation = f"let stored = localStorage.getItem(key); {create} return stored ? JSON.parse(stored) : null;"
    return (
        f"const key = {key}; const operation = async () => {{ {operation} }}; "
        "return navigator.locks ? await navigator.locks.request(key, operation) : await operation();"
    )


def validate_intent(intent: Any, payload: dict[str, Any]) -> str:
    if not isinstance(intent, dict) or not intent.get("request_key"):
        raise ValueError("无法保存本次请求。请允许浏览器本地存储后再启动。")
    if intent.get("payload") != payload:
        raise ValueError("已有请求使用不同的参数。请先查看原任务；要开始另一份训练，请点击“新训练”。")
    return str(intent["request_key"])


def gpu_options(observation: dict[str, Any]) -> dict[int, str]:
    labels = {"free": "空闲", "light": "占用", "busy": "占用"}
    return {
        int(gpu["index"]): (
            f"GPU {gpu['index']} · {gpu['name']} · "
            f"{gpu.get('memory_used_mib', '?')}/{gpu.get('memory_total_mib', '?')} MiB · "
            f"{labels.get(gpu.get('occupancy'), '状态待确认')}"
        )
        for gpu in observation.get("gpus", [])
    }


def stage_label(event: dict[str, Any]) -> str:
    stage = str(event.get("stage") or event.get("sync_stage") or "snapshot")
    text = STAGES.get(stage, stage)
    if event.get("detail"):
        text += f"：{event['detail']}"
    return text


def _requested_source() -> str | None:
    try:
        request = ui.context.client.request
    except RuntimeError:
        return None  # Rendering outside a request (for example an isolated UI test).
    return request.query_params.get("source") if request is not None else None


def render_sync_launch_panel(project_id: int) -> None:
    from app.sync_launch import get_sync_options, sync_and_launch

    requested_source = _requested_source()
    options = get_sync_options(project_id, source_path=requested_source)
    if not options.get("enabled") and not options.get("error"):
        with ui.column().classes("st-panel st-empty"):
            ui.label("此项目尚未配置同步启动。可以切换到命令启动，使用已关联的机器和工作目录。")
        return

    client = ui.context.client
    with ui.column().classes("st-sync-layout"):
        form = ui.column().classes("st-panel st-sync-fields")
        review = ui.column().classes("st-panel st-sync-review")
    with review:
        ui.label("本次启动").classes("st-project-section-title")
        summary_source = ui.label("尚未选择代码来源").classes("st-project-path")
        summary_machine = ui.label("").classes("st-sync-summary-machine")
        summary_gpus = ui.label("").classes("st-page-subtitle")
        summary_parameters = ui.label("").classes("st-page-subtitle")
    with form:
        if not options.get("enabled"):
            ui.label(str(options.get("error") or "同步启动尚未配置。请联系开发者完成一次性接入。")) .classes("text-negative")
            return
        for problem in options.get("problems", []):
            ui.label(str(problem)).classes("text-negative")
        ui.label("代码、实验与资源").classes("st-project-section-title st-sync-wide")
        sources = {source["path"]: source for source in options.get("sources", [])}
        selected = options.get("selected_source")
        if isinstance(selected, dict):
            selected = selected.get("path")
        if requested_source:
            selected = requested_source if requested_source in sources else None
        elif selected not in sources:
            selected = next(iter(sources)) if len(sources) == 1 else None
        source_select = ui.select(
            {path: source_label(source) for path, source in sources.items()},
            label="当前代码来源（本地目录 / worktree）",
            value=selected,
        ).props("outlined dense").classes("w-full min-w-0 st-sync-wide")
        source_summary = ui.label("").classes("st-project-path st-sync-wide")
        ui.label("包含已保存修改和同步范围内的新增文件；不包含编辑器中未保存的内容。快照完成后的编辑用于下一次启动。").classes("st-page-subtitle st-sync-wide")

        def update_source() -> None:
            source = sources.get(source_select.value)
            source_summary.set_text(source_label(source) if source else "请选择一次明确的代码来源；不会根据分支名猜测。")
            summary_source.set_text(source_label(source) if source else "尚未选择代码来源")

        source_select.on_value_change(lambda _event: update_source())
        update_source()
        experiments = {str(item["id"]): item for item in options.get("experiments", [])}
        experiment_select = ui.select(
            {key: value["name"] for key, value in experiments.items()},
            label="实验",
            value=next(iter(experiments), None),
        ).props("outlined dense").classes("w-full")
        servers = {int(item["id"]): item for item in options.get("servers", [])}
        server_select = ui.select(
            {key: item["alias"] for key, item in servers.items()},
            label="机器",
            value=next(iter(servers), None),
        ).props("outlined dense").classes("w-full")
        gpu_select = ui.select({}, label="GPU", multiple=True, value=[]).props("outlined dense use-chips").classes("w-full st-sync-wide")
        gpu_notice = ui.label("正在获取 GPU 状态…").classes("st-page-subtitle st-sync-wide")

        defaults = experiments.get(experiment_select.value, {}).get("defaults", {})
        with ui.expansion("高级选项", value=False).classes("w-full st-sync-wide"):
            num_envs = ui.number("每卡环境数", value=defaults.get("num_envs", 4096), min=1, step=1, precision=0)
            seed = ui.number("Seed", value=defaults.get("seed", 42), min=0, step=1, precision=0)
            iterations = ui.number("最大迭代数", value=defaults.get("max_iterations", 30000), min=1, step=1, precision=0)
        with review:
            counts = ui.label("").classes("st-sync-counts")
            ui.label("W&B 默认开启 · 环境固定为 Newton").classes("st-page-subtitle")
            ui.label("准备代码 → 同步 → 准备环境 → 检查并启动 → 训练中").classes("st-sync-flow")
            progress = ui.label("等待启动").classes("st-sync-progress")
            results = ui.column().classes("w-full min-w-0 gap-2 break-all")
        busy = False
        restoring = False
        gpu_generation = 0
        tracked_run_id: int | None = None
        active_request_key: str | None = None
        saved_run_id: int | None = None
        poll_enabled = False
        polling = False
        poll_task = None

        def update_counts() -> None:
            counts.set_text(environment_count_label(num_envs.value, list(gpu_select.value or [])))
            summary_machine.set_text("机器：" + str(servers.get(server_select.value, {}).get("alias") or "尚未选择"))
            summary_gpus.set_text("GPU：" + (", ".join(str(item) for item in gpu_select.value or []) or "尚未选择"))
            summary_parameters.set_text(f"Seed {seed.value} · 最大迭代 {iterations.value}")

        num_envs.on_value_change(lambda _event: update_counts())
        gpu_select.on_value_change(lambda _event: update_counts())
        seed.on_value_change(lambda _event: update_counts())
        iterations.on_value_change(lambda _event: update_counts())
        update_counts()

        def update_defaults() -> None:
            if restoring:
                return
            values = experiments.get(experiment_select.value, {}).get("defaults", {})
            num_envs.value = values.get("num_envs", 4096)
            seed.value = values.get("seed", 42)
            iterations.value = values.get("max_iterations", 30000)
            update_counts()

        experiment_select.on_value_change(lambda _event: update_defaults())

        async def refresh_gpus(*, preserve: list[int] | None = None) -> None:
            nonlocal gpu_generation
            gpu_generation += 1
            generation = gpu_generation
            server_id = server_select.value
            if server_id is None:
                gpu_select.set_options({}, value=[])
                gpu_notice.set_text("请先选择机器。")
                return
            server = servers[int(server_id)]
            gpu_notice.set_text("正在获取 GPU 状态…")
            observation = await collect_machine_gpu_status({"server_id": server_id, "server_alias": server["alias"]})
            if generation != gpu_generation or server_id != server_select.value:
                return
            available = gpu_options(observation)
            requested = preserve if preserve is not None else list(server.get("gpu_ids") or [])
            # Keep an existing request retryable while the machine is unreachable.
            # A missing observation is never permission to alter its GPU choices.
            for gpu in requested:
                available.setdefault(gpu, f"GPU {gpu} · 状态待确认（当前未返回）")
            gpu_select.set_options(available, value=requested)
            gpu_notice.set_text(
                "当前占用仅供选择参考；启动前再次检查，启动中的任务也计入占用。"
                if observation.get("gpu_metrics_available")
                else f"无法读取 GPU：{observation.get('error') or '未返回 GPU 信息'}。请检查机器连接后刷新。"
            )
            update_counts()

        async def change_server() -> None:
            if not restoring:
                gpu_select.set_options({}, value=[])
                await refresh_gpus()

        server_select.on_value_change(lambda _event: change_server())
        refresh_button = ui.button("刷新 GPU 状态", on_click=lambda: refresh_gpus(preserve=list(gpu_select.value or []))).props("flat icon=refresh")

        def form_payload() -> dict[str, Any]:
            if source_select.value not in sources:
                raise ValueError("请选择完整的本地代码来源。")
            if experiment_select.value not in experiments:
                raise ValueError("请选择已配置的实验。")
            if server_select.value not in servers:
                raise ValueError("请选择已配置的机器。")
            if not gpu_select.value:
                raise ValueError("请至少选择一张 GPU。")
            parameters = {}
            for name, field, minimum in [("num_envs", num_envs, 1), ("seed", seed, 0), ("max_iterations", iterations, 1)]:
                value = field.value
                if value is None or float(value) != int(value) or int(value) < minimum:
                    raise ValueError(f"{field.label}必须是大于等于 {minimum} 的整数。")
                parameters[name] = int(value)
            return {
                "project_id": project_id,
                "server_id": int(server_select.value),
                "experiment_id": str(experiment_select.value),
                "source_path": str(source_select.value),
                "gpu_ids": sorted(int(gpu) for gpu in gpu_select.value),
                "parameters": parameters,
            }

        def ui_active() -> bool:
            return not progress.is_deleted and bool(getattr(client, "has_socket_connection", True))

        def stop_polling() -> None:
            nonlocal poll_enabled, poll_task
            poll_enabled = False
            if poll_task is not None:
                poll_task.cancel()
                poll_task = None

        def render_run_link() -> None:
            ui.link(
                f"查看本次任务 #{tracked_run_id} / 日志 / W&B / 停止",
                f"/runs/{tracked_run_id}",
            )

        async def track_run(run_id: Any) -> None:
            nonlocal tracked_run_id, saved_run_id, poll_enabled, poll_task
            if not run_id:
                return
            tracked_run_id = int(run_id)
            if not poll_enabled:
                poll_enabled = True
                poll_task = create_scoped_timer(results, 5, refresh_run, immediate=False)
            if active_request_key and saved_run_id != tracked_run_id and ui_active():
                try:
                    await ui.run_javascript(
                        request_storage_script(
                            project_id, run_id=tracked_run_id, request_key=active_request_key,
                        ),
                        timeout=5,
                    )
                    saved_run_id = tracked_run_id
                except Exception:
                    # The persisted request key still recovers the original task.
                    pass

        async def display_run(run: dict[str, Any]) -> None:
            await track_run(run.get("id") or run.get("run_id"))
            if not ui_active():
                return
            state = run.get("state") or run.get("status") or "unknown"
            stage = run.get("sync_stage") or "launch"
            state_text = {
                "preparing": STAGES.get(stage, "准备中"),
                "running": "训练中",
                "starting": "检查并启动：等待正常训练输出",
                "unknown": "状态待确认：请核实原任务",
                "failed": "任务失败",
                "stopped": "已停止",
                "succeeded": "已完成",
            }.get(state, state)
            detail = run.get("status_detail")
            progress.set_text(f"{state_text}{'：' + str(detail) if detail else ''}")
            results.clear()
            with results:
                render_run_link()
                if run.get("sync_metadata"):
                    render_sync_summary(run, details=False)
            if state in {"succeeded", "failed", "stopped"}:
                stop_polling()

        async def refresh_run() -> None:
            nonlocal polling
            if not poll_enabled or polling or busy or restoring or not tracked_run_id or not ui_active():
                return
            polling = True
            run_id = tracked_run_id
            try:
                record = get_run_record(run_id)
                if record is None:
                    raise ValueError("原任务记录不存在，请检查任务列表。")
                observed = (await observe_run_records([record]))[0]
                if tracked_run_id == run_id and not busy and ui_active():
                    await display_run(observed)
            except Exception as exc:
                if tracked_run_id == run_id and ui_active():
                    progress.set_text(f"状态待确认：{exc}；正在查询原任务，不会重新启动。")
            finally:
                polling = False

        async def report(event: dict[str, Any]) -> None:
            await track_run(event.get("run_id"))
            if not ui_active():
                return
            progress.set_text(stage_label(event))
            if tracked_run_id:
                results.clear()
                with results:
                    render_run_link()

        async def submit() -> None:
            nonlocal busy, active_request_key
            if busy:
                return
            busy = True
            for control in controls:
                control.disable()
            try:
                payload = form_payload()
                intent = await ui.run_javascript(request_storage_script(project_id, payload=payload), timeout=5)
                request_key = validate_intent(intent, payload)
                active_request_key = request_key
                run = await sync_and_launch(**payload, request_key=request_key, progress=report)
                if progress.is_deleted:
                    return
                await display_run(run)
            except Exception as exc:
                if not progress.is_deleted:
                    progress.set_text(str(exc))
                    ui.notify(str(exc), type="negative")
            finally:
                busy = False
                if not launch_button.is_deleted:
                    for control in controls:
                        control.enable()

        async def new_request() -> None:
            nonlocal tracked_run_id, active_request_key, saved_run_id
            if busy:
                return
            try:
                await ui.run_javascript(request_storage_script(project_id, reset=True), timeout=5)
            except Exception as exc:
                ui.notify(f"无法创建新的请求：{exc}", type="negative")
                return
            stop_polling()
            tracked_run_id = saved_run_id = None
            active_request_key = None
            results.clear()
            progress.set_text("新的训练请求已就绪；原任务继续保留。")

        with review:
            with ui.row().classes("st-actions"):
                launch_button = ui.button("同步并启动", color=None, on_click=submit).props("icon=play_arrow no-caps").classes("st-primary")
                new_button = ui.button("新训练", on_click=new_request).props("flat no-caps")
            ui.label("重复点击或刷新后重试会查询同一次请求。开始另一份训练请点“新训练”。").classes("st-page-subtitle")
        controls = [source_select, experiment_select, server_select, gpu_select, num_envs, seed, iterations, refresh_button, launch_button, new_button]

        async def restore_request() -> None:
            nonlocal restoring, active_request_key, saved_run_id
            try:
                intent = await ui.run_javascript(request_storage_script(project_id), timeout=5)
                payload = intent.get("payload") if isinstance(intent, dict) else None
                if payload:
                    restoring = True
                    active_request_key = str(intent["request_key"])
                    saved_run_id = int(intent["run_id"]) if intent.get("run_id") else None
                    if payload.get("source_path") and not requested_source:
                        previous_source = str(payload["source_path"])
                        # Querying a frozen task must survive deleting its worktree.
                        # The backend validates this path for any genuinely new task.
                        sources.setdefault(previous_source, {"path": previous_source, "branch": "上次请求的来源（当前不可用）"})
                        source_select.set_options(
                            {path: source_label(source) for path, source in sources.items()},
                            value=previous_source,
                        )
                    if payload.get("experiment_id") in experiments:
                        experiment_select.value = payload["experiment_id"]
                    if payload.get("server_id") in servers:
                        server_select.value = payload["server_id"]
                    parameters = payload.get("parameters", {})
                    num_envs.value = parameters.get("num_envs", num_envs.value)
                    seed.value = parameters.get("seed", seed.value)
                    iterations.value = parameters.get("max_iterations", iterations.value)
                    progress.set_text("已恢复上次请求；点击“同步并启动”核实原任务，或点“新训练”开始另一份。")
                    if saved_run_id:
                        await track_run(saved_run_id)
                        with results:
                            render_run_link()
                        progress.set_text("已恢复原任务，正在自动核实状态。")
                    await refresh_gpus(preserve=payload.get("gpu_ids", []))
                    update_source()
                else:
                    await refresh_gpus()
            except Exception as exc:
                gpu_notice.set_text(f"加载未完成：{exc}。请刷新 GPU 状态后重试。")
            finally:
                restoring = False

        create_scoped_timer(results, 0.1, restore_request, once=True)


def render_sync_summary(run: dict[str, Any], *, details: bool = True) -> None:
    """Display saved provenance and a real W&B URL without claiming connectivity."""
    metadata = run.get("sync_metadata") or {}
    if not metadata:
        return
    ui.label(f"机器 / GPU：{run.get('server_alias', '')} / {', '.join(str(item) for item in metadata.get('gpu_ids', []))}").classes("font-semibold")
    ui.label(f"代码版本：{metadata.get('fingerprint') or '准备中'}").classes("font-mono text-sm break-all")
    if run.get("sync_stage"):
        ui.label(f"启动阶段：{STAGES.get(run['sync_stage'], run['sync_stage'])}")
    parameters = metadata.get("parameters") or {}
    if parameters.get("num_envs") is not None:
        ui.label(environment_count_label(parameters["num_envs"], metadata.get("gpu_ids", [])))
    wandb_url = metadata.get("wandb_url")
    if isinstance(wandb_url, str) and wandb_url.startswith(("https://", "http://")):
        ui.link("W&B", wandb_url, new_tab=True)
    else:
        ui.label("W&B：待确认（尚未取得真实 run URL）").classes("text-grey-7")
    if details:
        with ui.expansion("代码与启动详情", value=False).classes("w-full"):
            for label, value in [
                ("本地代码来源", metadata.get("source_path")),
                ("分支", metadata.get("branch")),
                ("Commit", metadata.get("commit")),
                ("远端快照", metadata.get("remote_workdir") or run.get("workdir")),
                ("独立日志", metadata.get("log_path")),
                ("请求 ID", run.get("request_key")),
            ]:
                ui.label(f"{label}：{value or '未记录'}").classes("font-mono text-sm break-all")
            ui.label("启动参数：" + json.dumps(parameters, ensure_ascii=False, sort_keys=True)).classes("font-mono text-sm break-all")
            ui.label("包含冻结时已保存的实际文件；未保存的编辑器内容不包含在内。").classes("text-grey-7")
