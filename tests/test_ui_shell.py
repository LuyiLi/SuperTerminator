from __future__ import annotations

from pathlib import Path
import asyncio
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import create_engine_for_settings, init_db, session_scope
from app.models import Project, ProjectServer, ProjectWorkdir, Server


class CallRecorder(list):
    def __init__(self):
        super().__init__()
        self.stack = []


class FakeElement:
    def __init__(self, recorder, kind: str, *args, **kwargs):
        self.recorder = recorder
        self.kind = kind
        self.args = args
        self.kwargs = kwargs
        self.events = {}
        self.is_deleted = False
        self.value = kwargs.get("value")
        self.children = []
        self.parent = recorder.stack[-1] if recorder.stack else None
        if self.parent is not None:
            self.parent.children.append(self)
        recorder.append((kind, args, kwargs))

    def __enter__(self):
        self.recorder.stack.append(self)
        self.recorder.append((f"enter:{self.kind}", (), {}))
        return self

    def __exit__(self, exc_type, exc, tb):
        assert self.recorder.stack.pop() is self
        self.recorder.append((f"exit:{self.kind}", (), {}))
        return False

    def classes(self, value: str = "", **kwargs):
        self.recorder.append((f"classes:{self.kind}", (value,), {}))
        return self

    def props(self, value: str):
        self.recorder.append((f"props:{self.kind}", (value,), {}))
        return self

    def style(self, value: str):
        self.recorder.append((f"style:{self.kind}", (value,), {}))
        return self

    def set_text(self, text: str):
        self.args = (text,)
        self.recorder.append((f"set_text:{self.kind}", (text,), {}))
        return self

    def tooltip(self, text: str):
        self.recorder.append((f"tooltip:{self.kind}", (text,), {}))
        return self

    def on(self, event: str, callback, *args, **kwargs):
        self.events[event] = callback
        self.recorder.append((f"on:{self.kind}", (event, callback), kwargs))
        return self

    def clear(self):
        for child in tuple(self.children):
            child.delete()
        self.recorder.append((f"clear:{self.kind}", (), {}))

    def open(self):
        self.value = True
        self.recorder.append((f"open:{self.kind}", (), {}))

    def close(self):
        self.value = False
        self.recorder.append((f"close:{self.kind}", (), {}))

    def delete(self):
        for child in tuple(self.children):
            child.delete()
        self.is_deleted = True
        if self.parent is not None and self in self.parent.children:
            self.parent.children.remove(self)
        self.recorder.append((f"delete:{self.kind}", (), {}))

    def toggle(self):
        self.value = not self.value

    def hide(self):
        self.value = False

    def add_slot(self, name: str):
        return FakeElement(self.recorder, f"slot:{name}")


class FakeUI:
    def __init__(self):
        self.calls = CallRecorder()
        self.elements = []
        self.context = SimpleNamespace(client=SimpleNamespace(content=FakeElement(self.calls, "content")))

    def _element(self, kind, *args, **kwargs):
        element = FakeElement(self.calls, kind, *args, **kwargs)
        self.elements.append(element)
        return element

    def add_css(self, css: str, **kwargs):
        self.calls.append(("add_css", (css,), kwargs))

    def add_head_html(self, html: str, **kwargs):
        self.calls.append(("add_head_html", (html,), kwargs))

    def page_title(self, title: str):
        self.calls.append(("page_title", (title,), {}))

    def header(self):
        return self._element("header")

    def left_drawer(self, *args, **kwargs):
        return self._element("left_drawer", *args, **kwargs)

    def column(self):
        return self._element("column")

    def card(self):
        return self._element("card")

    def label(self, text: str):
        return self._element("label", text)

    def icon(self, name: str):
        return self._element("icon", name)

    def row(self):
        return self._element("row")

    def grid(self, *args, **kwargs):
        return self._element("grid", *args, **kwargs)

    def element(self, *args, **kwargs):
        return self._element("element", *args, **kwargs)

    def linear_progress(self, *args, **kwargs):
        return self._element("linear_progress", *args, **kwargs)

    def circular_progress(self, *args, **kwargs):
        return self._element("circular_progress", *args, **kwargs)

    def badge(self, *args, **kwargs):
        return self._element("badge", *args, **kwargs)

    def button(self, *args, **kwargs):
        return self._element("button", *args, **kwargs)

    def dialog(self, *args, **kwargs):
        # NiceGUI creates a sibling canary which must share the dialog's lifetime owner.
        self._element("dialog_canary")
        return self._element("dialog", *args, **kwargs)

    def tooltip(self, *args, **kwargs):
        return self._element("tooltip", *args, **kwargs)

    def separator(self):
        return self._element("separator")

    def link(self, text: str | None = None, target: str | None = None):
        return self._element("link", text, target)


