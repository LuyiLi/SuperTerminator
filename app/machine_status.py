"""Read-only, uncached fleet GPU inspection for launch preflights."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import secrets
from typing import Any

from sqlalchemy import Engine

from app import metrics
from app.db import engine, session_scope
from app.models import Server
from app.ssh_client import SSHClient


GPU_QUERY_WALL_TIMEOUT_SECONDS = 12
GPU_FREE_MAX_MEMORY_MIB = 512
GPU_FREE_MAX_UTILIZATION_PERCENT = 5
GPU_LIGHT_MAX_MEMORY_PERCENT = 25
GPU_LIGHT_MAX_UTILIZATION_PERCENT = 25


@dataclass(frozen=True)
class MachineStatusObservation:
    """One point-in-time fleet reading collected for exactly one request."""

    observation_id: str
    observed_at: datetime
    machines: tuple[dict[str, Any], ...]
    summary: dict[str, Any]

    def as_result(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "observed_at": self.observed_at.isoformat(),
            "freshness": "collected_for_this_request",
            "summary": deepcopy(self.summary),
            "machines": deepcopy(list(self.machines)),
            "occupancy_thresholds": {
                "free_max_memory_mib": GPU_FREE_MAX_MEMORY_MIB,
                "free_max_utilization_percent": GPU_FREE_MAX_UTILIZATION_PERCENT,
                "light_max_memory_percent": GPU_LIGHT_MAX_MEMORY_PERCENT,
                "light_max_utilization_percent": GPU_LIGHT_MAX_UTILIZATION_PERCENT,
            },
            "next_step": (
                "Review every enabled machine and each GPU before preparing a task. This "
                "observation is never reused: prepare_training_run and launch_training_run "
                "each collect their own newer fleet reading. Raw utilization and memory "
                "values are authoritative; the free/light/busy label is only a scheduling aid."
            ),
        }


def _metric_int(value: Any) -> int:
    try:
        return max(int(float(value)), 0)
    except (TypeError, ValueError):
        return 0


def classify_gpu_occupancy(
    *, memory_total_mib: Any, memory_used_mib: Any, utilization_gpu_percent: Any
) -> str:
    """Classify a point-in-time GPU sample for MCP scheduling warnings."""

    total = _metric_int(memory_total_mib)
    used = _metric_int(memory_used_mib)
    utilization = _metric_int(utilization_gpu_percent)
    memory_percent = round((used / total) * 100, 1) if total > 0 else None
    if used <= GPU_FREE_MAX_MEMORY_MIB and utilization <= GPU_FREE_MAX_UTILIZATION_PERCENT:
        return "free"
    elif (
        memory_percent is not None
        and memory_percent < GPU_LIGHT_MAX_MEMORY_PERCENT
        and utilization < GPU_LIGHT_MAX_UTILIZATION_PERCENT
    ):
        return "light"
    return "busy"


def _gpu_occupancy(gpu: dict[str, Any], index: int) -> dict[str, Any]:
    total = _metric_int(gpu.get("memory_total_mib"))
    used = _metric_int(gpu.get("memory_used_mib"))
    utilization = _metric_int(gpu.get("utilization_gpu_percent"))
    memory_percent = round((used / total) * 100, 1) if total > 0 else None
    occupancy = classify_gpu_occupancy(
        memory_total_mib=total,
        memory_used_mib=used,
        utilization_gpu_percent=utilization,
    )
    return {
        "index": index,
        "name": str(gpu.get("name") or f"GPU {index}"),
        "memory_total_mib": total,
        "memory_used_mib": used,
        "memory_free_mib": max(total - used, 0),
        "memory_used_percent": memory_percent,
        "utilization_gpu_percent": utilization,
        "occupancy": occupancy,
        "occupied": occupancy != "free",
    }


async def collect_machine_gpu_status(
    server: dict[str, Any],
    *,
    ssh_client_factory: Callable[[], Any] = SSHClient,
) -> dict[str, Any]:
    """Collect one nvidia-smi reading using a single bounded SSH command."""

    base = {
        "server_id": int(server["server_id"]),
        "server_alias": str(server["server_alias"]),
        "server_name": str(server.get("server_name") or ""),
        "server_tags": list(server.get("server_tags") or []),
        "enabled": bool(server.get("enabled", True)),
    }
    if not base["enabled"]:
        return {
            **base,
            "online": None,
            "gpu_metrics_available": False,
            "state": "disabled",
            "error": None,
            "gpus": [],
            "free_gpu_indices": [],
            "occupied_gpu_indices": [],
        }

    try:
        result = await asyncio.wait_for(
            ssh_client_factory().run(
                base["server_alias"], metrics.GPU_QUERY_COMMAND, timeout=10
            ),
            timeout=GPU_QUERY_WALL_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return {
            **base,
            "online": False,
            "gpu_metrics_available": False,
            "state": "offline",
            "error": str(exc),
            "gpus": [],
            "free_gpu_indices": [],
            "occupied_gpu_indices": [],
        }

    if result.exit_status != 0:
        error = result.stderr.strip() or result.stdout.strip() or f"exit {result.exit_status}"
        return {
            **base,
            "online": result.exit_status != 255,
            "gpu_metrics_available": False,
            "state": "metrics_error",
            "error": error,
            "gpus": [],
            "free_gpu_indices": [],
            "occupied_gpu_indices": [],
        }

    gpus = [_gpu_occupancy(gpu, index) for index, gpu in enumerate(metrics.parse_gpu_csv(result.stdout))]
    free = [gpu["index"] for gpu in gpus if gpu["occupancy"] == "free"]
    occupied = [gpu["index"] for gpu in gpus if gpu["occupancy"] != "free"]
    return {
        **base,
        "online": True,
        "gpu_metrics_available": bool(gpus),
        "state": "online" if gpus else "no_gpu_metrics",
        "error": None if gpus else "nvidia-smi returned no GPU rows",
        "gpus": gpus,
        "free_gpu_indices": free,
        "occupied_gpu_indices": occupied,
    }


def _load_servers(*, target_engine: Engine) -> list[dict[str, Any]]:
    with session_scope(target_engine) as session:
        servers = session.query(Server).order_by(Server.alias).all()
        return [
            {
                "server_id": int(server.id),
                "server_alias": server.alias,
                "server_name": server.name,
                "server_tags": list(server.tags or []),
                "enabled": bool(server.enabled),
            }
            for server in servers
        ]


def _fleet_summary(machines: list[dict[str, Any]]) -> dict[str, Any]:
    enabled = [machine for machine in machines if machine["enabled"]]
    gpus = [gpu for machine in enabled for gpu in machine["gpus"]]
    return {
        "configured_machines": len(machines),
        "enabled_machines": len(enabled),
        "online_machines": sum(machine["online"] is True for machine in enabled),
        "unavailable_machines": sum(
            machine["online"] is not True or not machine["gpu_metrics_available"]
            for machine in enabled
        ),
        "total_gpus": len(gpus),
        "free_gpus": sum(gpu["occupancy"] == "free" for gpu in gpus),
        "light_gpus": sum(gpu["occupancy"] == "light" for gpu in gpus),
        "busy_gpus": sum(gpu["occupancy"] == "busy" for gpu in gpus),
    }


async def list_machine_statuses(
    *,
    target_engine: Engine = engine,
    ssh_client_factory: Callable[[], Any] = SSHClient,
    observation_id_factory: Callable[[], str] | None = None,
    wall_clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Inspect all configured machines now; results are never served from a cache."""

    observation = await capture_machine_statuses(
        target_engine=target_engine,
        ssh_client_factory=ssh_client_factory,
        observation_id_factory=observation_id_factory,
        wall_clock=wall_clock,
    )
    return observation.as_result()


