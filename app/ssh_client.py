from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path
import asyncssh

from app.schemas import CommandResult

if not hasattr(asyncssh, "wait_for"):
    asyncssh.wait_for = asyncio.wait_for  # type: ignore[attr-defined]


def _has_excluded_pattern_char(pattern: str) -> bool:
    return any(char in pattern for char in "*?!")


def _strip_inline_comment(line: str) -> str:
    return line.split("#", 1)[0].strip()


def _include_matches(patterns: Iterable[str], base_dir: Path) -> list[Path]:
    matches: list[Path] = []
    for pattern in patterns:
        expanded_pattern = Path(pattern).expanduser()
        if not expanded_pattern.is_absolute():
            expanded_pattern = base_dir / expanded_pattern
        matches.extend(sorted(expanded_pattern.parent.glob(expanded_pattern.name)))
    return matches


def scan_ssh_config_hosts(config_path: str | Path | None = None) -> list[str]:
    path = (
        Path(config_path).expanduser()
        if config_path is not None
        else Path.home() / ".ssh" / "config"
    )

    hosts: list[str] = []
    seen_aliases: set[str] = set()
    visited_files: set[Path] = set()

    def scan_file(file_path: Path) -> None:
        try:
            resolved_path = file_path.resolve()
        except OSError:
            return
        if resolved_path in visited_files or not resolved_path.exists():
            return
        visited_files.add(resolved_path)

        for raw_line in resolved_path.read_text().splitlines():
            line = _strip_inline_comment(raw_line)
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            keyword, argument = parts
            if keyword.lower() == "include":
                for included_path in _include_matches(argument.split(), resolved_path.parent):
                    scan_file(included_path)
                continue
            if keyword.lower() != "host":
                continue
            for alias in argument.split():
                if _has_excluded_pattern_char(alias) or alias in seen_aliases:
                    continue
                hosts.append(alias)
                seen_aliases.add(alias)

    scan_file(path)
    return hosts


class SSHClient:
    async def run(self, host_alias: str, command: str, timeout: int = 30) -> CommandResult:
        async with asyncssh.connect(host_alias) as conn:
            result = await asyncssh.wait_for(conn.run(command, check=False), timeout=timeout)
            return CommandResult(
                exit_status=result.exit_status,
                stdout=result.stdout,
                stderr=result.stderr,
            )
