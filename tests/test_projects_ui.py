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

    assert projects.list_enabled_servers(target_engine=engine) == [(enabled_id, "gpu01")]
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
    assert options.workdirs_by_server == {
        enabled_id: {
            "/data/default": "default (/data/default)",
            "/data/alt": "alt (/data/alt)",
        }
    }
    assert all(path.startswith("/data/") for path in options.workdir_options)


def test_unlink_server_from_project_removes_link_and_workdirs(engine, monkeypatch):
    from app.ui import projects

    notices = []
    reloaded = []
    monkeypatch.setattr(projects.ui, "notify", lambda message, **kwargs: notices.append((message, kwargs)))
    monkeypatch.setattr(projects.ui.navigate, "reload", lambda: reloaded.append(True))
    with session_scope(engine) as session:
        project = Project(name="Demo")
        server = Server(alias="gpu01", enabled=True)
        session.add_all([project, server])
        session.flush()
        link = ProjectServer(project_id=project.id, server_id=server.id, enabled=True)
        session.add(link)
        session.flush()
        session.add(ProjectWorkdir(project_server_id=link.id, path="/data/demo", label="main"))
        project_id = project.id
        server_id = server.id
        link_id = link.id

    projects._project_status_cache["gpu01"] = object()

    assert projects.unlink_server_from_project(link_id, target_engine=engine) is True

    assert projects.list_project_servers(project_id, target_engine=engine) == []
    assert projects.list_unlinked_enabled_servers(project_id, target_engine=engine) == [(server_id, "gpu01")]
    with session_scope(engine) as session:
        assert session.query(ProjectServer).count() == 0
        assert session.query(ProjectWorkdir).count() == 0
    assert "gpu01" not in projects._project_status_cache
    assert reloaded == [True]
    assert notices[-1] == ("Server 'gpu01' removed from project.", {"type": "positive"})

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

    assert [run["id"] for run in runs] == [new_id, old_id]
    assert runs[0]["name"] == "new"
    assert runs[0]["server_id"] == other_server_id
    assert runs[0]["server_alias"] == "gpu02"
    assert runs[0]["status"] == "running"
    assert runs[0]["is_favorite"] is False
    assert runs[1]["name"] == "old"
    assert runs[1]["server_id"] == server_id
    assert runs[1]["status"] == "exited"


def test_project_run_favorites_helpers_toggle_and_list(engine, monkeypatch):
    from app.ui import projects

    notices = []
    reloaded = []
    monkeypatch.setattr(projects.ui, "notify", lambda message, **kwargs: notices.append((message, kwargs)))
    monkeypatch.setattr(projects.ui.navigate, "reload", lambda: reloaded.append(True))

    with session_scope(engine) as session:
        project = Project(name="Demo")
        server = Server(alias="gpu01", enabled=True)
        session.add_all([project, server])
        session.flush()
        template = Template(project_id=project.id, name="manual", command_template="echo hi")
        session.add(template)
        session.flush()
        run = Run(
            project_id=project.id,
            server_id=server.id,
            template_id=template.id,
            workdir="/data/demo",
            name="repeat",
            tmux_session="tmux-repeat",
            rendered_command="python train.py",
            status="exited",
        )
        session.add(run)
        session.flush()
        project_id = project.id
        run_id = run.id
        server_id = server.id

    assert projects.list_project_favorite_runs(project_id, target_engine=engine) == []
    assert projects.set_run_favorite(run_id, True, target_engine=engine) is True

    favorite_runs = projects.list_project_favorite_runs(project_id, target_engine=engine)
    assert len(favorite_runs) == 1
    assert favorite_runs[0]["id"] == run_id
    assert favorite_runs[0]["name"] == "repeat"
    assert favorite_runs[0]["server_id"] == server_id
    assert favorite_runs[0]["status"] == "exited"
    assert favorite_runs[0]["is_favorite"] is True
    assert projects.set_run_favorite(run_id, False, target_engine=engine) is True
    assert projects.list_project_favorite_runs(project_id, target_engine=engine) == []
    assert reloaded == [True, True]
    assert notices[0] == ("Run favorited.", {"type": "positive"})
    assert notices[1] == ("Run removed from favorites.", {"type": "positive"})


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
    assert "bash -lc" in fake.commands[-1][1]
    assert "cd /data/demo" in fake.commands[-1][1]
    assert "echo before && torchrun train.py" in fake.commands[-1][1]

    with session_scope(engine) as session:
        stored = session.get(Run, run.id)
        assert stored is not None
        assert stored.rendered_command == "echo before && torchrun train.py"
        assert stored.template.name == "Direct command"