def test_app_frame_sets_title_navigation_and_invokes_content(monkeypatch):
    from app.ui import layout

    fake_ui = FakeUI()
    monkeypatch.setattr(layout, "ui", fake_ui)
    monkeypatch.setattr(layout, "_sidebar_projects", lambda: [(42, "Alpha"), (7, "Beta")])
    connection_installs = []
    monkeypatch.setattr(layout, "install_connection_status", lambda: connection_installs.append(True))
    rendered = []

    layout.app_frame("Projects", lambda: rendered.append("content"))

    assert ("page_title", ("SuperTerminator",), {}) in fake_ui.calls
    assert ("label", ("SuperTerminator",), {}) in fake_ui.calls
    assert ("left_drawer", (), {"value": False}) in fake_ui.calls
    assert any(
        call[0] == "props:left_drawer" and "show-if-above" in call[1][0]
        and "breakpoint=767" in call[1][0]
        and "no-swipe-open" in call[1][0] and "no-swipe-close" in call[1][0]
        for call in fake_ui.calls
    )
    assert connection_installs == [True]
    assert any(call[0] == "element" and call[1] == ("main",) for call in fake_ui.calls)
    assert any(call[0] == "props:element" and "id=st-main" in call[1][0] for call in fake_ui.calls)
    assert [call for call in fake_ui.calls if call[0] == "link"] == [
        ("link", (None, "/"), {}),
        ("link", (None, "/projects"), {}),
        ("link", (None, "/runs"), {}),
        ("link", (None, "/servers"), {}),
        ("link", (None, "/settings"), {}),
        ("link", (None, "/projects/42"), {}),
        ("link", (None, "/projects/7"), {}),
    ]
    assert ("label", ("PROJECTS",), {}) in fake_ui.calls
    assert rendered == ["content"]


def test_components_render_empty_state_and_error_label(monkeypatch):
    from app.ui import components

    fake_ui = FakeUI()
    monkeypatch.setattr(components, "ui", fake_ui)

    components.empty_state("Nothing here yet")
    components.error_label("Boom")

    assert ("card", (), {}) in fake_ui.calls
    assert ("classes:card", ("w-full",), {}) in fake_ui.calls
    assert ("label", ("Nothing here yet",), {}) in fake_ui.calls
    assert ("classes:label", ("text-grey-7",), {}) in fake_ui.calls
    assert ("label", ("Boom",), {}) in fake_ui.calls
    assert ("classes:label", ("text-negative",), {}) in fake_ui.calls


def test_gpu_panel_shows_raw_metrics_without_occupancy_badges(monkeypatch):
    from app.ui import dashboard

    fake_ui = FakeUI()
    monkeypatch.setattr(dashboard, "ui", fake_ui)
    status = SimpleNamespace(
        gpu=[
            {
                "name": "A100",
                "memory_total_mib": 81920,
                "memory_used_mib": 1024,
                "utilization_gpu_percent": 73,
            }
        ]
    )

    dashboard.render_gpu_panel(status)

    labels = [call[1][0] for call in fake_ui.calls if call[0] == "label"]
    assert "GPU 0 · A100" in labels
    assert "Util active 73%" in labels
    assert "Memory 1.0 GiB / 80.0 GiB 1%" in labels
    assert not any(call[0] == "badge" for call in fake_ui.calls)


def test_pages_use_app_frame_and_configured_content(monkeypatch):
    import app.main as main_module

    captured = []

    def fake_app_frame(title, content):
        captured.append((title, content))

    monkeypatch.setattr(main_module, "app_frame", fake_app_frame)

    main_module.home_page()
    main_module.projects_page()
    main_module.project_detail_page("42")
    main_module.runs_page()
    main_module.servers_page()

    assert captured == [
        ("Home", main_module.render_dashboard_page),
        ("Projects", main_module.render_projects_page),
        ("Project", captured[2][1]),
        ("Runs", captured[3][1]),
        ("Servers", main_module.render_servers_page),
    ]

    detail_calls = []
    monkeypatch.setattr(
        main_module, "render_project_detail", lambda project_id: detail_calls.append(project_id)
    )
    captured[2][1]()
    assert detail_calls == [42]


