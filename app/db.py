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
    migrate_run_favorites(target_engine)
    migrate_run_observability(target_engine)
    migrate_sync_launch(target_engine)


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


def migrate_run_favorites(target_engine: Engine = engine) -> None:
    """Add run favorite flags for existing SQLite DBs."""

    with target_engine.begin() as connection:
        table_exists = connection.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runs'")
        ).scalar()
        if not table_exists:
            return

        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(runs)"))}
        if "is_favorite" in columns:
            return

        connection.execute(
            text("ALTER TABLE runs ADD COLUMN is_favorite BOOLEAN NOT NULL DEFAULT 0")
        )


def migrate_run_observability(target_engine: Engine = engine) -> None:
    """Add run identity/status fields and backfill names from saved commands."""

    from app.run_config import extract_training_run_name

    with target_engine.begin() as connection:
        table_exists = connection.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runs'")
        ).scalar()
        if not table_exists:
            return

        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(runs)"))}
        migrations = {
            "training_run_name": (
                "ALTER TABLE runs ADD COLUMN training_run_name "
                "VARCHAR(255) NOT NULL DEFAULT ''"
            ),
            "exit_code": "ALTER TABLE runs ADD COLUMN exit_code INTEGER",
            "status_source": (
                "ALTER TABLE runs ADD COLUMN status_source "
                "VARCHAR(32) NOT NULL DEFAULT 'database'"
            ),
            "status_detail": (
                "ALTER TABLE runs ADD COLUMN status_detail TEXT NOT NULL DEFAULT ''"
            ),
            "launch_source": (
                "ALTER TABLE runs ADD COLUMN launch_source "
                "VARCHAR(32) NOT NULL DEFAULT 'ui'"
            ),
            "source_run_id": "ALTER TABLE runs ADD COLUMN source_run_id INTEGER",
            "launch_preflight_status": (
                "ALTER TABLE runs ADD COLUMN launch_preflight_status "
                "VARCHAR(32) NOT NULL DEFAULT ''"
            ),
            "launch_preflight_observed_at": (
                "ALTER TABLE runs ADD COLUMN launch_preflight_observed_at DATETIME"
            ),
            "launch_preflight_detail": (
                "ALTER TABLE runs ADD COLUMN launch_preflight_detail "
                "TEXT NOT NULL DEFAULT ''"
            ),
            "last_observed_at": "ALTER TABLE runs ADD COLUMN last_observed_at DATETIME",
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(text(statement))

        if "rendered_command" not in columns:
            return

        rows = connection.execute(
            text(
                "SELECT id, rendered_command FROM runs "
                "WHERE training_run_name IS NULL OR training_run_name = ''"
            )
        ).all()
        for run_id, rendered_command in rows:
            training_name = extract_training_run_name(rendered_command or "")
            if training_name:
                connection.execute(
                    text(
                        "UPDATE runs SET training_run_name = :training_name "
                        "WHERE id = :run_id"
                    ),
                    {"training_name": training_name, "run_id": run_id},
                )


def migrate_sync_launch(target_engine: Engine = engine) -> None:
    """Add nullable idempotency keys; historical tasks keep their original behavior."""
    with target_engine.begin() as connection:
        # Several MCP processes may initialize the same SQLite file concurrently.
        connection.execute(text("BEGIN IMMEDIATE"))
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(runs)"))}
        for name, definition in {
            "request_key": "VARCHAR(128)",
            "sync_stage": "VARCHAR(32) NOT NULL DEFAULT ''",
            "sync_metadata": "JSON NOT NULL DEFAULT '{}'",
        }.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE runs ADD COLUMN {name} {definition}"))
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_runs_request_key ON runs (request_key)"
        ))


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
