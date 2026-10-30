"""Built-in Docker container service.

Security defaults are fixed in this module, not delegated to the model:
- no privileged mode, no Docker socket, no host home/root mount;
- cap-drop ALL, no-new-privileges, non-root runtime user, read-only rootfs;
- network none by default, bounded memory/CPU/pids/file size/output/time;
- only /workspace is writable and persistent.

Storage B is a Docker named volume with soft quota checks. Storage C is a
fixed-size filesystem mounted by an administrator script. The service only
runs that setup after the user supplies a Linux sudo password or confirms the
Windows UAC prompt; ordinary container operations never elevate, and C never
falls back silently when it is not ready.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import platform
import posixpath
import re
import time
from pathlib import Path
from typing import Any

from app_config.paths import AppPaths

from .admin import run_admin_setup
from .docker_cli import CommandResult, DockerCLI
from .errors import ContainerError
from .storage import ContainerStorage

logger = logging.getLogger(__name__)

_MIB = 1024 * 1024
_CLIENT_TIMEOUT = 30.0
_INIT_TIMEOUT = 60.0
_MAX_COMMAND_BYTES = 64 * 1024
_MAX_TOOL_WRITE_BYTES = 8 * 1024 * 1024
_CONTAINER_WORKDIR = "/workspace"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CREDENTIAL_URL_RE = re.compile(
    r"(https?://)[^/\s@]+@", re.IGNORECASE
)


def _safe_output_tail(text: str, limit: int = 600) -> str:
    """Docker pull output is external; keep a short sanitized tail for errors."""
    cleaned = _ANSI_RE.sub("", str(text or ""))
    cleaned = _CREDENTIAL_URL_RE.sub(r"\1***@", cleaned)
    return cleaned.strip()[-limit:]


_RUNTIME_UID = 1000
_RUNTIME_GID = 1000


def _validate_container_path(path: str, *, allow_root: bool = False) -> str:
    text = str(path or "").strip()
    if not text or not text.startswith("/"):
        raise ContainerError(
            "容器路径必须是 /workspace 下的绝对路径",
            code="container_path_invalid",
        )
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ContainerError("容器路径包含控制字符", code="container_path_invalid")
    if ".." in text.split("/"):
        raise ContainerError("容器路径不允许 ..", code="container_path_invalid")
    normalized = posixpath.normpath(text)
    if normalized == "/workspace":
        if allow_root:
            return normalized
        raise ContainerError("该操作需要具体文件路径", code="container_path_invalid")
    if not normalized.startswith("/workspace/"):
        raise ContainerError(
            "容器路径必须位于 /workspace", code="container_path_forbidden"
        )
    return normalized


def _resolve_host_workspace_path(workspace_dir: Path | None, raw: str) -> Path:
    if workspace_dir is None:
        raise ContainerError(
            "未配置 workspace，不能导入/导出文件",
            code="container_workspace_missing",
        )
    root = Path(workspace_dir).resolve()
    value = str(raw or "").strip()
    if not value:
        raise ContainerError("宿主路径不能为空", code="container_path_invalid")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    try:
        target = path.resolve()
    except (OSError, RuntimeError):
        raise ContainerError("宿主路径无效", code="container_path_invalid") from None
    if not target.is_relative_to(root):
        raise ContainerError(
            "宿主路径必须位于 AI workspace", code="container_path_forbidden"
        )
    return target


def _map_failure(result: CommandResult, *, default: str = "container_error") -> ContainerError:
    text = f"{result.stdout}\n{result.stderr}".lower()
    if "cannot connect to the docker daemon" in text or "is the docker daemon running" in text:
        return ContainerError(
            "Docker daemon 未运行或不可连接（Docker Desktop 是否已启动？）",
            code="container_docker_unavailable",
        )
    if "permission denied" in text:
        return ContainerError(
            "Docker 权限不足；请检查当前用户是否在 docker 组或 Docker Desktop 授权",
            code="container_permission_denied",
        )
    if "no such image" in text or "pull access denied" in text:
        return ContainerError(
            "容器镜像不存在；请先 docker pull 或在设置中开启自动拉取",
            code="container_image_missing",
        )
    if "no such container" in text or "no such object" in text:
        return ContainerError("容器不存在", code="container_missing")
    if "no space left on device" in text:
        return ContainerError("容器存储空间已满", code="container_storage_full")
    if "timed out" in text:
        return ContainerError("Docker 命令超时", code="container_timeout")
    return ContainerError("Docker 命令执行失败", code=default)


class ContainerService:
    def __init__(
        self,
        config: Any,
        paths: AppPaths,
        *,
        docker: DockerCLI | None = None,
        platform_name: str | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.platform_name = platform_name or platform.system()
        self.docker = docker or DockerCLI()
        self.storage = ContainerStorage(
            config, paths, platform_name=self.platform_name
        )
        self._lifecycle_lock = asyncio.Lock()
        self._image_lock = asyncio.Lock()
        self._setup_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._idle_task: asyncio.Task[None] | None = None
        self._last_used = time.monotonic()
        self._closed = False

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "enabled", False))

    @property
    def name(self) -> str:
        return str(getattr(self.config, "container_name", "karen-container"))

    @property
    def image(self) -> str:
        return str(getattr(self.config, "image", "python:3.12-slim"))

    @property
    def max_output_bytes(self) -> int:
        return int(getattr(self.config, "max_output_bytes", 256 * 1024))

    @property
    def image_mirror(self) -> str:
        raw = str(getattr(self.config, "image_mirror", "") or "").strip()
        if not raw:
            return ""
        if "://" in raw:
            raw = raw.split("://", 1)[1]
        return raw.rstrip("/")

    def _mirror_pull_image(self) -> str:
        """Return the mirrored pull name for Docker Hub images, else the original.

        The local tag stays exactly ``self.image`` so create/config signatures and
        existing containers are unaffected by the mirror.
        """
        mirror = self.image_mirror
        if not mirror:
            return self.image
        first = self.image.split("/", 1)[0]
        if "/" in self.image and ("." in first or ":" in first):
            return self.image  # non-Docker-Hub registry
        if "/" not in self.image:
            return f"{mirror}/library/{self.image}"
        return f"{mirror}/{self.image}"

    async def _run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        timeout: float | None = None,
        max_output_bytes: int | None = None,
        raise_on_timeout: bool = True,
        terminate_on_truncate: bool = True,
    ) -> CommandResult:
        try:
            result = await self.docker.run(
                args,
                input_bytes=input_bytes,
                timeout=timeout if timeout is not None else _CLIENT_TIMEOUT,
                max_output_bytes=max_output_bytes or self.max_output_bytes,
                terminate_on_truncate=terminate_on_truncate,
            )
        except FileNotFoundError:
            raise ContainerError(
                "未找到 Docker CLI；请安装 Docker Desktop / Docker Engine",
                code="container_docker_missing",
            ) from None
        except OSError:
            raise ContainerError(
                "无法启动 Docker CLI", code="container_docker_missing"
            ) from None
        if result.timed_out and raise_on_timeout:
            raise ContainerError("Docker 命令超时", code="container_timeout")
        return result

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise ContainerError("容器功能未启用", code="container_disabled")

    async def _server_version(self) -> str:
        result = await self._run(
            ["version", "--format", "{{.Server.Version}}"], timeout=8.0
        )
        if result.returncode != 0:
            raise _map_failure(result, default="container_docker_unavailable")
        return result.stdout.strip()

    async def _image_present(self, image: str | None = None) -> bool:
        target = image or self.image
        result = await self._run(
            ["image", "inspect", "--format", "{{.Id}}", target], timeout=15.0
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    async def _tag_image(self, source: str) -> None:
        if source == self.image:
            return
        tagged = await self._run(["tag", source, self.image], timeout=30.0)
        if tagged.returncode != 0:
            raise _map_failure(tagged, default="container_image_tag_failed")
        logger.info("容器镜像已标记：%s -> %s", source, self.image)

    async def _container_state(self) -> str | None:
        result = await self._run(
            [
                "inspect",
                "--type",
                "container",
                "--format",
                "{{.State.Status}}",
                self.name,
            ],
            timeout=10.0,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    async def _volume_present(self) -> bool:
        result = await self._run(
            ["volume", "inspect", str(getattr(self.config, "volume_name", ""))],
            timeout=10.0,
        )
        return result.returncode == 0

    async def probe(self, *, include_docker: bool = True) -> dict[str, Any]:
        """Return a bounded status object; never raises for ordinary probe errors."""
        data: dict[str, Any] = {
            "enabled": self.enabled,
            "platform": self.platform_name,
            "image": self.image,
            "container_name": self.name,
            "limits": {
                "memory_mb": int(getattr(self.config, "memory_mb", 512)),
                "cpus": float(getattr(self.config, "cpus", 1.0)),
                "pids_limit": int(getattr(self.config, "pids_limit", 256)),
                "file_size_mb": int(getattr(self.config, "file_size_mb", 64)),
                "exec_timeout_seconds": int(
                    getattr(self.config, "exec_timeout_seconds", 120)
                ),
                "network": str(getattr(self.config, "network", "none")),
            },
            "storage": self.storage.status(include_host_paths=True).as_dict(),
        }
        if not include_docker:
            return data
        docker: dict[str, Any] = {
            "available": False,
            "server_version": "",
            "error": "",
            "code": "",
        }
        if not self.docker.available():
            docker.update(
                error="未找到 Docker CLI",
                code="container_docker_missing",
            )
        else:
            try:
                docker["server_version"] = await self._server_version()
                docker["available"] = True
            except ContainerError as exc:
                docker.update(error=exc.safe_message(), code=exc.code)
            except Exception:  # noqa: BLE001
                docker.update(
                    error="Docker 探测失败", code="container_docker_unavailable"
                )
        data["docker"] = docker
        if docker["available"]:
            try:
                data["image_present"] = await self._image_present()
            except ContainerError:
                data["image_present"] = False
            try:
                state = await self._container_state()
                data["container"] = {
                    "name": self.name,
                    "exists": state is not None,
                    "state": state or "absent",
                }
            except ContainerError:
                data["container"] = {
                    "name": self.name,
                    "exists": False,
                    "state": "unknown",
                }
        else:
            data["image_present"] = False
            data["container"] = {
                "name": self.name,
                "exists": False,
                "state": "unknown",
            }
        storage_data = data.get("storage") if isinstance(data.get("storage"), dict) else {}
        data["ready"] = bool(
            docker.get("available")
            and data.get("image_present")
            and storage_data.get("ready")
        )
        return data

    async def status(self, *, public: bool = False) -> dict[str, Any]:
        data = await self.probe(include_docker=True)
        storage = data.get("storage")
        if isinstance(storage, dict) and public:
            storage.pop("mountpoint", None)
            storage.pop("image_path", None)
        if public:
            data.pop("platform", None)
        return data

    async def prepare(self) -> dict[str, Any]:
        """Warm the image at startup without starting a container.

        A missing image is pulled when ``auto_pull_image`` is enabled. C
        storage is only reported, not mounted here; mounting remains an
        explicit administrator action.
        """
        self._require_enabled()
        await self._server_version()
        await self._ensure_image()
        status = self.storage.status(include_host_paths=False)
        return {
            "image": self.image,
            "image_ready": True,
            "storage_ready": status.ready,
            "storage_code": status.code,
            "storage_error": status.error,
        }

    async def setup_storage(
        self, *, password: str | None = None
    ) -> dict[str, Any]:
        """Run the one-time C storage administrator setup.

        Linux uses ``sudo -S`` with the password supplied on stdin; Windows
        uses a UAC prompt and requires no password. Disabled containers may
        still initialize storage so the user can complete setup before the
        restart that enables the feature.
        """
        async with self._setup_lock:
            before = self.storage.status(include_host_paths=False)
            if before.mode != "image_fixed":
                return {
                    "ok": True,
                    "already_ready": True,
                    "not_required": True,
                    "admin_method": "none",
                    "storage": self.storage.status(
                        include_host_paths=True
                    ).as_dict(include_host_paths=True),
                }
            if before.code == "container_storage_size_mismatch":
                raise ContainerError(
                    before.error or "固定大小存储镜像大小与当前配置不一致",
                    code=before.code,
                    details={
                        "marker_size_mb": before.marker_size_mb,
                        "size_mb": before.size_mb,
                    },
                )
            if before.ready:
                return {
                    "ok": True,
                    "already_ready": True,
                    "admin_method": "none",
                    "storage": self.storage.status(
                        include_host_paths=True
                    ).as_dict(include_host_paths=True),
                }
            result = await run_admin_setup(
                self.storage,
                password,
                platform_name=self.platform_name,
            )
            if not result.ok:
                raise ContainerError(
                    result.message or "C 存储初始化失败",
                    code=result.code or "container_admin_setup_failed",
                    details={
                        "exit_code": result.exit_code,
                        "output_tail": result.output_tail,
                        "timed_out": result.timed_out,
                        "admin_method": result.admin_method,
                        "storage": result.storage,
                    },
                )
            return {
                "ok": True,
                "already_ready": False,
                "admin_method": result.admin_method,
                "exit_code": result.exit_code,
                "output_tail": result.output_tail,
                "storage": result.storage,
            }

    async def _ensure_image(self) -> None:
        # Startup prepare and a user-triggered ensure_running may race. Serialize
        # image inspection/pull/tag so the same large image is never downloaded
        # twice concurrently.
        async with self._image_lock:
            await self._ensure_image_locked()

    async def _ensure_image_locked(self) -> None:
        if await self._image_present():
            return
        pull_image = self._mirror_pull_image()
        # A previous mirror pull may have completed but failed before the local
        # tag step (network reset, client restart). Reuse it instead of pulling
        # the same layers again.
        if pull_image != self.image and await self._image_present(pull_image):
            await self._tag_image(pull_image)
            return
        if bool(getattr(self.config, "auto_pull_image", False)):
            result = await self._run(
                ["pull", pull_image],
                timeout=600.0,
                max_output_bytes=64 * 1024,
                terminate_on_truncate=False,
            )
            if result.returncode != 0:
                detail = _safe_output_tail(
                    f"{result.stderr}\n{result.stdout}"
                )
                logger.warning(
                    "容器镜像拉取失败 image=%s pull=%s exit=%s output=%s",
                    self.image,
                    pull_image,
                    result.returncode,
                    detail or "无输出",
                )
                message = (
                    f"容器镜像 {self.image} 拉取失败（pull={pull_image}，"
                    f"exit={result.returncode}）"
                )
                if result.timed_out:
                    message += "：Docker 命令超时"
                if "i/o timeout" in detail or "dial tcp" in detail:
                    message += "：Docker 镜像源不可达，可在设置→容器填写镜像加速前缀"
                if detail:
                    message += f"：{detail}"
                raise ContainerError(
                    message,
                    code="container_image_pull_failed",
                )
            await self._tag_image(pull_image)
            return
        raise ContainerError(
            f"容器镜像 {self.image} 不存在；请先 docker pull，"
            "或开启 features.container.auto_pull_image",
            code="container_image_missing",
        )

    async def _ensure_volume(self) -> None:
        if await self._volume_present():
            return
        result = await self._run(
            [
                "volume",
                "create",
                "--label",
                "karen.container=true",
                str(getattr(self.config, "volume_name", "karen-container-workspace")),
            ],
            timeout=20.0,
        )
        if result.returncode != 0:
            raise _map_failure(result, default="container_volume_failed")

    async def _init_workspace(self, mount_args: list[str]) -> None:
        result = await self._run(
            [
                "run",
                "--rm",
                "--network",
                "none",
                "--memory",
                "256m",
                "--memory-swap",
                "256m",
                "--pids-limit",
                "32",
                "--cpus",
                "1",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=16m",
                "--user",
                "0:0",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "CHOWN",
                "--cap-add",
                "DAC_OVERRIDE",
                "--security-opt",
                "no-new-privileges",
                "--entrypoint",
                "/bin/sh",
                *mount_args,
                self.image,
                "-c",
                f"chown -R {_RUNTIME_UID}:{_RUNTIME_GID} /workspace",
            ],
            timeout=_INIT_TIMEOUT,
            max_output_bytes=64 * 1024,
        )
        if result.returncode != 0:
            raise _map_failure(result, default="container_workspace_init_failed")

    def _config_signature(self) -> str:
        payload = {
            "image": self.image,
            "storage_mode": self.storage.mode,
            "volume_name": str(getattr(self.config, "volume_name", "")),
            "mountpoint": (
                str(self.storage.mountpoint)
                if self.storage.mode == "image_fixed"
                else ""
            ),
            "image_path": (
                str(self.storage.image_path)
                if self.storage.mode == "image_fixed"
                else ""
            ),
            "memory_mb": int(getattr(self.config, "memory_mb", 512)),
            "cpus": float(getattr(self.config, "cpus", 1.0)),
            "pids_limit": int(getattr(self.config, "pids_limit", 256)),
            "file_size_mb": int(getattr(self.config, "file_size_mb", 64)),
            "network": str(getattr(self.config, "network", "none")),
            "user": f"{_RUNTIME_UID}:{_RUNTIME_GID}",
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

    async def _assert_existing_config(self) -> None:
        result = await self._run(
            [
                "inspect",
                "--format",
                '{{ index .Config.Labels "karen.config" }}',
                self.name,
            ],
            timeout=10.0,
        )
        if result.returncode != 0:
            raise ContainerError(
                "无法验证已有容器配置；请执行 karen container reset 重建",
                code="container_config_unverified",
            )
        if result.stdout.strip() != self._config_signature():
            raise ContainerError(
                "容器配置已变化；请执行 karen container reset 重建（持久数据保留）",
                code="container_config_mismatch",
            )

    def _create_args(self, mount_args: list[str]) -> list[str]:
        memory = int(getattr(self.config, "memory_mb", 512))
        cpus = float(getattr(self.config, "cpus", 1.0))
        pids = int(getattr(self.config, "pids_limit", 256))
        file_size = int(getattr(self.config, "file_size_mb", 64)) * _MIB
        network = str(getattr(self.config, "network", "none"))
        if network not in {"none", "bridge"}:
            raise ContainerError("容器网络配置无效", code="container_config_invalid")
        return [
            "create",
            "--name",
            self.name,
            "--label",
            "karen.managed=true",
            "--label",
            f"karen.config={self._config_signature()}",
            "--network",
            network,
            "--memory",
            f"{memory}m",
            "--memory-swap",
            f"{memory}m",
            "--cpus",
            f"{cpus:g}",
            "--pids-limit",
            str(pids),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--ulimit",
            "nofile=1024:2048",
            "--ulimit",
            f"fsize={file_size}:{file_size}",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--env",
            "HOME=/tmp",
            "--env",
            "TMPDIR=/tmp",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--user",
            f"{_RUNTIME_UID}:{_RUNTIME_GID}",
            "--workdir",
            _CONTAINER_WORKDIR,
            "--stop-timeout",
            "5",
            "--init",
            *mount_args,
            "--entrypoint",
            "/bin/sh",
            self.image,
            "-c",
            "trap 'exit 0' TERM INT; while :; do sleep 3600; done",
        ]

    async def _wait_running(self) -> None:
        for _ in range(30):
            state = await self._container_state()
            if state == "running":
                return
            await asyncio.sleep(0.2)
        raise ContainerError(
            "容器启动后未进入 running 状态", code="container_start_failed"
        )

    async def ensure_running(self) -> None:
        self._require_enabled()
        async with self._lifecycle_lock:
            await self._server_version()
            await self._ensure_image()
            storage_status = self.storage.ensure_ready()
            state = await self._container_state()
            if state == "running":
                await self._assert_existing_config()
                self._touch()
                return
            if state is not None:
                await self._assert_existing_config()
                result = await self._run(["start", self.name], timeout=30.0)
                if result.returncode != 0:
                    raise _map_failure(result, default="container_start_failed")
                await self._wait_running()
                self._touch()
                return

            if storage_status.mode == "volume_soft":
                await self._ensure_volume()
            mount_args = self.storage.docker_mount_args()
            if storage_status.mode == "volume_soft":
                # C setup already chowns its dedicated mount; avoid a recursive
                # chown over potentially large existing data on every create.
                await self._init_workspace(mount_args)
            created = await self._run(
                self._create_args(mount_args),
                timeout=30.0,
                max_output_bytes=64 * 1024,
            )
            if created.returncode != 0:
                raise _map_failure(created, default="container_create_failed")
            started = await self._run(["start", self.name], timeout=30.0)
            if started.returncode != 0:
                raise _map_failure(started, default="container_start_failed")
            await self._wait_running()
            self._touch()

    async def exec(
        self,
        command: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        await self.ensure_running()
        text = str(command or "")
        if not text.strip():
            raise ContainerError("命令不能为空", code="container_invalid_argument")
        if len(text.encode("utf-8")) > _MAX_COMMAND_BYTES:
            raise ContainerError("命令过长", code="container_invalid_argument")
        timeout = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else float(getattr(self.config, "exec_timeout_seconds", 120))
        )
        timeout = max(1.0, min(timeout, 1800.0))
        result = await self._run(
            ["exec", "-i", "--workdir", _CONTAINER_WORKDIR, self.name, "/bin/sh", "-lc", text],
            timeout=timeout,
            max_output_bytes=self.max_output_bytes,
            raise_on_timeout=False,
        )
        self._touch()
        return {
            "ok": result.returncode == 0 and not result.timed_out,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "truncated": result.truncated,
            "timed_out": result.timed_out,
        }

    async def read_file(self, path: str) -> dict[str, Any]:
        target = _validate_container_path(path)
        await self.ensure_running()
        result = await self._run(
            ["exec", self.name, "cat", "--", target],
            timeout=30.0,
            max_output_bytes=self.max_output_bytes,
            raise_on_timeout=False,
        )
        self._touch()
        if result.timed_out:
            raise ContainerError("读取文件超时", code="container_timeout")
        if result.returncode != 0:
            raise ContainerError("读取容器文件失败", code="container_file_read_failed")
        if result.truncated:
            raise ContainerError(
                "文件超过读取上限，请使用 exec 分段处理",
                code="container_file_too_large",
            )
        return {"ok": True, "path": target, "content": result.stdout}

    async def _soft_usage_bytes(self) -> int | None:
        result = await self._run(
            ["exec", self.name, "du", "-sb", _CONTAINER_WORKDIR],
            timeout=20.0,
            max_output_bytes=16 * 1024,
            raise_on_timeout=False,
        )
        if result.returncode != 0 or result.timed_out:
            return None
        first = result.stdout.strip().split(None, 1)[0] if result.stdout.strip() else ""
        try:
            return max(0, int(first))
        except ValueError:
            return None

    async def _quota_bytes(self) -> int:
        return int(getattr(self.config, "workspace_size_mb", 2048)) * _MIB

    async def _check_before_write(
        self, incoming: int, *, tool_content: bool = True
    ) -> None:
        if tool_content and incoming > _MAX_TOOL_WRITE_BYTES:
            raise ContainerError(
                "单次写入超过 8 MiB 工具上限", code="container_file_too_large"
            )
        file_limit = int(getattr(self.config, "file_size_mb", 64)) * _MIB
        if incoming > file_limit:
            raise ContainerError(
                "文件超过单文件上限", code="container_file_too_large"
            )
        if self.storage.mode == "image_fixed":
            self.storage.check_free_space(incoming)
            return
        usage = await self._soft_usage_bytes()
        if usage is not None and usage + incoming > await self._quota_bytes():
            raise ContainerError(
                "容器工作区软配额不足", code="container_storage_full"
            )

    async def _check_after_write(self) -> None:
        if self.storage.mode != "volume_soft":
            return
        usage = await self._soft_usage_bytes()
        if usage is not None and usage > await self._quota_bytes():
            raise ContainerError(
                "容器工作区超过软配额，后续写入已阻止",
                code="container_storage_full",
            )

    async def write_file(self, path: str, content: str) -> dict[str, Any]:
        target = _validate_container_path(path)
        data = str(content if content is not None else "").encode("utf-8")
        await self.ensure_running()
        async with self._write_lock:
            await self._check_before_write(len(data))
            result = await self._run(
                [
                    "exec",
                    "-i",
                    self.name,
                    "/bin/sh",
                    "-c",
                    'cat > "$1"',
                    "_",
                    target,
                ],
                input_bytes=data,
                timeout=30.0,
                max_output_bytes=16 * 1024,
            )
            if result.returncode != 0:
                raise _map_failure(result, default="container_file_write_failed")
            await self._check_after_write()
        self._touch()
        return {"ok": True, "path": target, "bytes": len(data)}

    async def edit_file(self, path: str, old: str, new: str) -> dict[str, Any]:
        if not str(old):
            raise ContainerError("old 不能为空", code="container_invalid_argument")
        current = await self.read_file(path)
        text = str(current["content"])
        if old not in text:
            raise ContainerError("未找到要替换的文本", code="container_edit_not_found")
        updated = text.replace(old, new, 1)
        result = await self.write_file(path, updated)
        result["replaced"] = 1
        return result

    async def list_files(self, path: str = _CONTAINER_WORKDIR) -> dict[str, Any]:
        target = _validate_container_path(path, allow_root=True)
        await self.ensure_running()
        result = await self._run(
            ["exec", self.name, "ls", "-la", "--", target],
            timeout=30.0,
            max_output_bytes=self.max_output_bytes,
            raise_on_timeout=False,
        )
        self._touch()
        if result.timed_out:
            raise ContainerError("列目录超时", code="container_timeout")
        if result.returncode != 0:
            raise ContainerError("列目录失败", code="container_list_failed")
        return {
            "ok": True,
            "path": target,
            "listing": result.stdout,
            "truncated": result.truncated,
        }

    async def import_file(
        self,
        host_path: str,
        container_path: str,
        *,
        workspace_dir: Path | None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        source = _resolve_host_workspace_path(workspace_dir, host_path)
        target = _validate_container_path(container_path)
        if not source.is_file():
            raise ContainerError("宿主来源不是普通文件", code="container_path_invalid")
        size = source.stat().st_size
        await self.ensure_running()
        async with self._write_lock:
            if not overwrite:
                exists = await self._run(
                    ["exec", self.name, "test", "-e", target],
                    timeout=15.0,
                )
                if exists.returncode == 0:
                    raise ContainerError(
                        "容器目标已存在；如需覆盖请设置 overwrite=true",
                        code="container_target_exists",
                    )
            await self._check_before_write(size, tool_content=False)
            result = await self._run(
                ["cp", str(source), f"{self.name}:{target}"],
                timeout=60.0,
                max_output_bytes=32 * 1024,
            )
            if result.returncode != 0:
                raise _map_failure(result, default="container_import_failed")
            await self._check_after_write()
        self._touch()
        return {"ok": True, "source": source.name, "path": target, "bytes": size}

    async def export_file(
        self,
        container_path: str,
        host_path: str,
        *,
        workspace_dir: Path | None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        source = _validate_container_path(container_path)
        target = _resolve_host_workspace_path(workspace_dir, host_path)
        await self.ensure_running()
        exists = await self._run(
            ["exec", self.name, "test", "-f", source],
            timeout=15.0,
        )
        if exists.returncode != 0:
            raise ContainerError(
                "容器内文件不存在或不是普通文件", code="container_path_invalid"
            )
        if target.exists() and not overwrite:
            raise ContainerError("宿主目标已存在", code="container_target_exists")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise ContainerError(
                "无法创建宿主目标目录", code="container_path_invalid"
            ) from None
        result = await self._run(
            ["cp", f"{self.name}:{source}", str(target)],
            timeout=60.0,
            max_output_bytes=32 * 1024,
        )
        if result.returncode != 0:
            raise _map_failure(result, default="container_export_failed")
        self._touch()
        return {"ok": True, "source": source, "target": target.name}

    async def reset(self) -> dict[str, Any]:
        self._require_enabled()
        async with self._lifecycle_lock:
            state = await self._container_state()
            if state is not None:
                result = await self._run(["rm", "-f", self.name], timeout=30.0)
                if result.returncode != 0:
                    raise _map_failure(result, default="container_reset_failed")
        self._touch()
        return {"ok": True, "reset": True, "volume_preserved": True}

    async def stop(self) -> None:
        try:
            state = await self._container_state()
            if state in {"running", "paused"}:
                await self._run(["stop", "--time", "5", self.name], timeout=20.0)
        except Exception:  # noqa: BLE001
            logger.debug("停止容器失败（忽略）", exc_info=True)

    def _touch(self) -> None:
        self._last_used = time.monotonic()

    async def _idle_loop(self) -> None:
        timeout_minutes = int(getattr(self.config, "idle_timeout_minutes", 30))
        if timeout_minutes <= 0:
            return
        interval = max(5.0, min(60.0, timeout_minutes * 30.0))
        while not self._closed:
            await asyncio.sleep(interval)
            if self._closed:
                return
            if time.monotonic() - self._last_used < timeout_minutes * 60:
                continue
            try:
                if await self._container_state() == "running":
                    await self.stop()
                    logger.info("容器空闲超时，已停止 %s", self.name)
            except Exception:  # noqa: BLE001
                logger.debug("空闲停止容器失败（忽略）", exc_info=True)

    def start(self) -> None:
        if self._idle_task is not None and not self._idle_task.done():
            return
        if not self.enabled:
            return
        if int(getattr(self.config, "idle_timeout_minutes", 30)) <= 0:
            return
        self._closed = False
        self._idle_task = asyncio.create_task(self._idle_loop())

    async def aclose(self) -> None:
        self._closed = True
        task = self._idle_task
        self._idle_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.enabled:
            with contextlib.suppress(Exception):
                await self.stop()


__all__ = ["ContainerService"]
