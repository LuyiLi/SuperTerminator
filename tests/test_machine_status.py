from __future__ import annotations

from pathlib import Path

import pytest

from app import metrics
from app.config import Settings
from app.db import create_engine_for_settings, init_db, session_scope
from app.machine_status import (
    capture_machine_statuses,
    evaluate_launch_feasibility,
    list_machine_statuses,
)
from app.models import Server
from app.schemas import CommandResult


class FleetSSH:
    def __init__(self, responses):
        self.responses = responses
        self.calls: list[tuple[str, str, int]] = []

    async def run(self, alias: str, command: str, timeout: int = 30) -> CommandResult:
        self.calls.append((alias, command, timeout))
        response = self.responses[alias]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def engine(tmp_path: Path):
    target = create_engine_for_settings(Settings(db_path=tmp_path / "app.db"))
    init_db(target)
    with session_scope(target) as session:
        session.add_all(
            [
                Server(alias="gpu-a", name="A", enabled=True),
                Server(alias="gpu-b", name="B", enabled=True),
                Server(alias="gpu-disabled", name="Disabled", enabled=False),
            ]
        )
    return target


@pytest.mark.asyncio
async def test_list_machine_statuses_reports_every_gpu_and_disabled_machine(engine):
    fake = FleetSSH(
        {
            "gpu-a": CommandResult(
                0,
                "A100, 81920, 0, 0\n"
                "A100, 81920, 1024, 10\n"
                "A100, 81920, 60000, 90\n",
                "",
            ),
            "gpu-b": RuntimeError("host unreachable"),
        }
    )
    result = await list_machine_statuses(
        target_engine=engine,
        ssh_client_factory=lambda: fake,
        observation_id_factory=lambda: "obs_fleet",
    )

    assert result["observation_id"] == "obs_fleet"
    assert result["freshness"] == "collected_for_this_request"
    assert result["summary"] == {
        "configured_machines": 3,
        "enabled_machines": 2,
        "online_machines": 1,
        "unavailable_machines": 1,
        "total_gpus": 3,
        "free_gpus": 1,
        "light_gpus": 1,
        "busy_gpus": 1,
    }
    by_alias = {machine["server_alias"]: machine for machine in result["machines"]}
    assert [gpu["occupancy"] for gpu in by_alias["gpu-a"]["gpus"]] == [
        "free",
        "light",
        "busy",
    ]
    assert by_alias["gpu-a"]["gpus"][1]["memory_free_mib"] == 80896
    assert by_alias["gpu-a"]["free_gpu_indices"] == [0]
    assert by_alias["gpu-a"]["occupied_gpu_indices"] == [1, 2]
    assert by_alias["gpu-b"]["state"] == "offline"
    assert by_alias["gpu-disabled"]["state"] == "disabled"
    assert sorted(fake.calls) == sorted(
        [
            ("gpu-a", metrics.GPU_QUERY_COMMAND, 10),
            ("gpu-b", metrics.GPU_QUERY_COMMAND, 10),
        ]
    )


@pytest.mark.asyncio
async def test_feasibility_uses_raw_target_gpu_metrics_and_nproc(engine):
    fake = FleetSSH(
        {
            "gpu-a": CommandResult(
                0,
                "A100, 81920, 0, 0\nA100, 81920, 4096, 40\n",
                "",
            ),
            "gpu-b": RuntimeError("offline"),
        }
    )
    observation = await capture_machine_statuses(
        target_engine=engine,
        ssh_client_factory=lambda: fake,
        observation_id_factory=lambda: "obs_eval",
    )
    gpu_a_id = next(
        machine["server_id"]
        for machine in observation.machines
        if machine["server_alias"] == "gpu-a"
    )

    caution = evaluate_launch_feasibility(
        observation,
        server_id=gpu_a_id,
        config={"cuda_visible_devices": "0,1", "nproc_per_node": 2},
    )
    assert caution["status"] == "caution"
    assert caution["can_launch"] is True
    assert caution["requested_gpu_indices"] == [0, 1]
    assert "already occupied" in caution["warnings"][0]

    missing = evaluate_launch_feasibility(
        observation,
        server_id=gpu_a_id,
        config={"cuda_visible_devices": "0,9", "nproc_per_node": 2},
    )
    assert missing["status"] == "blocked"
    assert "do not exist" in missing["blockers"][0]

    unknown = evaluate_launch_feasibility(
        observation,
        server_id=gpu_a_id,
        config={"cuda_visible_devices": None, "nproc_per_node": 1},
    )
    assert unknown["status"] == "blocked"
    assert any("without numeric CUDA_VISIBLE_DEVICES" in item for item in unknown["blockers"])

    too_many_processes = evaluate_launch_feasibility(
        observation,
        server_id=gpu_a_id,
        config={"cuda_visible_devices": "0", "nproc_per_node": 2},
    )
    assert too_many_processes["status"] == "blocked"
    assert any("nproc_per_node" in item for item in too_many_processes["blockers"])


@pytest.mark.asyncio
async def test_every_machine_status_request_collects_new_metrics(engine):
    fake = FleetSSH(
        {
            "gpu-a": CommandResult(0, "A100, 81920, 0, 0\n", ""),
            "gpu-b": RuntimeError("offline"),
        }
    )
    ids = iter(["obs_first", "obs_second"])

    first = await list_machine_statuses(
        target_engine=engine,
        ssh_client_factory=lambda: fake,
        observation_id_factory=lambda: next(ids),
    )
    fake.responses["gpu-a"] = CommandResult(0, "A100, 81920, 60000, 99\n", "")
    second = await list_machine_statuses(
        target_engine=engine,
        ssh_client_factory=lambda: fake,
        observation_id_factory=lambda: next(ids),
    )

    first_gpu = next(item for item in first["machines"] if item["server_alias"] == "gpu-a")[
        "gpus"
    ][0]
    second_gpu = next(
        item for item in second["machines"] if item["server_alias"] == "gpu-a"
    )["gpus"][0]
    assert first["observation_id"] == "obs_first"
    assert second["observation_id"] == "obs_second"
    assert first_gpu["memory_used_mib"] == 0
    assert second_gpu["memory_used_mib"] == 60000
    assert [call[0] for call in fake.calls].count("gpu-a") == 2
    assert [call[0] for call in fake.calls].count("gpu-b") == 2
