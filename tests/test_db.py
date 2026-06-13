from pathlib import Path

from app.config import Settings
from app.db import create_engine_for_settings, init_db, session_scope
from app.models import Project, Server


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