def test_project_defaults_to_monitor_and_keeps_launch_draft_when_switching(monkeypatch):
    from nicegui import ui
    from app.ui import projects, runs

    monkeypatch.setattr(projects, "_project_query", lambda: {})
    monkeypatch.setattr(projects, "get_project", lambda project_id: SimpleNamespace(
        id=project_id, name="Demo", git_url="git@example/demo.git", default_workdir="/data/demo"
    ))
    views = []
    real_views = projects._ProjectViews

    def capture_views(*args, **kwargs):
        result = real_views(*args, **kwargs)
        views.append(result)
        return result

    monkeypatch.setattr(projects, "_ProjectViews", capture_views)
    rendered = []
    monitor = {}
    controls = {}
    reused = []

    def workbench(**kwargs):
        rendered.append("monitor")
        monitor.update(kwargs)

    def launch(project_id, on_back):
        rendered.append("launch")
        controls["draft"] = ui.input("Draft")
        controls["back"] = on_back
        return reused.append

    monkeypatch.setattr(runs, "render_run_workbench", workbench)
    monkeypatch.setattr(projects, "_render_project_launch", launch)
    with ui.column() as owner:
        projects.render_project_detail(7)
    try:
        assert rendered == ["monitor"]
        assert monitor["project_id"] == 7
        views[0].show("launch")
        controls["draft"].value = "edited command"
        controls["back"]()
        assert views[0].current == "monitor"
        assert not views[0].panels["launch"].visible
        views[0].show("launch")
        assert controls["draft"].value == "edited command"
        assert rendered == ["monitor", "launch"]
        controls["back"]()
        source = {"id": 9, "project_id": 7, "name": "previous"}
        monitor["on_reuse"](source)
        assert views[0].current == "launch"
        assert reused == [source]
    finally:
        owner.delete()


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

        def props(self, value):
            calls.append(("props", value))
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
    assert ("label", "train") in calls
    assert ("label", "#9 · gpu01 · running") in calls
    assert ("label", "conda activate env") in calls
    assert ("button", "Use config", True) in calls
    assert ("button", "Favorite", True) in calls
    assert ("props", "icon=star_border") in calls
    assert ("link", "Open", "/runs/9") in calls

    calls.clear()
    monkeypatch.setattr(projects, "list_project_recent_runs", lambda project_id: [])
    projects.render_project_recent_runs_panel(1)
    assert ("label", "No runs for this project yet.") in calls


