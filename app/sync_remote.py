"""SSH transport for immutable snapshots and the small remote run protocol."""

from __future__ import annotations

import asyncio
import base64
import inspect
import io
import json
from pathlib import Path
import shlex
import tarfile
import tempfile
from typing import Any
import uuid

import asyncssh

from app.ssh_client import SSHClient


class SyncRemoteError(RuntimeError):
    def __init__(self, stage: str, message: str, *, definitive: bool = False):
        super().__init__(message)
        self.stage = stage
        self.definitive = definitive


class SyncRemote:
    def __init__(self, ssh_client=None):
        self.ssh_client = ssh_client or SSHClient()
        self._source = base64.b64encode(Path(__file__).with_name("sync_remote_worker.py").read_bytes()).decode()

    async def _call(self, alias: str, action: str, payload: dict, timeout: int = 120) -> dict:
        payload = dict(payload)
        stage = ("sync" if payload.get("phase") == "source" else "environment") if action == "prepare" else action
        if action == "launch":
            payload["worker_source"] = self._source
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        script = "exec(__import__('base64').b64decode(" + repr(self._source) + "))"
        command = shlex.join(["python3", "-c", script, action, encoded])
        response = await self.ssh_client.run(alias, command, timeout=timeout)
        try:
            result = json.loads(response.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            detail = (response.stderr or response.stdout or "远端没有返回任务状态")[-2000:]
            raise SyncRemoteError(stage, detail) from exc
        if not result.get("ok"):
            raise SyncRemoteError(stage, result.get("error", "远端操作失败"), definitive=True)
        if response.exit_status != 0:
            raise SyncRemoteError(stage, "远端检查未完成，请查看任务详情")
        return result["result"]

    async def _upload(self, alias: str, local_path: Path, remote_path: str) -> None:
        # Tests and alternative transports may supply upload; SSHClient.run stays
        # unchanged so existing machine/log/start/stop callers keep their contract.
        upload = getattr(self.ssh_client, "upload", None)
        if upload is not None:
            await upload(alias, str(local_path), remote_path)
            return
        async with asyncssh.connect(alias) as connection:
            async with connection.start_sftp_client() as sftp:
                await sftp.makedirs(str(Path(remote_path).parent), exist_ok=True)
                await sftp.put(str(local_path), remote_path)

    @staticmethod
    def _archive(snapshot, target: Path) -> None:
        manifest = snapshot.manifest
        with tarfile.open(target, "w") as bundle:
            contents = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            metadata = tarfile.TarInfo(".st-manifest.json")
            metadata.size = len(contents)
            metadata.mode = 0o600
            bundle.addfile(metadata, io.BytesIO(contents))
            for item in manifest["files"]:
                path = Path(snapshot.path) / item["path"]
                bundle.add(path, arcname=item["path"], recursive=False)

    async def prepare(self, server_alias: str, snapshot, profile: dict, progress=None) -> dict:
        async def emit(stage):
            if progress is not None:
                result = progress({"stage": stage})
                if inspect.isawaitable(result):
                    await result

        await emit("sync")
        remote_path = str(Path(profile["managed_root"]) / ".superterminator" / "incoming" /
                          (snapshot.fingerprint + "." + uuid.uuid4().hex + ".tar"))
        with tempfile.TemporaryDirectory(prefix="superterminator-upload-") as temporary:
            archive = Path(temporary) / "snapshot.tar"
            await asyncio.to_thread(self._archive, snapshot, archive)
            await self._upload(server_alias, archive, remote_path)
        payload = {"profile": profile, "fingerprint": snapshot.fingerprint, "archive": remote_path}
        await self._call(server_alias, "prepare", {**payload, "phase": "source"}, timeout=300)
        await emit("environment")
        return await self._call(server_alias, "prepare", payload,
                                timeout=int(profile.get("environment_timeout", 1800)) + 150)

    async def launch(self, server_alias: str, run_id: int, workdir: str, command: str,
                     gpu_ids: list[int], profile: dict, session_name: str) -> dict:
        payload = {"run_id": run_id, "workdir": workdir, "command": command,
                   "gpu_ids": gpu_ids, "profile": profile, "session_name": session_name}
        try:
            return await self._call(server_alias, "launch", payload, timeout=180)
        except Exception as exc:
            # Never issue another launch after losing an SSH acknowledgement.
            result = None
            try:
                result = await self.inspect(server_alias, run_id, profile)
            except Exception:
                pass
            if result and result.get("claimed"):
                return result
            if result and isinstance(exc, SyncRemoteError) and exc.definitive:
                raise
            return {"state": "unknown", "live_verified": False,
                    "status_detail": "状态待确认：启动连接中断，未重启任务；请查询原任务 (" + type(exc).__name__ + ")"}

    async def inspect(self, server_alias: str, run_id: int, profile: dict) -> dict:
        return await self._call(server_alias, "inspect", {"run_id": run_id, "profile": profile}, timeout=30)

    async def stop(self, server_alias: str, run_id: int, profile: dict) -> dict:
        return await self._call(server_alias, "stop", {"run_id": run_id, "profile": profile}, timeout=30)
