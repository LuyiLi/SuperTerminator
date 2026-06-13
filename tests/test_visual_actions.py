from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.config import Settings
from app.db import create_engine_for_settings, init_db, session_scope
from app.metrics import CPU_COMMAND, DISK_COMMAND, GPU_QUERY_COMMAND, MEMORY_COMMAND
from app.models import Preset, Project, ProjectServer, ProjectWorkdir, Run, Server, Template
from app.schemas import CommandResult
from app.visual_actions import (
    capture_run_output,
    collect_server_status,
    launch_run,
    stop_run,
    test_connection,
)


class FakeSSHClient:
    def __init__(self, responses: dict[str, CommandResult] | None = None):
        self.commands: list[tuple[str, str, int]] = []
        self.responses = responses or {}

    async def run(self, host_alias: str, command: str, timeout: int = 30) -> CommandResult:
        self.commands.append((host_alias, command, timeout))
        if command in self.responses:
            return self.responses[command]
        if command == "echo ok":
            return CommandResult(0, "ok\n", "")
        if command == "hostname":
            return CommandResult(0, "gpu01\n", "")
        if command == GPU_QUERY_COMMAND:
            return CommandResult(0, "A100, 81920, 1024, 50\n", "")
        if command == CPU_COMMAND:
            return CommandResult(0, "42.0\n", "")
        if command == MEMORY_COMMAND:
            return CommandResult(0, "MemTotal: 1000 kB\nMemAvailable: 500 kB\n", "")
        if command == DISK_COMMAND:
            return CommandResult(0, "/dev/sda1 7.0T 3.2T 3.8T 46% /data\n", "")
        if command.startswith("tmux new-session"):
            return CommandResult(0, "", "")
        if command.startswith("tmux capture-pane"):
            return CommandResult(0, "line one\nline two\n", "")
        if command.startswith("tmux kill-session"):
            return CommandResult(0, "", "")
        return CommandResult(1, "", f"unexpected command: {command}")


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 6, 13, 22, 30, 0, tzinfo=tz)


@pytest.fixture
def engine(tmp_path: Path):
    engine = create_engine_for_settings(Settings(db_path=tmp_path / "app.db"))
    init_db(engine)
    return engine


@pytest.mark.asyncio
async def test_test_connection_returns_ok_when_echo_succeeds():
    fake = FakeSSHClient()

    ok, message = await test_connection("gpu01", fake)

    assert (ok, message) == (True, "ok")
    assert fake.commands == [("gpu01", "echo ok", 10)]


@pytest.mark.asyncio
async def test_test_connection_returns_useful_error_when_echo_fails():
    fake = FakeSSHClient({"echo ok": CommandResult(255, "", "ssh: host not found\n")})

    ok, message = await test_connection("missing", fake)

    assert ok is False
    assert message == "ssh: host not found"


@pytest.mark.asyncio
async def test_collect_server_status_returns_offline_status_without_metrics_when_connection_fails():
    fake = FakeSSHClient({"echo ok": CommandResult(1, "", "network unreachable\n")})

    status = await collect_server_status("gpu01", fake)

    assert status.online is False
    assert status.error == "network unreachable"
    assert status.hostname == ""
    assert status.gpu == []
    assert fake.commands == [("gpu01", "echo ok", 10)]


@pytest.mark.asyncio
async def test_collect_server_status_runs_metric_commands_and_parses_frozen_status():
    fake = FakeSSHClient()

    status = await collect_server_status("gpu01", fake)

    assert status.online is True
    assert status.error == ""
    assert status.hostname == "gpu01"
    assert status.gpu == [
        {
            "name": "A100",
            "memory_total_mib": 81920,
            "memory_used_mib": 1024,
            "utilization_gpu_percent": 50,
        }
    ]
    assert status.cpu_percent == 42.0
    assert status.memory == {"total_kib": 1000, "available_kib": 500, "used_percent": 50.0}
    assert status.disks == [
        {
            "filesystem": "/dev/sda1",
            "size": "7.0T",
            "used": "3.2T",
            "avail": "3.8T",
            "use_percent": "46%",
            "mount": "/data",
        }
    ]
    assert fake.commands == [
        ("gpu01", "echo ok", 10),
        ("gpu01", "hostname", 10),
        ("gpu01", GPU_QUERY_COMMAND, 10),
        ("gpu01", CPU_COMMAND, 10),
        ("gpu01", MEMORY_COMMAND, 10),
        ("gpu01", DISK_COMMAND, 10),
    ]


@pytest.mark.asyncio
async def test_collect_server_status_allows_malformed_cpu_to_parse_as_none():
    fake = FakeSSHClient({CPU_COMMAND: CommandResult(0, "not-a-number\n", "")})

    status = await collect_server_status("gpu01", fake)

    assert status.online is True
    assert status.cpu_percent is None