def test_launch_tab_uses_pasted_command_mode_and_refills_from_run(command_form):
    assert command_form.find("命令启动") is not None
    assert command_form.find("目标机器").options == {3: "gpu01"}
    assert command_form.find("目标机器").value == 3
    assert command_form.find("工作目录").options == {"/data/demo": "main"}
    assert command_form.find("工作目录").value == "/data/demo"
    assert command_form.find("完整启动命令") is not None
    assert command_form.find("启动训练").enabled

    command_form.reuse(
        {
            "name": "old run",
            "server_id": 3,
            "workdir": "/data/demo",
            "rendered_command": "torchrun train.py --run_name=meaningful_v4",
        }
    )

    assert command_form.find("完整启动命令").value == "torchrun train.py --run_name=meaningful_v4"
    assert command_form.find("目标机器").value == 3
    assert command_form.find("工作目录").value == "/data/demo"
    assert command_form.find("训练名称 (--run_name)").value == "meaningful_v4"
    assert "copy of" not in command_form.find("训练名称 (--run_name)").value


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

    monkeypatch.setattr(projects, "ui", FakeUI())
    monkeypatch.setattr(
        projects,
        "create_scoped_timer",
        lambda _owner, interval, _callback, **kwargs: calls.append(("timer", (interval,), kwargs)),
    )
    monkeypatch.setattr(
        projects,
        "list_project_servers",
        lambda project_id: [{"server_alias": "gpu01"}, {"server_alias": "gpu02"}],
    )
    monkeypatch.setattr(projects, "render_pending_server_card", lambda alias: calls.append(("pending", alias)), raising=False)
    monkeypatch.setattr(projects, "render_server_card", lambda status: calls.append(("status", status.alias)), raising=False)
    projects._project_status_cache.clear()

    projects.render_project_servers_status_panel(1)

    assert ("label", "项目资源") in calls
    assert ("pending", "gpu01") in calls
    assert ("pending", "gpu02") in calls
    assert any(call[0] == "button" and call[1] == "刷新资源" for call in calls)
    assert any(call[0] == "timer" and call[1][0] == 0 for call in calls)

    calls.clear()
    monkeypatch.setattr(projects, "list_project_servers", lambda project_id: [])
    projects.render_project_servers_status_panel(1)
    assert ("label", "尚未关联机器。请到项目配置 → 机器与目录中添加。") in calls


def test_project_form_selects_use_nicegui_value_to_label_contract(monkeypatch):
    from app.ui import projects

    captured_selects = []

    class FakeElement:
        value = None

        def classes(self, *_args, **_kwargs):
            return self

        def props(self, *_args, **_kwargs):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    fake_ui = SimpleNamespace(
        label=lambda *_args, **_kwargs: FakeElement(),
        expansion=lambda *_args, **_kwargs: FakeElement(),
        column=lambda *_args, **_kwargs: FakeElement(),
        input=lambda *_args, **_kwargs: FakeElement(),
        textarea=lambda *_args, **_kwargs: FakeElement(),
        button=lambda *_args, **_kwargs: FakeElement(),
        separator=lambda *_args, **_kwargs: None,
        select=lambda options, **kwargs: captured_selects.append((kwargs.get("label"), options)) or FakeElement(),
    )
    monkeypatch.setattr(projects, "ui", fake_ui)
    monkeypatch.setattr(projects, "list_enabled_servers", lambda: [(7, "gpu01"), (8, "gpu02")])
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
        ("Server", {7: "gpu01", 8: "gpu02"}),
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


def test_hide_command_history_value_persists_for_db_directory(engine):
    from app.ui import projects

    assert projects.hide_command_history_value(
        "param_value", "cmd:torchrun|option|--task", "OldTask", target_engine=engine
    )
    hidden = projects._load_hidden_history(engine)
    assert projects._history_id("param_value", "cmd:torchrun|option|--task", "OldTask") in hidden
    assert projects._visible_history_options(
        ["OldTask", "NewTask"],
        scope="param_value",
        key="cmd:torchrun|option|--task",
        hidden=hidden,
    ) == ["NewTask"]


@pytest.mark.parametrize("target", ["https://example.com/runs", "//example.com/runs", "/projects/1", "https://["])
def test_project_return_path_rejects_non_runs_destinations(target):
    from app.ui.projects import _run_return_path

    assert _run_return_path(target) is None
    assert _run_return_path("/runs?run=9&filter=attention&tab=logs") == "/runs?run=9&filter=attention&tab=logs"


