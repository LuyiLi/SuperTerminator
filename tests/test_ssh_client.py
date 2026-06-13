from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from app.schemas import CommandResult, ServerStatus
from app.ssh_client import SSHClient, scan_ssh_config_hosts


def test_server_status_is_frozen_dataclass():
    status = ServerStatus(alias="gpu01", online=True)

    with pytest.raises(FrozenInstanceError):
        status.online = False


def test_scan_ssh_config_hosts_ignores_wildcards_and_supports_multiple_aliases(tmp_path: Path):
    config = tmp_path / "config"
    config.write_text(
        "\n".join(
            [
                "Host *",
                "  ForwardAgent no",
                "Host gpu01 gpu-one gpu01",
                "  HostName gpu01.example.com",
                "Host bastion !blocked qa? temp* good!bad",
                "  HostName bastion.example.com",
            ]
        )
    )

    assert scan_ssh_config_hosts(config) == ["gpu01", "gpu-one", "bastion"]


@pytest.mark.asyncio
async def test_ssh_client_run_returns_command_result(monkeypatch):
    calls = {}

    class FakeRunResult:
        exit_status = 7
        stdout = "out"
        stderr = "err"

    class FakeConnection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def run(self, command, check):
            calls["command"] = command
            calls["check"] = check
            return FakeRunResult()

    def fake_connect(host_alias, known_hosts):
        calls["host_alias"] = host_alias
        calls["known_hosts"] = known_hosts
        return FakeConnection()

    async def fake_wait_for(awaitable, timeout):
        calls["timeout"] = timeout
        return await awaitable

    import app.ssh_client as ssh_client_module

    monkeypatch.setattr(ssh_client_module.asyncssh, "connect", fake_connect)
    monkeypatch.setattr(ssh_client_module.asyncssh, "wait_for", fake_wait_for, raising=False)

    result = await SSHClient().run("gpu01", "uptime", timeout=5)

    assert result == CommandResult(exit_status=7, stdout="out", stderr="err")
    assert calls == {
        "host_alias": "gpu01",
        "known_hosts": None,
        "command": "uptime",
        "check": False,
        "timeout": 5,
    }
