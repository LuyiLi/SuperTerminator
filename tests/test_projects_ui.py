from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import create_engine_for_settings, init_db, session_scope
from app.models import Preset, Project, ProjectServer, ProjectWorkdir, Run, Server, Template
from app.schemas import CommandResult


@pytest.fixture
def engine(tmp_path: Path):
    engine = create_engine_for_settings(Settings(db_path=tmp_path / "app.db"))
    init_db(engine)
    return engine


def test_project_helpers_create_and_list_projects(engine, monkeypatch):
    from app.ui import projects

    notices = []
    monkeypatch.setattr(projects.ui, "notify", lambda message, **kwargs: notices.append((message, kwargs)))
    monkeypatch.setattr(projects.ui.navigate, "reload", lambda: None)

    project_id = projects.create_project(" Demo ", " git@example/demo.git ", " /data/demo ", target_engine=engine)

    with session_scope(engine) as session:
        stored = session.get(Project, project_id)
        assert stored is not None
        assert stored.name == "Demo"
        assert stored.git_url == "git@example/demo.git"
        assert stored.default_workdir == "/data/demo"

    listed = projects.list_projects(target_engine=engine)
    assert [(project.id, project.name) for project in listed] == [(project_id, "Demo")]
    assert notices[-1] == ("Project 'Demo' created.", {"type": "positive"})


def test_create_project_rejects_blank_name(engine, monkeypatch):
    from app.ui import projects

    notices = []
    monkeypatch.setattr(projects.ui, "notify", lambda message, **kwargs: notices.append((message, kwargs)))

    assert projects.create_project("   ", "", "", target_engine=engine) is None
    assert notices == [("Project name is required.", {"type": "negative"})]


