"""更新器：镜像校验、fast-forward、脏工作树/依赖变化拒绝、数据目录隔离。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest

from core.updater import (
    UpdateError,
    UpdateService,
    resolve_update_repo_url,
)


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout


def _seed_repo(tmp_path: Path, *, dependency_change: bool = False):
    seed = tmp_path / "seed"
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    data = tmp_path / "data"
    seed.mkdir()
    _git(seed, "init", "-b", "refactor/main")
    _git(seed, "config", "user.email", "test@example.invalid")
    _git(seed, "config", "user.name", "Test")
    (seed / "app.py").write_text("old\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "old")
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "refactor/main")
    subprocess.run(
        ["git", "clone", "-b", "refactor/main", str(remote), str(work)],
        check=True,
        capture_output=True,
    )
    _git(work, "config", "user.email", "test@example.invalid")
    _git(work, "config", "user.name", "Test")
    _git(work, "switch", "-c", "feature/current")
    (work / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "feature")
    data.mkdir()
    (data / "user.txt").write_text("do-not-touch\n", encoding="utf-8")

    (seed / "app.py").write_text("new\n", encoding="utf-8")
    if dependency_change:
        (seed / "pyproject.toml").write_text("[project]\nname='new'\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "new")
    _git(seed, "push", "origin", "refactor/main")
    return work, data, remote


@pytest.mark.asyncio
async def test_check_and_apply_fast_forward_switch_and_preserves_data(tmp_path):
    work, data, remote = _seed_repo(tmp_path)
    restarted = []

    async def restart():
        restarted.append(True)

    service = UpdateService(
        work,
        data,
        mirror=f"file://{remote}",
        allow_local_remote=True,
        restart_callback=restart,
    )
    checked = await service.check()
    assert checked["can_apply"] is True
    assert checked["current_branch"] == "feature/current"
    assert checked["target_ref"] == "refactor/main"
    assert checked["target_commit"] != checked["local_target_commit"]
    assert checked["dependency_changes"] == []

    applied = await service.apply(
        confirmation_token=checked["confirmation_token"],
        target_sha=checked["target_commit"],
        confirm_branch_switch=True,
    )
    await asyncio.sleep(1.2)
    assert applied["new_commit"] == checked["target_commit"]
    assert _git(work, "branch", "--show-current").strip() == "refactor/main"
    assert (work / "app.py").read_text(encoding="utf-8") == "new\n"
    assert not (work / "feature.txt").exists()
    assert (data / "user.txt").read_text(encoding="utf-8") == "do-not-touch\n"
    assert (data / "update_history.jsonl").exists()
    backup = _git(work, "show-ref", applied["backup_ref"]).strip()
    assert backup
    assert restarted


@pytest.mark.asyncio
async def test_dirty_worktree_blocks_check(tmp_path):
    work, data, remote = _seed_repo(tmp_path)
    (work / "app.py").write_text("dirty\n", encoding="utf-8")
    service = UpdateService(work, data, mirror=f"file://{remote}", allow_local_remote=True)
    with pytest.raises(UpdateError) as exc:
        await service.check()
    assert exc.value.code == "worktree_dirty"


@pytest.mark.asyncio
async def test_dependency_change_blocks_apply(tmp_path):
    work, data, remote = _seed_repo(tmp_path, dependency_change=True)
    service = UpdateService(work, data, mirror=f"file://{remote}", allow_local_remote=True)
    checked = await service.check()
    assert checked["can_apply"] is False
    assert checked["reason"] == "dependency_change_requires_install"
    assert "pyproject.toml" in checked["dependency_changes"]
    with pytest.raises(UpdateError) as exc:
        await service.apply(
            confirmation_token=checked["confirmation_token"],
            target_sha=checked["target_commit"],
            confirm_branch_switch=True,
        )
    assert exc.value.code == "dependency_change_requires_install"


@pytest.mark.asyncio
async def test_data_dir_inside_project_is_rejected(tmp_path):
    work, _data, remote = _seed_repo(tmp_path)
    inside = work / "user-data"
    inside.mkdir()
    service = UpdateService(work, inside, mirror=f"file://{remote}", allow_local_remote=True)
    with pytest.raises(UpdateError) as exc:
        await service.check()
    assert exc.value.code == "data_dir_inside_project"


def test_mirror_url_supports_full_and_prefix_and_rejects_unsafe():
    official = resolve_update_repo_url("")
    assert official == "https://github.com/CN-BI5UNF/Debata-Refactor.git"
    prefix = resolve_update_repo_url("https://ghproxy.net/")
    assert prefix == (
        "https://ghproxy.net/https://github.com/CN-BI5UNF/Debata-Refactor.git"
    )
    full = resolve_update_repo_url(
        "https://mirror.example/CN-BI5UNF/Debata-Refactor.git"
    )
    assert full.endswith("/CN-BI5UNF/Debata-Refactor.git")
    full_without_suffix = resolve_update_repo_url(
        "https://mirror.example/CN-BI5UNF/Debata-Refactor/"
    )
    assert full_without_suffix == (
        "https://mirror.example/CN-BI5UNF/Debata-Refactor.git"
    )

    for bad in (
        "http://mirror.example/CN-BI5UNF/Debata-Refactor.git",
        "https://user:pass@mirror.example/CN-BI5UNF/Debata-Refactor.git",
        "https://127.0.0.1/CN-BI5UNF/Debata-Refactor.git",
        "https://localhost/CN-BI5UNF/Debata-Refactor.git",
        "https://mirror.example/other/repo.git",
    ):
        with pytest.raises(UpdateError):
            resolve_update_repo_url(bad)


@pytest.mark.asyncio
async def test_apply_creates_local_target_when_only_remote_tracking_exists(tmp_path):
    work, data, remote = _seed_repo(tmp_path)
    # 模拟真实现场：只有 refs/remotes/refactor/main，没有本地 refactor/main。
    _git(work, "branch", "-D", "refactor/main")
    assert _git(
        work, "rev-parse", "--verify", "refs/heads/refactor/main", check=False
    ).strip() == ""

    service = UpdateService(
        work,
        data,
        mirror=f"file://{remote}",
        allow_local_remote=True,
    )
    checked = await service.check()
    assert checked["local_target_commit"] is None
    assert checked["can_apply"] is True

    applied = await service.apply(
        confirmation_token=checked["confirmation_token"],
        target_sha=checked["target_commit"],
        confirm_branch_switch=True,
    )
    assert applied["new_commit"] == checked["target_commit"]
    assert _git(work, "branch", "--show-current").strip() == "refactor/main"
    assert (data / "user.txt").read_text(encoding="utf-8") == "do-not-touch\n"


@pytest.mark.asyncio
async def test_git_timeout_kills_helpers_holding_pipes(tmp_path, monkeypatch):
    """git 超时要杀掉整个进程组，不能被子进程管道拖住。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_git = bindir / "git"
    fake_git.write_text("#!/bin/sh\nsleep 10 &\nwait\n", encoding="utf-8")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    repo = tmp_path / "repo"
    repo.mkdir()
    service = UpdateService(repo, tmp_path / "data", allow_local_remote=True)

    started = time.monotonic()
    with pytest.raises(UpdateError) as exc:
        await service._git("fetch", "ignored", timeout=0.5)
    elapsed = time.monotonic() - started

    assert exc.value.code == "git_timeout"
    assert elapsed < 5.0, f"超时清理耗时 {elapsed:.1f}s，子进程可能仍在持有管道"
