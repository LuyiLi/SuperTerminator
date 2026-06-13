from __future__ import annotations

import asyncio
from pathlib import Path
import asyncssh

from app.schemas import CommandResult

if not hasattr(asyncssh, "wait_for"):
    asyncssh.wait_for = asyncio.wait_for  # type: ignore[attr-defined]


def _has_excluded_pattern_char(pattern: str) -> bool:
    return any(char in pattern for char in "*?!")


def scan_ssh_config_hosts(config_path: str | Path | None = None) -> list[str]:
    path = (
        Path(config_path).expanduser()
        if config_path is not None
        else Path.home() / ".ssh" / "config"
    )
    if not path.exists():
        return []

    hosts: list[str] = []
    seen: set[str] = set()
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "host":
            continue
        for alias in parts[1].split():
            if _has_excluded_pattern_char(alias) or alias in seen:
                continue
            hosts.append(alias)
            seen.add(alias)
    return hosts


class SSHClient:
    async def run(self, host_alias: str, command: str, timeout: int = 30) -> CommandResult:
        async with asyncssh.connect(host_alias, known_hosts=None) as conn:
            result = await asyncssh.wait_for(conn.run(command, check=False), timeout=timeout)
            return CommandResult(
                exit_status=result.exit_status,
                stdout=result.stdout,
                stderr=result.stderr,
            )