async def capture_machine_statuses(
    *,
    target_engine: Engine = engine,
    ssh_client_factory: Callable[[], Any] = SSHClient,
    observation_id_factory: Callable[[], str] | None = None,
    wall_clock: Callable[[], datetime] | None = None,
) -> MachineStatusObservation:
    """Collect and return one new fleet observation for the current request."""

    servers = _load_servers(target_engine=target_engine)
    results = await asyncio.gather(
        *(
            collect_machine_gpu_status(server, ssh_client_factory=ssh_client_factory)
            for server in servers
        )
    )
    make_id = observation_id_factory or (
        lambda: f"obs_{secrets.token_urlsafe(18)}"
    )
    now = wall_clock or (lambda: datetime.now(timezone.utc))
    return MachineStatusObservation(
        observation_id=make_id(),
        observed_at=now(),
        machines=tuple(deepcopy(results)),
        summary=_fleet_summary(results),
    )


def parse_requested_gpu_indices(config: dict[str, Any]) -> tuple[list[int], list[str]]:
    raw = str(config.get("cuda_visible_devices") or "").strip()
    if not raw:
        return [], ["CUDA_VISIBLE_DEVICES is not set; selected physical GPUs are unknown"]
    indices: list[int] = []
    warnings: list[str] = []
    for item in raw.split(","):
        token = item.strip()
        if not token:
            continue
        try:
            index = int(token)
        except ValueError:
            warnings.append(f"Unsupported CUDA_VISIBLE_DEVICES entry: {token!r}")
            continue
        if index not in indices:
            indices.append(index)
    if not indices:
        warnings.append("No numeric GPU indices could be resolved")
    return indices, warnings