def test_main_initializes_database_and_runs_nicegui_with_settings(monkeypatch):
    import app.main as main_module

    called = []
    monkeypatch.setattr(main_module, "settings", SimpleNamespace(
        host="0.0.0.0", port=9000, reload=True, reconnect_seconds=180,
    ))
    monkeypatch.setattr(main_module, "init_db", lambda: called.append("init_db"))
    monkeypatch.setattr(
        main_module.ui,
        "run",
        lambda **kwargs: called.append(("run", kwargs)),
    )

    main_module.main()

    assert called == [
        "init_db",
        ("run", {"host": "0.0.0.0", "port": 9000, "reload": True, "title": "gpu-ssh-panel",
                 "reconnect_timeout": 180, "message_history_length": 2000}),
    ]


def test_settings_page_uses_app_frame_and_shows_effective_settings(monkeypatch):
    import app.main as main_module
    from app.ui import settings as settings_ui

    fake_ui = FakeUI()
    monkeypatch.setattr(settings_ui, "ui", fake_ui)
    monkeypatch.setattr(settings_ui, "load_settings", lambda: SimpleNamespace(
        db_path=Path("/tmp/panel.db"), refresh_seconds=20,
        run_output_seconds=4, show_debug_terminal=True,
    ))
    captured = []
    monkeypatch.setattr(main_module, "app_frame", lambda title, content: captured.append((title, content)))
    main_module.settings_page()
    assert captured == [("Settings", settings_ui.render_settings_page)]
    captured[0][1]()
    labels = [call[1][0] for call in fake_ui.calls if call[0] == "label"]
    assert {"/tmp/panel.db", "20 秒", "4 秒", "显示"} <= set(labels)


def test_servers_module_add_server_trims_upserts_and_enables(tmp_path: Path, monkeypatch):
    from app.ui import servers

    engine = create_engine_for_settings(Settings(db_path=tmp_path / "app.db"))
    init_db(engine)
    notices = []
    reloaded = []
    monkeypatch.setattr(
        servers.ui,
        "notify",
        lambda message, **kwargs: notices.append((message, kwargs)),
    )
    monkeypatch.setattr(servers.ui.navigate, "reload", lambda: reloaded.append(True))

    assert servers.add_server(" gpu01 ", " GPU 01 ", target_engine=engine) is True
    assert servers.add_server("gpu01", "Renamed", target_engine=engine) is True

    with session_scope(engine) as session:
        stored = session.query(Server).filter_by(alias="gpu01").one()
        assert stored.name == "Renamed"
        assert stored.enabled is True

    assert reloaded == [True, True]
    assert notices[-1][1]["type"] == "positive"


def test_servers_module_remove_server_disables_and_unlinks_projects(tmp_path: Path, monkeypatch):
    from app.ui import servers

    engine = create_engine_for_settings(Settings(db_path=tmp_path / "app.db"))
    init_db(engine)
    notices = []
    reloaded = []
    monkeypatch.setattr(
        servers.ui,
        "notify",
        lambda message, **kwargs: notices.append((message, kwargs)),
    )
    monkeypatch.setattr(servers.ui.navigate, "reload", lambda: reloaded.append(True))

    with session_scope(engine) as session:
        project = Project(name="Demo")
        server = Server(alias="gpu01", enabled=True)
        session.add_all([project, server])
        session.flush()
        link = ProjectServer(project_id=project.id, server_id=server.id, enabled=True)
        session.add(link)
        session.flush()
        session.add(ProjectWorkdir(project_server_id=link.id, path="/data/demo"))
        server_id = server.id

    assert servers.remove_server(server_id, target_engine=engine) is True

    with session_scope(engine) as session:
        stored = session.get(Server, server_id)
        assert stored is not None
        assert stored.enabled is False
        assert session.query(ProjectServer).count() == 0
        assert session.query(ProjectWorkdir).count() == 0

    assert servers._load_managed_servers(target_engine=engine) == []
    assert reloaded == [True]
    assert notices[-1] == ("Server 'gpu01' removed.", {"type": "positive"})

def test_servers_module_add_server_rejects_blank_alias(monkeypatch):
    from app.ui import servers

    notices = []
    monkeypatch.setattr(
        servers.ui,
        "notify",
        lambda message, **kwargs: notices.append((message, kwargs)),
    )

    assert servers.add_server("   ") is False

    assert notices == [("SSH alias is required.", {"type": "negative"})]


def test_dashboard_module_loads_enabled_aliases(tmp_path: Path):
    from app.ui import dashboard

    engine = create_engine_for_settings(Settings(db_path=tmp_path / "app.db"))
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all(
            [
                Server(alias="gpu02", name="GPU 02", enabled=True),
                Server(alias="gpu01", name="GPU 01", enabled=True),
                Server(alias="old", name="Old", enabled=False),
            ]
        )

    assert dashboard.load_enabled_server_aliases(target_engine=engine) == ["gpu01", "gpu02"]


