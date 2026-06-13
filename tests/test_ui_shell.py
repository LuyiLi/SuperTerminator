from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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

    def link(self, text: str, target: str):
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
    assert ("classes:header", ("items-center",), {}) in fake_ui.calls
    assert ("classes:label", ("text-lg font-bold",), {}) in fake_ui.calls
    assert ("classes:label", ("text-sm opacity-70",), {}) in fake_ui.calls
    assert ("left_drawer", (), {"value": True}) in fake_ui.calls
    assert ("classes:left_drawer", ("bg-grey-1",), {}) in fake_ui.calls
    assert ("classes:link", ("block p-2",), {}) in fake_ui.calls
    assert ("classes:column", ("w-full p-4 gap-4",), {}) in fake_ui.calls
    assert [call for call in fake_ui.calls if call[0] == "link"] == [
        ("link", (label, target), {}) for label, target in layout.NAV_ITEMS
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
    main_module.runs_page()
    main_module.servers_page()

    assert captured == [
        ("Home", main_module.render_dashboard_page),
        ("Projects", captured[1][1]),
        ("Runs", captured[2][1]),
        ("Servers", main_module.render_servers_page),
    ]

    messages = []
    monkeypatch.setattr(main_module, "empty_state", lambda message: messages.append(message))
    captured[1][1]()
    captured[2][1]()
    assert messages == ["Projects will appear here.", "Runs will appear here."]


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
