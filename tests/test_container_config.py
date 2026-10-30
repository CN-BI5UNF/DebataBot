"""Container feature config and storage policy contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app_config.paths import AppPaths
from app_config.schema import ContainerFeatureConfig, FeaturesConfig
from features.container.storage import ContainerStorage


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        project_root=tmp_path / "project",
        data_root=tmp_path / "data",
    )


def test_container_defaults_are_safe_and_disabled():
    cfg = ContainerFeatureConfig()
    assert cfg.enabled is False
    assert cfg.storage_mode == "volume_soft"
    assert cfg.network == "none"
    assert cfg.auto_pull_image is True
    assert cfg.image_mirror == ""
    assert cfg.image == "python:3.12-slim"
    assert FeaturesConfig().container.enabled is False


def test_container_names_and_image_validation():
    with pytest.raises(ValidationError):
        ContainerFeatureConfig(container_name="-bad")
    with pytest.raises(ValidationError):
        ContainerFeatureConfig(volume_name="bad name")
    with pytest.raises(ValidationError):
        ContainerFeatureConfig(image=" ")
    with pytest.raises(ValidationError):
        ContainerFeatureConfig(memory_mb=32)
    with pytest.raises(ValidationError):
        ContainerFeatureConfig(workspace_size_mb=32)
    with pytest.raises(ValidationError):
        ContainerFeatureConfig(network="host")


def test_linux_setup_script_keeps_marker_outside_container_workspace():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "container"
        / "karen-container-storage-setup.sh"
    ).read_text(encoding="utf-8")
    assert 'MARKER="$(dirname "$IMAGE")/.karen-container-storage.json"' in script
    assert '"$MOUNTPOINT/.karen-container-storage.json"' not in script


def test_volume_soft_storage_is_ready_without_host_mount():
    cfg = ContainerFeatureConfig(storage_mode="volume_soft")
    paths = _paths(Path("/tmp/karen-container-test"))
    status = ContainerStorage(cfg, paths, platform_name="Linux").status()
    assert status.ready is True
    assert status.mode == "volume_soft"
    assert status.mountpoint == ""


def test_image_fixed_storage_requires_mount_and_marker(tmp_path, monkeypatch):
    cfg = ContainerFeatureConfig(
        storage_mode="image_fixed",
        workspace_size_mb=128,
        image_fixed_mountpoint=str(tmp_path / "workspace"),
        image_fixed_image_path=str(tmp_path / "workspace.ext4"),
    )
    paths = _paths(tmp_path)
    mountpoint = tmp_path / "workspace"
    mountpoint.mkdir()
    storage = ContainerStorage(cfg, paths, platform_name="Linux")

    not_ready = storage.status()
    assert not_ready.ready is False
    assert not_ready.code == "container_storage_marker_invalid"

    (tmp_path / ".karen-container-storage.json").write_text(
        json.dumps({"mode": "image_fixed", "size_mb": 128}),
        encoding="utf-8",
    )
    monkeypatch.setattr(storage, "_is_mount", lambda: True)
    ready = storage.status()
    assert ready.ready is True
    assert ready.mountpoint == str(mountpoint)
    assert ready.marker_size_mb == 128

    (tmp_path / ".karen-container-storage.json").write_text(
        json.dumps({"mode": "image_fixed", "size_mb": 256}),
        encoding="utf-8",
    )
    mismatch = storage.status()
    assert mismatch.ready is False
    assert mismatch.code == "container_storage_size_mismatch"
    assert mismatch.marker_size_mb == 256
    assert "256" in mismatch.error and "128" in mismatch.error
    assert ready.mode == "image_fixed"

    # 采用已有镜像的实际大小后恢复可用；再改成别的大小必须继续报错。
    cfg.workspace_size_mb = 256
    assert storage.status().ready is True
    cfg.workspace_size_mb = 512
    mismatch = storage.status()
    assert mismatch.ready is False
    assert mismatch.code == "container_storage_size_mismatch"


def test_image_fixed_docker_mount_and_setup_commands(tmp_path, monkeypatch):
    cfg = ContainerFeatureConfig(
        storage_mode="image_fixed",
        workspace_size_mb=128,
        image_fixed_mountpoint=str(tmp_path / "workspace"),
        image_fixed_image_path=str(tmp_path / "workspace.ext4"),
    )
    paths = _paths(tmp_path)
    mountpoint = tmp_path / "workspace"
    mountpoint.mkdir()
    (tmp_path / ".karen-container-storage.json").write_text(
        json.dumps({"mode": "image_fixed", "size_mb": 128}),
        encoding="utf-8",
    )
    storage = ContainerStorage(cfg, paths, platform_name="Linux")
    monkeypatch.setattr(storage, "_is_mount", lambda: True)

    args = storage.docker_mount_args()
    assert args[0] == "--mount"
    assert args[1] == (
        f"type=bind,source={mountpoint},target=/workspace"
    )
    setup = storage.setup_commands()
    assert "karen-container-storage-setup.sh" in setup["command"]
    assert setup["requires_admin"] is True


def test_windows_setup_uses_powershell_and_rejects_folder_mount_guidance(tmp_path):
    cfg = ContainerFeatureConfig(
        storage_mode="image_fixed",
        image_fixed_mountpoint=r"K:\workspace",
    )
    paths = _paths(tmp_path)
    storage = ContainerStorage(cfg, paths, platform_name="Windows")
    setup = storage.setup_commands()
    assert setup["argv"][0] == "powershell"
    assert "-File" in setup["argv"]
    assert any("karen-container-vhdx-setup.ps1" in part for part in setup["argv"])
    assert storage.image_path.name == "workspace.vhdx"
