"""W00：进程加载身份必须与当前磁盘源码身份分开。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.build_identity import (
    capture_build_identity,
    compute_build_identity,
    loaded_build_identity,
    reset_build_identity_for_tests,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git 不可用")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    (root / "core").mkdir()
    (root / "core" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "README.md").write_text("# test\n", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "chat.db").write_bytes(b"SQLite format 3\x00secret")
    (root / ".env").write_text("API_KEY=do-not-hash\n", encoding="utf-8")
    _git(root, "add", "core/app.py", "README.md")
    _git(root, "commit", "-q", "-m", "base")
    return root


def test_identity_tracks_wip_without_reading_data(tmp_path):
    root = _repo(tmp_path)
    identity = compute_build_identity(root)

    assert len(identity["base_commit"]) == 40
    assert identity["dirty"] is True  # data/ 与 .env 未跟踪，但...
    # WIP 路径只列受控源码，不把 .env/data 暴露出去。
    assert "core/app.py" not in identity["wip_paths"]
    assert all(not path.startswith(("data/", ".env")) for path in identity["wip_paths"])
    assert identity["source_file_count"] >= 2

    (root / "core" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    changed = compute_build_identity(root)
    assert changed["dirty"] is True
    assert changed["source_bundle_sha256"] != identity["source_bundle_sha256"]
    assert "core/app.py" in changed["wip_paths"]


def test_captured_identity_does_not_follow_disk_changes(tmp_path):
    root = _repo(tmp_path)
    reset_build_identity_for_tests()
    try:
        loaded = capture_build_identity(root)
        assert loaded_build_identity() == loaded

        (root / "core" / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        current = compute_build_identity(root)
        still_loaded = loaded_build_identity()
        assert still_loaded["source_bundle_sha256"] != current["source_bundle_sha256"]
        assert still_loaded["captured_at"] == loaded["captured_at"]
    finally:
        reset_build_identity_for_tests()


def test_identity_does_not_include_secret_or_data_bytes(tmp_path):
    root = _repo(tmp_path)
    identity = compute_build_identity(root)
    serialized = repr(identity)
    assert "do-not-hash" not in serialized
    assert "secret" not in serialized
    assert "data/chat.db" not in identity["wip_paths"]
    assert identity["identity_kind"] == "startup_source_snapshot"


def test_identity_never_reads_symlink_or_outside_root(tmp_path, monkeypatch):
    import core.build_identity as module

    root = _repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE-SECRET", encoding="utf-8")
    (root / "core" / "link.py").symlink_to(outside)
    (root / "core" / "changed.py").write_text("VALUE = 4\n", encoding="utf-8")
    read_paths: list[str] = []
    original = module._read_source_file

    def guarded(path):
        read_paths.append(str(path))
        if "outside" in str(path) or path.name in {".env", "data/chat.db"}:
            raise AssertionError(f"attempted sensitive read: {path}")
        return original(path)

    monkeypatch.setattr(module, "_read_source_file", guarded)
    identity = module.compute_build_identity(root)
    assert "core/link.py" not in identity["wip_paths"]
    assert all("outside" not in path for path in read_paths)
    assert all(not path.endswith("/.env") for path in read_paths)


@pytest.mark.asyncio
async def test_build_api_keeps_loaded_and_source_separate(tmp_path):
    from types import SimpleNamespace

    from webui.api.system import build_identity

    root = _repo(tmp_path)
    reset_build_identity_for_tests()
    try:
        loaded = capture_build_identity(root)
        (root / "core" / "app.py").write_text("VALUE = 99\n", encoding="utf-8")
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(
                host=SimpleNamespace(project_root=root)
            ))
        )
        data = await build_identity(request)
        assert data["loaded"]["source_bundle_sha256"] == loaded["source_bundle_sha256"]
        assert data["source"]["source_bundle_sha256"] != loaded["source_bundle_sha256"]
        assert data["loaded_matches_source"] is False
    finally:
        reset_build_identity_for_tests()
