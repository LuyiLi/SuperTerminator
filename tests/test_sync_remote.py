from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import time
from types import SimpleNamespace

import pytest

from app import sync_remote_worker as worker
from app.schemas import CommandResult
from app.sync_remote import SyncRemote, SyncRemoteError


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return {"managed_root": str(tmp_path / "managed"), "external_paths": {},
            "min_free_bytes": 0, "import_module": "example", "python_version": "3.12",
            "wandb": {"base_url": "https://api.wandb.ai", "entity": "test", "project": "test"}}


@pytest.fixture
def snapshot(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    files = {"pyproject.toml": b"[project]\n", "uv.lock": b"version = 1\n", "new.py": b"SAVED = True\n"}
    entries = []
    for name, contents in sorted(files.items()):
        (source / name).write_bytes(contents)
        (source / name).chmod(0o644)
        entries.append({"path": name, "sha256": hashlib.sha256(contents).hexdigest(),
                        "size": len(contents), "mode": 0o644})
    manifest = {"version": 1, "files": entries}
    fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return SimpleNamespace(path=source, manifest=manifest, fingerprint=fingerprint)


def incoming(snapshot, profile):
    _, control = worker.roots(profile)
    path = control / "incoming" / "upload.tar"
    SyncRemote._archive(snapshot, path)
    return {"profile": profile, "fingerprint": snapshot.fingerprint, "archive": str(path)}


@pytest.fixture
def ready(snapshot, profile, monkeypatch):
    monkeypatch.setattr(worker, "verify_environment", lambda *args: "Python 3.12 Newton 1.6.0")
    monkeypatch.setattr(worker.shutil, "which", lambda *args, **kwargs: "/usr/bin/uv")
    calls = []
    monkeypatch.setattr(worker, "run_checked", lambda command, **kwargs: calls.append((command, kwargs)) or "")
    result = worker.prepare(incoming(snapshot, profile))
    return result, calls


def test_prepare_publishes_then_builds_environment_at_final_path(ready, snapshot):
    result, calls = ready
    workdir = Path(result["workdir"])
    assert workdir.name == snapshot.fingerprint
    assert (workdir / "new.py").read_text() == "SAVED = True\n"
    assert (workdir / ".st-source.json").is_file()
    assert (workdir / ".st-env.json").is_file()
    assert not (workdir / ".git").exists()
    assert calls[0][0] == ["/usr/bin/uv", "sync", "--locked", "--python", "3.12"]
    assert calls[0][1]["cwd"] == workdir
    assert calls[0][1]["env"]["UV_PROJECT_ENVIRONMENT"] == str(workdir / ".venv")
    assert calls[0][1]["pass_fds"]  # lock survives loss of the SSH helper


def test_reuse_does_not_sync_or_rebuild_environment(ready, snapshot, profile):
    first, calls = ready
    second = worker.prepare(incoming(snapshot, profile))
    assert second["workdir"] == first["workdir"]
    assert second["environment_reused"] is True
    assert len(calls) == 1


def test_ready_source_cannot_be_overwritten(ready, snapshot, profile):
    result, _ = ready
    (Path(result["workdir"]) / "new.py").write_text("CHANGED\n")
    with pytest.raises(ValueError, match="快照文件"):
        worker.prepare(incoming(snapshot, profile))
    assert (Path(result["workdir"]) / "new.py").read_text() == "CHANGED\n"


def test_interrupted_or_corrupt_transfer_never_publishes(snapshot, profile):
    payload = incoming(snapshot, profile)
    Path(payload["archive"]).write_bytes(b"interrupted")
    with pytest.raises(tarfile.ReadError):
        worker.prepare(payload)
    assert not (Path(profile["managed_root"]) / "versions" / snapshot.fingerprint).exists()


def test_content_validation_rejects_bad_archive(snapshot, profile):
    (snapshot.path / "new.py").write_text("not the frozen file")
    with pytest.raises(ValueError, match="Archive size|快照文件"):
        worker.prepare(incoming(snapshot, profile))
    assert not (Path(profile["managed_root"]) / "versions" / snapshot.fingerprint).exists()


def test_failed_environment_has_no_ready_marker(snapshot, profile, monkeypatch):
    monkeypatch.setattr(worker.shutil, "which", lambda *args, **kwargs: "/bin/uv")
    def fail(*args, **kwargs):
        raise ValueError("dependency unavailable")
    monkeypatch.setattr(worker, "run_checked", fail)
    with pytest.raises(ValueError, match="dependency unavailable"):
        worker.prepare(incoming(snapshot, profile))
    workdir = Path(profile["managed_root"]) / "versions" / snapshot.fingerprint
    assert (workdir / ".st-source.json").exists()
    assert not (workdir / ".st-env.json").exists()


def test_missing_external_data_stops_before_publish(snapshot, profile):
    profile["external_paths"] = {"dataset": "/absent/superterminator-test-data"}
    with pytest.raises(ValueError, match="数据/资产路径不存在"):
        worker.prepare(incoming(snapshot, profile))


def test_missing_registered_uv_has_actionable_error(snapshot, profile):
    profile["uv_path"] = "/missing/superterminator/uv"
    with pytest.raises(ValueError, match="已登记的 uv 路径不可执行"):
        worker.prepare(incoming(snapshot, profile))


def test_environment_symlink_is_rejected_before_uv_runs(snapshot, profile, tmp_path, monkeypatch):
    payload = incoming(snapshot, profile)
    source = worker.prepare({**payload, "phase": "source"})
    external = tmp_path / "isaacsim5"
    external.mkdir()
    (external / "untouched").write_text("existing environment")
    (Path(source["workdir"]) / ".venv").symlink_to(external, target_is_directory=True)
    calls = []
    monkeypatch.setattr(worker, "run_checked", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match=".venv 不是独立目录"):
        worker.prepare(payload)
    assert calls == []
    assert (external / "untouched").read_text() == "existing environment"


def launch_payload(result, profile, run_id=1, command="sleep 30"):
    return {"profile": profile, "run_id": run_id, "workdir": result["workdir"],
            "gpu_ids": [0], "command": command, "session_name": "task-" + str(run_id),
            "worker_source": base64.b64encode(Path(worker.__file__).read_bytes()).decode()}


def mock_preflight(monkeypatch):
    monkeypatch.setattr(worker, "check_gpus", lambda ids: None)
    monkeypatch.setattr(worker, "check_wandb", lambda profile: None)
    def direct_supervisor(command, session_name, log_path):
        # Tests use real detached child processes without touching the user's tmux server.
        with log_path.open("ab") as log:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True, close_fds=True)
    monkeypatch.setattr(worker, "start_supervisor", direct_supervisor)


