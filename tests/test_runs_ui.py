from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.db import create_engine_for_settings, init_db, session_scope
from app.models import Project, Run, Server, Template


def _seed_run(engine, *, name: str, server_alias: str = "gpu01") -> int:
    with session_scope(engine) as session:
        project = Project(name=f"project-{name}")
        server = Server(alias=server_alias, name=server_alias.upper())
        template = Template(project=project, name="train", command_template="python train.py")
        run = Run(
            project=project,
            server=server,
            template=template,
            workdir="/data/demo",
            name=name,
            tmux_session=f"gpu-panel-20260614-120000-{name}",
            rendered_command=f"echo {name}",
            status="running",
        )
        session.add(run)
        session.flush()
        return run.id


def test_runs_helpers_list_recent_and_load_detail(tmp_path: Path):
    from app.ui import runs

    engine = create_engine_for_settings(Settings(db_path=tmp_path / "app.db"))
    init_db(engine)
    first_id = _seed_run(engine, name="first", server_alias="gpu01")
    second_id = _seed_run(engine, name="second", server_alias="gpu02")

    recent = runs.list_recent_runs(target_engine=engine)

    assert [item["id"] for item in recent] == [second_id, first_id]
    assert recent[0] == {
        "id": second_id,
        "name": "second",
        "project_name": "project-second",
        "server_alias": "gpu02",
        "status": "running",
        "tmux_session": f"gpu-panel-20260614-120000-second",
    }

    detail = runs.get_run_detail(second_id, target_engine=engine)
    assert detail is not None
    assert detail["id"] == second_id
    assert detail["project_name"] == "project-second"
    assert detail["server_alias"] == "gpu02"
    assert detail["workdir"] == "/data/demo"
    assert detail["rendered_command"] == "echo second"


def test_mark_run_exited_updates_status(tmp_path: Path):
    from app.ui import runs

    engine = create_engine_for_settings(Settings(db_path=tmp_path / "app.db"))
    init_db(engine)
    run_id = _seed_run(engine, name="to-stop")

    assert runs.mark_run_exited(run_id, target_engine=engine) is True

    with session_scope(engine) as session:
        assert session.get(Run, run_id).status == "exited"


def test_runs_routes_are_wired(monkeypatch):
    import app.main as main_module

    captured = []
    monkeypatch.setattr(main_module, "app_frame", lambda title, content: captured.append((title, content)))

    main_module.runs_page()
    main_module.run_detail_page("42")

    assert captured[0] == ("Runs", main_module.render_runs_page)
    assert captured[1][0] == "Run"
    assert callable(captured[1][1])

    detail_calls = []
    monkeypatch.setattr(main_module, "render_run_detail", lambda run_id: detail_calls.append(run_id))
    captured[1][1]()
    assert detail_calls == [42]
