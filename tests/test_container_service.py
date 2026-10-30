"""ContainerService security args, lifecycle, paths and bounded exec."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app_config.paths import AppPaths
from app_config.schema import ContainerFeatureConfig
from features.container import ContainerError, ContainerService
from features.container.docker_cli import CommandResult


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        project_root=tmp_path / "project",
        data_root=tmp_path / "data",
    )


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str = "") -> CommandResult:
    return CommandResult(returncode=1, stdout="", stderr=stderr)


class FakeDocker:
    def __init__(self, *, running_after_start: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.running_after_start = running_after_start
        self.started = False

    def available(self) -> bool:
        return True

    async def run(self, args, **kwargs):
        self.calls.append(list(args))
        if args and args[0] == "version":
            return _ok("29.0.0")
        if args[:2] == ["image", "inspect"]:
            return _ok("sha256:image")
        if args[:2] == ["volume", "inspect"]:
            return _fail("No such volume")
        if args[:2] == ["volume", "create"]:
            return _ok("volume")
        if args[:2] == ["inspect", "--type"]:
            if self.started and self.running_after_start:
                return _ok("running")
            return _fail("No such object")
        if args and args[0] == "run":
            return _ok()
        if args and args[0] == "create":
            return _ok("container-id")
        if args and args[0] == "start":
            self.started = True
            return _ok()
        if args and args[0] == "exec":
            if "du" in args:
                return _ok("1024\t/workspace\n")
            return _ok("ok")
        return _ok()


@pytest.mark.asyncio
async def test_ensure_running_builds_hardened_container_args(tmp_path):
    docker = FakeDocker()
    service = ContainerService(
        ContainerFeatureConfig(
            enabled=True,
            storage_mode="volume_soft",
            workspace_size_mb=2048,
        ),
        _paths(tmp_path),
        docker=docker,
        platform_name="Linux",
    )

    await service.ensure_running()

    create = next(call for call in docker.calls if call and call[0] == "create")
    joined = " ".join(create)
    assert "--network none" in joined
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--read-only" in joined
    assert "--user 1000:1000" in joined
    assert "--pids-limit 256" in joined
    assert "--memory 512m" in joined
    assert "--memory-swap 512m" in joined
    assert "type=volume,source=karen-container-workspace,target=/workspace" in joined
    assert "docker.sock" not in joined
    assert "--privileged" not in joined
    init = next(
        call for call in docker.calls if call and call[0] == "run"
    )
    assert "--cap-drop ALL" in " ".join(init)
    assert "chown -R 1000:1000 /workspace" in " ".join(init)


@pytest.mark.asyncio
async def test_prepare_checks_image_without_starting_container(tmp_path):
    docker = FakeDocker()
    service = ContainerService(
        ContainerFeatureConfig(enabled=True, storage_mode="volume_soft"),
        _paths(tmp_path),
        docker=docker,
        platform_name="Linux",
    )

    result = await service.prepare()

    assert result["image_ready"] is True
    assert result["storage_ready"] is True
    assert not any(call and call[0] in {"create", "run", "start"} for call in docker.calls)


@pytest.mark.asyncio
async def test_prepare_pulls_missing_image_when_enabled(tmp_path):
    class PullingDocker(FakeDocker):
        def __init__(self) -> None:
            super().__init__()
            self.present = False

        async def run(self, args, **kwargs):
            self.calls.append(list(args))
            if args[:2] == ["image", "inspect"]:
                return _ok("sha256:image") if self.present else _fail("No such image")
            if args and args[0] == "pull":
                self.present = True
                return _ok("pulled")
            return await super().run(args, **kwargs)

    docker = PullingDocker()
    service = ContainerService(
        ContainerFeatureConfig(
            enabled=True, storage_mode="volume_soft", auto_pull_image=True
        ),
        _paths(tmp_path),
        docker=docker,
        platform_name="Linux",
    )
    result = await service.prepare()
    assert result["image_ready"] is True
    assert any(call and call[0] == "pull" for call in docker.calls)
    assert not any(call and call[0] in {"create", "run", "start"} for call in docker.calls)


@pytest.mark.asyncio
async def test_prepare_pulls_from_mirror_and_tags_original_name(tmp_path):
    class MirrorDocker(FakeDocker):
        def __init__(self) -> None:
            super().__init__()
            self.present = False

        async def run(self, args, **kwargs):
            self.calls.append(list(args))
            if args[:2] == ["image", "inspect"]:
                return _ok("sha256:image") if self.present else _fail("No such image")
            if args and args[0] == "pull":
                assert args[1] == "docker.m.daocloud.io/library/python:3.12-slim"
                self.present = True
                return _ok("pulled")
            if args and args[0] == "tag":
                assert args[1:] == [
                    "docker.m.daocloud.io/library/python:3.12-slim",
                    "python:3.12-slim",
                ]
                return _ok()
            return await super().run(args, **kwargs)

    service = ContainerService(
        ContainerFeatureConfig(
            enabled=True,
            storage_mode="volume_soft",
            auto_pull_image=True,
            image_mirror="docker.m.daocloud.io",
        ),
        _paths(tmp_path),
        docker=MirrorDocker(),
        platform_name="Linux",
    )
    result = await service.prepare()
    assert result["image_ready"] is True
    assert any(call[:1] == ["tag"] for call in service.docker.calls)


@pytest.mark.asyncio
async def test_prepare_reuses_already_pulled_mirror_image(tmp_path):
    class MirrorCachedDocker(FakeDocker):
        async def run(self, args, **kwargs):
            self.calls.append(list(args))
            if args[:2] == ["image", "inspect"]:
                return (
                    _fail("No such image")
                    if args[-1] == "python:3.12-slim"
                    else _ok("sha256:mirror")
                )
            if args and args[0] == "tag":
                return _ok()
            return await super().run(args, **kwargs)

    docker = MirrorCachedDocker()
    service = ContainerService(
        ContainerFeatureConfig(
            enabled=True,
            storage_mode="volume_soft",
            auto_pull_image=True,
            image_mirror="docker.m.daocloud.io",
        ),
        _paths(tmp_path),
        docker=docker,
        platform_name="Linux",
    )
    result = await service.prepare()
    assert result["image_ready"] is True
    assert any(call and call[0] == "tag" for call in docker.calls)
    assert not any(call and call[0] == "pull" for call in docker.calls)


@pytest.mark.asyncio
async def test_image_pull_failure_includes_sanitized_reason(tmp_path):
    class FailingDocker(FakeDocker):
        async def run(self, args, **kwargs):
            self.calls.append(list(args))
            if args[:2] == ["image", "inspect"]:
                return _fail("No such image")
            if args and args[0] == "pull":
                return _fail(
                    "https://user:secret@registry.example/v2/: TLS handshake timeout"
                )
            return await super().run(args, **kwargs)

    service = ContainerService(
        ContainerFeatureConfig(
            enabled=True, storage_mode="volume_soft", auto_pull_image=True
        ),
        _paths(tmp_path),
        docker=FailingDocker(),
        platform_name="Linux",
    )
    with pytest.raises(ContainerError) as error:
        await service.prepare()
    assert error.value.code == "container_image_pull_failed"
    assert "TLS handshake timeout" in str(error.value)
    assert "user:secret@" not in str(error.value)


@pytest.mark.asyncio
async def test_existing_container_with_lost_config_signature_must_be_reset(tmp_path):
    class ExistingDocker(FakeDocker):
        def __init__(self, label: str) -> None:
            super().__init__()
            self.label = label

        async def run(self, args, **kwargs):
            self.calls.append(list(args))
            if args[:2] == ["inspect", "--type"]:
                return _ok("running")
            if args and args[0] == "inspect" and "--format" in args:
                return _ok(self.label)
            return await super().run(args, **kwargs)

    cfg = ContainerFeatureConfig(enabled=True, storage_mode="volume_soft")
    service = ContainerService(cfg, _paths(tmp_path), docker=ExistingDocker("wrong"))
    with pytest.raises(ContainerError) as error:
        await service.ensure_running()
    assert error.value.code == "container_config_mismatch"
    assert not any(call and call[0] == "create" for call in service.docker.calls)

    docker = ExistingDocker(service._config_signature())
    service = ContainerService(cfg, _paths(tmp_path), docker=docker)
    await service.ensure_running()
    assert not any(call and call[0] == "create" for call in docker.calls)


@pytest.mark.asyncio
async def test_image_fixed_requires_ready_mount_before_create(tmp_path, monkeypatch):
    cfg = ContainerFeatureConfig(
        enabled=True,
        storage_mode="image_fixed",
        image_fixed_mountpoint=str(tmp_path / "workspace"),
        image_fixed_image_path=str(tmp_path / "workspace.ext4"),
    )
    mountpoint = tmp_path / "workspace"
    mountpoint.mkdir()
    (tmp_path / ".karen-container-storage.json").write_text(
        json.dumps({"mode": "image_fixed", "size_mb": 2048}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "features.container.storage.ContainerStorage._is_mount",
        lambda self: True,
    )
    docker = FakeDocker()
    service = ContainerService(cfg, _paths(tmp_path), docker=docker, platform_name="Linux")

    await service.ensure_running()

    create = next(call for call in docker.calls if call and call[0] == "create")
    assert (
        f"type=bind,source={mountpoint},target=/workspace"
        in " ".join(create)
    )
    assert not any(call[:2] == ["volume", "create"] for call in docker.calls)


@pytest.mark.asyncio
async def test_unready_image_fixed_never_silently_falls_back(tmp_path):
    cfg = ContainerFeatureConfig(
        enabled=True,
        storage_mode="image_fixed",
        image_fixed_mountpoint=str(tmp_path / "missing"),
        image_fixed_image_path=str(tmp_path / "workspace.ext4"),
    )
    docker = FakeDocker()
    service = ContainerService(cfg, _paths(tmp_path), docker=docker, platform_name="Linux")

    with pytest.raises(ContainerError) as error:
        await service.ensure_running()
    assert error.value.code in {
        "container_storage_not_ready",
        "container_storage_marker_invalid",
    }
    assert not any(call and call[0] == "create" for call in docker.calls)


@pytest.mark.asyncio
async def test_exec_returns_bounded_structured_result(tmp_path):
    docker = FakeDocker()
    service = ContainerService(
        ContainerFeatureConfig(enabled=True, storage_mode="volume_soft"),
        _paths(tmp_path),
        docker=docker,
        platform_name="Linux",
    )

    async def noop() -> None:
        return None

    service.ensure_running = noop  # type: ignore[method-assign]
    docker.run = lambda args, **kwargs: _async_result(  # type: ignore[method-assign]
        CommandResult(
            returncode=7,
            stdout="out",
            stderr="err",
            truncated=True,
            timed_out=False,
        )
    )

    result = await service.exec("echo x")
    assert result == {
        "ok": False,
        "exit_code": 7,
        "stdout": "out",
        "stderr": "err",
        "truncated": True,
        "timed_out": False,
    }


async def _async_result(result: CommandResult) -> CommandResult:
    return result


@pytest.mark.asyncio
async def test_container_paths_are_limited_to_workspace(tmp_path):
    service = ContainerService(
        ContainerFeatureConfig(enabled=True, storage_mode="volume_soft"),
        _paths(tmp_path),
        docker=FakeDocker(),
        platform_name="Linux",
    )
    with pytest.raises(ContainerError):
        await service.read_file("/etc/passwd")
    with pytest.raises(ContainerError):
        await service.read_file("/workspace/../etc/passwd")


@pytest.mark.asyncio
async def test_volume_soft_quota_blocks_before_write(tmp_path):
    docker = FakeDocker()
    service = ContainerService(
        ContainerFeatureConfig(
            enabled=True,
            storage_mode="volume_soft",
            workspace_size_mb=64,
        ),
        _paths(tmp_path),
        docker=docker,
        platform_name="Linux",
    )

    async def noop() -> None:
        return None

    service.ensure_running = noop  # type: ignore[method-assign]
    docker.run = lambda args, **kwargs: _async_result(  # type: ignore[method-assign]
        _ok(f"{64 * 1024 * 1024}\t/workspace\n")
    )

    with pytest.raises(ContainerError) as error:
        await service._check_before_write(1)
    assert error.value.code == "container_storage_full"