def test_duplicate_claim_and_concurrent_gpu_reservation(ready, profile, monkeypatch):
    result, _ = ready
    mock_preflight(monkeypatch)
    launches = []
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: launches.append(args))
    payload = launch_payload(result, profile)
    def start(run_id):
        try:
            return worker.launch({**payload, "run_id": run_id})
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(start, [1, 2]))
    assert sum(isinstance(response, dict) for response in responses) == 1
    assert any("占用或预留" in response for response in responses if isinstance(response, str))
    successful = next(response for response in responses if isinstance(response, dict))
    replay = worker.launch({**payload, "run_id": successful["run_id"], "command": "different"})
    assert replay["state"] == "unknown"  # durable claim, no known process
    assert replay["command"] == "sleep 30"
    assert len(launches) == 1


def test_supervisor_uses_existing_tmux_convention(tmp_path, monkeypatch):
    calls = []
    def execute(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="")
    monkeypatch.setattr(worker.subprocess, "run", execute)
    worker.start_supervisor(["python3", "-c", "print('ready')"], "gpu-panel-20260926-120000-9", tmp_path / "supervisor.log")
    assert calls[0][:5] == ["tmux", "new-session", "-d", "-s", "gpu-panel-20260926-120000-9"]
    assert "python3 -c" in calls[0][5]


def test_gpu_checks_include_compute_processes(monkeypatch):
    outputs = iter(["0, GPU-abc, 0, 0\n", "GPU-abc, 12345\n"])
    monkeypatch.setattr(worker, "run_checked", lambda *args, **kwargs: next(outputs))
    with pytest.raises(ValueError, match="已占用"):
        worker.check_gpus([0])