def _seed_launch_data(engine):
    with session_scope(engine) as session:
        server = Server(alias="gpu01", name="GPU 01")
        project = Project(name="demo", default_workdir="/data/demo")
        session.add_all([server, project])
        session.flush()
        link = ProjectServer(project_id=project.id, server_id=server.id)
        session.add(link)
        session.flush()
        session.add(
            ProjectWorkdir(
                project_server_id=link.id,
                path="/data/demo",
                label="main",
                is_default=True,
            )
        )
        template = Template(
            project_id=project.id,
            name="train",
            command_template="python train.py --lr {{lr}} --epochs {{epochs}}",
            variables_schema=[
                {"name": "lr", "default": "1e-3", "required": True},
                {"name": "epochs", "default": "10", "required": True},
            ],
        )
        session.add(template)
        session.flush()
        preset = Preset(
            project_id=project.id,
            template_id=template.id,
            name="quick",
            values_json={"epochs": "2"},
        )
        session.add(preset)
        session.flush()
        return project.id, server.id, template.id, preset.id


@pytest.mark.asyncio
async def test_launch_run_creates_run_starts_tmux_and_returns_detached_running_run(engine, monkeypatch):
    monkeypatch.setattr("app.visual_actions.datetime", FixedDateTime)
    project_id, server_id, template_id, preset_id = _seed_launch_data(engine)
    fake = FakeSSHClient()

    run = await launch_run(
        engine=engine,
        ssh_client=fake,
        project_id=project_id,
        server_id=server_id,
        template_id=template_id,
        preset_id=preset_id,
        workdir="/data/demo",
        run_name="debug run",
        form_values={"lr": "1e-4"},
    )

    assert run.status == "running"
    assert run.id == 1
    assert run.tmux_session == "gpu-panel-20260613-223000-1"
    assert run.rendered_command == "python train.py --lr 1e-4 --epochs 2"
    assert fake.commands[-1][0] == "gpu01"
    assert fake.commands[-1][2] == 15
    assert "tmux new-session -d -s gpu-panel-20260613-223000-1" in fake.commands[-1][1]
    assert "cd /data/demo && python train.py --lr 1e-4 --epochs 2" in fake.commands[-1][1]

    with session_scope(engine) as session:
        stored = session.query(Run).one()
        assert stored.status == "running"
        assert stored.tmux_session == "gpu-panel-20260613-223000-1"
        assert stored.rendered_command == "python train.py --lr 1e-4 --epochs 2"


@pytest.mark.asyncio
async def test_launch_run_marks_status_unknown_when_tmux_start_fails(engine, monkeypatch):
    monkeypatch.setattr("app.visual_actions.datetime", FixedDateTime)
    project_id, server_id, template_id, _preset_id = _seed_launch_data(engine)
    fake = FakeSSHClient()
    async def fail_start(host_alias: str, command: str, timeout: int = 30) -> CommandResult:
        fake.commands.append((host_alias, command, timeout))
        if command.startswith("tmux new-session"):
            return CommandResult(1, "", "tmux failed")
        return CommandResult(1, "", "unexpected")
    fake.run = fail_start

    run = await launch_run(
        engine=engine,
        ssh_client=fake,
        project_id=project_id,
        server_id=server_id,
        template_id=template_id,
        preset_id=None,
        workdir="/data/demo",
        run_name="debug run",
        form_values={"lr": "1e-4", "epochs": "4"},
    )

    assert run.status == "unknown"


@pytest.mark.asyncio
async def test_capture_run_output_returns_stdout_and_honors_line_count():
    fake = FakeSSHClient()

    output = await capture_run_output("gpu01", "gpu-panel-20260613-223000-1", fake, lines=50)

    assert output == "line one\nline two\n"
    assert fake.commands == [
        ("gpu01", "tmux capture-pane -t gpu-panel-20260613-223000-1 -p -S -50", 10)
    ]


@pytest.mark.asyncio
async def test_capture_run_output_returns_stderr_or_not_found_message_on_failure():
    command = "tmux capture-pane -t gpu-panel-20260613-223000-1 -p -S -300"
    fake = FakeSSHClient({command: CommandResult(1, "", "no such session\n")})

    assert await capture_run_output("gpu01", "gpu-panel-20260613-223000-1", fake) == "no such session"

    fake = FakeSSHClient({command: CommandResult(1, "", "")})
    assert await capture_run_output("gpu01", "gpu-panel-20260613-223000-1", fake) == "tmux session not found"


@pytest.mark.asyncio
async def test_stop_run_kills_tmux_session_and_reports_stopped():
    fake = FakeSSHClient()

    result = await stop_run("gpu01", "gpu-panel-20260613-223000-1", fake)

    assert result == (True, "stopped")
    assert fake.commands == [("gpu01", "tmux kill-session -t gpu-panel-20260613-223000-1", 10)]


@pytest.mark.asyncio
async def test_stop_run_returns_error_on_failure():
    command = "tmux kill-session -t gpu-panel-20260613-223000-1"
    fake = FakeSSHClient({command: CommandResult(1, "", "no such session\n")})

    result = await stop_run("gpu01", "gpu-panel-20260613-223000-1", fake)

    assert result == (False, "no such session")
