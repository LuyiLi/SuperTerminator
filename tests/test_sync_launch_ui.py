from __future__ import annotations

import asyncio
import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.ui import sync_launch


class Element:
    def __init__(self, text="", value=None, **kwargs):
        self.text = text
        self.label = text
        self.value = value
        self.options = kwargs.get("options", {})
        self.on_click = kwargs.get("on_click")
        self.change = None
        self.disabled = False
        self.is_deleted = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def classes(self, *_args):
        return self

    def props(self, *_args):
        return self

    def on_value_change(self, callback):
        self.change = callback
        return self

    def set_text(self, text):
        self.text = text

    def set_options(self, options, value=None):
        self.options = options
        self.value = value

    def clear(self):
        pass

    def disable(self):
        self.disabled = True

    def enable(self):
        self.disabled = False


class FakeUI:
    def __init__(self, source=None):
        self.elements = []
        self.context = SimpleNamespace(client=SimpleNamespace(request=SimpleNamespace(query_params={"source": source} if source else {})))
        self.notices = []
        self.intent = None

    def add(self, element):
        self.elements.append(element)
        return element

    def label(self, text):
        return self.add(Element(text))

    def expansion(self, text, **kwargs):
        return self.add(Element(text, **kwargs))

    def select(self, options, *, label, **kwargs):
        return self.add(Element(label, options=options, **kwargs))

    def number(self, label, **kwargs):
        return self.add(Element(label, **kwargs))

    def column(self):
        return self.add(Element())

    def row(self):
        return self.add(Element())

    def button(self, text, **kwargs):
        return self.add(Element(text, **kwargs))

    def link(self, text, target, **kwargs):
        element = self.add(Element(text, **kwargs))
        element.target = target
        return element

    def notify(self, message, **kwargs):
        self.notices.append((message, kwargs))

    def find(self, label):
        return next(element for element in self.elements if element.label == label)

    async def run_javascript(self, operation, **_kwargs):
        if operation.get("reset"):
            self.intent = None
            return True
        if self.intent is None and operation.get("payload"):
            self.intent = {"request_key": "persistent-request-1", "payload": deepcopy(operation["payload"])}
        if operation.get("run_id") and self.intent and self.intent["request_key"] == operation.get("request_key"):
            self.intent["run_id"] = operation["run_id"]
        return deepcopy(self.intent)


@pytest.fixture
def panel(monkeypatch):
    fake_ui = FakeUI()
    options = {
        "enabled": True,
        "sources": [{"path": "/local/tree-a", "branch": "main", "commit": "abc"}, {"path": "/local/tree-b", "branch": "feature", "commit": "def"}],
        "experiments": [{"id": "flat", "name": "Flat", "defaults": {"num_envs": 2048, "seed": 7, "max_iterations": 100}}],
        "servers": [{"id": 2, "alias": "lhy"}],
    }
    backend = SimpleNamespace(get_sync_options=lambda *_args, **_kwargs: deepcopy(options), sync_and_launch=None)
    monkeypatch.setitem(sys.modules, "app.sync_launch", backend)
    monkeypatch.setattr(sync_launch, "ui", fake_ui)
    monkeypatch.setattr(sync_launch, "request_storage_script", lambda project_id, **kwargs: {"project_id": project_id, **kwargs})
    timers = []
    monkeypatch.setattr(sync_launch, "create_scoped_timer", lambda _owner, _interval, callback, **_kwargs: timers.append(callback))

    async def observe(_server):
        return {"gpu_metrics_available": True, "gpus": [
            {"index": 0, "name": "GPU model", "memory_used_mib": 0, "memory_total_mib": 24000, "occupancy": "free"},
            {"index": 1, "name": "GPU model", "memory_used_mib": 12000, "memory_total_mib": 24000, "occupancy": "busy"},
        ]}

    monkeypatch.setattr(sync_launch, "collect_machine_gpu_status", observe)
    return SimpleNamespace(ui=fake_ui, options=options, backend=backend, timers=timers)


def test_source_is_never_guessed_between_worktrees(panel):
    sync_launch.render_sync_launch_panel(1)
    source = panel.ui.find("当前代码来源（本地目录 / worktree）")
    assert source.value is None
    assert source.options["/local/tree-a"] == "/local/tree-a · main"
    assert source.options["/local/tree-b"] == "/local/tree-b · feature"
    assert any("包含已保存修改" in element.text for element in panel.ui.elements)
    assert panel.ui.find("每卡环境数").value == 2048
    assert any("高级选项" == element.text for element in panel.ui.elements)


