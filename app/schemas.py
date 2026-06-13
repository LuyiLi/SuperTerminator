from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CommandResult:
    exit_status: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ServerStatus:
    alias: str
    online: bool
    error: str = ""
    hostname: str = ""
    gpu: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    cpu_percent: float | None = None
    memory: Mapping[str, Any] | None = None
    disks: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
