"""Small stdlib-only remote helper. Sent over SSH; never imports the panel.

The durable claim precedes process creation. An incomplete claim stays unknown,
reserves its GPUs, and can never be used to launch another process.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import netrc
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request
import uuid


TERMINAL = {"succeeded", "failed", "stopped"}


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def read_json(path):
    return json.loads(Path(path).read_text())


@contextmanager
def lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield stream.fileno()


def roots(profile, *, create=True):
    root = Path(profile["managed_root"])
    if not root.is_absolute() or root == Path("/") or "isaacsim5" in root.parts:
        raise ValueError("Newton 托管目录必须为独立的绝对路径，不能使用 isaacsim5")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or "isaacsim5" in root.resolve().parts:
        raise ValueError("托管根目录不能指向 isaacsim5 或符号链接")
    control = root / ".superterminator"
    if create:
        control.mkdir(exist_ok=True)
        (control / "runs").mkdir(exist_ok=True)
        (root / "versions").mkdir(exist_ok=True)
        (control / "incoming").mkdir(exist_ok=True)
    return root, control


def version_path(root, fingerprint):
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("Invalid snapshot fingerprint")
    return root / "versions" / fingerprint


def validate_manifest(manifest, fingerprint):
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != fingerprint:
        raise ValueError("快照清单指纹不匹配，请重新冻结代码")
    seen = set()
    for entry in manifest["files"]:
        relative = PurePosixPath(entry["path"])
        if (relative.is_absolute() or ".." in relative.parts or not relative.parts
                or relative.parts[0] in {".git", ".venv", "logs", ".st-source.json", ".st-env.json"}
                or str(relative) != entry["path"] or entry["path"] in seen):
            raise ValueError("Unsafe or duplicate snapshot file")
        seen.add(entry["path"])


def verify_source(workdir, manifest):
    for entry in manifest["files"]:
        path = workdir / entry["path"]
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(workdir.resolve()):
            raise ValueError("快照文件缺失或变更: " + entry["path"])
        if path.stat().st_size != entry["size"]:
            raise ValueError("快照文件大小不匹配: " + entry["path"])
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != entry["sha256"]:
            raise ValueError("快照文件校验失败: " + entry["path"])
        if (path.stat().st_mode & 0o777) != (entry["mode"] & 0o777):
            raise ValueError("快照文件权限变更: " + entry["path"])


def check_assets_and_disk(profile, workdir, extra_bytes=0):
    for name, value in profile.get("external_paths", {}).items():
        path = Path(value)
        if not path.is_absolute() or not path.exists():
            raise ValueError("数据/资产路径不存在: " + str(name) + " = " + str(path))
    required = int(profile.get("min_free_bytes", 10 * 1024 ** 3)) + extra_bytes
    if shutil.disk_usage(workdir).free < required:
        raise ValueError("托管目录磁盘空间不足，请释放空间后重试")


def run_checked(command, *, cwd=None, env=None, timeout=1800, pass_fds=()):
    result = subprocess.run(command, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                            pass_fds=pass_fds)
    if result.returncode:
        raise ValueError("命令失败 " + command[0] + ": " + result.stdout[-6000:])
    return result.stdout


def environment_variables(workdir):
    environment = os.environ.copy()
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["UV_PROJECT_ENVIRONMENT"] = str(workdir / ".venv")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PATH"] = str(Path.home() / ".local" / "bin") + os.pathsep + environment.get("PATH", "")
    return environment


def verify_environment(workdir, profile):
    python = workdir / ".venv" / "bin" / "python"
    script = (
        "import importlib, pathlib, sys; "
        "root=pathlib.Path(sys.argv[1]).resolve(); "
        "assert pathlib.Path(sys.prefix).resolve()==root/'.venv', 'wrong Python environment'; "
        "module=importlib.import_module(sys.argv[2]); "
        "paths=[getattr(module,'__file__',None)] + list(getattr(module,'__path__',[])); "
        "assert any(p and pathlib.Path(p).resolve().is_relative_to(root) "
        "and not pathlib.Path(p).resolve().is_relative_to(root/'.venv') for p in paths), "
        "'project import does not point to this snapshot'; "
        "import importlib.metadata; "
        "assert importlib.metadata.version('newton') == sys.argv[4], 'wrong Newton version'; "
        "exec(\"import gymnasium\\nfrom isaaclab_tasks.utils import load_cfg_from_registry\\n"
        "from isaaclab_newton.physics import NewtonCfg\\n"
        "gymnasium.spec(sys.argv[3])\\ncfg=load_cfg_from_registry(sys.argv[3], 'env_cfg_entry_point')\\n"
        "assert isinstance(cfg.sim.physics, NewtonCfg), 'task does not use Newton'\" if sys.argv[3] else 'pass'); "
        "print(sys.version)"
    )
    environment = environment_variables(workdir)
    environment["CUDA_VISIBLE_DEVICES"] = ""
    return run_checked([str(python), "-c", script, str(workdir),
                        profile.get("import_module", "whole_body_tracking"), profile.get("task", ""),
                        profile.get("newton_version", "1.6.0")],
                       cwd=workdir, env=environment, timeout=90).strip()


def prepare(payload):
    profile = payload["profile"]
    root, control = roots(profile)
    workdir = version_path(root, payload["fingerprint"])
    archive = Path(payload["archive"])
    if archive.parent != control / "incoming" or not archive.name.endswith(".tar"):
        raise ValueError("Invalid incoming archive path")
    # One lock protects publication and environment preparation for this version.
    with lock(control / ("environment-" + workdir.name + ".lock")) as environment_lock:
        if not (workdir / ".st-source.json").exists():
            if workdir.exists():
                raise ValueError("版本目录未完成，不能启动；请检查中断的同步")
            with tarfile.open(archive, "r") as bundle:
                member = bundle.getmember(".st-manifest.json")
                if member.size > 16 * 1024 * 1024:
                    raise ValueError("Snapshot manifest too large")
                manifest = json.load(bundle.extractfile(member))
                validate_manifest(manifest, workdir.name)
                expected = {item["path"]: item for item in manifest["files"]}
                check_assets_and_disk(profile, root, sum(item["size"] for item in expected.values()))
                staging = control / "incoming" / (workdir.name + "." + uuid.uuid4().hex)
                staging.mkdir()
                seen = set()
                try:
                    for member in bundle.getmembers():
                        if member.name == ".st-manifest.json":
                            continue
                        if member.name not in expected or not member.isfile() or member.name in seen:
                            raise ValueError("Archive contains an unexpected file")
                        seen.add(member.name)
                        entry = expected[member.name]
                        if member.size != entry["size"]:
                            raise ValueError("Archive size does not match manifest")
                        destination = staging / member.name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with bundle.extractfile(member) as source, destination.open("wb") as target:
                            shutil.copyfileobj(source, target)
                        destination.chmod(entry["mode"] & 0o777)
                    if seen != set(expected):
                        raise ValueError("同步内容不完整，不能启动")
                    verify_source(staging, manifest)
                    atomic_json(staging / ".st-source.json", manifest)
                    os.rename(staging, workdir)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
        else:
            manifest = read_json(workdir / ".st-source.json")
            validate_manifest(manifest, workdir.name)
            verify_source(workdir, manifest)
        archive.unlink(missing_ok=True)
        if payload.get("phase") == "source":
            return {"workdir": str(workdir), "log_root": str(workdir / "logs")}
        check_assets_and_disk(profile, workdir)
        venv = workdir / ".venv"
        if venv.is_symlink() or (venv.exists() and not venv.is_dir()):
            raise ValueError("快照 .venv 不是独立目录，不能准备环境；请检查并移除错误链接")
        environment_ready = workdir / ".st-env.json"
        reused = environment_ready.exists()
        if reused:
            ready = read_json(environment_ready)
            if ready.get("import_module") != profile.get("import_module", "whole_body_tracking") or ready.get("python_version") != profile.get("python_version"):
                raise ValueError("固定 Newton 环境配置已改变；请人工检查，不能重建已有版本环境")
            python_info = verify_environment(workdir, profile)
        else:
            # Never repair an environment used by an uncertain or active task.
            for record in (control / "runs").glob("*/record.json"):
                task = read_json(record)
                if task.get("workdir") == str(workdir) and inspect_record(task).get("state") not in TERMINAL:
                    raise ValueError("该环境仍有训练使用或状态待确认，不能重建")
            for filename in ("pyproject.toml", "uv.lock"):
                if not (workdir / filename).is_file():
                    raise ValueError("缺少 " + filename + "，请修正同步配置")
            environment = environment_variables(workdir)
            uv = profile.get("uv_path")
            if uv and (not Path(uv).is_absolute() or not Path(uv).is_file() or not os.access(uv, os.X_OK)):
                raise ValueError("已登记的 uv 路径不可执行，请修正机器配置: " + str(uv))
            uv = uv or shutil.which("uv", path=environment["PATH"])
            if not uv:
                raise ValueError("远端未安装 uv，请完成一次性机器配置")
            command = [uv, "sync", "--locked"]
            if profile.get("python_version"):
                command += ["--python", profile["python_version"]]
            run_checked(command, cwd=workdir, env=environment,
                        timeout=int(profile.get("environment_timeout", 1800)),
                        pass_fds=(environment_lock,))
            python_info = verify_environment(workdir, profile)
            verify_source(workdir, manifest)
            atomic_json(environment_ready, {"fingerprint": workdir.name, "python": python_info,
                        "import_module": profile.get("import_module", "whole_body_tracking"),
                        "python_version": profile.get("python_version")})
        return {"workdir": str(workdir), "log_root": str(workdir / "logs"),
                "environment_reused": reused, "python": python_info}


def process_identity(pid):
    try:
        contents = Path("/proc", str(int(pid)), "stat").read_text()
        fields = contents[contents.rfind(")") + 2:].split()
        if fields[0] == "Z":
            return None
        return {"pid": int(pid), "start_time": fields[19], "pgid": int(fields[2])}
    except (OSError, ValueError, IndexError):
        return None


def process_alive(identity):
    return bool(identity and process_identity(identity["pid"]) == identity)


def group_members(pgid):
    return [item for path in Path("/proc").iterdir() if path.name.isdigit()
            and (item := process_identity(int(path.name))) and item["pgid"] == pgid]


def tail(path, limit=256 * 1024):
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - limit))
            return stream.read().decode(errors="replace")
    except OSError:
        return ""


def matching_wandb_url(value, settings):
    if not value or not settings.get("entity") or not settings.get("project"):
        return None
    try:
        base = urllib.parse.urlparse(settings["base_url"])
        candidate = urllib.parse.urlparse(value)
        hosts = {base.hostname}
        if base.hostname == "api.wandb.ai":
            hosts.add("wandb.ai")
        if (candidate.scheme not in {"http", "https"} or candidate.hostname not in hosts
                or candidate.port != base.port or candidate.username or candidate.password):
            return None
        parts = candidate.path.strip("/").split("/")
        if (len(parts) != 4 or urllib.parse.unquote(parts[0]) != settings["entity"]
                or urllib.parse.unquote(parts[1]) != settings["project"] or parts[2] != "runs"
                or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[3])):
            return None
        return value
    except (KeyError, ValueError):
        return None


def inspect_record(record):
    result = dict(record)
    output = tail(record.get("log_path", "/dev/null"))
    settings = record.get("wandb", {})
    # Validate both fresh output and cached values against the configured service.
    urls = re.findall(r"https?://[^\s\x1b<>\"']+", output)
    verified_urls = [verified for url in urls
                     if (verified := matching_wandb_url(url.rstrip(".,;)"), settings))]
    result["wandb_url"] = (verified_urls[-1] if verified_urls
                          else matching_wandb_url(record.get("wandb_url"), settings))
    alive = process_alive(record.get("process"))
    survivors = [item for item in record.get("remaining_processes", []) if process_alive(item)]
    result["process_alive"] = alive
    result["live_verified"] = True
    if survivors:
        result.update(state="unknown", process_alive=True,
                      status_detail=("状态待确认：已发送停止信号，但任务进程尚未退出，请刷新检查"
                                     if record.get("stop_requested") else
                                     "训练主进程已退出但子进程仍存在，请停止该任务或人工检查"))
        return result
    if record["state"] in TERMINAL:
        return result
    if alive:
        if re.search(r"Learning iteration\s+\d+\s*/\s*\d+", output):
            result.update(state="running", status_detail="已确认训练进程和正常训练输出")
        else:
            result.update(state="starting", status_detail="训练进程存在，等待正常训练输出")
    elif process_alive(record.get("supervisor")):
        result.update(state="starting", status_detail="启动监督进程存在，等待训练进程")
    else:
        result.update(state="unknown", live_verified=False,
                      status_detail="状态待确认：任务已登记但无法核实训练进程，禁止重复启动")
        if record.get("launch_error"):
            result["status_detail"] += "；启动阶段返回: " + record["launch_error"]
    return result


def run_record_path(control, run_id):
    if type(run_id) is not int or run_id <= 0:
        raise ValueError("Invalid run ID")
    return control / "runs" / str(run_id) / "record.json"


def inspect(payload):
    _, control = roots(payload["profile"], create=False)
    record_path = run_record_path(control, payload["run_id"])
    if not record_path.exists():
        return {"state": "unknown", "claimed": False, "status_detail": "远端未找到任务登记，不能据此自动重启"}
    return inspect_record(read_json(record_path))


def check_gpus(gpu_ids):
    if not gpu_ids or len(set(gpu_ids)) != len(gpu_ids) or any(type(item) is not int or item < 0 for item in gpu_ids):
        raise ValueError("请选择有效且不重复的 GPU")
    output = run_checked(["nvidia-smi", "--query-gpu=index,uuid,memory.used,utilization.gpu",
                          "--format=csv,noheader,nounits"], timeout=15)
    devices = {}
    for line in output.splitlines():
        index, device_uuid, memory, utilization = [part.strip() for part in line.split(",")]
        devices[int(index)] = (device_uuid, int(memory), int(utilization))
    processes = run_checked(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid",
                            "--format=csv,noheader,nounits"], timeout=15)
    occupied = {line.split(",", 1)[0].strip() for line in processes.splitlines() if "," in line}
    for index in gpu_ids:
        if index not in devices:
            raise ValueError("GPU " + str(index) + " 不存在")
        device_uuid, memory, utilization = devices[index]
        if device_uuid in occupied or memory > 512 or utilization > 5:
            raise ValueError("GPU " + str(index) + " 已占用，请选择空闲 GPU")


def check_wandb(profile):
    settings = profile.get("wandb", {})
    base = settings.get("base_url", "https://api.wandb.ai").rstrip("/")
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("W&B 服务地址无效")
    if not settings.get("entity") or not settings.get("project"):
        raise ValueError("请登记 W&B entity 和 project")
    key = os.environ.get("WANDB_API_KEY")
    if not key:
        try:
            auth = netrc.netrc()
            credentials = auth.authenticators(parsed.netloc) or auth.authenticators(parsed.hostname)
            key = credentials[2] if credentials else None
        except (OSError, netrc.NetrcParseError):
            pass
    if not key:
        raise ValueError("W&B 凭据缺失，请在该机器完成登录后重试")
    credentials = base64.b64encode(("api:" + key).encode()).decode()
    query = {"query": "query($entity: String!, $project: String!) { viewer { id } project(name: $project, entityName: $entity) { name } }",
             "variables": {"entity": settings["entity"], "project": settings["project"]}}
    request = urllib.request.Request(base + "/graphql", data=json.dumps(query).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Basic " + credentials})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.load(response)
        if result.get("errors") or not result.get("data", {}).get("viewer", {}).get("id"):
            raise ValueError("W&B 登录验证失败")
        if not result.get("data", {}).get("project", {}).get("name"):
            raise ValueError("W&B 项目不存在或当前凭据无访问权限，请检查 entity/project")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("W&B 连接/认证检查失败，请检查服务和已有凭据 (" +
                         str(getattr(exc, "code", type(exc).__name__)) + ")") from exc


def start_supervisor(command, session_name, log_path):
    """Use the panel's tmux session convention; process ownership stays explicit."""
    shell_command = "exec " + shlex.join(command) + " > " + shlex.quote(str(log_path)) + " 2>&1"
    result = subprocess.run(["tmux", "new-session", "-d", "-s", session_name, shell_command],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, timeout=15)
    if result.returncode:
        raise ValueError("tmux 启动失败: " + result.stdout[-2000:])