@pytest.mark.asyncio
async def test_servers_module_test_server_notifies_when_connection_check_raises(monkeypatch):
    from app.ui import servers

    notices = []

    async def boom(alias, ssh_client):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(servers, "test_connection", boom)
    monkeypatch.setattr(
        servers.ui,
        "notify",
        lambda message, **kwargs: notices.append((message, kwargs)),
    )

    await servers._test_server("gpu01")

    assert notices == [("gpu01: network exploded", {"type": "negative"})]


def test_dashboard_refresh_guard_marks_older_generations_stale():
    from app.ui.dashboard import DashboardRefreshGuard

    guard = DashboardRefreshGuard()
    older = guard.next_generation()
    newer = guard.next_generation()

    assert older < newer
    assert guard.is_current(older) is False
    assert guard.is_current(newer) is True


def test_dashboard_visual_format_helpers():
    from app.ui import dashboard

    assert dashboard.percent_color(10) == "positive"
    assert dashboard.percent_color(70) == "warning"
    assert dashboard.percent_color(95) == "negative"
    assert dashboard.percent_track_class(10) == "bg-green-2"
    assert dashboard.percent_track_class(70) == "bg-amber-2"
    assert dashboard.percent_track_class(95) == "bg-red-2"
    assert dashboard.ratio_percent(25, 100) == 25.0
    assert dashboard.ratio_percent(1, 0) == 0.0
    assert dashboard.format_mib(81920) == "80.0 GiB"
    assert dashboard.format_kib(1048576) == "1.0 GiB"
    assert dashboard.parse_percent_value("46%") == 46.0


def test_dashboard_refresh_timers_share_the_resource_container(monkeypatch):
    from app.ui import dashboard

    fake_ui = FakeUI()
    timers = []

    def fake_scoped_timer(owner, interval, callback, **kwargs):
        timers.append((owner, interval, callback, kwargs))
        return None

    monkeypatch.setattr(dashboard, "ui", fake_ui)
    monkeypatch.setattr(dashboard, "create_scoped_timer", fake_scoped_timer)
    dashboard.render_dashboard_page()

    assert len(timers) == 2
    assert timers[0][0] is timers[1][0]
    assert timers[0][0] in fake_ui.elements
    assert timers[0][1] == dashboard.settings.refresh_seconds
    assert timers[0][3]["immediate"] is False
    assert timers[1][1] == 0
    assert timers[1][3]["once"] is True
    assert timers[0][2] is timers[1][2]


@pytest.mark.asyncio
async def test_scoped_polling_pauses_for_connection_and_ancestor_visibility(monkeypatch):
    from nicegui import Client, ui
    from nicegui.page import page
    from app.ui import scoped_timer

    sleeps = asyncio.Queue()
    ticks = asyncio.Queue()

    async def controlled_sleep(_delay):
        await sleeps.put(None)
        await ticks.get()

    monkeypatch.setattr(scoped_timer, "asyncio", SimpleNamespace(
        sleep=controlled_sleep, get_running_loop=asyncio.get_running_loop,
        CancelledError=asyncio.CancelledError,
    ))
    client = Client(page("/__test_scoped_polling"))
    with client:
        with ui.column() as panel:
            owner = ui.column()
    monkeypatch.setattr(client, "tab_id", None)
    monkeypatch.setattr(client, "st_page_visible", True, raising=False)
    calls = []

    def collect():
        assert ui.context.slot.parent is owner
        calls.append("sample")

    async def tick():
        ticks.put_nowait(None)
        await asyncio.wait_for(sleeps.get(), timeout=1)

    task = scoped_timer.create_scoped_timer(owner, 10, collect, immediate=False)
    try:
        await asyncio.wait_for(sleeps.get(), timeout=1)
        await tick()
        assert calls == []  # Initial connection has not arrived.
        client.tab_id = "connected"
        panel.set_visibility(False)
        await tick()
        assert calls == []  # The owner itself is visible, but its view is hidden.
        panel.set_visibility(True)
        client.st_page_visible = False
        await tick()
        assert calls == []
        client.st_page_visible = True
        await tick()
        assert calls == ["sample"]
        client.tab_id = None
        await tick()
        await tick()
        assert calls == ["sample"]
        client.tab_id = "reconnected"
        await tick()
        assert calls == ["sample", "sample"]  # No catch-up burst.
    finally:
        client.delete()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()


