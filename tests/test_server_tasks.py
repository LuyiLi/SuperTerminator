from __future__ import annotations

import asyncio

import pytest
from nicegui import Client
from nicegui.page import page

from app.config import Settings
from app.db import create_engine_for_settings, init_db, session_scope
from app.models import Project, Run, Server, Template
from app.run_status import list_server_active_runs
from app.ui import server_tasks


def test_machine_tasks_use_exact_host_and_keep_old_active_runs(tmp_path):
    engine = create_engine_for_settings(Settings(db_path=tmp_path / "tasks.db"))
    init_db(engine)
    with session_scope(engine) as session:
        project = Project(name="training")
        host = Server(alias="gpu01")
        other = Server(alias="gpu010")
        session.add_all([project, host, other])
        session.flush()
        template = Template(project_id=project.id, name="train", command_template="python train.py")
        session.add(template)
        session.flush()

        def add(server, state):
            run = Run(project_id=project.id, server_id=server.id, template_id=template.id,
                      name=f"{server.alias}-{state}", status=state, workdir="/train",
                      rendered_command="CUDA_VISIBLE_DEVICES=0,2 python train.py --task walk")
            session.add(run)
            return run

        oldest = add(host, "running")
        add(host, "unknown")
        add(host, "preparing")
        add(other, "running")
        for state in ["succeeded"] * 505 + ["failed", "stopped", "exited", "lost"]:
            add(host, state)
        session.flush()
        oldest_id = oldest.id

    records = list_server_active_runs("gpu01", target_engine=engine)
    assert {run["state"] for run in records} == {"running", "unknown", "preparing"}
    assert len(records) == 3
    assert records[-1]["id"] == oldest_id
    assert all(run["server_alias"] == "gpu01" for run in records)
    assert records[-1]["config"]["cuda_visible_devices"] == "0,2"
    assert list_server_active_runs("gpu0", target_engine=engine) == []
    engine.dispose()


@pytest.fixture
def panel(monkeypatch):
    records = [dict(id=11, run_id=11, name="long_training_name_" * 8, project_name="Walking",
                    server_alias="gpu01", state="running", live_verified=False,
                    config={"cuda_visible_devices": "0,2", "task": "Tracking-v0"},
                    started_at="2026-10-02T10:00:00")]
    timers = []
    monkeypatch.setattr(server_tasks, "list_server_active_runs", lambda alias: list(records))
    monkeypatch.setattr(server_tasks, "create_scoped_timer", lambda *args, **kwargs: timers.append((args, kwargs)))
    client = Client(page("/__test_server_tasks"))
    with client:
        controller = server_tasks.ServerTasksPanel("gpu01")
    client.tab_id = "test-connected-tab"
    yield controller, client, records, timers
    client.delete()


def texts(client):
    return [getattr(element, "text", "") for element in client.elements.values()]


def test_machine_details_show_task_identity_gpu_and_direct_link(panel):
    controller, client, records, timers = panel
    assert records[0]["name"] in texts(client)
    assert {"#11 · Walking", "GPU 0,2", "Task · Tracking-v0", "运行记录"} <= set(texts(client))
    assert any(element._props.get("href") == "/runs/11" for element in client.elements.values())
    assert len(timers) == 1
    assert timers[0][0][0] is controller.container
    assert timers[0][0][1] >= 30
    client.outbox.updates.clear()
    with client:
        controller.render(records)
    assert not client.outbox.updates


async def test_machine_tasks_survive_network_failure_then_reconcile_completion(panel, monkeypatch):
    controller, client, records, _ = panel

    async def offline(_records):
        raise TimeoutError("network unavailable")

    monkeypatch.setattr(server_tasks, "observe_run_records", offline)
    with client:
        await controller.refresh()
    assert records[0]["name"] in texts(client)
    assert "待核实" in texts(client)
    assert "暂无运行中的面板任务" not in texts(client)

    async def completed(_records):
        return [dict(records[0], state="succeeded", live_verified=True)]

    monkeypatch.setattr(server_tasks, "observe_run_records", completed)
    with client:
        await controller.refresh()
    assert records[0]["name"] not in texts(client)
    assert "暂无运行中的面板任务" in texts(client)


@pytest.mark.parametrize("suspension", ["disconnect", "hidden", "closed"])
async def test_machine_tasks_stop_polling_when_unavailable(panel, monkeypatch, suspension):
    controller, client, _records, _ = panel
    if suspension == "disconnect":
        client.tab_id = None
    elif suspension == "hidden":
        client.st_page_visible = False
    else:
        controller.container.delete()
    monkeypatch.setattr(server_tasks, "list_server_active_runs", lambda _alias: pytest.fail("unexpected poll"))
    client.outbox.updates.clear()
    with client:
        await controller.refresh()
    assert not client.outbox.updates


async def test_late_machine_task_observation_does_not_update_closed_dialog(panel, monkeypatch):
    controller, client, records, _ = panel
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(_records):
        started.set()
        await release.wait()
        return [dict(records[0], live_verified=True)]

    monkeypatch.setattr(server_tasks, "observe_run_records", slow)
    with client:
        pending = asyncio.create_task(controller.refresh())
        await started.wait()
        await controller.refresh()  # Does not launch a second observation while in flight.
        controller.container.delete()
        client.outbox.updates.clear()
        release.set()
        await pending
    assert not client.outbox.updates