@pytest.fixture
def command_form(monkeypatch):
    from nicegui import ui
    from app.ui import projects

    monkeypatch.setattr(projects, "build_launch_options", lambda _id: SimpleNamespace(
        server_options={3: "gpu01"}, workdir_options={"/data/demo": "main"},
        workdirs_by_server={3: {"/data/demo": "main"}},
    ))
    monkeypatch.setattr(projects, "list_project_historical_commands", lambda _id: ["python train.py --seed 42"])
    monkeypatch.setattr(projects, "_load_hidden_history", lambda: set())
    notices = []
    monkeypatch.setattr(projects, "_notify", lambda message, **kwargs: notices.append(message))
    monkeypatch.setattr(ui.navigate, "to", lambda _target: None)
    callbacks = {}
    real_button = ui.button

    def record_button(*args, **kwargs):
        label = args[0] if args else kwargs.get("text", "")
        if kwargs.get("on_click") is not None:
            callbacks[label] = kwargs["on_click"]
        return real_button(*args, **kwargs)

    monkeypatch.setattr(ui, "button", record_button)
    with ui.column() as owner:
        reuse = projects._render_launch_tab(7)

    def find(label):
        return next(item for item in owner.descendants()
                    if item.props.get("label") == label or getattr(item, "text", None) == label)

    def click(label):
        return callbacks[label]

    yield SimpleNamespace(owner=owner, reuse=reuse, find=find, click=click, notices=notices)
    owner.delete()


@pytest.mark.asyncio
async def test_command_editor_requires_application_and_never_overwrites_new_script(command_form, monkeypatch):
    from app.ui import projects

    launched = []
    monkeypatch.setattr(projects, "launch_direct_command_from_project", lambda **kwargs: launched.append(kwargs))
    command_form.reuse({"id": 8, "name": "old", "server_id": 3, "workdir": "/data/demo",
                        "rendered_command": "python train.py --seed 42"})
    expansion = command_form.find("参数编辑器")
    assert expansion.value is False
    value = next(item for item in command_form.owner.descendants()
                 if item.props.get("label") == "value" and getattr(item, "value", None) == "42")
    value.set_options(["42", "43"], value="43")
    assert not command_form.find("启动训练").enabled
    await command_form.click("启动训练")()
    assert launched == []
    command_form.click("应用到命令")()
    assert "--seed=43" in command_form.find("完整启动命令").value
    assert command_form.find("启动训练").enabled
    command_form.find("完整启动命令").value += " --resume"
    before = command_form.find("完整启动命令").value
    command_form.click("应用到命令")()
    assert command_form.find("完整启动命令").value == before
    assert "请先重新解析" in command_form.notices[-1]
    command_form.click("解析 / 编辑参数")()
    assert command_form.find("参数编辑器").value is True


def test_reuse_missing_machine_or_workdir_never_selects_another_target(command_form):
    command_form.reuse({"id": 8, "name": "old", "server_id": 999, "workdir": "/old",
                        "rendered_command": "python train.py --seed 42"})
    assert command_form.find("目标机器").value is None
    assert command_form.find("工作目录").value is None
    assert "请明确选择新的目标" in command_form.notices[-1]
    command_form.reuse({"id": 9, "name": "old", "server_id": 3, "workdir": "/old",
                        "rendered_command": "python train.py --seed 42"})
    assert command_form.find("目标机器").value == 3
    assert command_form.find("工作目录").value is None


@pytest.mark.asyncio
async def test_command_launch_ignores_double_click_during_inflight_submission(command_form, monkeypatch):
    import asyncio
    from app.ui import projects

    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def launch(**kwargs):
        calls.append(kwargs)
        entered.set()
        await release.wait()
        return SimpleNamespace(id=99, status="running", tmux_session="run-99")

    monkeypatch.setattr(projects, "launch_direct_command_from_project", launch)
    command_form.reuse({"id": 8, "name": "old", "server_id": 3, "workdir": "/data/demo",
                        "rendered_command": "python train.py --seed 42"})
    handler = command_form.click("启动训练")
    first = asyncio.create_task(handler())
    await entered.wait()
    await handler()
    assert len(calls) == 1
    assert calls[0]["source_run_id"] == 8
    assert not command_form.find("启动训练").enabled
    release.set()
    await first
