from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.db import create_engine_for_settings, init_db, session_scope
from app.models import Project, Run, Server, Template
from app.run_status import (
    get_run_record,
    list_run_records,
    observe_run_records,
    parse_training_progress,
    tail_training_run,
)
from app.schemas import CommandResult


@pytest.fixture
def engine(tmp_path: Path):
    target = create_engine_for_settings(Settings(db_path=tmp_path / "app.db"))
    init_db(target)
    return target


def _seed_run(engine, *, run_id_hint: str, status: str = "running") -> int:
    with session_scope(engine) as session:
        project = session.query(Project).filter_by(name="demo").one_or_none()
        if project is None:
            project = Project(name="demo")
            session.add(project)
        server = session.query(Server).filter_by(alias="gpu01").one_or_none()
        if server is None:
            server = Server(alias="gpu01")
            session.add(server)
        session.flush()
        template = session.query(Template).filter_by(project_id=project.id).one_or_none()
        if template is None:
            template = Template(
                project_id=project.id,
                name="direct",
                command_template="python train.py",
            )
            session.add(template)
            session.flush()
        run = Run(
            project_id=project.id,
            server_id=server.id,
            template_id=template.id,
            workdir="/data/demo",
            name=f"copy of {run_id_hint}",
            rendered_command=f"python train.py --run_name={run_id_hint}",
            tmux_session="placeholder",
            status=status,
            training_run_name=run_id_hint,
        )
        session.add(run)
        session.flush()
        run.tmux_session = f"gpu-panel-20260613-223000-{run.id}"
        return run.id


def _block(
    run_id: int,
    *,
    state: str = "",
    alive: bool,
    exit_code: str = "",
    reason: str = "",
    legacy_exit_code: str = "",
) -> str:
    return "\n".join(
        [
            f"__ST_BEGIN__={run_id}",
            f"state={state}",
            f"exit_code={exit_code}",
            f"reason={reason}",
            "started_at=2026-07-12T10:00:00+08:00",
            "ended_at=2026-07-12T10:05:00+08:00" if state in {"failed", "succeeded"} else "ended_at=",
            "pid=123",
            "supervisor_kind=tmux",
            f"legacy_exit_code={legacy_exit_code}",
            f"supervisor_alive={1 if alive else 0}",
            f"__ST_END__={run_id}",
        ]
    )


class FakeSSH:
    def __init__(self, output: str = "", error: Exception | None = None):
        self.output = output
        self.error = error
        self.calls = []

    async def run(self, alias, command, timeout=30):
        self.calls.append((alias, command, timeout))
        if self.error:
            raise self.error
        return CommandResult(0, self.output, "")


def test_records_use_configured_name_instead_of_copy_of(engine):
    run_id = _seed_run(engine, run_id_hint="actual_name")

    record = get_run_record(run_id, target_engine=engine)
    assert record is not None
    assert record["name"] == "actual_name"
    assert record["panel_name"] == "copy of actual_name"
    assert list_run_records(query="actual", target_engine=engine)[0]["run_id"] == run_id


@pytest.mark.asyncio
async def test_observation_distinguishes_failed_running_lost_and_legacy_missing(engine):
    failed_id = _seed_run(engine, run_id_hint="failed")
    running_id = _seed_run(engine, run_id_hint="running")
    lost_id = _seed_run(engine, run_id_hint="lost")
    legacy_id = _seed_run(engine, run_id_hint="legacy")
    legacy_failed_id = _seed_run(engine, run_id_hint="legacy-failed")
    records = list_run_records(target_engine=engine)
    output = "\n".join(
        [
            _block(failed_id, state="failed", alive=True, exit_code="7", reason="exit_code:7"),
            _block(running_id, state="running", alive=True),
            _block(lost_id, state="running", alive=False),
            _block(legacy_id, state="", alive=False),
            _block(legacy_failed_id, state="", alive=True, legacy_exit_code="9"),
        ]
    )
    fake = FakeSSH(output)

    observed = await observe_run_records(
        records,
        ssh_client_factory=lambda: fake,
        target_engine=engine,
    )
    by_id = {item["run_id"]: item for item in observed}

    assert by_id[failed_id]["state"] == "failed"
    assert by_id[failed_id]["session_alive"] is True
    assert by_id[failed_id]["exit_code"] == 7
    assert by_id[running_id]["state"] == "running"
    assert by_id[lost_id]["state"] == "lost"
    assert by_id[legacy_id]["state"] == "finished_unknown"
    assert by_id[legacy_failed_id]["state"] == "failed"
    assert by_id[legacy_failed_id]["status_source"] == "legacy_output_marker"
    assert by_id[legacy_failed_id]["exit_code"] == 9
    assert len(fake.calls) == 1  # one batch command for one host

    with session_scope(engine) as session:
        assert session.get(Run, failed_id).status == "failed"
        assert session.get(Run, running_id).status == "running"
        assert session.get(Run, lost_id).status == "lost"
        assert session.get(Run, legacy_id).status == "finished_unknown"


@pytest.mark.asyncio
async def test_ssh_error_persists_unknown_instead_of_leaving_false_running_state(engine):
    run_id = _seed_run(engine, run_id_hint="network")
    record = get_run_record(run_id, target_engine=engine)
    assert record is not None

    observed = await observe_run_records(
        [record],
        ssh_client_factory=lambda: FakeSSH(error=RuntimeError("network down")),
        target_engine=engine,
    )

    assert observed[0]["state"] == "unknown"
    assert observed[0]["status_detail"] == "network down"
    with session_scope(engine) as session:
        stored = session.get(Run, run_id)
        assert stored.status == "unknown"
        assert stored.status_source == "ssh_error"


@pytest.mark.asyncio
async def test_large_host_run_set_is_split_before_remote_argument_limits(engine):
    run_ids = [_seed_run(engine, run_id_hint=f"batch-{index}") for index in range(45)]
    records = list_run_records(limit=100, target_engine=engine)
    output = "\n".join(_block(run_id, state="", alive=False) for run_id in run_ids)
    fake = FakeSSH(output)

    observed = await observe_run_records(
        records,
        ssh_client_factory=lambda: fake,
        persist=False,
        target_engine=engine,
    )

    assert len(observed) == 45
    assert len(fake.calls) == 3
    assert all(len(command) < 100_000 for _alias, command, _timeout in fake.calls)


def test_parse_training_progress_uses_latest_iteration_block():
    output = """
Learning iteration 10/100
Run name: older
Learning iteration 25/100
Run name: actual
Time elapsed: 00:05:00
ETA: 00:15:00
Mean reward: 42.50
Steps per second: 12345
"""
    assert parse_training_progress(output) == {
        "iteration": 25,
        "max_iterations": 100,
        "percent": 25.0,
        "run_name": "actual",
        "elapsed": "00:05:00",
        "eta": "00:15:00",
        "mean_reward": 42.5,
        "steps_per_second": 12345,
    }


@pytest.mark.asyncio
async def test_tail_training_run_returns_progress_and_name_mismatch_warning(engine):
    run_id = _seed_run(engine, run_id_hint="configured")

    async def fake_capture(*_args, **_kwargs):
        return "Learning iteration 5/10\nRun name: observed\n"

    result = await tail_training_run(
        run_id,
        lines=1000,
        capture=fake_capture,
        ssh_client_factory=object,
        target_engine=engine,
    )

    assert result["lines"] == 500
    assert result["progress"]["percent"] == 50.0
    assert "does not match" in result["warning"]
