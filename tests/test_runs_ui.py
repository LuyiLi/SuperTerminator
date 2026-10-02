from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import create_engine_for_settings, init_db, session_scope
from app.models import Project, Run, Server, Template


def _seed_run(engine, *, name: str, server_alias: str = "gpu01") -> int:
    with session_scope(engine) as session:
        project = Project(name=f"project-{name}")
        server = Server(alias=server_alias, name=server_alias.upper())
        template = Template(project=project, name="train", command_template="python train.py")
        run = Run(
            project=project,
            server=server,
            template=template,
            workdir="/data/demo",
            name=name,
            tmux_session=f"gpu-panel-20260614-120000-{name}",
            rendered_command=f"echo {name}",
            status="running",
        )
        session.add(run)
        session.flush()
        return run.id


def test_runs_helpers_list_recent_and_load_detail(tmp_path: Path):
    from app.ui import runs

    engine = create_engine_for_settings(Settings(db_path=tmp_path / "app.db"))
    init_db(engine)
    first_id = _seed_run(engine, name="first", server_alias="gpu01")
    second_id = _seed_run(engine, name="second", server_alias="gpu02")

    recent = runs.list_recent_runs(target_engine=engine)

    assert [item["id"] for item in recent] == [second_id, first_id]
    assert recent[0]["id"] == second_id
    assert recent[0]["name"] == "second"
    assert recent[0]["project_name"] == "project-second"
    assert recent[0]["server_alias"] == "gpu02"
    assert recent[0]["status"] == "running"
    assert recent[0]["tmux_session"] == "gpu-panel-20260614-120000-second"

    detail = runs.get_run_detail(second_id, target_engine=engine)
    assert detail is not None
    assert detail["id"] == second_id
    assert detail["project_name"] == "project-second"
    assert detail["server_alias"] == "gpu02"
    assert detail["workdir"] == "/data/demo"
    assert detail["rendered_command"] == "echo second"


def test_mark_run_exited_records_explicit_stop(tmp_path: Path):
    from app.ui import runs

    engine = create_engine_for_settings(Settings(db_path=tmp_path / "app.db"))
    init_db(engine)
    run_id = _seed_run(engine, name="to-stop")

    assert runs.mark_run_exited(run_id, target_engine=engine) is True

    with session_scope(engine) as session:
        stored = session.get(Run, run_id)
        assert stored.status == "stopped"
        assert stored.status_source == "user_stop"


def test_run_output_refresh_guard_marks_older_generations_stale():
    from app.ui.runs import RunOutputRefreshGuard

    guard = RunOutputRefreshGuard()
    older = guard.next_generation()
    newer = guard.next_generation()

    assert older < newer
    assert guard.is_current(older) is False
    assert guard.is_current(newer) is True


def test_launch_provenance_label_distinguishes_codex_and_web_runs():
    from app.ui.runs import launch_preflight_label, launch_provenance_label

    assert launch_provenance_label({"launch_source": "ui"}) == "Launch source: Web UI"
    assert launch_provenance_label(
        {"launch_source": "mcp", "source_run_id": 189}
    ) == "Launch source: Codex MCP · based on run #189"
    assert launch_preflight_label(
        {
            "launch_preflight_status": "clear",
            "launch_preflight_observed_at": "2026-07-13T21:00:00",
            "launch_preflight_detail": "observation obs_1; GPUs [4, 5]",
        }
    ) == (
        "GPU preflight: clear · 2026-07-13T21:00:00 · observation obs_1; GPUs [4, 5]"
    )