@pytest.mark.asyncio
async def test_scoped_once_action_runs_even_when_view_is_hidden_or_disconnected(monkeypatch):
    from nicegui import Client, ui
    from nicegui.page import page
    from app.ui.scoped_timer import create_scoped_timer

    client = Client(page("/__test_scoped_once"))
    with client:
        owner = ui.column().set_visibility(False)
    monkeypatch.setattr(owner.client, "tab_id", None)
    monkeypatch.setattr(owner.client, "st_page_visible", False, raising=False)
    calls = []
    try:
        task = create_scoped_timer(owner, 0, lambda: calls.append("action"), once=True)
        await asyncio.wait_for(task, timeout=1)
        assert calls == ["action"]
    finally:
        client.delete()


@pytest.mark.asyncio
async def test_dashboard_refresh_renders_immediate_snapshot_before_metrics_return(monkeypatch):
    from app.schemas import ServerStatus
    from app.ui import dashboard

    fake_ui = FakeUI()
    calls = []
    release_collect = asyncio.Event()

    async def slow_collect(alias, _ssh_client):
        calls.append(("collect-start", alias))
        await release_collect.wait()
        return ServerStatus(alias=alias, online=True, cpu_percent=12)

    monkeypatch.setattr(dashboard, "ui", fake_ui)
    monkeypatch.setattr(dashboard, "create_scoped_timer", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dashboard, "load_enabled_server_aliases", lambda: ["gpu01"])
    monkeypatch.setattr(dashboard, "collect_server_status", slow_collect)
    monkeypatch.setattr(
        dashboard,
        "_render_immediate_statuses",
        lambda _container, aliases: calls.append(("immediate", tuple(aliases))),
    )
    monkeypatch.setattr(
        dashboard,
        "render_results",
        lambda _container, aliases, results: calls.append(("results", tuple(aliases), len(results))),
    )

    dashboard.render_dashboard_page()
    refresh = next(
        element.kwargs["on_click"] for element in fake_ui.elements
        if element.kind == "button" and "on_click" in element.kwargs
    )
    task = asyncio.create_task(refresh())
    for _ in range(5):
        await asyncio.sleep(0)
        if ("collect-start", "gpu01") in calls:
            break

    assert calls == [("immediate", ("gpu01",)), ("collect-start", "gpu01")]

    release_collect.set()
    await task
    assert calls[-1] == ("results", ("gpu01",), 1)


@pytest.mark.asyncio
async def test_dashboard_only_rebuilds_when_full_snapshot_changes(monkeypatch):
    from app.schemas import ServerStatus
    from app.ui import dashboard

    fake_ui = FakeUI()
    aliases = ["gpu01"]
    status = ServerStatus(alias="gpu01", online=True, cpu_percent=12,
                          gpu=({"memory_used_mib": 1024},))

    async def collect(_alias, _ssh_client):
        return status

    monkeypatch.setattr(dashboard, "ui", fake_ui)
    monkeypatch.setattr(dashboard, "create_scoped_timer", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dashboard, "load_enabled_server_aliases", lambda: list(aliases))
    monkeypatch.setattr(dashboard, "collect_server_status", collect)
    dashboard.render_dashboard_page()
    refresh = next(element.kwargs["on_click"] for element in fake_ui.elements
                   if element.kind == "button" and "on_click" in element.kwargs)
    await refresh()
    clear_count = lambda: sum(call[0] == "clear:column" for call in fake_ui.calls)
    assert clear_count() == 2  # Initial preview and first completed sample.
    initial_element_count = len(fake_ui.elements)
    initial_update_count = sum(call[0] == "set_text:label" for call in fake_ui.calls)
    status = ServerStatus(alias="gpu01", online=True, cpu_percent=12,
                          gpu=({"memory_used_mib": 1024},))
    await refresh()
    assert clear_count() == 2
    assert len(fake_ui.elements) == initial_element_count
    assert sum(call[0] == "set_text:label" for call in fake_ui.calls) == initial_update_count
    status = ServerStatus(alias="gpu01", online=True, cpu_percent=12,
                          gpu=({"memory_used_mib": 2048},))
    await refresh()
    assert clear_count() == 3  # Exact GPU readings also invalidate the snapshot.
    status = ServerStatus(alias="gpu01", online=False, error="network timeout")
    await refresh()
    assert clear_count() == 4
    await refresh()
    assert clear_count() == 4
    aliases.clear()
    await refresh()
    assert clear_count() == 5
    await refresh()
    assert clear_count() == 5


