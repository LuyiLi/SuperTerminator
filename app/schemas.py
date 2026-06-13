from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
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

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "gpu",
            tuple(MappingProxyType(dict(item)) for item in self.gpu),
        )
        object.__setattr__(
            self,
            "memory",
            None if self.memory is None else MappingProxyType(dict(self.memory)),
        )
        object.__setattr__(
            self,
            "disks",
            tuple(MappingProxyType(dict(item)) for item in self.disks),
        )