def wait_state(profile, run_id, states, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = worker.inspect({"profile": profile, "run_id": run_id})
        if result["state"] in states:
            return result
        time.sleep(0.05)
    raise AssertionError(result)


def test_real_supervisor_requires_training_output_and_stops_only_task(ready, profile, monkeypatch):
    result, _ = ready
    mock_preflight(monkeypatch)
    command = shlex.join([sys.executable, "-u", "-c", "import time; time.sleep(.3); print('Learning iteration 1/10'); print('wandb: https://wandb.ai/test/test/runs/real123'); time.sleep(30)"])
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        launched = worker.launch(launch_payload(result, profile, command=command))
        assert launched["state"] == "starting"
        observed = wait_state(profile, 1, {"running"})
        assert observed["process_alive"]
        assert observed["wandb_url"] == "https://wandb.ai/test/test/runs/real123"
        replay = worker.launch(launch_payload(result, profile, command=command))
        assert replay["process"] == observed["process"]
        stopped = worker.stop({"profile": profile, "run_id": 1})
        assert stopped["state"] == "stopped"
        assert bystander.poll() is None
        assert not worker.process_alive(observed["process"])
        mirror = Path.home() / ".gpu-ssh-panel" / "runs" / "1" / "output.log"
        assert mirror.resolve() == Path(stopped["log_path"])
    finally:
        bystander.terminate()
        bystander.wait()
        observed = worker.inspect({"profile": profile, "run_id": 1})
        if observed.get("process_alive"):
            worker.stop({"profile": profile, "run_id": 1})


def test_natural_exit_and_distinct_logs(ready, profile, monkeypatch):
    result, _ = ready
    mock_preflight(monkeypatch)
    worker.launch(launch_payload(result, profile, command="printf 'Learning iteration 1/1\\n'"))
    first = wait_state(profile, 1, {"succeeded"})
    worker.launch(launch_payload(result, profile, run_id=2, command="exit 7"))
    second = wait_state(profile, 2, {"failed"})
    assert first["log_path"] != second["log_path"]
    assert second["exit_code"] == 7


def test_surviving_worker_is_not_reported_as_success(ready, profile, monkeypatch):
    result, _ = ready
    mock_preflight(monkeypatch)
    child = "import time; time.sleep(30)"
    parent = "import subprocess,sys; subprocess.Popen([sys.executable,'-c'," + repr(child) + "])"
    command = shlex.join([sys.executable, "-c", parent])
    worker.launch(launch_payload(result, profile, command=command))
    deadline = time.monotonic() + 8
    observed = {}
    try:
        while time.monotonic() < deadline:
            observed = worker.inspect({"profile": profile, "run_id": 1})
            if observed.get("remaining_processes"):
                break
            time.sleep(.05)
        assert observed["state"] == "unknown"
        assert observed["remaining_processes"]
        assert observed["process_alive"]
    finally:
        if observed.get("remaining_processes"):
            worker.stop({"profile": profile, "run_id": 1})


def test_pid_reuse_does_not_signal_another_process(profile, monkeypatch):
    _, control = worker.roots(profile)
    record_path = worker.run_record_path(control, 3)
    record_path.parent.mkdir()
    identity = worker.process_identity(__import__("os").getpid())
    identity["start_time"] = "old-process"
    worker.atomic_json(record_path, {"run_id": 3, "state": "running", "process": identity})
    called = []
    monkeypatch.setattr(worker.os, "killpg", lambda *args: called.append(args))
    with pytest.raises(ValueError, match="无法验证任务进程身份"):
        worker.stop({"profile": profile, "run_id": 3})
    assert not called


def test_no_running_state_from_session_or_old_output(tmp_path):
    output = tmp_path / "output.log"
    output.write_text("Learning iteration 4/100\n")
    result = worker.inspect_record({"state": "starting", "log_path": str(output), "session_name": "exists"})
    assert result["state"] == "unknown"


@pytest.mark.parametrize("url,accepted", [
    ("https://wandb.ai/test/test/runs/real123", True),
    ("https://api.wandb.ai/test/test/runs/real123", True),
    ("https://unrelated.example/test/test/runs/not-wandb", False),
    ("https://wandb.ai/other/test/runs/wrong-entity", False),
    ("https://wandb.ai/test/other/runs/wrong-project", False),
    ("https://wandb.ai/test/test/runs/id/extra-path", False),
    ("https://wandb.ai:8080/test/test/runs/wrong-port", False),
    ("https://wandb.ai@other.example/test/test/runs/wrong-host", False),
])
def test_wandb_url_must_match_configured_service_and_project(tmp_path, profile, url, accepted):
    output = tmp_path / "output.log"
    output.write_text("logged URL: " + url + "\n")
    record = {"state": "succeeded", "log_path": str(output), "wandb": profile["wandb"]}
    assert worker.inspect_record(record)["wandb_url"] == (url if accepted else None)
    # Previously cached URLs follow the same validation, including after log rotation.
    output.write_text("")
    record["wandb_url"] = url
    assert worker.inspect_record(record)["wandb_url"] == (url if accepted else None)


def test_self_hosted_wandb_url_requires_exact_host_and_port(profile):
    settings = {**profile["wandb"], "base_url": "http://100.105.111.86:8080"}
    assert worker.matching_wandb_url("http://100.105.111.86:8080/test/test/runs/actual", settings)
    assert worker.matching_wandb_url("http://100.105.111.86/test/test/runs/actual", settings) is None
    assert worker.matching_wandb_url("https://wandb.ai/test/test/runs/actual", settings) is None


def test_stop_retains_unknown_when_owned_process_survives_kill(profile, monkeypatch):
    _, control = worker.roots(profile)
    record_path = worker.run_record_path(control, 4)
    record_path.parent.mkdir()
    owned = {"pid": 123456, "pgid": 123456, "start_time": "12345"}
    worker.atomic_json(record_path, {"run_id": 4, "state": "running", "process": owned})
    monkeypatch.setattr(worker, "process_alive", lambda identity: identity == owned)
    monkeypatch.setattr(worker, "group_members", lambda pgid: [owned])
    signals = []
    monkeypatch.setattr(worker.os, "killpg", lambda pid, signal: signals.append(signal))
    monkeypatch.setattr(worker.os, "kill", lambda pid, signal: signals.append(signal))
    ticks = iter([0, 6, 10, 13])
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(ticks))
    observed = worker.stop({"profile": profile, "run_id": 4})
    assert observed["state"] == "unknown"
    assert observed["remaining_processes"] == [owned]
    assert observed["process_alive"]
    assert worker.signal.SIGKILL in signals
    assert "ended_at" not in worker.read_json(record_path)


