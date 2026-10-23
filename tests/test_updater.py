"""更新器：镜像校验、fast-forward、脏工作树/依赖变化拒绝、数据目录隔离。"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from core.updater import (
    MIRROR_PRESETS,
    UpdateError,
    UpdateService,
    mirror_presets,
    resolve_update_repo_url,
)


def _git(
    cwd: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> str:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
        env=run_env,
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


def _seed_release_repo(tmp_path: Path):
    """两个带更新日志的 Release：2026-10-20 和 2026-10-23。"""
    seed = tmp_path / "seed"
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    data = tmp_path / "data"
    seed.mkdir()
    _git(seed, "init", "-b", "refactor/main")
    _git(seed, "config", "user.email", "test@example.invalid")
    _git(seed, "config", "user.name", "Test")
    (seed / "app.py").write_text("base\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "base")
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
    data.mkdir()
    (data / "user.txt").write_text("do-not-touch\n", encoding="utf-8")

    def release(tag: str, title: str, body: str, content: str, date: str) -> None:
        (seed / "app.py").write_text(content, encoding="utf-8")
        notes_dir = seed / "releases"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / f"{tag}.md").write_text(
            f"# {title}\n\n{body}\n", encoding="utf-8"
        )
        _git(seed, "add", ".")
        _git(seed, "commit", "-m", title)
        _git(
            seed,
            "tag",
            "-a",
            tag,
            "-m",
            title,
            env={"GIT_COMMITTER_DATE": date},
        )
        _git(seed, "push", "origin", "refactor/main")
        _git(seed, "push", "origin", tag)

    release(
        "snapshot-20261020",
        "开发快照 2026-10-20",
        "- 第一个版本\n- 修复启动问题",
        "release one\n",
        "2026-10-20T10:00:00+08:00",
    )
    release(
        "snapshot-20261023",
        "开发快照 2026-10-23",
        "- 版本列表\n- 支持回退\n\n这一行用于展开/收起测试。",
        "release two\n",
        "2026-10-23T10:00:00+08:00",
    )
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
    # 当前在 feature/current 时，本地 refactor/main 的旧 tip 也要备份。
    assert applied["backup_branch_ref"]
    assert _git(work, "show-ref", applied["backup_branch_ref"]).strip()
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
    assert official == "https://github.com/CN-BI5UNF/DebataBot.git"
    prefix = resolve_update_repo_url("https://ghproxy.net/")
    assert prefix == (
        "https://ghproxy.net/https://github.com/CN-BI5UNF/DebataBot.git"
    )
    full = resolve_update_repo_url(
        "https://mirror.example/CN-BI5UNF/DebataBot.git"
    )
    assert full.endswith("/CN-BI5UNF/DebataBot.git")
    full_without_suffix = resolve_update_repo_url(
        "https://mirror.example/CN-BI5UNF/DebataBot/"
    )
    assert full_without_suffix == (
        "https://mirror.example/CN-BI5UNF/DebataBot.git"
    )

    presets = mirror_presets()
    assert presets == MIRROR_PRESETS
    assert {item["id"] for item in presets} >= {
        "official", "ghproxy", "ghfast", "ghproxy_net"
    }
    # 返回副本，调用方修改不能污染模块常量。
    presets[0]["value"] = "changed"
    assert MIRROR_PRESETS[0]["value"] == ""

    for bad in (
        "http://mirror.example/CN-BI5UNF/DebataBot.git",
        "https://user:pass@mirror.example/CN-BI5UNF/DebataBot.git",
        "https://127.0.0.1/CN-BI5UNF/DebataBot.git",
        "https://localhost/CN-BI5UNF/DebataBot.git",
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


@pytest.mark.asyncio
async def test_releases_catalog_notes_update_and_rollback(tmp_path):
    work, data, remote = _seed_release_repo(tmp_path)
    service = UpdateService(
        work,
        data,
        mirror=f"file://{remote}",
        allow_local_remote=True,
    )
    catalog = await service.releases()
    tags = [item["tag"] for item in catalog["releases"]]
    assert tags == ["snapshot-20261023", "snapshot-20261020"]
    latest = catalog["releases"][0]
    assert latest["name"] == "开发快照 2026-10-23"
    assert "支持回退" in latest["notes"]
    assert latest["relation"] == "newer"
    assert latest["is_current"] is False

    checked = await service.check()
    assert checked["target_tag"] == "snapshot-20261023"
    assert checked["can_apply"] is True
    assert checked["available"] is True
    assert checked["available_release"]["tag"] == "snapshot-20261023"
    assert "版本列表" in checked["target_notes"]

    applied = await service.apply(
        confirmation_token=checked["confirmation_token"],
        target_sha=checked["target_commit"],
        confirm_branch_switch=True,
    )
    assert applied["target_tag"] == "snapshot-20261023"
    assert (work / "app.py").read_text(encoding="utf-8") == "release two\n"
    assert _git(work, "branch", "--show-current").strip() == "refactor/main"
    assert (data / "user.txt").read_text(encoding="utf-8") == "do-not-touch\n"
    history = (data / "update_history.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(history[-1])["target_tag"] == "snapshot-20261023"
    state = json.loads((data / "update_state.json").read_text(encoding="utf-8"))
    assert state["available"] is None

    old = await service.check(target_tag="snapshot-20261020")
    assert old["is_rollback"] is True
    assert old["can_apply"] is True
    assert old["target_commit"] == catalog["releases"][1]["commit"]
    with pytest.raises(UpdateError) as exc:
        await service.apply(
            confirmation_token=old["confirmation_token"],
            target_sha=old["target_commit"],
            confirm_branch_switch=True,
        )
    assert exc.value.code == "rollback_not_confirmed"
    rolled_back = await service.apply(
        confirmation_token=old["confirmation_token"],
        target_sha=old["target_commit"],
        confirm_branch_switch=True,
        confirm_rollback=True,
    )
    assert rolled_back["kind"] == "older"
    assert (work / "app.py").read_text(encoding="utf-8") == "release one\n"
    assert (data / "user.txt").read_text(encoding="utf-8") == "do-not-touch\n"


@pytest.mark.asyncio
async def test_auto_check_nine_days_state_and_suppression(tmp_path):
    work, data, remote = _seed_release_repo(tmp_path)
    service = UpdateService(
        work,
        data,
        mirror=f"file://{remote}",
        allow_local_remote=True,
        check_interval_days=9,
    )
    result = await service.auto_check_if_due(now=time.time())
    assert result["available"] is True
    state_path = data / "update_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["available"]["tag"] == "snapshot-20261023"
    assert state["next_check_at"] - state["last_check_at"] == pytest.approx(
        9 * 86400, abs=5
    )
    assert stat.S_IMODE(os.stat(state_path).st_mode) == 0o600

    skipped = await service.auto_check_if_due(now=state["last_check_at"] + 3600)
    assert skipped["skipped"] is True
    assert skipped["reason"] == "not_due"

    snapshot = await service.set_suppress_badge(True)
    assert snapshot["suppressed"] is True
    assert snapshot["available"] is False
    assert snapshot["release"]["tag"] == "snapshot-20261023"
    assert snapshot["interval_days"] == 9

    snapshot = await service.set_suppress_badge(False)
    assert snapshot["suppressed"] is False
    assert snapshot["available"] is True


@pytest.mark.asyncio
async def test_auto_check_failure_retries_in_six_hours(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "refactor/main", str(repo)], check=True)
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    data = tmp_path / "data"
    data.mkdir()
    service = UpdateService(
        repo,
        data,
        mirror=f"file://{tmp_path / 'missing.git'}",
        allow_local_remote=True,
    )
    now = time.time()
    result = await service.auto_check_if_due(now=now)
    assert result["ok"] is False
    assert result["error"] in {"git_failed", "git_timeout"}
    state = json.loads((data / "update_state.json").read_text(encoding="utf-8"))
    assert "git_failed" in state["last_error"]
    assert state["next_check_at"] - now == pytest.approx(6 * 3600, abs=5)
    assert state["available"] is None


def test_build_service_cache_and_http_error_mapping(tmp_path):
    from types import SimpleNamespace

    from webui.api import update as update_api

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = SimpleNamespace(
        update=SimpleNamespace(
            mirror="",
            check_timeout_seconds=60,
            check_interval_days=9,
        )
    )
    rt = SimpleNamespace(
        config=config,
        paths=SimpleNamespace(DATA_DIR=data_dir),
        project_root=tmp_path / "project",
    )
    host = SimpleNamespace(rt=rt, project_root=tmp_path / "project")
    service = update_api.build_service(host)
    assert isinstance(service, UpdateService)
    assert service.check_interval_days == 9
    assert update_api.build_service(host) is service
    assert update_api.service_for_host(host) is service

    config.update.check_interval_days = 15
    changed = update_api.build_service(host)
    assert changed is not service
    assert changed.check_interval_days == 15

    assert update_api._http_error(UpdateError("release_not_found", "x")).status_code == 409
    assert update_api._http_error(UpdateError("git_timeout", "x")).status_code == 502
    assert update_api._http_error(UpdateError("mirror_scheme", "x")).status_code == 400


@pytest.mark.asyncio
async def test_version_and_available_snapshots_are_offline(tmp_path):
    work, data, remote = _seed_release_repo(tmp_path)
    service = UpdateService(
        work,
        data,
        mirror=f"file://{remote}",
        allow_local_remote=True,
    )
    version = await service.version_info()
    assert version["version_label"] == "开发版"
    assert version["commit"]
    assert version["commit_short"] == version["commit"][:10]

    snapshot = await service.available_snapshot()
    assert snapshot["available"] is False
    assert snapshot["release"] is None
    assert snapshot["interval_days"] == 9
    assert snapshot["current_commit"] == version["commit"]
