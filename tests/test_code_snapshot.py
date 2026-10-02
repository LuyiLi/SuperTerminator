import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess

import pytest

from app import code_snapshot
from app.code_snapshot import SnapshotChangedError, SnapshotError, freeze_snapshot, source_metadata


INCLUDE = ("source", "scripts", "config", "assets")


def write(root, relative, content):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    write(root, "pyproject.toml", '[project]\nname = "example"\n')
    write(root, "uv.lock", "version = 1\n")
    write(root, "source/module.py", "value = 1\n")
    write(root, "scripts/train.sh", "#!/bin/sh\ntrue\n").chmod(0o755)
    write(root, "config/newton.yaml", "seed: 1\n")
    write(root, "assets/robot.xml", "<robot />\n")
    return root


def freeze(project, tmp_path, **kwargs):
    return freeze_snapshot(project, tmp_path / "cache", include=INCLUDE, **kwargs)


def git(root, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=check, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "Snapshot test", "GIT_AUTHOR_EMAIL": "test@example.com",
             "GIT_COMMITTER_NAME": "Snapshot test", "GIT_COMMITTER_EMAIL": "test@example.com"},
    )


def init_repo(root):
    git(root, "init", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial saved files")


def test_saved_dirty_worktree_files_and_new_modules_are_frozen(project, tmp_path):
    init_repo(project)
    worktree = tmp_path / "selected-worktree"
    git(project, "worktree", "add", "-b", "newton", str(worktree))
    write(worktree, "source/module.py", "value = 2\n")
    write(worktree, "source/new_module.py", "new = True\n")

    snapshot = freeze(worktree, tmp_path)

    assert snapshot.source_path == str(worktree)
    assert snapshot.branch == "newton"
    assert snapshot.commit == git(worktree, "rev-parse", "HEAD").stdout.strip()
    assert (snapshot.path / "source/module.py").read_text() == "value = 2\n"
    assert (snapshot.path / "source/new_module.py").read_text() == "new = True\n"
    assert not (snapshot.path / ".git").exists()
    assert git(worktree, "status", "--porcelain").stdout
    paths = {item["path"] for item in snapshot.manifest["files"]}
    assert {"pyproject.toml", "uv.lock", "config/newton.yaml", "assets/robot.xml"} <= paths
    assert snapshot.total_bytes == sum(item["size"] for item in snapshot.manifest["files"])


def test_edits_or_deleting_source_after_freeze_cannot_change_snapshot(project, tmp_path):
    snapshot = freeze(project, tmp_path)
    write(project, "source/module.py", "edited after freeze\n")
    (project / "assets/robot.xml").unlink()

    assert (snapshot.path / "source/module.py").read_text() == "value = 1\n"
    assert (snapshot.path / "assets/robot.xml").read_text() == "<robot />\n"
    assert (snapshot.path / "source/module.py").stat().st_ino != (project / "source/module.py").stat().st_ino


def test_same_content_in_different_worktrees_reuses_snapshot(project, tmp_path):
    init_repo(project)
    worktree = tmp_path / "another-branch"
    git(project, "worktree", "add", "-b", "another", str(worktree))
    first = freeze(project, tmp_path)
    os.utime(worktree / "source/module.py", ns=(123456789, 123456789))
    second = freeze(worktree, tmp_path)

    assert first.fingerprint == second.fingerprint
    assert first.path == second.path
    assert first.branch != second.branch
    assert first.source_path != second.source_path
    assert len(list((tmp_path / "cache").iterdir())) == 1


def test_content_permissions_and_relative_paths_are_in_fingerprint(project, tmp_path):
    first = freeze(project, tmp_path)
    write(project, "config/newton.yaml", "seed: 2\n")
    changed = freeze(project, tmp_path)
    assert changed.path != first.path
    assert (first.path / "config/newton.yaml").read_text() == "seed: 1\n"
    (project / "scripts/train.sh").chmod(0o644)
    permissions = freeze(project, tmp_path)
    assert permissions.fingerprint != changed.fingerprint
    (project / "scripts/train.sh").rename(project / "scripts/renamed.sh")
    renamed = freeze(project, tmp_path)
    assert renamed.fingerprint != permissions.fingerprint


def test_detects_concurrent_addition_retries_then_freezes_consistent_files(project, tmp_path, monkeypatch):
    original = code_snapshot._copy_files
    attempts = []

    def copy_and_edit(*args):
        original(*args)
        attempts.append(1)
        if len(attempts) == 1:
            write(project, "source/new.py", "created while freezing\n")
            write(project, "source/module.py", "value = 2\n")

    monkeypatch.setattr(code_snapshot, "_copy_files", copy_and_edit)
    snapshot = freeze(project, tmp_path)

    assert len(attempts) == 2
    assert (snapshot.path / "source/new.py").read_text() == "created while freezing\n"
    assert (snapshot.path / "source/module.py").read_text() == "value = 2\n"
    assert list((tmp_path / "cache").iterdir()) == [snapshot.path]


@pytest.mark.parametrize("change", ["modify", "add", "delete", "replace_identical"])
def test_changes_while_freezing_never_publish_inconsistent_snapshot(project, tmp_path, monkeypatch, change):
    original = code_snapshot._copy_files

    def copy_and_change(*args):
        original(*args)
        path = project / "source/module.py"
        if change == "modify":
            path.write_text("value = 2\n")
        elif change == "add":
            write(project, "source/new.py", "new = 1\n")
        elif change == "delete":
            path.unlink()
        else:
            content = path.read_text()
            path.unlink()
            path.write_text(content)

    monkeypatch.setattr(code_snapshot, "_copy_files", copy_and_change)
    with pytest.raises(SnapshotChangedError, match="暂停保存"):
        freeze(project, tmp_path, retries=0)
    assert list((tmp_path / "cache").iterdir()) == []


def test_copy_detects_changed_file_before_post_inventory(project, tmp_path, monkeypatch):
    original = code_snapshot._copy_files

    def change_before_copy(*args):
        write(project, "source/module.py", "value = 2\n")
        original(*args)

    monkeypatch.setattr(code_snapshot, "_copy_files", change_before_copy)
    with pytest.raises(SnapshotChangedError):
        freeze(project, tmp_path, retries=0)
    assert list((tmp_path / "cache").iterdir()) == []


def test_safety_exclusions_apply_even_to_whole_project(project, tmp_path):
    excluded = [
        ".git/config", ".venv/bin/python", "logs/run/train.log", "data/motion.csv",
        "source/__pycache__/module.pyc", "source/checkpoints/model.ckpt", ".env",
        ".env.backup", "source/credentials.json", "source/private.key", "wandb/run/token",
    ]
    for relative in excluded:
        write(project, relative, "must never synchronize\n")
    # Remove this fake .git: metadata errors should not hide a broken repository.
    (project / ".git/config").unlink()
    (project / ".git").rmdir()
    init_repo(project)
    snapshot = freeze_snapshot(project, tmp_path / "cache", include=(".",))

    assert not (snapshot.path / ".git").exists()
    assert not any((snapshot.path / relative).exists() for relative in excluded)
    assert (snapshot.path / "source/module.py").exists()


def test_explicit_globs_and_exclusions_keep_config_and_locks(project, tmp_path):
    write(project, "source/extra.txt", "not selected")
    write(project, "source/private/skip.py", "excluded")
    snapshot = freeze_snapshot(
        project, tmp_path / "cache", include=("source/**/*.py", "config/*.yaml"),
        exclude=("source/private",),
    )
    # A direct file is selected with ** matching zero directories.
    assert (snapshot.path / "source/module.py").exists()
    assert (snapshot.path / "config/newton.yaml").exists()
    assert (snapshot.path / "pyproject.toml").exists()
    assert (snapshot.path / "uv.lock").exists()
    assert not (snapshot.path / "source/extra.txt").exists()
    assert not (snapshot.path / "source/private").exists()


def test_internal_symlinks_are_materialized_and_external_ones_rejected(project, tmp_path):
    (project / "source/alias.py").symlink_to("module.py")
    snapshot = freeze(project, tmp_path)
    assert (snapshot.path / "source/alias.py").read_text() == "value = 1\n"
    assert not (snapshot.path / "source/alias.py").is_symlink()

    outside = write(tmp_path, "outside.py", "private\n")
    (project / "source/escape.py").symlink_to(outside)
    with pytest.raises(SnapshotError, match="目录之外"):
        freeze(project, tmp_path)


def test_symlinks_cannot_bypass_credentials_exclusions(project, tmp_path):
    write(project, ".env", "SECRET=private")
    (project / "source/alias.py").symlink_to("../.env")
    with pytest.raises(SnapshotError, match="被排除"):
        freeze(project, tmp_path)


def test_internal_directory_symlink_and_cycle(project, tmp_path):
    write(project, "library/helper.py", "helper = True\n")
    (project / "source/library").symlink_to("../library", target_is_directory=True)
    snapshot = freeze(project, tmp_path)
    assert (snapshot.path / "source/library/helper.py").read_text() == "helper = True\n"
    assert not (snapshot.path / "source/library").is_symlink()
    (project / "source/cycle").symlink_to(".", target_is_directory=True)
    with pytest.raises(SnapshotError, match="循环"):
        freeze(project, tmp_path)


def test_special_files_and_oversized_assets_fail_instead_of_silent_omission(project, tmp_path):
    fifo = project / "source/fifo"
    os.mkfifo(fifo)
    with pytest.raises(SnapshotError, match="普通文件"):
        freeze(project, tmp_path)
    fifo.unlink()
    write(project, "assets/large.usd", "x" * 100)
    with pytest.raises(SnapshotError, match="文件过大.*large.usd"):
        freeze(project, tmp_path, max_file_bytes=64)
    assert list((tmp_path / "cache").iterdir()) == []


def test_unresolved_git_conflicts_refuse_freeze(project, tmp_path):
    init_repo(project)
    git(project, "checkout", "-b", "other")
    write(project, "source/module.py", "other = 1\n")
    git(project, "commit", "-am", "other")
    git(project, "checkout", "main")
    write(project, "source/module.py", "main = 1\n")
    git(project, "commit", "-am", "main")
    assert git(project, "merge", "other", check=False).returncode != 0

    with pytest.raises(SnapshotError, match="未解决的 Git 冲突"):
        freeze(project, tmp_path)
    assert list((tmp_path / "cache").iterdir()) == []


def test_plain_source_is_supported_but_lock_and_project_config_required(project, tmp_path):
    assert source_metadata(project) == {"source_path": str(project), "branch": None, "commit": None}
    (project / "uv.lock").unlink()
    with pytest.raises(SnapshotError, match="缺少.*uv.lock"):
        freeze(project, tmp_path)


def test_reuse_verifies_cache_instead_of_overwriting_corruption(project, tmp_path):
    snapshot = freeze(project, tmp_path)
    write(snapshot.path, "source/module.py", "corrupted\n")
    with pytest.raises(SnapshotError, match="缓存.*(失败|大小上限)"):
        freeze(project, tmp_path)
    assert (snapshot.path / "source/module.py").read_text() == "corrupted\n"


def test_concurrent_identical_freezes_publish_one_cache(project, tmp_path):
    with ThreadPoolExecutor(max_workers=4) as executor:
        snapshots = list(executor.map(lambda _: freeze(project, tmp_path), range(4)))
    assert len({snapshot.fingerprint for snapshot in snapshots}) == 1
    assert list((tmp_path / "cache").iterdir()) == [snapshots[0].path]


def test_source_and_sync_configuration_are_explicit(project, tmp_path):
    with pytest.raises(SnapshotError, match="明确"):
        source_metadata("")
    with pytest.raises(SnapshotError, match="之外"):
        freeze_snapshot(project, project / "cache", include=INCLUDE)
    with pytest.raises(SnapshotError, match="不能离开"):
        freeze_snapshot(project, tmp_path / "cache", include=("../another",))
    with pytest.raises(SnapshotError, match="缺少.*uv.lock"):
        freeze(project, tmp_path, exclude=("uv.lock",))


def test_cached_snapshot_still_checks_for_edits_during_freeze(project, tmp_path, monkeypatch):
    first = freeze(project, tmp_path)
    original = code_snapshot._verify_cache
    checks = []

    def verify_then_edit(*args):
        original(*args)
        checks.append(1)
        write(project, "source/module.py", "value = 2\n")

    monkeypatch.setattr(code_snapshot, "_verify_cache", verify_then_edit)
    second = freeze(project, tmp_path)
    assert checks == [1]
    assert second.fingerprint != first.fingerprint
    assert (first.path / "source/module.py").read_text() == "value = 1\n"
    assert (second.path / "source/module.py").read_text() == "value = 2\n"
