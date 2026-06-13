from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from app.schemas import CommandResult, ServerStatus
from app.ssh_client import SSHClient, scan_ssh_config_hosts


def test_server_status_is_frozen_dataclass():
    status = ServerStatus(alias="gpu01", online=True)

    with pytest.raises(FrozenInstanceError):
        status.online = False


def test_scan_ssh_config_hosts_handles_inline_comments_in_host_lines(tmp_path: Path):
    config = tmp_path / "config"
    config.write_text(
        "\n".join(
            [
                "Host gpu01 gpu02 # production GPUs",
                "HostName ignored.example.com",
                "Host # comment-only host line",
                "Host gpu03#literal",
            ]
        )
    )

    assert scan_ssh_config_hosts(config) == ["gpu01", "gpu02", "gpu03"]


def test_scan_ssh_config_hosts_follows_relative_include_globs_and_avoids_cycles(tmp_path: Path):
    config_dir = tmp_path / "ssh"
    config_dir.mkdir()
    includes_dir = config_dir / "conf.d"
    includes_dir.mkdir()
    config = config_dir / "config"
    included = includes_dir / "gpu.conf"
    cycle = includes_dir / "cycle.inc"

    config.write_text(
        "\n".join(
            [
                "Host root-a",
                "Include conf.d/*.conf",
                "Host root-b root-a",
            ]
        )
    )
    included.write_text(
        "\n".join(
            [
                "Host included-a bad* !negated",
                "Include cycle.inc",
                "Host included-b",
            ]
        )
    )
    cycle.write_text(
        "\n".join(
            [
                "Include ../config",
                "Host cycle-a",
            ]
        )
    )

    assert scan_ssh_config_hosts(config) == [
        "root-a",
        "included-a",
        "cycle-a",
        "included-b",
        "root-b",
    ]


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

    def fake_connect(host_alias, **kwargs):
        calls["host_alias"] = host_alias
        calls["connect_kwargs"] = kwargs
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
        "connect_kwargs": {},
        "command": "uptime",
        "check": False,
        "timeout": 5,
    }
