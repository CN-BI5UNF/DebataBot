"""Container storage policies B/C.

B (``volume_soft``) is a Docker named volume plus application-level quota
checks. C (``image_fixed``) is a fixed-size filesystem image mounted by a
one-time administrator script; this module only verifies the mount marker and
never calls sudo or creates/mounts filesystems itself.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app_config.paths import AppPaths

from .errors import ContainerError

_MIB = 1024 * 1024
_MARKER_NAME = ".karen-container-storage.json"


@dataclass(frozen=True, slots=True)
class StorageStatus:
    mode: str
    ready: bool
    size_mb: int
    mountpoint: str
    image_path: str
    mounted: bool
    free_bytes: int | None
    error: str = ""
    code: str = ""
    marker_size_mb: int | None = None

    def as_dict(self, *, include_host_paths: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "mode": self.mode,
            "ready": self.ready,
            "size_mb": self.size_mb,
            "mounted": self.mounted,
            "free_bytes": self.free_bytes,
            "error": self.error,
            "code": self.code,
            "marker_size_mb": self.marker_size_mb,
        }
        if include_host_paths:
            data["mountpoint"] = self.mountpoint
            data["image_path"] = self.image_path
        return data


class ContainerStorage:
    def __init__(
        self,
        config: Any,
        paths: AppPaths,
        *,
        platform_name: str | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.platform_name = (platform_name or platform.system()).lower()

    @property
    def mode(self) -> str:
        return str(getattr(self.config, "storage_mode", "image_fixed"))

    @property
    def size_bytes(self) -> int:
        return int(getattr(self.config, "workspace_size_mb", 2048)) * _MIB

    @property
    def mountpoint(self) -> Path:
        configured = str(getattr(self.config, "image_fixed_mountpoint", "") or "").strip()
        return Path(configured) if configured else Path(self.paths.CONTAINER_WORKSPACE_DIR)

    @property
    def image_path(self) -> Path:
        configured = str(
            getattr(self.config, "image_fixed_image_path", "") or ""
        ).strip()
        if configured:
            return Path(configured)
        if self.platform_name.startswith("win"):
            return Path(self.paths.CONTAINER_VHDX_PATH)
        return Path(self.paths.CONTAINER_IMAGE_PATH)

    @property
    def marker_path(self) -> Path:
        # Keep the marker outside the container-visible workspace so the model
        # cannot read or delete the host-side readiness proof.
        return self.image_path.parent / _MARKER_NAME

    def _is_mount(self) -> bool:
        try:
            if not self.mountpoint.exists():
                return False
            if self.platform_name.startswith("win"):
                # A VHDX is normally mounted as K:\; the configured workspace
                # is K:\workspace, so test the drive anchor as well as the
                # directory itself.
                anchor = Path(self.mountpoint).anchor
                return bool(
                    os.path.ismount(self.mountpoint)
                    or (anchor and os.path.ismount(anchor))
                )
            return bool(os.path.ismount(self.mountpoint))
        except OSError:
            return False

    def status(self, *, include_host_paths: bool = True) -> StorageStatus:
        if self.mode == "volume_soft":
            return StorageStatus(
                mode=self.mode,
                ready=True,
                size_mb=int(getattr(self.config, "workspace_size_mb", 2048)),
                mountpoint="",
                image_path="",
                mounted=False,
                free_bytes=None,
            )
        mountpoint = self.mountpoint
        image_path = self.image_path
        if not mountpoint.exists() or not mountpoint.is_dir():
            return StorageStatus(
                mode=self.mode,
                ready=False,
                size_mb=int(getattr(self.config, "workspace_size_mb", 2048)),
                mountpoint=str(mountpoint),
                image_path=str(image_path),
                mounted=False,
                free_bytes=None,
                error="固定大小存储挂载点不存在；请先执行 container storage setup",
                code="container_storage_not_ready",
            )
        marker_error = ""
        marker_code = ""
        marker: dict[str, Any] = {}
        marker_size_mb: int | None = None
        try:
            marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
            if not isinstance(marker, dict):
                raise ValueError
        except (OSError, ValueError, json.JSONDecodeError):
            marker_error = "固定大小存储缺少有效标记；请重建或修复挂载"
            marker_code = "container_storage_marker_invalid"
        if marker and marker.get("mode") != "image_fixed":
            marker_error = "存储标记模式与配置不一致"
            marker_code = "container_storage_marker_invalid"
        try:
            marker_size = int(marker.get("size_mb") or 0) if marker else 0
        except (TypeError, ValueError):
            marker_size = -1
        if marker and marker_size >= 0:
            marker_size_mb = marker_size
        configured_size = int(getattr(self.config, "workspace_size_mb", 2048))
        if marker and marker_size != configured_size:
            marker_error = (
                f"存储镜像实际为 {marker_size} MiB，当前配置为 "
                f"{configured_size} MiB；请采用实际大小，或新建镜像并迁移"
            )
            marker_code = "container_storage_size_mismatch"
        mounted = self._is_mount()
        if not mounted and not marker_error:
            marker_error = "固定大小存储未挂载；请执行 setup 或检查挂载状态"
            marker_code = "container_storage_not_mounted"
        free_bytes: int | None = None
        try:
            free_bytes = int(shutil.disk_usage(mountpoint).free)
        except OSError:
            free_bytes = None
        ready = mounted and not marker_error
        return StorageStatus(
            mode=self.mode,
            ready=ready,
            size_mb=int(getattr(self.config, "workspace_size_mb", 2048)),
            mountpoint=str(mountpoint),
            image_path=str(image_path),
            mounted=mounted,
            free_bytes=free_bytes,
            error=marker_error,
            code=marker_code,
            marker_size_mb=marker_size_mb,
        )

    def ensure_ready(self) -> StorageStatus:
        status = self.status(include_host_paths=False)
        if not status.ready:
            raise ContainerError(status.error, code=status.code or "container_storage_not_ready")
        return status

    def docker_mount_args(self) -> list[str]:
        if self.mode == "volume_soft":
            volume = str(getattr(self.config, "volume_name", "karen-container-workspace"))
            return ["--mount", f"type=volume,source={volume},target=/workspace"]
        status = self.ensure_ready()
        source = status.mountpoint
        if any(ch in source for ch in (",", "\n", "\r", "\x00")):
            raise ContainerError(
                "固定大小存储路径包含 Docker --mount 不支持的字符",
                code="container_storage_path_invalid",
            )
        return ["--mount", f"type=bind,source={source},target=/workspace"]

    def setup_argv(self) -> list[str]:
        """Return the administrator command as argv, never a host shell string."""
        size = str(int(getattr(self.config, "workspace_size_mb", 2048)))
        if self.platform_name.startswith("win"):
            script = self.paths.PROJECT_ROOT / "scripts" / "container" / "karen-container-vhdx-setup.ps1"
            return [
                "powershell",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-SizeMB",
                size,
                "-VhdxPath",
                str(self.image_path),
                "-MountPoint",
                str(self.mountpoint),
            ]
        script = self.paths.PROJECT_ROOT / "scripts" / "container" / "karen-container-storage-setup.sh"
        return [
            "sudo",
            "bash",
            str(script),
            "--size-mb",
            size,
            "--image",
            str(self.image_path),
            "--mountpoint",
            str(self.mountpoint),
        ]

    def setup_commands(self) -> dict[str, Any]:
        if self.mode != "image_fixed":
            return {
                "mode": self.mode,
                "command": "",
                "argv": [],
                "script": "",
                "requires_admin": False,
                "size_mb": int(getattr(self.config, "workspace_size_mb", 2048)),
                "mountpoint": "",
                "image_path": "",
                "note": "当前为 B 命名卷软配额，不需要 C 固定大小镜像安装。",
            }
        size = int(getattr(self.config, "workspace_size_mb", 2048))
        argv = self.setup_argv()
        command = " ".join(
            f'"{part}"' if any(ch.isspace() for ch in part) else part
            for part in argv
        )
        script_index = 4 if self.platform_name.startswith("win") else 2
        return {
            "mode": self.mode,
            "command": command,
            "argv": argv,
            "script": argv[script_index],
            "requires_admin": True,
            "size_mb": size,
            "mountpoint": str(self.mountpoint),
            "image_path": str(self.image_path),
            "note": "C 模式需要一次性管理员挂载；仅在用户确认后提权，不会静默降级到 B。",
        }

    def check_free_space(self, incoming_bytes: int) -> None:
        """C 模式写入前检查宿主挂载剩余空间，避免明显 ENOSPC。"""
        if incoming_bytes < 0:
            raise ContainerError("写入大小无效", code="container_invalid_argument")
        status = self.ensure_ready()
        if status.free_bytes is not None and incoming_bytes > status.free_bytes:
            raise ContainerError(
                "容器存储剩余空间不足", code="container_storage_full"
            )


__all__ = ["ContainerStorage", "StorageStatus"]
