from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CommandResult:
    exit_status: int
    stdout: str
    stderr: str


@dataclass
class ServerStatus:
    alias: str
    online: bool
    error: str = ""
    hostname: str = ""
    gpu: list[dict[str, Any]] = field(default_factory=list)
    cpu_percent: float | None = None
    memory: dict[str, Any] | None = None
    disks: list[dict[str, Any]] = field(default_factory=list)
