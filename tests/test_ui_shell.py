from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import create_engine_for_settings, init_db, session_scope
from app.models import Server


class FakeElement:
    def __init__(self, recorder, kind: str, *args, **kwargs):
        self.recorder = recorder
        self.kind = kind
        self.args = args
        self.kwargs = kwargs
        recorder.append((kind, args, kwargs))

    def __enter__(self):
        self.recorder.append((f"enter:{self.kind}", (), {}))
        return self

    def __exit__(self, exc_type, exc, tb):
        self.recorder.append((f"exit:{self.kind}", (), {}))
        return False

    def classes(self, value: str):
        self.recorder.append((f"classes:{self.kind}", (value,), {}))
        return self


class FakeUI:
    def __init__(self):
        self.calls = []

    def page_title(self, title: str):
        self.calls.append(("page_title", (title,), {}))

    def header(self):
        return FakeElement(self.calls, "header")

    def left_drawer(self, *args, **kwargs):
        return FakeElement(self.calls, "left_drawer", *args, **kwargs)

    def column(self):
        return FakeElement(self.calls, "column")

    def card(self):
        return FakeElement(self.calls, "card")

    def label(self, text: str):
        return FakeElement(self.calls, "label", text)

    def icon(self, name: str):
        return FakeElement(self.calls, "icon", name)

    def row(self):
        return FakeElement(self.calls, "row")

    def link(self, text: str | None = None, target: str | None = None):
        return FakeElement(self.calls, "link", text, target)


def test_app_frame_sets_title_navigation_and_invokes_content(monkeypatch):
    from app.ui import layout

    fake_ui = FakeUI()
    monkeypatch.setattr(layout, "ui", fake_ui)
    rendered = []

    layout.app_frame("Projects", lambda: rendered.append("content"))

    assert ("page_title", ("gpu-ssh-panel",), {}) in fake_ui.calls
    assert ("label", ("gpu-ssh-panel",), {}) in fake_ui.calls
    assert ("label", ("Projects",), {}) in fake_ui.calls
    assert any(call[0] == "classes:header" and "bg-white" in call[1][0] for call in fake_ui.calls)
    assert ("classes:label", ("text-lg font-bold",), {}) in fake_ui.calls
    assert ("classes:label", ("text-sm opacity-70",), {}) in fake_ui.calls
    assert ("left_drawer", (), {"value": True}) in fake_ui.calls
    assert any(call[0] == "classes:left_drawer" and "bg-grey-1" in call[1][0] for call in fake_ui.calls)
    assert any(call[0] == "classes:link" and "rounded-xl" in call[1][0] for call in fake_ui.calls)
    assert ("classes:column", ("w-full p-4 gap-4",), {}) in fake_ui.calls
    assert [call for call in fake_ui.calls if call[0] == "link"] == [
        ("link", (None, target), {}) for _label, target, _icon in layout.NAV_ITEMS
    ]
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
    monkeypatch.setattr(main_module, "settings", SimpleNamespace(host="0.0.0.0", port=9000, reload=True))
    monkeypatch.setattr(main_module, "init_db", lambda: called.append("init_db"))
    monkeypatch.setattr(
        main_module.ui,
        "run",
        lambda **kwargs: called.append(("run", kwargs)),
    )

    main_module.main()

    assert called == [
        "init_db",
        ("run", {"host": "0.0.0.0", "port": 9000, "reload": True, "title": "gpu-ssh-panel"}),
    ]


def test_settings_page_uses_app_frame_and_shows_settings(monkeypatch):
    import app.main as main_module

    fake_ui = FakeUI()
    captured = {}

    def fake_app_frame(title, content):
        captured["title"] = title
        content()

    monkeypatch.setattr(main_module, "ui", fake_ui)
    monkeypatch.setattr(main_module, "app_frame", fake_app_frame)
    monkeypatch.setattr(
        main_module,
        "settings",
        SimpleNamespace(db_path="/tmp/panel.db", refresh_seconds=42, show_debug_terminal=True),
    )

    main_module.settings_page()

    assert captured["title"] == "Settings"
    labels = [call[1][0] for call in fake_ui.calls if call[0] == "label"]
    assert "Database: /tmp/panel.db" in labels
    assert "Refresh interval: 42s" in labels
    assert "Show debug terminal: True" in labels


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
    assert dashboard.ratio_percent(25, 100) == 25.0
    assert dashboard.ratio_percent(1, 0) == 0.0
    assert dashboard.format_mib(81920) == "80.0 GiB"
    assert dashboard.format_kib(1048576) == "1.0 GiB"
    assert dashboard.parse_percent_value("46%") == 46.0


def test_dashboard_uses_two_column_grid_classes(monkeypatch):
    from app.ui import dashboard

    created = []

    class FakeElement:
        def __init__(self, kind):
            self.kind = kind
            created.append((kind, None))

        def classes(self, value):
            created.append((f"classes:{self.kind}", value))
            return self

        def clear(self):
            created.append((f"clear:{self.kind}", None))

        def props(self, value):
            created.append((f"props:{self.kind}", value))
            return self

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeUI:
        def label(self, *_args, **_kwargs):
            return FakeElement("label")

        def button(self, *_args, **_kwargs):
            return FakeElement("button")

        def timer(self, *_args, **_kwargs):
            return FakeElement("timer")

        def column(self):
            return FakeElement("column")

        def row(self):
            return FakeElement("row")

    monkeypatch.setattr(dashboard, "ui", FakeUI())

    dashboard.render_dashboard_page()

    assert ("classes:column", "w-full grid grid-cols-1 lg:grid-cols-2 gap-4") in created


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
    assert calls[0][2]["show_value"] is False
    assert any(
        call[0] == "classes" and "h-2" in call[1] and "bg-grey-7" in call[1]
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
    monkeypatch.setattr(layout, "_current_path", lambda: "/projects/42")

    layout.app_frame("Projects", lambda: None)

    labels = [call[1][0] for call in fake_ui.calls if call[0] == "label"]
    assert "GPU SSH Panel" in labels
    assert "Local SuperTerminal" in labels
    assert any(call[0] == "icon" and call[1][0] == "folder_open" for call in fake_ui.calls)
    assert any(
        call[0] == "classes:link" and "bg-primary" in call[1][0]
        for call in fake_ui.calls
    )
