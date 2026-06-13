from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.db import create_engine_for_settings, init_db, session_scope
from app.models import Preset, Project, ProjectServer, ProjectWorkdir, Server, Template


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
    assert options.server_options == {"gpu01": enabled_id}
    assert options.workdir_options == {"gpu01: default (/data/default)": "/data/default", "gpu01: alt (/data/alt)": "/data/alt"}


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
    assert options.template_options == {"train": template_id}
    assert options.preset_options == {"None": None, "quick": preset_id}


def test_project_detail_route_is_wired(monkeypatch):
    import app.main as main_module

    captured = []
    monkeypatch.setattr(main_module, "app_frame", lambda title, content: captured.append((title, content)))

    main_module.projects_page()
    main_module.project_detail_page("42")

    assert captured[0] == ("Projects", main_module.render_projects_page)
    assert captured[1][0] == "Project"
    assert callable(captured[1][1])
