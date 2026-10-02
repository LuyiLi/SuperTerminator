"""Freeze saved project files into content-addressed, independent code snapshots.

Includes are relative paths or shell globs; a matching directory includes its
contents. File permissions are normalized to 0644/0755, preserving executability. Required lock/config files and safety exclusions cannot be overridden.
No Git checkout, commit, or remote worktree is involved in a snapshot.
"""
from __future__ import annotations

import fnmatch
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass


class SnapshotError(RuntimeError):
    """The selected saved files cannot safely be used for a launch."""


class SnapshotChangedError(SnapshotError):
    """Files changed during all attempts to freeze the selected source."""


@dataclass(frozen=True)
class Snapshot:
    fingerprint: str
    path: Path
    manifest: dict
    source_path: str
    branch: str | None
    commit: str | None
    total_bytes: int


@dataclass(frozen=True)
class _File:
    path: str
    sha256: str
    size: int
    mode: int
    identity: tuple

    def manifest_entry(self) -> dict:
        return {"path": self.path, "sha256": self.sha256, "size": self.size, "mode": self.mode}


class _SourceChanged(Exception):
    pass


_REQUIRED = ("pyproject.toml", "uv.lock")
# These are excluded even when a caller includes the entire project.
_PRIVATE_DIRS = frozenset({
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".cache", ".tox", ".nox", "node_modules", ".ssh", ".aws",
    ".config", ".codex", ".codex_runs", "logs", "wandb", "checkpoints",
    "checkpoint", "tensorboard",
})
_LARGE_ROOTS = frozenset({"data", "datasets", "artifacts", "outputs", "runs", "results"})
_PRIVATE_FILES = (
    ".env", ".env.*", ".netrc", ".git-credentials", ".pypirc", "id_rsa*",
    "id_ed25519*", "*.pem", "*.key", "credentials.json", "credentials.yaml",
    "credentials.yml", "service-account*.json", "*.ckpt", "*.pth", "*.pt",
    "*.safetensors", "*.pyc", "*.pyo",
)
_DEFAULT_MAX_FILE_BYTES = 32 * 1024 * 1024


def _source_root(path: str | Path) -> Path:
    if not str(path).strip():
        raise SnapshotError("请选择明确的本地代码目录。")
    try:
        root = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SnapshotError(f"本地代码目录不可访问：{path}。请检查所选 worktree。") from exc
    if not root.is_dir():
        raise SnapshotError(f"代码来源不是目录：{root}")
    return root


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    # Ambient Git variables must not redirect a selected worktree to another repo.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            timeout=15, check=False, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotError("无法检查所选目录的 Git 状态；请检查 git 是否可用。") from exc


def source_metadata(path: str | Path) -> dict:
    """Describe exactly the selected directory; never infer a source from cwd."""
    root = _source_root(path)
    repo = _git(root, "rev-parse", "--is-inside-work-tree")
    if repo.returncode:
        # A plain exported source tree is supported; a broken .git must be repaired.
        if any((ancestor / ".git").exists() for ancestor in (root, *root.parents)):
            raise SnapshotError(f"无法读取所选目录的 Git 元数据：{root}")
        return {"source_path": str(root), "branch": None, "commit": None}
    if repo.stdout.strip() != "true":
        raise SnapshotError(f"代码来源不是 Git 工作目录：{root}")
    branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    commit = _git(root, "rev-parse", "--verify", "HEAD")
    return {
        "source_path": str(root),
        "branch": branch.stdout.strip() if branch.returncode == 0 else None,
        "commit": commit.stdout.strip() if commit.returncode == 0 else None,
    }


def _check_conflicts(root: Path) -> None:
    result = _git(root, "ls-files", "--unmerged", "-z")
    if result.returncode:
        if any((ancestor / ".git").exists() for ancestor in (root, *root.parents)):
            raise SnapshotError("无法检查 Git 冲突；请修复所选 worktree 的 Git 状态。")
    elif result.stdout:
        raise SnapshotError("所选代码有未解决的 Git 冲突；请先解决冲突再启动。")


def _patterns(values: tuple[str, ...]) -> tuple[str, ...]:
    result = []
    for value in values:
        if not isinstance(value, str) or not value or "\\" in value:
            raise SnapshotError("同步范围必须是非空的相对 POSIX 路径或 glob。")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\x00" in value:
            raise SnapshotError(f"同步范围不能离开代码来源目录：{value!r}")
        result.append(str(path))
    return tuple(dict.fromkeys(result))