@pytest.mark.asyncio
async def test_refresh_run_output_does_not_overwrite_with_stale_result():
    from app.ui import runs

    guard = runs.RunOutputRefreshGuard()
    output = SimpleNamespace(value="")
    run = {"id": 123, "server_alias": "gpu01", "tmux_session": "session-a"}
    first_capture_started = asyncio.Event()
    first_capture_can_finish = asyncio.Event()
    second_capture_can_finish = asyncio.Event()
    calls = 0

    async def fake_capture(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_capture_started.set()
            await first_capture_can_finish.wait()
            return "old output"
        await second_capture_can_finish.wait()
        return "new output"

    first = asyncio.create_task(
        runs.refresh_run_output(output, run, guard, capture=fake_capture, ssh_client_factory=object)
    )
    await first_capture_started.wait()
    second = asyncio.create_task(
        runs.refresh_run_output(output, run, guard, capture=fake_capture, ssh_client_factory=object)
    )
    await asyncio.sleep(0)

    first_capture_can_finish.set()
    await asyncio.sleep(0)
    assert output.value == ""

    second_capture_can_finish.set()
    await asyncio.gather(first, second)

    assert output.value == "new output"


@pytest.mark.asyncio
async def test_stop_run_session_notifies_negative_when_stopped_run_cannot_be_marked_exited(monkeypatch):
    from app.ui import runs

    notices = []
    monkeypatch.setattr(runs.ui, "notify", lambda message, **kwargs: notices.append((message, kwargs)))

    async def fake_stop(*_args, **_kwargs):
        return True, "stopped"

    await runs.stop_run_session(
        123,
        {"server_alias": "gpu01", "tmux_session": "session-a"},
        stop=fake_stop,
        mark_exited=lambda _run_id: False,
        ssh_client_factory=object,
    )

    assert notices == [("Run stopped but could not update local status.", {"type": "negative"})]


def test_runs_routes_are_wired(monkeypatch):
    import app.main as main_module

    captured = []
    monkeypatch.setattr(main_module, "app_frame", lambda title, content: captured.append((title, content)))

    main_module.runs_page()
    main_module.run_detail_page("42")

    assert captured[0] == ("Runs", main_module.render_runs_page)
    assert captured[1][0] == "Run"
    assert callable(captured[1][1])

    detail_calls = []
    monkeypatch.setattr(main_module, "render_run_detail", lambda run_id: detail_calls.append(run_id))
    captured[1][1]()
    assert detail_calls == [42]


def _workbench_record(run_id=1, *, state="running", project_id=10):
    return {
        "id": run_id, "name": f"training-{run_id}", "training_run_name": f"full_training_name_{run_id}",
        "project_id": project_id, "project_name": "Example", "server_alias": "gpu01",
        "status": state, "state": state, "tmux_session": f"session-{run_id}",
        "rendered_command": "python train.py", "workdir": "/data/train", "config": {},
    }


def test_workbench_filters_finished_unknown_separately_from_actionable_uncertainty():
    from app.ui.runs import RunWorkbenchState, run_state_view

    records = [_workbench_record(index, state=state) for index, state in enumerate(
        ["running", "unknown", "lost", "failed", "finished_unknown", "exited", "succeeded"], 1)]
    state = RunWorkbenchState(records=records)
    assert [item["id"] for item in state.filtered("attention")] == [2, 3, 4]
    assert [item["id"] for item in state.filtered("finished")] == [5, 6, 7]
    assert run_state_view(records[1])[:2] == ("待核实", "unknown")
    assert run_state_view(records[4])[:2] == ("结果未知", "unknown")
    assert run_state_view(records[3])[:2] == ("失败", "failed")


def test_workbench_refresh_keeps_selection_filters_and_tab_when_selected_run_finishes():
    from app.ui.runs import RunWorkbenchState

    record = _workbench_record()
    state = RunWorkbenchState(records=[record], selected_id=1, query="training", detail_tab="logs")
    state.replace_records([_workbench_record(state="succeeded")])
    assert state.selected_id == 1
    assert state.selected()["state"] == "succeeded"
    assert state.filtered() == []
    assert state.query == "training"
    assert state.filter_key == "active"
    assert state.detail_tab == "logs"


def test_workbench_return_context_round_trip_and_invalid_values():
    from urllib.parse import parse_qs, urlsplit
    from app.ui.runs import RunWorkbenchState, parse_workbench_context, reuse_project_url

    state = RunWorkbenchState(selected_id=17, filter_key="attention", project_filter=10,
                              query="worker & error", detail_tab="logs")
    url = reuse_project_url(_workbench_record(17), state)
    outer_query = parse_qs(urlsplit(url).query)
    assert outer_query["reuse_run"] == ["17"]
    return_url = outer_query["return_to"][0]
    assert urlsplit(return_url).path == "/runs"
    context = parse_workbench_context({key: value[0] for key, value in parse_qs(urlsplit(return_url).query).items()})
    assert context == {"run": 17, "filter": "attention", "project": 10,
                       "q": "worker & error", "tab": "logs"}
    assert parse_workbench_context({"run": "-1", "project": "abc", "filter": "anything", "tab": "bad"}) == {}


def test_workbench_progress_uses_recorded_epoch_unit_and_preserves_iteration_labels():
    from app.ui.runs import RunOutputSnapshot, progress_summary

    snapshot = RunOutputSnapshot()
    snapshot.accept("Learning iteration 5/20")
    assert progress_summary(snapshot.progress) == "5 / 20 iterations · 25%"
    snapshot.accept("2026-10-02 14:34:04,616 epoch=32/100 step=1900/2785 loss=0.023205")
    assert progress_summary(snapshot.progress) == "32 / 100 epochs · 32%"
    assert "iteration" not in snapshot.progress
    snapshot.accept("later log output without a progress line")
    assert progress_summary(snapshot.progress) == "32 / 100 epochs · 32%"
    snapshot.accept("Learning iteration 10/20")
    assert progress_summary(snapshot.progress) == "10 / 20 iterations · 50%"
    assert "epoch" not in snapshot.progress


@pytest.mark.asyncio
async def test_workbench_capture_error_preserves_last_real_output_and_progress():
    from app.ui.runs import RunOutputSnapshot, RunOutputRefreshGuard, capture_workbench_output

    snapshot = RunOutputSnapshot()
    snapshot.accept("Learning iteration 25/100\nETA: 1h\nactual training output")
    first_timestamp = snapshot.collected_at

    async def failed_capture(*_args, **_kwargs):
        raise RuntimeError("SSH timed out")

    await capture_workbench_output(_workbench_record(), snapshot, RunOutputRefreshGuard(),
                                   capture=failed_capture, ssh_client_factory=object)
    assert snapshot.value.endswith("actual training output")
    assert snapshot.progress["percent"] == 25
    assert snapshot.collected_at == first_timestamp
    assert snapshot.error == "SSH timed out"
    snapshot.accept("later real output without a progress line")
    assert snapshot.error == ""
    assert snapshot.progress["percent"] == 25


@pytest.mark.asyncio
async def test_legacy_capture_exception_does_not_overwrite_last_real_log(monkeypatch):
    from app.ui import runs
    output = SimpleNamespace(value="last actual training output")
    monkeypatch.setattr(runs, "_notify", lambda *_args, **_kwargs: None)

    async def failure(*_args, **_kwargs):
        raise RuntimeError("network failure")

    await runs.refresh_run_output(output, _workbench_record(), runs.RunOutputRefreshGuard(),
                                  capture=failure, ssh_client_factory=object)
    assert output.value == "last actual training output"


@pytest.fixture
def native_workbench(monkeypatch):
    """Build real NiceGUI elements without timers, SSH, or a running web server."""
    from nicegui import Client
    from nicegui.page import page
    from app.ui import runs

    records = [_workbench_record(), _workbench_record(2)]
    client = Client(page("/__test_run_workbench"))
    timers = []
    monkeypatch.setattr(runs, "list_run_records", lambda **_kwargs: list(records))
    monkeypatch.setattr(runs, "get_run_detail", lambda run_id: next((r for r in records if r["id"] == run_id), None))
    monkeypatch.setattr(runs, "create_scoped_timer", lambda *args, **kwargs: timers.append((args, kwargs)))
    with client:
        controller = runs.render_run_workbench()
    yield controller, client, records, timers
    client.delete()


def test_native_workbench_select_and_tabs_keep_list_mounted(native_workbench):
    controller, client, _records, _timers = native_workbench
    original_list = controller.list_container
    with client:
        controller.select(2)
        controller.set_tab("logs")
    assert controller.list_container is original_list
    assert not original_list.is_deleted
    assert controller.state.selected_id == 2
    assert controller.state.detail_tab == "logs"
    assert controller.refs["output"].text == "等待采集输出…"
    before = tuple(element.id for element in original_list.default_slot.children)
    controller.render_list()
    assert tuple(element.id for element in original_list.default_slot.children) == before


def test_native_workbench_resources_opens_without_losing_selection(native_workbench):
    from nicegui.elements.dialog import Dialog
    controller, client, _records, timers = native_workbench
    with client:
        controller.set_tab("logs")
        controller.show_resources()
    dialogs = [element for element in client.elements.values() if isinstance(element, Dialog)]
    assert len(dialogs) == 1
    assert dialogs[0].value is True
    assert controller.state.selected_id == 1
    assert controller.state.detail_tab == "logs"
    assert timers[-1][1]["once"] is True
    assert timers[-1][0][1] == 0


@pytest.mark.asyncio
async def test_native_workbench_stop_requires_confirmation_and_freezes_target(native_workbench, monkeypatch):
    from nicegui import background_tasks
    from app.ui import runs
    controller, client, _records, _timers = native_workbench
    stopped = []
    pending = []
    monkeypatch.setattr(background_tasks, "create_or_defer", lambda coroutine, **_kwargs: pending.append(coroutine))

    async def stop(run_id, run):
        stopped.append((run_id, run["tmux_session"]))

    async def refresh():
        pass

    monkeypatch.setattr(runs, "stop_run_session", stop)
    monkeypatch.setattr(controller, "refresh_status", refresh)
    with client:
        controller.confirm_stop()
        assert stopped == []
        button = next(element for element in client.elements.values()
                      if getattr(element, "text", None) == "确认停止 #1")
        controller.select(2)
        listener = next(iter(button._event_listeners.values()))
        listener.handler(SimpleNamespace())
        for coroutine in pending:
            await coroutine
    assert stopped == [(1, "session-1")]
    assert controller.state.selected_id == 2


@pytest.mark.asyncio
async def test_workbench_status_checks_only_live_uncertain_and_selected_records(native_workbench, monkeypatch):
    from app.ui import runs
    controller, client, records, _timers = native_workbench
    records.extend([_workbench_record(3, state="succeeded"), _workbench_record(4, state="finished_unknown"),
                    _workbench_record(5, state="unknown")])
    observed_ids = []

    async def observe(items):
        observed_ids.extend(item["id"] for item in items)
        return items

    async def capture(_run):
        pass

    monkeypatch.setattr(runs, "observe_run_records", observe)
    monkeypatch.setattr(controller, "_capture", capture)
    with client:
        await controller.refresh_status()
    assert observed_ids == [1, 2, 5]
    assert controller.state.selected_id == 1


@pytest.mark.asyncio
async def test_selected_output_late_reply_cannot_replace_another_tasks_output(native_workbench, monkeypatch):
    from app.ui import runs
    controller, client, _records, _timers = native_workbench
    first_started, release_first = asyncio.Event(), asyncio.Event()

    async def capture(run):
        if run["id"] == 1:
            first_started.set()
            await release_first.wait()
        controller.state.outputs.setdefault(run["id"], runs.RunOutputSnapshot()).accept(f"actual output #{run['id']}")

    monkeypatch.setattr(controller, "_capture", capture)
    with client:
        first = asyncio.create_task(controller.refresh_selected_output())
        await first_started.wait()
        controller.select(2)
        await controller.refresh_selected_output()
        assert controller.refs["output"].text == "actual output #2"
        release_first.set()
        await first
    assert controller.refs["output"].text == "actual output #2"
    assert controller.state.outputs[1].value == "actual output #1"


@pytest.mark.asyncio
async def test_output_capture_nonzero_exit_is_collection_error_not_training_log(monkeypatch):
    from app.schemas import CommandResult
    from app.ui import runs

    async def ssh_run(*_args, **_kwargs):
        return CommandResult(exit_status=1, stdout="", stderr="tmux server unavailable")

    monkeypatch.setattr(runs, "SSHClient", lambda: SimpleNamespace(run=ssh_run))
    client = runs._CheckedOutputSSHClient()
    with pytest.raises(RuntimeError, match="tmux server unavailable"):
        await client.run("gpu01", "capture")


def test_native_workbench_config_refresh_retains_expanded_provenance(native_workbench):
    controller, client, _records, _timers = native_workbench
    with client:
        controller.set_tab("config")
        controller.refs["full_name"].set_value(True)
        controller.refs["config_more"].set_value(True)
        controller.render_inspector()
    assert controller.refs["full_name"].value is True
    assert controller.refs["config_more"].value is True
