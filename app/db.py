from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, load_settings
from app.models import Base


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def create_engine_for_settings(settings: Settings) -> Engine:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{settings.db_path}", future=True)
    event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


settings = load_settings()
engine = create_engine_for_settings(settings)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db(target_engine: Engine = engine) -> None:
    Base.metadata.create_all(target_engine)
    migrate_project_workdir_default_index(target_engine)


def migrate_project_workdir_default_index(target_engine: Engine = engine) -> None:
    """Ensure existing SQLite DBs enforce one default workdir per project server."""

    with target_engine.begin() as connection:
        table_exists = connection.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'project_workdirs'"
            )
        ).scalar()
        if not table_exists:
            return

        index_exists = connection.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'index' AND name = 'ix_project_workdirs_one_default'"
            )
        ).scalar()
        if index_exists:
            return

        connection.execute(
            text(
                "UPDATE project_workdirs "
                "SET is_default = 0 "
                "WHERE is_default = 1 "
                "AND id NOT IN ("
                "  SELECT MAX(id) FROM project_workdirs "
                "  WHERE is_default = 1 "
                "  GROUP BY project_server_id"
                ")"
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_project_workdirs_one_default "
                "ON project_workdirs (project_server_id) "
                "WHERE is_default = 1"
            )
        )


@contextmanager
def session_scope(target_engine: Engine = engine) -> Iterator[Session]:
    session_factory = sessionmaker(bind=target_engine, expire_on_commit=False, future=True)
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