def evaluate_launch_feasibility(
    observation: MachineStatusObservation,
    *,
    server_id: int,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate the target GPUs while retaining raw metrics for user judgment."""

    machine = next(
        (
            item
            for item in observation.machines
            if int(item["server_id"]) == int(server_id)
        ),
        None,
    )
    requested, warnings = parse_requested_gpu_indices(config)
    blockers: list[str] = []
    selected: list[dict[str, Any]] = []
    if not requested:
        blockers.append(
            "Cannot verify GPU feasibility without numeric CUDA_VISIBLE_DEVICES entries"
        )
    if machine is None:
        blockers.append(f"Target server {server_id} is absent from the fleet observation")
    elif machine["online"] is not True:
        blockers.append(f"Target server {machine['server_alias']} is offline")
    elif not machine["gpu_metrics_available"]:
        blockers.append(
            f"Target server {machine['server_alias']} has no usable GPU metrics"
        )
    else:
        by_index = {int(gpu["index"]): gpu for gpu in machine["gpus"]}
        missing = [index for index in requested if index not in by_index]
        if missing:
            blockers.append(f"Requested GPU indices do not exist on target: {missing}")
        selected = [deepcopy(by_index[index]) for index in requested if index in by_index]

    nproc = config.get("nproc_per_node")
    if isinstance(nproc, int) and requested and nproc > len(requested):
        blockers.append(
            f"--nproc_per_node={nproc} exceeds {len(requested)} selected GPU(s)"
        )
    occupied = [gpu for gpu in selected if gpu["occupancy"] != "free"]
    if occupied:
        warnings.append(
            "Requested GPUs are already occupied: "
            + ", ".join(
                f"GPU {gpu['index']} ({gpu['occupancy']}, "
                f"{gpu['memory_used_mib']} MiB, {gpu['utilization_gpu_percent']}% util)"
                for gpu in occupied
            )
        )

    if blockers:
        status = "blocked"
    elif occupied:
        status = "caution"
    else:
        status = "clear"
    return {
        "status": status,
        "can_launch": not blockers,
        "observation_id": observation.observation_id,
        "observed_at": observation.observed_at.isoformat(),
        "target_server_id": int(server_id),
        "target_server_alias": machine["server_alias"] if machine is not None else None,
        "requested_gpu_indices": requested,
        "selected_gpus": selected,
        "warnings": warnings,
        "blockers": blockers,
    }
