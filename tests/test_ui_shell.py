from __future__ import annotations

from types import SimpleNamespace


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

    def left_drawer(self):
        return FakeElement(self.calls, "left_drawer")

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

    assert ("page_title", ("Projects",), {}) in fake_ui.calls
    assert ("label", ("gpu-ssh-panel",), {}) in fake_ui.calls
    assert ("label", ("Projects",), {}) in fake_ui.calls
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
    assert ("label", ("Nothing here yet",), {}) in fake_ui.calls
    assert ("label", ("Boom",), {}) in fake_ui.calls
    assert ("classes:label", ("text-negative",), {}) in fake_ui.calls


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
    assert "Database path: /tmp/panel.db" in labels
    assert "Refresh interval: 42 seconds" in labels
    assert "Debug terminal: enabled" in labels