def launch(payload):
    profile = payload["profile"]
    root, control = roots(profile)
    record_path = run_record_path(control, payload["run_id"])
    workdir = Path(payload["workdir"])
    if workdir != version_path(root, workdir.name):
        raise ValueError("启动目录不是托管快照")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", payload["session_name"]):
        raise ValueError("Invalid tmux session name")
    with lock(control / "machine-launch.lock"), lock(control / ("environment-" + workdir.name + ".lock")):
        if record_path.exists():
            return inspect_record(read_json(record_path))
        if not (workdir / ".st-source.json").is_file() or not (workdir / ".st-env.json").is_file():
            raise ValueError("快照或环境未就绪，不能启动")
        manifest = read_json(workdir / ".st-source.json")
        validate_manifest(manifest, workdir.name)
        verify_source(workdir, manifest)
        verify_environment(workdir, profile)
        check_assets_and_disk(profile, workdir)
        check_wandb(profile)
        if not shutil.which("tmux"):
            raise ValueError("远端未安装 tmux，请完成一次性机器配置")
        for path in (control / "runs").glob("*/record.json"):
            other = inspect_record(read_json(path))
            if other["state"] not in TERMINAL and set(other["gpu_ids"]) & set(payload["gpu_ids"]):
                raise ValueError("GPU 被任务 " + str(other["run_id"]) + " 占用或预留（含启动中/状态待确认）")
        check_gpus(payload["gpu_ids"])
        record_path.parent.mkdir()
        log_dir = workdir / "logs" / ("task-" + str(payload["run_id"]))
        log_dir.mkdir(parents=True, exist_ok=False)
        record = {"run_id": payload["run_id"], "claimed": True, "state": "starting",
                  "workdir": str(workdir), "gpu_ids": payload["gpu_ids"],
                  "command": payload["command"], "session_name": payload["session_name"],
                  "log_path": str(log_dir / "output.log"), "wandb_url": None,
                  "wandb": {key: profile["wandb"][key] for key in ("base_url", "entity", "project")},
                  "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "status_detail": "任务已登记，准备启动"}
        atomic_json(record_path, record)
        # tmux detaches the supervisor from SSH. Its training child gets a private
        # process group. The claim survives a lost launch acknowledgement.
        source = payload["worker_source"]
        supervisor_payload = {"profile": profile, "run_id": payload["run_id"]}
        encoded_payload = base64.b64encode(json.dumps(supervisor_payload).encode()).decode()
        command = [sys.executable, "-c", "exec(__import__('base64').b64decode(" + repr(source) + "))",
                   "supervise", encoded_payload]
        try:
            start_supervisor(command, payload["session_name"], record_path.parent / "supervisor.log")
        except Exception as exc:
            with lock(record_path.parent / "run.lock"):
                current = read_json(record_path)
                current["launch_error"] = str(exc)
                atomic_json(record_path, current)
            raise
        return {**record, "process_alive": False, "live_verified": False}


def supervise(payload):
    _, control = roots(payload["profile"])
    record_path = run_record_path(control, payload["run_id"])
    record = read_json(record_path)
    record["supervisor"] = process_identity(os.getpid())
    workdir = Path(record["workdir"])
    environment = environment_variables(workdir)
    environment.update(CUDA_VISIBLE_DEVICES=",".join(map(str, record["gpu_ids"])),
                       WANDB_MODE="online", WANDB_DISABLED="false", PYTHONUNBUFFERED="1",
                       WANDB_BASE_URL=payload["profile"]["wandb"]["base_url"],
                       WANDB_ENTITY=payload["profile"]["wandb"]["entity"],
                       WANDB_PROJECT=payload["profile"]["wandb"]["project"],
                       WANDB_DIR=str(Path(record["log_path"]).parent),
                       WANDB_RUN_ID="st-" + str(payload["run_id"]))
    with lock(record_path.parent / "run.lock"):
        atomic_json(record_path, record)
        legacy = Path.home() / ".gpu-ssh-panel" / "runs" / str(payload["run_id"])
        legacy.mkdir(parents=True, exist_ok=True)
        output = legacy / "output.log"
        if not output.exists() and not output.is_symlink():
            output.symlink_to(record["log_path"])
        with Path(record["log_path"]).open("ab", buffering=0) as log:
            process = subprocess.Popen(["bash", "-c", record["command"]], cwd=workdir,
                                       env=environment, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=log, start_new_session=True, close_fds=True)
        record["process"] = process_identity(process.pid)
        if record["process"] is None:
            record["process"] = {"pid": process.pid, "start_time": "exited", "pgid": process.pid}
        atomic_json(record_path, record)
    return_code = process.wait()
    with lock(record_path.parent / "run.lock"):
        current = read_json(record_path)
        if current.get("state") != "stopped":
            remaining = group_members(process.pid)
            current["remaining_processes"] = remaining
            current["state"] = ("unknown" if remaining else "stopped" if current.get("stop_requested")
                                else "succeeded" if return_code == 0 else "failed")
            current["status_detail"] = ("训练主进程已退出但子进程仍存在，请停止该任务" if remaining
                                        else "训练已退出 (exit " + str(return_code) + ")")
        current["exit_code"] = return_code
        current["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        current["wandb_url"] = inspect_record(current).get("wandb_url")
        atomic_json(record_path, current)
    return {"state": current["state"]}


def stop(payload):
    _, control = roots(payload["profile"], create=False)
    record_path = run_record_path(control, payload["run_id"])
    if not record_path.exists():
        raise ValueError("远端任务不存在，不能安全停止")
    with lock(record_path.parent / "run.lock"):
        record = read_json(record_path)
        if record["state"] in TERMINAL:
            return inspect_record(record)
        identity = record.get("process")
        leader_alive = process_alive(identity) and identity["pgid"] == identity["pid"]
        survivors = [item for item in record.get("remaining_processes", []) if process_alive(item)]
        if not leader_alive and not survivors:
            raise ValueError("状态待确认：无法验证任务进程身份，未发送停止信号")
        members = group_members(identity["pgid"]) if leader_alive else survivors
        # Signal a private group only while its leader's identity still matches.
        if leader_alive:
            if not process_alive(identity):
                raise ValueError("训练进程已改变，请重新查询状态")
            os.killpg(identity["pgid"], signal.SIGTERM)
        else:
            for item in members:
                if process_alive(item):
                    try:
                        os.kill(item["pid"], signal.SIGTERM)
                    except ProcessLookupError:
                        pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(process_alive(item) for item in members):
            time.sleep(0.1)
        for item in members:
            if process_alive(item):
                try:
                    os.kill(item["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and any(process_alive(item) for item in members):
            time.sleep(0.05)
        remaining = [item for item in members if process_alive(item)]
        # A surviving known member proves this is still the owned group. Discover
        # late children only in that case; never adopt a potentially reused PGID.
        residual_group = group_members(identity["pgid"]) if identity else []
        if remaining:
            remaining = residual_group or remaining
        record["remaining_processes"] = remaining
        if remaining or residual_group:
            record.update(state="unknown", stop_requested=True,
                          status_detail="状态待确认：已发送停止信号，但进程组尚未确认退出，请刷新检查")
            record.pop("ended_at", None)
        else:
            record.update(state="stopped", status_detail="用户停止了该任务的进程组",
                          ended_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        atomic_json(record_path, record)
        return inspect_record(record)


def main():
    action = sys.argv[1]
    payload = json.loads(base64.b64decode(sys.argv[2]))
    try:
        result = {"prepare": prepare, "launch": launch, "inspect": inspect,
                  "stop": stop, "supervise": supervise}[action](payload)
        print(json.dumps({"ok": True, "result": result}))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "action": action}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