@pytest.mark.asyncio
async def test_dashboard_refresh_does_not_queue_behind_previous_refresh(monkeypatch):
    from app.schemas import ServerStatus
    from app.ui import dashboard

    fake_ui = FakeUI()
    collect_starts = []
    release_collect = asyncio.Event()

    async def slow_collect(alias, _ssh_client):
        collect_starts.append(alias)
        await release_collect.wait()
        return ServerStatus(alias=alias, online=True)

    monkeypatch.setattr(dashboard, "ui", fake_ui)
    monkeypatch.setattr(dashboard, "create_scoped_timer", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dashboard, "load_enabled_server_aliases", lambda: ["gpu01"])
    monkeypatch.setattr(dashboard, "collect_server_status", slow_collect)
    monkeypatch.setattr(dashboard, "_render_immediate_statuses", lambda _container, _aliases: None)
    monkeypatch.setattr(dashboard, "render_results", lambda _container, _aliases, _results: None)

    dashboard.render_dashboard_page()
    refresh = next(
        element.kwargs["on_click"] for element in fake_ui.elements
        if element.kind == "button" and "on_click" in element.kwargs
    )
    first = asyncio.create_task(refresh())
    for _ in range(5):
        await asyncio.sleep(0)
        if len(collect_starts) == 1:
            break
    second = asyncio.create_task(refresh())
    for _ in range(5):
        await asyncio.sleep(0)
        if len(collect_starts) == 2:
            break

    assert collect_starts == ["gpu01", "gpu01"]

    release_collect.set()
    await first
    await second


def test_dashboard_summary_helpers_format_clean_integer_percentages():
    from app.schemas import ServerStatus
    from app.ui import dashboard

    status = ServerStatus(
        alias="gpu01",
        online=True,
        gpu=[
            {"utilization_gpu_percent": 33.333333333333336},
            {"utilization_gpu_percent": 66.66666666666667},
        ],
        cpu_percent=48.000000000000004,
    )

    assert dashboard.format_percent(72.00000000004) == "72%"
    assert dashboard.gpu_average_utilization(status) == 50.0
    assert dashboard.format_percent(dashboard.gpu_average_utilization(status)) == "50%"
    assert dashboard.format_percent(status.cpu_percent) == "48%"


@pytest.mark.parametrize("value", [None, "", "unavailable", float("nan"), float("inf")])
def test_dashboard_missing_percent_is_unknown_instead_of_idle(value):
    from app.ui import dashboard

    assert dashboard.metric_percent(value) is None
    assert dashboard.metric_percent(0) == 0


@pytest.mark.parametrize("used,total", [(None, 100), (5, None), (5, 0), (float("nan"), 10)])
def test_dashboard_missing_capacity_is_unknown_instead_of_empty(used, total):
    from app.ui import dashboard

    assert dashboard.metric_ratio(used, total) is None
    assert dashboard.metric_ratio(0, 100) == 0
    assert dashboard.metric_ratio(25, 100) == 25


def test_dashboard_partial_gpu_metrics_preserve_the_available_measurement():
    from app.ui import dashboard

    assert dashboard.gpu_metric_values({"utilization_gpu_percent": 0}) == (0, None)
    assert dashboard.gpu_metric_values(
        {"memory_used_mib": 4096, "memory_total_mib": 8192}
    ) == (None, 50)
    assert dashboard.gpu_metric_values({}) == (None, None)


@pytest.mark.parametrize("gpu_count", [1, 4, 8, 12])
def test_dashboard_all_gpus_have_readings_and_their_own_detail_target(monkeypatch, gpu_count):
    from app.schemas import ServerStatus
    from app.ui import dashboard

    fake_ui = FakeUI()
    opened = []
    status = ServerStatus(
        alias="gpu01",
        online=True,
        gpu=[
            {"utilization_gpu_percent": index, "memory_used_mib": 4096,
             "memory_total_mib": 8192}
            for index in range(gpu_count)
        ],
    )
    monkeypatch.setattr(dashboard, "ui", fake_ui)
    monkeypatch.setattr(dashboard, "open_server_details", lambda *args: opened.append(args))

    dashboard.render_collapsed_summary(status)

    gpu_buttons = [
        element for element in fake_ui.elements
        if element.kind == "element" and element.args == ("button",)
        and any(child.kind == "tooltip" and child.args[0].startswith("GPU ")
                for child in element.children)
    ]
    assert len(gpu_buttons) == gpu_count
    for index, button in enumerate(gpu_buttons):
        tooltip = next(child for child in button.children if child.kind == "tooltip")
        assert f"GPU {index} · 计算 {index}%" in tooltip.args[0]
        assert "4.0 GiB / 8.0 GiB" in tooltip.args[0]
        button.events["click"]()
    assert opened == [(status, index) for index in range(gpu_count)]


