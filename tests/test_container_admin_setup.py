"""C storage administrator setup without real sudo/UAC or host mounts."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app_config.paths import AppPaths
from app_config.schema import ContainerFeatureConfig
from features.container import ContainerError, ContainerService
from features.container.admin import (
    AdminCommandOutcome,
    AdminSetupResult,
    admin_capabilities,
    linux_setup_argv,
    run_admin_setup,
    windows_setup_argv,
)
from features.container.storage import ContainerStorage

LINUX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="Linux sudo/bash paths are unavailable"
)


def _make_storage(
    tmp_path: Path, *, platform_name: str = "Linux", suffix: str = ".sh"
) -> ContainerStorage:
    project = tmp_path / "project"
    script_dir = project / "scripts" / "container"
    script_dir.mkdir(parents=True)
    script_name = (
        "karen-container-vhdx-setup.ps1"
        if platform_name.startswith("Win")
        else "karen-container-storage-setup.sh"
    )
    (script_dir / script_name).write_text(
        "# fake setup script\n", encoding="utf-8"
    )
    paths = AppPaths(project_root=project, data_root=tmp_path / "data")
    cfg = ContainerFeatureConfig(
        enabled=True,
        storage_mode="image_fixed",
        workspace_size_mb=128,
        image_fixed_mountpoint=str(tmp_path / "workspace"),
        image_fixed_image_path=str(
            tmp_path / ("workspace.vhdx" if platform_name.startswith("Win") else "workspace.ext4")
        ),
    )
    storage = ContainerStorage(cfg, paths, platform_name=platform_name)
    storage.mountpoint.mkdir()
    return storage


def _write_marker(storage: ContainerStorage, monkeypatch) -> None:
    storage.marker_path.parent.mkdir(parents=True, exist_ok=True)
    storage.marker_path.write_text(
        json.dumps({"mode": "image_fixed", "size_mb": storage.size_bytes // (1024 * 1024)}),
        encoding="utf-8",
    )
    monkeypatch.setattr(storage, "_is_mount", lambda: True)


@LINUX_ONLY
def test_linux_setup_argv_never_contains_password(tmp_path):
    storage = _make_storage(tmp_path)
    argv = linux_setup_argv(storage, environ={"KAREN_SUDO_BIN": "/bin/true"})
    assert argv[:5] == ["/bin/true", "-S", "-p", "", "--"]
    assert argv[5].endswith("bash")
    assert str(storage.mountpoint) in argv
    assert "secret" not in " ".join(argv)


@LINUX_ONLY
@pytest.mark.asyncio
async def test_linux_setup_pipes_password_on_stdin(tmp_path, monkeypatch):
    storage = _make_storage(tmp_path)
    captured: dict[str, object] = {}

    async def runner(argv, stdin_bytes, _timeout):
        captured["argv"] = list(argv)
        captured["stdin"] = stdin_bytes
        _write_marker(storage, monkeypatch)
        return AdminCommandOutcome(returncode=0)

    result = await run_admin_setup(
        storage,
        "s3cret",
        environ={"KAREN_SUDO_BIN": "/bin/true"},
        runner=runner,
    )
    assert result.ok is True
    assert result.admin_method == "sudo_password"
    assert captured["stdin"] == b"s3cret\n"
    assert "s3cret" not in " ".join(captured["argv"])


@LINUX_ONLY
@pytest.mark.asyncio
async def test_linux_auth_failure_is_mapped(tmp_path):
    storage = _make_storage(tmp_path)

    async def runner(_argv, _stdin, _timeout):
        return AdminCommandOutcome(
            returncode=1, output_tail="Sorry, try again.\nsudo: 1 incorrect password attempt"
        )

    result = await run_admin_setup(
        storage,
        "wrong",
        environ={"KAREN_SUDO_BIN": "/bin/true"},
        runner=runner,
    )
    assert result.ok is False
    assert result.code == "container_admin_auth_failed"


@pytest.mark.asyncio
async def test_password_is_required_on_linux(tmp_path):
    storage = _make_storage(tmp_path)
    with pytest.raises(ContainerError) as error:
        await run_admin_setup(storage, "", runner=_never_runner)
    assert error.value.code == "container_admin_password_required"


async def _never_runner(_argv, _stdin, _timeout):  # pragma: no cover - must not run
    raise AssertionError("runner must not be called")


@pytest.mark.asyncio
async def test_windows_uses_encoded_uac_launcher_and_no_password(tmp_path, monkeypatch):
    storage = _make_storage(tmp_path, platform_name="Windows", suffix=".ps1")
    captured: dict[str, object] = {}

    async def runner(argv, stdin_bytes, _timeout):
        captured["argv"] = list(argv)
        captured["stdin"] = stdin_bytes
        _write_marker(storage, monkeypatch)
        return AdminCommandOutcome(returncode=0)

    result = await run_admin_setup(
        storage,
        "ignored-password",
        platform_name="Windows",
        environ={"KAREN_POWERSHELL_BIN": sys.executable},
        runner=runner,
    )
    assert result.ok is True
    assert result.admin_method == "uac"
    assert captured["stdin"] is None
    encoded = captured["argv"][-1]
    launcher = base64.b64decode(encoded).decode("utf-16-le")
    assert "Start-Process" in launcher
    assert "-Verb RunAs" in launcher
    assert "karen-container-vhdx-setup.ps1" in launcher
    assert "ignored-password" not in " ".join(captured["argv"])
    assert "ignored-password" not in launcher


def test_windows_setup_argv_has_no_shell_script(tmp_path):
    storage = _make_storage(tmp_path, platform_name="Windows", suffix=".ps1")
    argv = windows_setup_argv(storage, environ={"KAREN_POWERSHELL_BIN": sys.executable})
    launcher = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert "Start-Process" in launcher
    assert "-Verb RunAs" in launcher


@LINUX_ONLY
def test_admin_capabilities_explain_password_transport_risk(tmp_path):
    linux = admin_capabilities("Linux", environ={"KAREN_SUDO_BIN": "/bin/true"})
    assert linux["method"] == "sudo_password"
    assert linux["requires_password"] is True
    assert "stdin" in linux["note"]

    windows = admin_capabilities(
        "Windows", environ={"KAREN_POWERSHELL_BIN": sys.executable}
    )
    assert windows["method"] == "uac"
    assert windows["requires_password"] is False
    assert "UAC" in windows["note"]


@pytest.mark.asyncio
async def test_service_setup_returns_ready_without_running_again(tmp_path, monkeypatch):
    storage = _make_storage(tmp_path)
    _write_marker(storage, monkeypatch)
    monkeypatch.setattr(ContainerStorage, "_is_mount", lambda self: True)
    service = ContainerService(
        storage.config,
        storage.paths,
        docker=SimpleNamespace(available=lambda: False),
        platform_name="Linux",
    )
    result = await service.setup_storage(password="not-needed")
    assert result["ok"] is True
    assert result["already_ready"] is True


@pytest.mark.asyncio
async def test_service_setup_refuses_size_mismatch_without_prompting_sudo(tmp_path, monkeypatch):
    storage = _make_storage(tmp_path)
    storage.marker_path.parent.mkdir(parents=True, exist_ok=True)
    storage.marker_path.write_text(
        json.dumps({"mode": "image_fixed", "size_mb": 256}), encoding="utf-8"
    )
    monkeypatch.setattr(ContainerStorage, "_is_mount", lambda self: True)
    service = ContainerService(
        storage.config,
        storage.paths,
        docker=SimpleNamespace(available=lambda: False),
        platform_name="Linux",
    )
    called = []
    monkeypatch.setattr(
        "features.container.service.run_admin_setup",
        lambda *args, **kwargs: called.append(True),
    )
    with pytest.raises(ContainerError) as error:
        await service.setup_storage(password="unused")
    assert error.value.code == "container_storage_size_mismatch"
    assert error.value.details["marker_size_mb"] == 256
    assert called == []


@pytest.mark.asyncio
async def test_service_setup_surfaces_safe_error_details(tmp_path, monkeypatch):
    storage = _make_storage(tmp_path)
    service = ContainerService(
        storage.config,
        storage.paths,
        docker=SimpleNamespace(available=lambda: False),
        platform_name="Linux",
    )

    async def fake_admin(_storage, _password, **_kwargs):
        return AdminSetupResult(
            ok=False,
            code="container_admin_auth_failed",
            message="sudo 密码不正确或认证失败",
            exit_code=1,
            output_tail="incorrect password",
            admin_method="sudo_password",
        )

    monkeypatch.setattr("features.container.service.run_admin_setup", fake_admin)
    with pytest.raises(ContainerError) as error:
        await service.setup_storage(password="wrong")
    assert error.value.code == "container_admin_auth_failed"
    assert error.value.details["exit_code"] == 1


@pytest.mark.asyncio
async def test_service_setup_b_mode_is_noop(tmp_path):
    storage = _make_storage(tmp_path)
    service = ContainerService(
        ContainerFeatureConfig(enabled=True, storage_mode="volume_soft"),
        storage.paths,
        docker=SimpleNamespace(available=lambda: False),
        platform_name="Linux",
    )
    result = await service.setup_storage(password=None)
    assert result["not_required"] is True


def test_windows_launcher_rejects_untrusted_script_path(tmp_path):
    storage = _make_storage(tmp_path, platform_name="Windows", suffix=".ps1")
    outside = tmp_path / "outside.ps1"
    outside.write_text("bad", encoding="utf-8")
    original = storage.setup_argv

    def tampered():
        argv = original()
        return argv[:4] + [str(outside)] + argv[5:]

    storage.setup_argv = tampered  # type: ignore[method-assign]
    with pytest.raises(ContainerError) as error:
        windows_setup_argv(storage, environ={"KAREN_POWERSHELL_BIN": sys.executable})
    assert error.value.code == "container_admin_script_invalid"
