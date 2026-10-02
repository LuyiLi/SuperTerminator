from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import time

import pytest

from app.config import Settings
from app.db import create_engine_for_settings, init_db, session_scope
from app.models import Project, Run, Server, Template
from app.run_runtime import build_managed_run_script
from app.run_status import get_training_run
from app.schemas import CommandResult


class LocalShellClient:
    """Execute the exact SSH payload locally under an isolated HOME."""

    def __init__(self, home: Path):
        self.home = home

    async def run(self, _alias: str, command: str, timeout: int = 30) -> CommandResult:
        def execute() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["bash", "-c", command],
                env={**os.environ, "HOME": str(self.home)},
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )

        result = await asyncio.to_thread(execute)
        return CommandResult(result.returncode, result.stdout, result.stderr)


@pytest.mark.asyncio
async def test_failed_training_is_reported_failed_while_debug_supervisor_is_alive(tmp_path: Path):
    target_engine = create_engine_for_settings(Settings(db_path=tmp_path / "app.db"))
    init_db(target_engine)
    home = tmp_path / "home"
    workdir = tmp_path / "work"
    home.mkdir()
    workdir.mkdir()

    with session_scope(target_engine) as session:
        project = Project(name="demo")
        server = Server(alias="local-test")
        template = Template(project=project, name="direct", command_template="echo boom")
        run = Run(
            project=project,
            server=server,
            template=template,
            workdir=str(workdir),
            name="controlled_failure",
            training_run_name="controlled_failure",
            rendered_command="echo boom; exit 7",
            tmux_session="pending",
            status="running",
        )
        session.add(run)
        session.flush()
        run.tmux_session = f"nohup-{run.id}"
        run_id = run.id

    script = build_managed_run_script(
        run_id,
        workdir=str(workdir),
        rendered_command="echo boom; exit 7",
        keepalive_on_error_seconds=1,
    )
    process = subprocess.Popen(
        ["bash", "-c", script],
        env={**os.environ, "HOME": str(home)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    state_path = home / ".gpu-ssh-panel" / "runs" / str(run_id) / "state"
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if state_path.exists() and state_path.read_text().strip() == "failed":
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError("controlled run never published failed state")

    client = LocalShellClient(home)
    observed = await get_training_run(
        run_id,
        tail_lines=50,
        refresh_live=True,
        ssh_client_factory=lambda: client,
        target_engine=target_engine,
    )

    assert process.poll() is None
    assert observed["training_run_name"] == "controlled_failure"
    assert observed["state"] == "failed"
    assert observed["session_alive"] is True
    assert observed["exit_code"] == 7
    assert "boom" in observed["output_tail"]
    assert process.wait(timeout=3) == 7