def test_server_workdir_helpers_link_enabled_servers_and_options(engine, monkeypatch):
    from app.ui import projects

    monkeypatch.setattr(projects.ui, "notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(projects.ui.navigate, "reload", lambda: None)
    with session_scope(engine) as session:
        project = Project(name="Demo", default_workdir="/data/default")
        enabled = Server(alias="gpu01", name="GPU 01", enabled=True)
        disabled = Server(alias="old", name="Old", enabled=False)
        session.add_all([project, enabled, disabled])
        session.flush()
        project_id = project.id
        enabled_id = enabled.id

    assert projects.list_unlinked_enabled_servers(project_id, target_engine=engine) == [(enabled_id, "gpu01")]

    link_id = projects.link_server_to_project(project_id, enabled_id, "/data/default", target_engine=engine)
    workdir_id = projects.add_project_workdir(link_id, "/data/alt", "alt", is_default=False, target_engine=engine)

    assert projects.list_unlinked_enabled_servers(project_id, target_engine=engine) == []
    linked = projects.list_project_servers(project_id, target_engine=engine)
    assert [(item["id"], item["server_id"], item["server_alias"]) for item in linked] == [
        (link_id, enabled_id, "gpu01")
    ]
    assert [(w["id"], w["path"], w["label"], w["is_default"]) for w in linked[0]["workdirs"]] == [
        (workdir_id - 1, "/data/default", "default", True),
        (workdir_id, "/data/alt", "alt", False),
    ]

    options = projects.build_launch_options(project_id, target_engine=engine)
    assert options.server_options == {enabled_id: "gpu01"}
    assert all(isinstance(value, int) for value in options.server_options)
    assert options.workdir_options == {
        "/data/default": "gpu01: default (/data/default)",
        "/data/alt": "gpu01: alt (/data/alt)",
    }
    assert all(path.startswith("/data/") for path in options.workdir_options)


def test_default_workdir_helpers_keep_only_one_default(engine, monkeypatch):
    from app.ui import projects

    monkeypatch.setattr(projects.ui, "notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(projects.ui.navigate, "reload", lambda: None)
    with session_scope(engine) as session:
        project = Project(name="Demo")
        server = Server(alias="gpu01", enabled=True)
        session.add_all([project, server])
        session.flush()
        link = ProjectServer(project_id=project.id, server_id=server.id, enabled=True)
        session.add(link)
        session.flush()
        link_id = link.id

    first_id = projects.add_project_workdir(
        link_id, "/data/one", "one", is_default=True, target_engine=engine
    )
    second_id = projects.add_project_workdir(
        link_id, "/data/two", "two", is_default=True, target_engine=engine
    )

    with session_scope(engine) as session:
        rows = session.query(ProjectWorkdir).filter_by(project_server_id=link_id).order_by(ProjectWorkdir.id).all()
        assert [(row.id, row.path, row.is_default) for row in rows] == [
            (first_id, "/data/one", False),
            (second_id, "/data/two", True),
        ]


def test_link_server_to_project_replaces_existing_default_workdir(engine, monkeypatch):
    from app.ui import projects

    monkeypatch.setattr(projects.ui, "notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(projects.ui.navigate, "reload", lambda: None)
    with session_scope(engine) as session:
        project = Project(name="Demo")
        server = Server(alias="gpu01", enabled=True)
        session.add_all([project, server])
        session.flush()
        project_id = project.id
        server_id = server.id

    link_id = projects.link_server_to_project(
        project_id, server_id, "/data/one", target_engine=engine
    )
    assert projects.link_server_to_project(
        project_id, server_id, "/data/two", target_engine=engine
    ) == link_id

    with session_scope(engine) as session:
        rows = session.query(ProjectWorkdir).filter_by(project_server_id=link_id).order_by(ProjectWorkdir.id).all()
        assert [(row.path, row.is_default) for row in rows] == [
            ("/data/one", False),
            ("/data/two", True),
        ]


def test_template_and_preset_helpers_build_schema_and_validate_json_like_values(engine, monkeypatch):
    from app.ui import projects

    monkeypatch.setattr(projects.ui, "notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(projects.ui.navigate, "reload", lambda: None)
    with session_scope(engine) as session:
        project = Project(name="Demo")
        session.add(project)
        session.flush()
        project_id = project.id

    template_id = projects.create_project_template(
        project_id,
        "train",
        "python train.py --lr {{ lr }} --epochs {{epochs}}",
        target_engine=engine,
    )
    preset_id = projects.create_project_preset(
        project_id,
        template_id,
        "quick",
        {"lr": "1e-4", "epochs": 2},
        target_engine=engine,
    )

    with session_scope(engine) as session:
        template = session.get(Template, template_id)
        preset = session.get(Preset, preset_id)
        assert template.variables_schema == [
            {"name": "lr", "label": "lr", "default": "", "description": "", "required": True},
            {"name": "epochs", "label": "epochs", "default": "", "description": "", "required": True},
        ]
        assert preset.values_json == {"lr": "1e-4", "epochs": "2"}

    options = projects.build_launch_options(project_id, target_engine=engine)
    assert options.template_options == {template_id: "train"}
    assert all(isinstance(value, int) for value in options.template_options)
    assert options.preset_options == {None: "None", preset_id: "quick"}
    assert None in options.preset_options
    assert any(isinstance(value, int) for value in options.preset_options if value is not None)


def test_list_project_recent_runs_returns_only_project_runs_newest_first(engine):
    from app.ui import projects

    with session_scope(engine) as session:
        project = Project(name="Demo")
        other_project = Project(name="Other")
        server = Server(alias="gpu01", enabled=True)
        other_server = Server(alias="gpu02", enabled=True)
        session.add_all([project, other_project, server, other_server])
        session.flush()
        template = Template(project_id=project.id, name="train", command_template="python train.py")
        other_template = Template(
            project_id=other_project.id, name="eval", command_template="python eval.py"
        )
        session.add_all([template, other_template])
        session.flush()
        old_run = Run(
            project_id=project.id,
            server_id=server.id,
            template_id=template.id,
            workdir="/data/demo",
            name="old",
            tmux_session="tmux-old",
            rendered_command="python train.py",
            status="exited",
        )
        new_run = Run(
            project_id=project.id,
            server_id=other_server.id,
            template_id=template.id,
            workdir="/data/demo",
            name="new",
            tmux_session="tmux-new",
            rendered_command="python train.py --new",
            status="running",
        )
        other_run = Run(
            project_id=other_project.id,
            server_id=server.id,
            template_id=other_template.id,
            workdir="/data/other",
            name="other",
            tmux_session="tmux-other",
            rendered_command="python eval.py",
            status="running",
        )
        session.add_all([old_run, new_run, other_run])
        session.flush()
        project_id = project.id
        new_id = new_run.id
        old_id = old_run.id
        server_id = server.id
        other_server_id = other_server.id

    runs = projects.list_project_recent_runs(project_id, limit=10, target_engine=engine)

    assert runs == [
        {
            "id": new_id,
            "name": "new",
            "server_id": other_server_id,
            "server_alias": "gpu02",
            "workdir": "/data/demo",
            "rendered_command": "python train.py --new",
            "status": "running",
            "tmux_session": "tmux-new",
        },
        {
            "id": old_id,
            "name": "old",
            "server_id": server_id,
            "server_alias": "gpu01",
            "workdir": "/data/demo",
            "rendered_command": "python train.py",
            "status": "exited",
            "tmux_session": "tmux-old",
        },
    ]


@pytest.mark.asyncio
async def test_launch_direct_command_from_project_creates_tmux_run(engine):
    from app.ui import projects

    class FakeSSHClient:
        def __init__(self):
            self.commands = []

        async def run(self, alias, command, timeout=30):
            self.commands.append((alias, command, timeout))
            return CommandResult(0, "", "")

    with session_scope(engine) as session:
        project = Project(name="Demo")
        server = Server(alias="gpu01", enabled=True)
        session.add_all([project, server])
        session.flush()
        link = ProjectServer(project_id=project.id, server_id=server.id, enabled=True)
        session.add(link)
        session.flush()
        session.add(ProjectWorkdir(project_server_id=link.id, path="/data/demo", label="main", is_default=True))
        session.flush()
        project_id = project.id
        server_id = server.id

    fake = FakeSSHClient()
    run = await projects.launch_direct_command_from_project(
        project_id=project_id,
        server_id=server_id,
        workdir="/data/demo",
        run_name="manual run",
        command="echo before && torchrun train.py",
        ssh_client=fake,
        target_engine=engine,
    )

    assert run.status == "running"
    assert run.name == "manual run"
    assert run.rendered_command == "echo before && torchrun train.py"
    assert fake.commands[-1][0] == "gpu01"
    assert "tmux new-session -d -s" in fake.commands[-1][1]
    assert "cd /data/demo && echo before && torchrun train.py" in fake.commands[-1][1]

    with session_scope(engine) as session:
        stored = session.get(Run, run.id)
        assert stored is not None
        assert stored.rendered_command == "echo before && torchrun train.py"
        assert stored.template.name == "Direct command"


def test_project_detail_uses_two_column_layout_and_default_open_launch(monkeypatch):
    from app.ui import projects

    calls = []

    class FakeElement:
        value = None

        def __init__(self, kind):
            self.kind = kind
            calls.append((kind, (), {}))

        def __enter__(self):
            calls.append((f"enter:{self.kind}", (), {}))
            return self

        def __exit__(self, exc_type, exc, tb):
            calls.append((f"exit:{self.kind}", (), {}))
            return False

        def classes(self, value):
            calls.append((f"classes:{self.kind}", (value,), {}))
            return self

    class FakeUI:
        def label(self, text):
            calls.append(("label", (text,), {}))
            return FakeElement("label")

        def grid(self, **kwargs):
            calls.append(("grid", (), kwargs))
            return FakeElement("grid")

        def column(self):
            return FakeElement("column")

        def expansion(self, text, *, value=False):
            calls.append(("expansion", (text,), {"value": value}))
            return FakeElement("expansion")

    monkeypatch.setattr(projects, "ui", FakeUI())
    monkeypatch.setattr(
        projects,
        "get_project",
        lambda project_id: SimpleNamespace(
            id=project_id, name="Demo", git_url="git@example/demo.git", default_workdir="/data/demo"
        ),
    )
    rendered = []
    monkeypatch.setattr(projects, "_render_launch_tab", lambda project_id: rendered.append(("launch", project_id)) or "use-command")
    monkeypatch.setattr(projects, "_render_templates_tab", lambda project_id: rendered.append(("templates", project_id)))
    monkeypatch.setattr(projects, "_render_presets_tab", lambda project_id: rendered.append(("presets", project_id)))
    monkeypatch.setattr(
        projects,
        "_render_servers_workdirs_tab",
        lambda project_id, default_workdir: rendered.append(("servers", project_id, default_workdir)),
    )
    monkeypatch.setattr(projects, "render_project_servers_status_panel", lambda project_id: rendered.append(("status", project_id)), raising=False)
    monkeypatch.setattr(projects, "render_project_recent_runs_panel", lambda project_id, on_use_command=None: rendered.append(("runs", project_id, on_use_command)), raising=False)

    projects.render_project_detail(7)

    assert any(call[0] == "grid" and call[2].get("columns") == 3 for call in calls)
    assert any(
        call[0] == "classes:grid" and "grid-cols-3" in call[1][0] and "grid-cols-1" not in call[1][0]
        for call in calls
    )
    assert ("expansion", ("Launch",), {"value": True}) in calls
    assert any(call[0] == "classes:column" and "col-span-2" in call[1][0] for call in calls)
    assert ("launch", 7) in rendered
    assert ("status", 7) in rendered
    assert ("runs", 7, "use-command") in rendered


def test_project_recent_runs_panel_renders_runs_and_empty_state(monkeypatch):
    from app.ui import projects

    calls = []

    class FakeElement:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def classes(self, value):
            calls.append(("classes", value))
            return self

    class FakeUI:
        def label(self, text):
            calls.append(("label", text))
            return FakeElement()

        def card(self):
            calls.append(("card", None))
            return FakeElement()

        def row(self):
            calls.append(("row", None))
            return FakeElement()

        def column(self):
            calls.append(("column", None))
            return FakeElement()

        def button(self, text, **kwargs):
            calls.append(("button", text, "on_click" in kwargs))
            if "on_click" in kwargs:
                self.last_on_click = kwargs["on_click"]
            return FakeElement()

        def link(self, text, target):
            calls.append(("link", text, target))
            return FakeElement()

    monkeypatch.setattr(projects, "ui", FakeUI())
    monkeypatch.setattr(
        projects,
        "list_project_recent_runs",
        lambda project_id: [
            {
                "id": 9,
                "name": "train",
                "server_id": 3,
                "server_alias": "gpu01",
                "workdir": "/data/demo",
                "rendered_command": "conda activate env\ntorchrun train.py --run_name demo",
                "status": "running",
                "tmux_session": "tmux-9",
            }
        ],
    )

    used = []
    projects.render_project_recent_runs_panel(1, on_use_command=lambda run: used.append(run))

    assert ("label", "Recent Runs") in calls
    assert ("label", "#9 train") in calls
    assert ("label", "gpu01 · running") in calls
    assert ("label", "tmux-9") in calls
    assert ("label", "conda activate env") in calls
    assert ("button", "Use command", True) in calls
    assert ("link", "Open", "/runs/9") in calls

    calls.clear()
    monkeypatch.setattr(projects, "list_project_recent_runs", lambda project_id: [])
    projects.render_project_recent_runs_panel(1)
    assert ("label", "No runs for this project yet.") in calls


def test_launch_tab_uses_pasted_command_mode_and_refills_from_run(monkeypatch):
    from app.ui import projects

    elements = {}
    calls = []

    class FakeElement:
        def __init__(self, label=None, value=None):
            self.label = label
            self.value = value

        def classes(self, value):
            calls.append(("classes", self.label, value))
            return self

        def props(self, value):
            calls.append(("props", self.label, value))
            return self

    class FakeUI:
        navigate = SimpleNamespace(to=lambda target: calls.append(("navigate", target)))

        def label(self, text):
            calls.append(("label", text))
            return FakeElement(text)

        def select(self, options, *, label, value=None):
            element = FakeElement(label, value)
            element.options = options
            elements[label] = element
            calls.append(("select", label, options, value))
            return element

        def input(self, label, *, value=""):
            element = FakeElement(label, value)
            elements[label] = element
            calls.append(("input", label, value))
            return element

        def textarea(self, label, **kwargs):
            element = FakeElement(label, kwargs.get("value", ""))
            elements[label] = element
            calls.append(("textarea", label))
            return element

        def button(self, text, **kwargs):
            calls.append(("button", text, "on_click" in kwargs))
            return FakeElement(text)

    monkeypatch.setattr(projects, "ui", FakeUI())
    monkeypatch.setattr(
        projects,
        "build_launch_options",
        lambda project_id: SimpleNamespace(
            server_options={3: "gpu01"},
            workdir_options={"/data/demo": "gpu01: main (/data/demo)"},
        ),
    )

    use_command = projects._render_launch_tab(1)

    assert ("label", "Launch Command") in calls
    assert ("select", "Server", {3: "gpu01"}, 3) in calls
    assert ("select", "Workdir", {"/data/demo": "gpu01: main (/data/demo)"}, "/data/demo") in calls
    assert any(call[0] == "textarea" and call[1] == "Command" for call in calls)
    assert any(call[0] == "button" and call[1] == "Run in tmux" for call in calls)

    use_command(
        {
            "name": "old run",
            "server_id": 3,
            "workdir": "/data/demo",
            "rendered_command": "torchrun train.py",
        }
    )

    assert elements["Command"].value == "torchrun train.py"
    assert elements["Server"].value == 3
    assert elements["Workdir"].value == "/data/demo"
    assert elements["Run name"].value == "copy of old run"


def test_project_servers_status_panel_renders_linked_statuses_immediately(monkeypatch):
    from app.ui import projects

    calls = []

    class FakeElement:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def classes(self, value):
            calls.append(("classes", value))
            return self

        def props(self, value):
            calls.append(("props", value))
            return self

        def clear(self):
            calls.append(("clear", None))

    class FakeUI:
        def label(self, text):
            calls.append(("label", text))
            return FakeElement()

        def column(self):
            calls.append(("column", None))
            return FakeElement()

        def row(self):
            calls.append(("row", None))
            return FakeElement()

        def button(self, text, **kwargs):
            calls.append(("button", text, "on_click" in kwargs))
            return FakeElement()

        def timer(self, *args, **kwargs):
            calls.append(("timer", args, kwargs))
            return FakeElement()

    monkeypatch.setattr(projects, "ui", FakeUI())
    monkeypatch.setattr(
        projects,
        "list_project_servers",
        lambda project_id: [{"server_alias": "gpu01"}, {"server_alias": "gpu02"}],
    )
    monkeypatch.setattr(projects, "render_pending_server_card", lambda alias: calls.append(("pending", alias)), raising=False)
    monkeypatch.setattr(projects, "render_server_card", lambda status: calls.append(("status", status.alias)), raising=False)
    projects._project_status_cache.clear()

    projects.render_project_servers_status_panel(1)

    assert ("label", "Project Servers") in calls
    assert ("pending", "gpu01") in calls
    assert ("pending", "gpu02") in calls
    assert any(call[0] == "button" and call[1] == "Refresh statuses" for call in calls)
    assert any(call[0] == "timer" and call[1][0] == 0 for call in calls)

    calls.clear()
    monkeypatch.setattr(projects, "list_project_servers", lambda project_id: [])
    projects.render_project_servers_status_panel(1)
    assert ("label", "No linked servers yet.") in calls


def test_project_form_selects_use_nicegui_value_to_label_contract(monkeypatch):
    from app.ui import projects

    captured_selects = []

    class FakeElement:
        value = None

        def classes(self, *_args, **_kwargs):
            return self

    fake_ui = SimpleNamespace(
        label=lambda *_args, **_kwargs: FakeElement(),
        input=lambda *_args, **_kwargs: FakeElement(),
        textarea=lambda *_args, **_kwargs: FakeElement(),
        button=lambda *_args, **_kwargs: FakeElement(),
        separator=lambda *_args, **_kwargs: None,
        select=lambda options, **kwargs: captured_selects.append((kwargs.get("label"), options)) or FakeElement(),
    )
    monkeypatch.setattr(projects, "ui", fake_ui)
    monkeypatch.setattr(projects, "list_unlinked_enabled_servers", lambda project_id: [(7, "gpu01")])
    monkeypatch.setattr(projects, "list_project_servers", lambda project_id: [])
    monkeypatch.setattr(
        projects,
        "list_project_templates",
        lambda project_id: [SimpleNamespace(id=11, name="train")],
    )
    monkeypatch.setattr(projects, "list_project_presets", lambda project_id: [])

    projects._render_servers_workdirs_tab(1, "/data/default")
    projects._render_presets_tab(1)

    assert captured_selects == [
        ("Enabled server not linked", {7: "gpu01"}),
        ("Template", {11: "train"}),
    ]


def test_project_detail_route_is_wired(monkeypatch):
    import app.main as main_module

    captured = []
    monkeypatch.setattr(main_module, "app_frame", lambda title, content: captured.append((title, content)))

    main_module.projects_page()
    main_module.project_detail_page("42")

    assert captured[0] == ("Projects", main_module.render_projects_page)
    assert captured[1][0] == "Project"
    assert callable(captured[1][1])
