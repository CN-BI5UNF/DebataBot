"""KarenBot 改名后的旧名兼容测试。"""

from __future__ import annotations

from pathlib import Path

from app_config import paths as paths_module
from app_config.paths import AppPaths


def _fake_user_data_dir(new_root: Path, old_root: Path):
    def fake(name: str, appauthor: bool = False) -> str:
        _ = appauthor
        return str(new_root if name == "KarenBot" else old_root)

    return fake


def test_legacy_data_root_is_used_when_new_root_is_absent(tmp_path, monkeypatch):
    new_root = tmp_path / "KarenBot"
    old_root = tmp_path / "Debata_Agent"
    old_root.mkdir()
    monkeypatch.setattr(
        paths_module,
        "user_data_dir",
        _fake_user_data_dir(new_root, old_root),
    )

    paths = AppPaths(project_root=paths_module.DEFAULT_PROJECT_ROOT)

    assert paths.ROOT_DATA_DIR == old_root


def test_new_data_root_wins_when_both_exist(tmp_path, monkeypatch):
    new_root = tmp_path / "KarenBot"
    old_root = tmp_path / "Debata_Agent"
    new_root.mkdir()
    old_root.mkdir()
    monkeypatch.setattr(
        paths_module,
        "user_data_dir",
        _fake_user_data_dir(new_root, old_root),
    )

    paths = AppPaths(project_root=paths_module.DEFAULT_PROJECT_ROOT)

    assert paths.ROOT_DATA_DIR == new_root


def test_karen_data_root_env_wins_over_legacy_env(tmp_path, monkeypatch):
    karen_root = tmp_path / "karen-env"
    legacy_root = tmp_path / "legacy-env"
    monkeypatch.setenv("KAREN_DATA_ROOT", str(karen_root))
    monkeypatch.setenv("DEBATA_DATA_ROOT", str(legacy_root))

    paths = AppPaths(project_root=paths_module.DEFAULT_PROJECT_ROOT)

    assert paths.ROOT_DATA_DIR == karen_root


def test_legacy_data_root_env_is_still_accepted(tmp_path, monkeypatch):
    legacy_root = tmp_path / "legacy-env"
    monkeypatch.delenv("KAREN_DATA_ROOT", raising=False)
    monkeypatch.setenv("DEBATA_DATA_ROOT", str(legacy_root))

    paths = AppPaths(project_root=paths_module.DEFAULT_PROJECT_ROOT)

    assert paths.ROOT_DATA_DIR == legacy_root


def test_application_version_falls_back_to_legacy_distribution(tmp_path, monkeypatch):
    from app_config import versioning

    def fake_version(name: str) -> str:
        if name == "debata_agent":
            return "0.9.0a0"
        raise versioning.importlib_metadata.PackageNotFoundError(name)

    monkeypatch.setattr(versioning.importlib_metadata, "version", fake_version)

    assert versioning.get_application_version(project_root=tmp_path) == "0.9.0a0"