def test_explicit_worktree_url_binds_source(panel):
    panel.ui.context.client.request.query_params = {"source": "/local/tree-b"}
    sync_launch.render_sync_launch_panel(1)
    assert panel.ui.find("当前代码来源（本地目录 / worktree）").value == "/local/tree-b"
    assert any(element.text == "/local/tree-b · feature" for element in panel.ui.elements)


@pytest.mark.asyncio
async def test_double_click_refresh_retry_preserve_request_and_parameters(panel):
    calls = []
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def launch(**kwargs):
        calls.append(kwargs)
        entered.set()
        await kwargs["progress"]({"stage": "snapshot", "run_id": 19})
        await finish.wait()
        return {"id": 19, "status": "unknown", "sync_stage": "launch"}

    panel.backend.sync_and_launch = launch
    sync_launch.render_sync_launch_panel(1)
    await panel.timers[0]()
    panel.ui.find("当前代码来源（本地目录 / worktree）").value = "/local/tree-b"
    panel.ui.find("GPU").value = [0]
    click = panel.ui.find("同步并启动").on_click
    first = asyncio.create_task(click())
    await entered.wait()
    await click()
    assert len(calls) == 1
    assert panel.ui.find("同步并启动").disabled
    finish.set()
    await first
    assert any("状态待确认" in element.text for element in panel.ui.elements)
    assert any(getattr(element, "target", None) == "/runs/19" for element in panel.ui.elements)

    # The page may reconnect with a new callback; browser storage survives it.
    panel.ui.elements.clear()
    sync_launch.render_sync_launch_panel(1)
    await panel.timers[-1]()
    assert panel.ui.find("当前代码来源（本地目录 / worktree）").value == "/local/tree-b"
    assert panel.ui.find("GPU").value == [0]
    await panel.ui.find("同步并启动").on_click()
    assert len(calls) == 2
    assert calls[0]["request_key"] == calls[1]["request_key"] == "persistent-request-1"
    assert calls[1]["parameters"] == {"num_envs": 2048, "seed": 7, "max_iterations": 100}
    panel.ui.find("Seed").value = 8
    await panel.ui.find("同步并启动").on_click()
    assert len(calls) == 2
    assert "已有请求使用不同的参数" in panel.ui.notices[-1][0]


@pytest.mark.asyncio
async def test_ambiguous_source_and_missing_gpu_do_not_submit(panel):
    async def launch(**_kwargs):
        pytest.fail("invalid form must not reach backend")

    panel.backend.sync_and_launch = launch
    sync_launch.render_sync_launch_panel(1)
    await panel.ui.find("同步并启动").on_click()
    assert "本地代码来源" in panel.ui.notices[-1][0]
    panel.ui.find("当前代码来源（本地目录 / worktree）").value = "/local/tree-a"
    await panel.ui.find("同步并启动").on_click()
    assert "至少选择一张 GPU" in panel.ui.notices[-1][0]
    assert panel.ui.intent is None


@pytest.mark.asyncio
async def test_restore_keeps_original_task_retryable_after_source_and_gpu_disappear(panel, monkeypatch):
    original = {
        "project_id": 1, "server_id": 2, "experiment_id": "flat",
        "source_path": "/local/deleted-worktree", "gpu_ids": [3],
        "parameters": {"num_envs": 2048, "seed": 7, "max_iterations": 100},
    }
    panel.ui.intent = {"request_key": "persistent-request-1", "payload": original}
    calls = []

    async def unavailable(_server):
        return {"gpu_metrics_available": False, "gpus": [], "error": "SSH disconnected"}

    async def lookup(**kwargs):
        calls.append(kwargs)
        return {"id": 19, "status": "unknown", "sync_stage": "launch"}

    monkeypatch.setattr(sync_launch, "collect_machine_gpu_status", unavailable)
    panel.backend.sync_and_launch = lookup
    sync_launch.render_sync_launch_panel(1)
    await panel.timers[0]()
    assert panel.ui.find("当前代码来源（本地目录 / worktree）").value == "/local/deleted-worktree"
    assert panel.ui.find("GPU").value == [3]
    assert "状态待确认" in panel.ui.find("GPU").options[3]
    await panel.ui.find("同步并启动").on_click()
    assert len(calls) == 1
    assert calls[0]["request_key"] == "persistent-request-1"
    assert calls[0]["gpu_ids"] == [3]
    assert calls[0]["source_path"] == "/local/deleted-worktree"