def test_dashboard_unknown_and_offline_readings_are_not_shown_as_idle(monkeypatch):
    from app.schemas import ServerStatus
    from app.ui import dashboard

    fake_ui = FakeUI()
    monkeypatch.setattr(dashboard, "ui", fake_ui)
    dashboard.render_collapsed_summary(ServerStatus(alias="partial", online=True, gpu=[{}]))

    tooltips = [call[1][0] for call in fake_ui.calls if call[0] == "tooltip"]
    assert "GPU 0 · 计算 未知 · 显存 未知" in tooltips
    assert not any("0%" in tooltip for tooltip in tooltips)
    assert not any(call[0] == "style:element" and "height:" in call[1][0]
                   for call in fake_ui.calls)

    offline = ServerStatus(
        alias="offline", online=False, gpu=[{"utilization_gpu_percent": 99}],
        cpu_percent=99, memory={"used_percent": 99},
        disks=[{"use_percent": "99%"}],
    )
    assert all(percent is None for _, percent, _ in dashboard.system_metrics(offline))
    fake_ui.calls.clear()
    dashboard.render_collapsed_summary(offline)
    assert not any(call[0] == "tooltip" and call[1][0].startswith("GPU ")
                   for call in fake_ui.calls)
    assert ("label", ("暂未获取指标",), {}) in fake_ui.calls


def test_dashboard_details_retain_all_gpu_models_and_disk_mounts(monkeypatch):
    from app.schemas import ServerStatus
    from app.ui import dashboard

    fake_ui = FakeUI()
    monkeypatch.setattr(dashboard, "ui", fake_ui)
    status = ServerStatus(
        alias="mixed-gpu", hostname="compute-host", online=True, cpu_percent=12,
        gpu=[{"name": f"model-{index}", "utilization_gpu_percent": 20,
              "memory_used_mib": 4096, "memory_total_mib": 8192}
             for index in range(9)],
        disks=[{"mount": f"/data/{index}", "used": "1T", "size": "2T", "use_percent": "50%"}
               for index in range(7)],
    )

    dashboard.render_server_details(status)

    labels = [call[1][0] for call in fake_ui.calls if call[0] == "label"]
    assert {"mixed-gpu", "compute-host", "在线", "12%"} <= set(labels)
    assert {f"GPU {index}" for index in range(9)} <= set(labels)
    assert {f"model-{index}" for index in range(9)} <= set(labels)
    assert {f"磁盘 /data/{index}" for index in range(7)} <= set(labels)
    assert "4.0 GiB / 8.0 GiB" in labels
    assert "1T / 2T · 50%" in labels
    assert "混合型号" in dashboard.gpu_spec(status)


def test_dashboard_open_details_survive_grid_refresh_and_are_cleaned_up_on_close(monkeypatch):
    from app.schemas import ServerStatus
    from app.ui import dashboard

    fake_ui = FakeUI()
    monkeypatch.setattr(dashboard, "ui", fake_ui)
    monkeypatch.setattr(dashboard, "_status_cache", {})
    status = ServerStatus(alias="gpu01", online=True, cpu_percent=12)
    container = fake_ui.column()
    dashboard.render_results(container, [status.alias], [status])
    page = fake_ui.context.client.content
    initial_page_children = tuple(page.children)

    for _ in range(3):
        old_card = container.children[0]
        with old_card:
            dashboard.open_server_details(status)
        dialog = next(element for element in reversed(fake_ui.elements) if element.kind == "dialog")
        owner = dialog.parent
        canary = next(child for child in owner.children if child.kind == "dialog_canary")
        assert dialog.value is True
        assert owner.parent is page

        dashboard.render_results(container, [status.alias], [status])

        assert old_card.is_deleted
        assert not owner.is_deleted
        assert not dialog.is_deleted
        assert dialog.value is True
        dialog.close()
        dialog.events["hide"]()
        assert dialog.is_deleted
        assert owner.is_deleted
        assert canary.is_deleted
        assert tuple(page.children) == initial_page_children


def test_dashboard_summary_includes_ram_percent_and_expansion_state():
    from app.schemas import ServerStatus
    from app.ui import dashboard

    status = ServerStatus(
        alias="gpu01",
        online=True,
        memory={"total_kib": 2000, "available_kib": 500, "used_percent": 75.000000000004},
    )

    assert dashboard.memory_used_percent(status) == 75.0
    assert dashboard.format_percent(dashboard.memory_used_percent(status)) == "75%"

    dashboard.set_server_expanded("gpu01", True)
    assert dashboard.is_server_expanded("gpu01") is True
    dashboard.set_server_expanded("gpu01", False)
    assert dashboard.is_server_expanded("gpu01") is False