def test_wandb_checks_port_credentials_and_project(profile, monkeypatch):
    monkeypatch.delenv("WANDB_API_KEY", raising=False)
    profile["wandb"]["base_url"] = "http://localhost:8080"
    lookups = []
    class Credentials:
        def authenticators(self, host):
            lookups.append(host)
            return ("api", None, "secret") if host == "localhost:8080" else None
    monkeypatch.setattr(worker.netrc, "netrc", Credentials)
    queries = []
    def request(request, timeout):
        queries.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"data": {"viewer": {"id": "ok"}, "project": {"name": "test"}}}).encode())
    monkeypatch.setattr(worker.urllib.request, "urlopen", request)
    worker.check_wandb(profile)
    assert lookups == ["localhost:8080"]
    assert queries[0]["variables"] == {"entity": "test", "project": "test"}


@pytest.mark.asyncio
async def test_lost_launch_ack_queries_original_task_once(profile):
    class SSH:
        def __init__(self):
            self.calls = []
        async def run(self, alias, command, timeout):
            self.calls.append(command)
            if len(self.calls) == 1:
                raise TimeoutError("lost ack")
            return CommandResult(0, json.dumps({"ok": True, "result": {"claimed": True, "state": "starting", "run_id": 9}}), "")
    ssh = SSH()
    remote = SyncRemote(ssh)
    result = await remote.launch("test", 9, "/unused", "unused", [0], profile, "task-9")
    assert result["run_id"] == 9
    assert len(ssh.calls) == 2
    assert shlex.split(ssh.calls[0])[-2] == "launch"
    assert shlex.split(ssh.calls[1])[-2] == "inspect"


@pytest.mark.asyncio
async def test_uncertain_launch_never_retries(profile):
    class SSH:
        async def run(self, *args, **kwargs):
            raise ConnectionError()
    result = await SyncRemote(SSH()).launch("test", 9, "/unused", "unused", [0], profile, "task-9")
    assert result["state"] == "unknown"


@pytest.mark.asyncio
async def test_known_preflight_failure_preserves_reason(profile):
    class SSH:
        async def run(self, alias, command, timeout):
            if shlex.split(command)[-2] == "launch":
                return CommandResult(1, json.dumps({"ok": False, "error": "GPU 0 已占用"}), "")
            return CommandResult(0, json.dumps({"ok": True, "result": {"state": "unknown", "claimed": False}}), "")
    with pytest.raises(SyncRemoteError, match="GPU 0 已占用"):
        await SyncRemote(SSH()).launch("test", 9, "/unused", "unused", [0], profile, "task-9")


def test_inspect_missing_task_never_creates_remote_directories(profile):
    result = worker.inspect({'profile': profile, 'run_id': 1})
    assert result['state'] == 'unknown' and result['claimed'] is False
    assert not Path(profile['managed_root']).exists()