@lru_cache(maxsize=32768)
def _glob_match(parts: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return _glob_match(parts, pattern[1:]) or bool(parts) and _glob_match(parts[1:], pattern)
    return bool(parts) and fnmatch.fnmatchcase(parts[0], pattern[0]) and _glob_match(parts[1:], pattern[1:])


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    # Match an ancestor too: selecting a directory includes its whole subtree.
    parts = tuple(path.split("/"))
    return any(
        pattern == "." or any(
            _glob_match(parts[:index], tuple(pattern.split("/")))
            for index in range(1, len(parts) + 1)
        )
        for pattern in patterns
    )


def _could_include(path: str, patterns: tuple[str, ...]) -> bool:
    if _matches(path, patterns):
        return True
    for pattern in patterns:
        anchor = []
        for part in pattern.split("/"):
            if any(character in part for character in "*?["):
                break
            anchor.append(part)
        prefix = "/".join(anchor)
        if not prefix or prefix == path or prefix.startswith(path + "/") or path.startswith(prefix + "/"):
            return True
    return False


def _excluded(path: str, patterns: tuple[str, ...]) -> bool:
    parts = path.split("/")
    return (
        any(part in _PRIVATE_DIRS for part in parts)
        or parts[0].lower() in _LARGE_ROOTS
        or any(fnmatch.fnmatchcase(part, pattern) for part in parts for pattern in _PRIVATE_FILES)
        or _matches(path, patterns)
    )


def _identity(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _resolve(root: Path, path: Path, exclude: tuple[str, ...]) -> Path:
    try:
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise SnapshotError(f"同步文件的符号链接指向代码目录之外：{path.relative_to(root)}") from exc
    except RuntimeError as exc:
        raise SnapshotError(f"同步范围中存在循环符号链接：{path.relative_to(root)}") from exc
    except FileNotFoundError as exc:
        raise _SourceChanged(f"文件消失或符号链接失效：{path.relative_to(root)}") from exc
    if _excluded(relative, exclude):
        raise SnapshotError(f"同步符号链接指向被排除的数据或凭据：{path.relative_to(root)}")
    return resolved


def _open_file(root: Path, path: Path):
    """Open a resolved regular file without following raced-in directory symlinks."""
    parts = path.relative_to(root).parts
    flags = os.O_RDONLY | os.O_NOFOLLOW
    directory = os.open(root, flags | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    stream = os.fdopen(descriptor, "rb")
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        stream.close()
        raise SnapshotError(f"同步范围只允许普通文件：{path.relative_to(root)}")
    return stream


def _read_file(root: Path, path: Path, exclude: tuple[str, ...], max_bytes: int,
               output: Path | None = None) -> _File:
    relative = path.relative_to(root).as_posix()
    resolved = _resolve(root, path, exclude)
    # Include every symlink in the identity so retargeting cannot produce a mixed tree.
    links = []
    cursor = root
    for component in path.relative_to(root).parts:
        cursor = cursor / component
        info = cursor.lstat()
        if stat.S_ISLNK(info.st_mode):
            links.append((cursor.relative_to(root).as_posix(), _identity(info), os.readlink(cursor)))
    digest = hashlib.sha256()
    length = 0
    with _open_file(root, resolved) as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > max_bytes:
            raise SnapshotError(f"同步文件过大：{relative}（{before.st_size} 字节，上限 {max_bytes}）；请将大资产配置为外部路径。")
        target = None
        try:
            if output is not None:
                output.parent.mkdir(parents=True, exist_ok=True)
                target = output.open("xb")
            while chunk := stream.read(1024 * 1024):
                length += len(chunk)
                if length > max_bytes:
                    raise SnapshotError(f"同步文件超过大小上限：{relative}；请使用外部资产路径。")
                digest.update(chunk)
                if target is not None:
                    target.write(chunk)
        finally:
            if target is not None:
                target.close()
        after = os.fstat(stream.fileno())
    if _identity(before) != _identity(after) or length != after.st_size:
        raise _SourceChanged(f"读取期间文件发生变化：{relative}")
    if resolved != _resolve(root, path, exclude) or _identity(resolved.stat()) != _identity(after):
        raise _SourceChanged(f"读取期间文件被替换：{relative}")
    if after.st_mode & 0o7000:
        raise SnapshotError(f"同步文件含特殊权限位：{relative}；请移除 setuid/setgid/sticky 权限。")
    # Normalize non-semantic owner/group write bits across Git worktrees.
    # Executability is the only Git file permission affecting the code version.
    mode = 0o755 if after.st_mode & 0o111 else 0o644
    if output is not None:
        output.chmod(mode)
    return _File(relative, digest.hexdigest(), length, mode,
                 (resolved.relative_to(root).as_posix(), tuple(links), _identity(after)))


def _inventory(root: Path, include: tuple[str, ...], exclude: tuple[str, ...],
               max_bytes: int) -> dict[str, _File]:
    files = {}

    def visit(directory: Path, ancestors: frozenset[Path]) -> None:
        resolved = _resolve(root, directory, exclude)
        if resolved in ancestors:
            raise SnapshotError(f"同步范围中存在循环目录链接：{directory.relative_to(root)}")
        ancestors = ancestors | {resolved}
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(root).as_posix()
            if _excluded(relative, exclude) or not _could_include(relative, include):
                continue
            actual = _resolve(root, path, exclude)
            info = actual.stat()
            if stat.S_ISDIR(info.st_mode):
                visit(path, ancestors)
            elif _matches(relative, include):
                if not stat.S_ISREG(info.st_mode):
                    raise SnapshotError(f"同步范围只允许普通文件和目录：{relative}")
                files[relative] = _read_file(root, path, exclude, max_bytes)

    visit(root, frozenset())
    for required in _REQUIRED:
        if required not in files:
            raise SnapshotError(f"代码来源缺少必须同步的文件 {required}；请检查目录和同步配置。")
    return files


def _copy_files(root: Path, target: Path, files: dict[str, _File],
                exclude: tuple[str, ...], max_bytes: int) -> None:
    for relative, expected in files.items():
        actual = _read_file(root, root / relative, exclude, max_bytes, target / relative)
        if actual != expected:
            raise _SourceChanged(f"冻结期间文件发生变化：{relative}")


def _verify_cache(path: Path, files: dict[str, _File]) -> None:
    actual_paths = set()
    for directory, dirs, filenames in os.walk(path, followlinks=False):
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise SnapshotError(f"本地快照缓存已损坏：{path}；请在确认没有准备中的任务后移走该缓存。")
        for name in filenames:
            file_path = Path(directory) / name
            relative = file_path.relative_to(path).as_posix()
            actual_paths.add(relative)
            expected = files.get(relative)
            if expected is None or file_path.is_symlink():
                raise SnapshotError(f"本地快照缓存包含意外文件：{file_path}；请检查缓存。")
            try:
                actual = _read_file(path, file_path, (), max(expected.size, 1))
            except (OSError, _SourceChanged, SnapshotError) as exc:
                raise SnapshotError(f"本地快照缓存校验失败：{file_path}；{exc}") from exc
            if (actual.manifest_entry() != expected.manifest_entry()
                    or stat.S_IMODE(file_path.stat().st_mode) != expected.mode):
                raise SnapshotError(f"本地快照缓存校验失败：{file_path}；请检查缓存，不要覆盖运行中的版本。")
    if actual_paths != set(files):
        raise SnapshotError(f"本地快照缓存文件不完整：{path}；请检查缓存。")


def freeze_snapshot(source_path: str | Path, cache_root: str | Path, *,
                    include: tuple[str, ...], exclude: tuple[str, ...] = (),
                    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES, retries: int = 2) -> Snapshot:
    """Return an independent saved-file snapshot after two matching inventories.

    ``retries`` counts retries after the first attempt. A changed inventory,
    identity or file while copying discards the candidate; it can never become a
    ready snapshot. Same content/path/mode in any worktree reuses the same cache.
    """
    root = _source_root(source_path)
    includes = _patterns((*include, *_REQUIRED))
    excludes = _patterns(exclude)
    if max_file_bytes < 1 or retries < 0:
        raise SnapshotError("快照文件上限必须大于零，重试次数不能小于零。")
    cache = Path(cache_root).expanduser().resolve()
    if cache == root or root in cache.parents:
        raise SnapshotError("快照缓存必须位于所选代码目录之外。")
    cache.mkdir(parents=True, exist_ok=True)
    last_change = ""
    for _attempt in range(retries + 1):
        temporary = None
        try:
            metadata = source_metadata(root)
            _check_conflicts(root)
            before = _inventory(root, includes, excludes, max_file_bytes)
            manifest = {"version": 1, "files": [before[key].manifest_entry() for key in sorted(before)]}
            encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
            fingerprint = hashlib.sha256(encoded).hexdigest()
            final = cache / fingerprint
            if final.exists() or final.is_symlink():
                if not final.is_dir() or final.is_symlink():
                    raise SnapshotError(f"本地快照缓存目录无效：{final}")
                _verify_cache(final, before)
            else:
                temporary = Path(tempfile.mkdtemp(prefix=".freezing-", dir=cache))
                _copy_files(root, temporary, before, excludes, max_file_bytes)
            after = _inventory(root, includes, excludes, max_file_bytes)
            _check_conflicts(root)
            if before != after or source_metadata(root) != metadata:
                raise _SourceChanged("冻结期间同步范围内的文件或 Git 分支发生变化")
            if temporary is not None:
                try:
                    temporary.rename(final)
                    temporary = None
                except OSError:
                    # Another request may have frozen the exact same content.
                    if not final.is_dir() or final.is_symlink():
                        raise
                    _verify_cache(final, before)
            return Snapshot(
                fingerprint=fingerprint, path=final, manifest=manifest,
                source_path=metadata["source_path"], branch=metadata["branch"],
                commit=metadata["commit"], total_bytes=sum(file.size for file in before.values()),
            )
        except (_SourceChanged, FileNotFoundError) as exc:
            last_change = str(exc)
        except OSError as exc:
            raise SnapshotError(f"无法冻结代码文件：{exc}。请检查路径权限和磁盘空间。") from exc
        finally:
            if temporary is not None:
                shutil.rmtree(temporary)
    raise SnapshotChangedError(
        f"准备代码失败：{last_change}；已尝试 {retries + 1} 次。请暂停保存文件后重试。"
    )
