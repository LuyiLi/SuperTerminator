from __future__ import annotations

import re
from typing import Any

GPU_QUERY_COMMAND = "nvidia-smi --query-gpu=name,memory.total,memory.used,utilization.gpu --format=csv,noheader,nounits"
CPU_COMMAND = r"LC_ALL=C top -bn1 | awk '/Cpu\(s\)/ {print 100 - $8}'"
MEMORY_COMMAND = "cat /proc/meminfo | grep -E 'MemTotal|MemAvailable'"
DISK_COMMAND = "df -h --output=source,size,used,avail,pcent,target | tail -n +2"

_INT_RE = re.compile(r"\d+")


def _int_from_text(value: str) -> int:
    match = _INT_RE.search(value)
    if not match:
        return 0
    return int(match.group(0))


def parse_gpu_csv(raw: str) -> list[dict[str, Any]]:
    gpus: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4:
            continue
        name, memory_total, memory_used, utilization_gpu = parts[:4]
        gpus.append(
            {
                "name": name,
                "memory_total_mib": _int_from_text(memory_total),
                "memory_used_mib": _int_from_text(memory_used),
                "utilization_gpu_percent": _int_from_text(utilization_gpu),
            }
        )
    return gpus


def parse_memory_line(raw: str) -> dict[str, float | int]:
    values: dict[str, int] = {}
    for line in raw.splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key in {"MemTotal", "MemAvailable"}:
            values[key] = _int_from_text(value)

    total_kib = values.get("MemTotal", 0)
    available_kib = values.get("MemAvailable", 0)

    used_percent = (
        0.0 if total_kib == 0 else round((total_kib - available_kib) / total_kib * 100, 1)
    )
    return {
        "total_kib": total_kib,
        "available_kib": available_kib,
        "used_percent": used_percent,
    }


def parse_cpu_percent(raw: str) -> float:
    return round(float(raw.strip()), 1)


def parse_disk_lines(raw: str) -> list[dict[str, Any]]:
    disks: list[dict[str, Any]] = []
    for line in (line for line in raw.splitlines() if line.strip()):
        parts = line.split(maxsplit=5)
        if len(parts) != 6 or line.lower().startswith("filesystem"):
            continue
        filesystem, size, used, avail, use_percent, mount = parts
        disks.append(
            {
                "filesystem": filesystem,
                "size": size,
                "used": used,
                "avail": avail,
                "use_percent": use_percent,
                "mount": mount,
            }
        )
    return disks
