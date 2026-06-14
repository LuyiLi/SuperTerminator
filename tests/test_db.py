from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.db import create_engine_for_settings, init_db, session_scope
from app.models import Preset, Project, ProjectServer, Server, Template


def test_init_db_creates_tables_and_persists_records(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "app.db")
    engine = create_engine_for_settings(settings)
    init_db(engine)

    with session_scope(engine) as session:
        session.add(Server(alias="gpu01", name="GPU 01"))
        session.add(Project(name="demo", default_workdir="/data/demo"))

    with session_scope(engine) as session:
        assert session.query(Server).filter_by(alias="gpu01").one().name == "GPU 01"
        assert session.query(Project).filter_by(name="demo").one().default_workdir == "/data/demo"


def test_sqlite_foreign_keys_are_enforced(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "app.db")
    engine = create_engine_for_settings(settings)
    init_db(engine)

    with pytest.raises(IntegrityError):
        with session_scope(engine) as session:
            session.add(ProjectServer(project_id=999, server_id=999))


def test_server_tags_in_place_mutation_persists(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "app.db")
    engine = create_engine_for_settings(settings)
    init_db(engine)

    with session_scope(engine) as session:
        server = Server(alias="gpu01", tags=["linux"])
        session.add(server)
        session.flush()
        server_id = server.id

    with session_scope(engine) as session:
        server = session.get(Server, server_id)
        assert server is not None
        server.tags.append("cuda")

    with session_scope(engine) as session:
        server = session.get(Server, server_id)
        assert server is not None
        assert server.tags == ["linux", "cuda"]


def test_preset_values_json_in_place_mutation_persists(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "app.db")
    engine = create_engine_for_settings(settings)
    init_db(engine)

    with session_scope(engine) as session:
        project = Project(name="demo")
        template = Template(
            project=project,
            name="deploy",
            command_template="echo {env}",
            variables_schema=[],
        )
        session.add_all([project, template])
        session.flush()

        preset = Preset(
            project_id=project.id,
            template=template,
            name="prod",
            values_json={"env": "staging"},
        )
        session.add(preset)
        session.flush()
        preset_id = preset.id

    with session_scope(engine) as session:
        preset = session.get(Preset, preset_id)
        assert preset is not None
        preset.values_json["env"] = "prod"

    with session_scope(engine) as session:
        preset = session.get(Preset, preset_id)
        assert preset is not None
        assert preset.values_json == {"env": "prod"}


def test_template_variables_schema_in_place_append_persists(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "app.db")
    engine = create_engine_for_settings(settings)
    init_db(engine)

    with session_scope(engine) as session:
        project = Project(name="demo")
        template = Template(
            project=project,
            name="deploy",
            command_template="echo {env}",
            variables_schema=[{"name": "env"}],
        )
        session.add_all([project, template])
        session.flush()
        template_id = template.id

    with session_scope(engine) as session:
        template = session.get(Template, template_id)
        assert template is not None
        template.variables_schema.append({"name": "region"})

    with session_scope(engine) as session:
        template = session.get(Template, template_id)
        assert template is not None
        assert template.variables_schema == [{"name": "env"}, {"name": "region"}]


def test_template_variables_schema_nested_dict_mutation_persists(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "app.db")
    engine = create_engine_for_settings(settings)
    init_db(engine)

    with session_scope(engine) as session:
        project = Project(name="demo")
        template = Template(
            project=project,
            name="deploy",
            command_template="echo {env}",
            variables_schema=[{"name": "env"}],
        )
        session.add_all([project, template])
        session.flush()
        template_id = template.id

    with session_scope(engine) as session:
        template = session.get(Template, template_id)
        assert template is not None
        template.variables_schema[0]["required"] = True

    with session_scope(engine) as session:
        template = session.get(Template, template_id)
        assert template is not None
        assert template.variables_schema[0]["required"] is True


def test_project_workdir_allows_only_one_default_per_project_server(tmp_path: Path):
    from sqlalchemy.exc import IntegrityError

    from app.models import ProjectServer, ProjectWorkdir

    settings = Settings(db_path=tmp_path / "app.db")
    engine = create_engine_for_settings(settings)
    init_db(engine)

    with session_scope(engine) as session:
        server = Server(alias="gpu01", name="GPU 01")
        project = Project(name="demo", default_workdir="/data/demo")
        session.add_all([server, project])
        session.flush()
        link = ProjectServer(project_id=project.id, server_id=server.id)
        session.add(link)
        session.flush()
        session.add(ProjectWorkdir(project_server_id=link.id, path="/data/one", label="one", is_default=True))
        link_id = link.id

    with pytest.raises(IntegrityError):
        with session_scope(engine) as session:
            session.add(ProjectWorkdir(project_server_id=link_id, path="/data/two", label="two", is_default=True))


def test_init_db_migrates_existing_project_workdir_default_index(tmp_path: Path):
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import IntegrityError

    from app.models import ProjectWorkdir

    db_path = tmp_path / "legacy.db"
    legacy_engine = create_engine(f"sqlite:///{db_path}", future=True)
    with legacy_engine.begin() as conn:
        conn.execute(text("CREATE TABLE project_servers (id INTEGER PRIMARY KEY)"))
        conn.execute(
            text(
                "CREATE TABLE project_workdirs ("
                "id INTEGER PRIMARY KEY, "
                "project_server_id INTEGER, "
                "path VARCHAR(1024), "
                "label VARCHAR(255), "
                "is_default BOOLEAN)"
            )
        )
        conn.execute(text("INSERT INTO project_servers (id) VALUES (1)"))
        conn.execute(
            text(
                "INSERT INTO project_workdirs "
                "(id, project_server_id, path, label, is_default) VALUES "
                "(1, 1, '/data/one', 'one', 1), "
                "(2, 1, '/data/two', 'two', 1)"
            )
        )
    legacy_engine.dispose()

    engine = create_engine_for_settings(Settings(db_path=db_path))
    init_db(engine)

    with engine.connect() as conn:
        indexes = [row[1] for row in conn.execute(text("PRAGMA index_list('project_workdirs')"))]
        defaults = conn.execute(
            text(
                "SELECT id, is_default FROM project_workdirs "
                "WHERE project_server_id = 1 ORDER BY id"
            )
        ).all()

    assert "ix_project_workdirs_one_default" in indexes
    assert defaults == [(1, 0), (2, 1)]

    with pytest.raises(IntegrityError):
        with session_scope(engine) as session:
            session.add(ProjectWorkdir(project_server_id=1, path="/data/three", label="three", is_default=True))