@pytest.mark.asyncio
async def test_form_observes_starting_to_running_and_wandb_without_relaunch(panel, monkeypatch):
    entered = asyncio.Event()
    finish = asyncio.Event()
    launches = []
    observations = []
    initial = {"id": 19, "run_id": 19, "status": "starting", "sync_stage": "launch"}

    async def launch(**kwargs):
        launches.append(kwargs)
        await kwargs["progress"]({"stage": "launch", "run_id": 19})
        entered.set()
        await finish.wait()
        return initial

    async def observe(records):
        observations.append(records)
        return [{
            **initial,
            "state": "running" if len(observations) == 1 else "succeeded",
            "sync_stage": "training",
            "server_alias": "lhy",
            "sync_metadata": {"gpu_ids": [0], "fingerprint": "abc", "wandb_url": "https://wandb.example/e/p/runs/real"},
        }]

    panel.backend.sync_and_launch = launch
    monkeypatch.setattr(sync_launch, "get_run_record", lambda run_id: initial if run_id == 19 else None)
    monkeypatch.setattr(sync_launch, "observe_run_records", observe)
    sync_launch.render_sync_launch_panel(1)
    await panel.timers[0]()
    panel.ui.find("当前代码来源（本地目录 / worktree）").value = "/local/tree-a"
    panel.ui.find("GPU").value = [0]
    submission = asyncio.create_task(panel.ui.find("同步并启动").on_click())
    await entered.wait()
    poll = panel.timers[-1]
    assert panel.ui.intent["run_id"] == 19
    await poll()  # Preparation owns state updates until its backend call returns.
    assert observations == []
    finish.set()
    await submission
    progress = panel.ui.find("等待启动")
    assert "等待正常训练输出" in progress.text
    panel.ui.context.client.has_socket_connection = False
    await poll()
    assert observations == []
    panel.ui.context.client.has_socket_connection = True
    await poll()
    assert progress.text == "训练中"
    assert panel.ui.find("W&B").target == "https://wandb.example/e/p/runs/real"
    await poll()
    assert progress.text == "已完成"
    await poll()  # Terminal state stops further remote observations.
    assert len(observations) == 2
    assert len(launches) == 1
    assert any(getattr(element, "target", None) == "/runs/19" for element in panel.ui.elements)


def test_environment_totals_gpu_occupancy_and_unknown_stage_are_truthful():
    assert sync_launch.environment_count_label(2048, [2, 3]) == "每卡 2048 个环境 × 2 张 GPU = 总共 4096 个环境"
    assert sync_launch.environment_count_label(2048, []) == "每卡 2048 个环境 × 0 张 GPU = 总共 0 个环境"
    assert sync_launch.stage_label({"stage": "environment", "detail": "uv sync 失败"}) == "准备环境：uv sync 失败"
    assert "占用" in sync_launch.gpu_options({"gpus": [{"index": 4, "name": "X", "occupancy": "busy"}]})[4]


def test_metadata_never_invents_wandb_url(monkeypatch):
    fake_ui = FakeUI()
    monkeypatch.setattr(sync_launch, "ui", fake_ui)
    run = {"server_alias": "lhy", "sync_metadata": {"source_path": "/local/tree", "branch": "main", "fingerprint": "f" * 64, "parameters": {"num_envs": 2048}, "gpu_ids": [0, 1]}}
    sync_launch.render_sync_summary(run)
    assert any("W&B：待确认" in element.text for element in fake_ui.elements)
    assert not any(hasattr(element, "target") for element in fake_ui.elements)
    assert any("总共 4096" in element.text for element in fake_ui.elements)
    run["sync_metadata"]["wandb_url"] = "https://wandb.example/entity/project/runs/abc"
    fake_ui.elements.clear()
    sync_launch.render_sync_summary(run)
    assert fake_ui.find("W&B").target == "https://wandb.example/entity/project/runs/abc"


@pytest.mark.asyncio
async def test_sync_stop_routes_by_persistent_task_id(monkeypatch):
    from app.ui import runs

    calls = []

    async def sync_stop(run_id):
        calls.append(run_id)
        return {"id": run_id, "status": "stopped"}

    async def legacy_stop(*_args):
        pytest.fail("sync task cannot use a broad legacy stop")

    monkeypatch.setitem(sys.modules, "app.sync_launch", SimpleNamespace(stop_sync_run=sync_stop))
    notices = []
    monkeypatch.setattr(runs, "_notify", lambda message, **kwargs: notices.append((message, kwargs)))
    await runs.stop_run_session(17, {"request_key": "request-a", "server_alias": "lhy", "tmux_session": "session-17"}, stop=legacy_stop)
    assert calls == [17]
    assert notices == [("任务已停止。", {"type": "positive"})]