def test_dashboard_thin_usage_bar_hides_value_text(monkeypatch):
    from app.ui import dashboard

    calls = []

    class FakeProgress:
        def classes(self, value):
            calls.append(("classes", value))
            return self

    class FakeUI:
        def linear_progress(self, *args, **kwargs):
            calls.append(("linear_progress", args, kwargs))
            return FakeProgress()

    monkeypatch.setattr(dashboard, "ui", FakeUI())

    dashboard.render_thin_usage_bar(42)

    assert calls[0][0] == "linear_progress"
    assert calls[0][2]["color"] == "positive"
    assert calls[0][2]["show_value"] is False
    assert any(
        call[0] == "classes" and "h-2" in call[1] and "bg-green-2" in call[1]
        for call in calls
    )
    assert not any(
        call[0] == "classes" and "border" in call[1]
        for call in calls
    )


def test_dashboard_cpu_panel_uses_circle_value_without_outer_minibar(monkeypatch):
    from app.schemas import ServerStatus
    from app.ui import dashboard

    calls = []

    class FakeNode:
        def __init__(self, kind):
            self.kind = kind

        def __enter__(self):
            calls.append((f"enter:{self.kind}", (), {}))
            return self

        def __exit__(self, exc_type, exc, tb):
            calls.append((f"exit:{self.kind}", (), {}))
            return False

        def classes(self, value):
            calls.append((f"classes:{self.kind}", (value,), {}))
            return self

        def props(self, value):
            calls.append((f"props:{self.kind}", (value,), {}))
            return self

    class FakeUI:
        def card(self):
            calls.append(("card", (), {}))
            return FakeNode("card")

        def label(self, text):
            calls.append(("label", (text,), {}))
            return FakeNode("label")

        def row(self):
            calls.append(("row", (), {}))
            return FakeNode("row")

        def column(self):
            calls.append(("column", (), {}))
            return FakeNode("column")

        def circular_progress(self, *args, **kwargs):
            calls.append(("circular_progress", args, kwargs))
            return FakeNode("circular_progress")

        def linear_progress(self, *args, **kwargs):
            calls.append(("linear_progress", args, kwargs))
            return FakeNode("linear_progress")

    monkeypatch.setattr(dashboard, "ui", FakeUI())

    dashboard._render_cpu(ServerStatus(alias="gpu01", online=True, cpu_percent=42))

    assert any(call[0] == "label" and call[1][0] == "CPU" for call in calls)
    assert any(call[0] == "circular_progress" and call[2].get("show_value") is False for call in calls)
    assert any(
        call[0] == "props:circular_progress" and "size=72px" in call[1][0]
        for call in calls
    )
    assert any(call[0] == "label" and call[1][0] == "42%" for call in calls)
    assert not any(call[0] == "label" and "42% used" in call[1][0] for call in calls)
    assert not any(call[0] == "linear_progress" for call in calls)


def test_layout_sidebar_has_brand_icons_and_active_state(monkeypatch):
    from app.ui import layout

    fake_ui = FakeUI()
    monkeypatch.setattr(layout, "ui", fake_ui)
    monkeypatch.setattr(layout, "install_connection_status", lambda: None)
    monkeypatch.setattr(layout, "_current_path", lambda: "/projects/42")
    monkeypatch.setattr(layout, "_sidebar_projects", lambda: [(42, "Alpha")])

    layout.app_frame("Projects", lambda: None)

    labels = [call[1][0] for call in fake_ui.calls if call[0] == "label"]
    assert "SuperTerminator" in labels
    assert any(call[0] == "icon" and call[1][0] == "folder_open" for call in fake_ui.calls)
    assert any(call[0] == "icon" and call[1][0] == "folder" for call in fake_ui.calls)
    assert "Alpha" in labels
    assert any(
        call[0] == "props:link" and "aria-current=page" in call[1][0]
        for call in fake_ui.calls
    )


def test_server_batch_import_preserves_names_and_refreshes_once(tmp_path, monkeypatch):
    from app.ui import servers

    target = create_engine_for_settings(Settings(db_path=tmp_path / "batch.db"))
    init_db(target)
    with session_scope(target) as session:
        session.add(Server(alias="existing", name="Keep my name", enabled=False))
    reloaded = []
    monkeypatch.setattr(servers, "_notify", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(servers.ui.navigate, "reload", lambda: reloaded.append(True))
    assert servers.import_servers(["existing", " new ", "new", ""], target_engine=target) == 2
    with session_scope(target) as session:
        rows = {row.alias: (row.name, row.enabled) for row in session.query(Server).all()}
    assert rows == {"existing": ("Keep my name", True), "new": ("new", True)}
    assert reloaded == [True]
    assert servers.import_servers([], target_engine=target) == 0
    assert reloaded == [True]
