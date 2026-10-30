"""完整视频的入站资产存储与投影。

设计原则：
- 保存原始视频字节，不抽帧、不转码、不截断；
- 只接受文件头可识别的受支持容器，拒绝把任意文件当视频；
- 内容寻址 + 原子写入 + 0600 + 拒绝符号链接/路径穿越；
- 远程下载复用图片链路的公网 IP 固定、重定向与大小限制；
- 投影时重新校验大小/哈希/容器，不信任 history metadata。
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import os
import re
import secrets
import stat
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from utils.video_contracts import (
    SUPPORTED_VIDEO_MIMES,
    VIDEO_TRANSFORM_VERSION,
    VideoAssetError,
    VideoLimits,
    build_video_content_blocks,
    detect_video_mime,
    validate_video_asset_ref,
    video_asset_relative_path,
)

_MAX_LOCAL_READ_CHUNK = 64 * 1024


def _check_size(data: bytes, limit: int) -> bytes:
    if not data:
        raise VideoAssetError("视频内容为空", code="video_asset_corrupt")
    if len(data) > limit:
        raise VideoAssetError("视频超过字节限制", code="video_too_large")
    return data


def _data_url(data: bytes, mime: str) -> bytes:
    return f"data:{mime};base64,".encode("ascii") + base64.b64encode(data)


def _local_path(source: str | Path) -> Path:
    if isinstance(source, Path):
        return source
    if re.match(r"^[A-Za-z]:[\\/]", source):
        return Path(source)
    try:
        parsed = urlsplit(source)
    except ValueError:
        raise VideoAssetError("本地视频来源无效", code="video_unsafe_source") from None
    if parsed.scheme == "file":
        if parsed.netloc not in {"", "localhost"} or parsed.query or parsed.fragment:
            raise VideoAssetError("本地视频来源无效", code="video_unsafe_source")
        value = unquote(parsed.path)
        if re.match(r"^/[A-Za-z]:/", value):
            value = value[1:]
        return Path(value)
    if parsed.scheme:
        raise VideoAssetError("视频来源不受支持", code="video_unsafe_source")
    return Path(source)


def _read_local_video(path: Path, limit: int) -> bytes:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise VideoAssetError(
                    "本地视频不是普通文件", code="video_unsafe_source"
                )
            if info.st_size > limit:
                raise VideoAssetError("视频超过字节限制", code="video_too_large")
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining > 0:
                chunk = stream.read(min(_MAX_LOCAL_READ_CHUNK, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return _check_size(b"".join(chunks), limit)
    except VideoAssetError:
        raise
    except (OSError, ValueError):
        raise VideoAssetError(
            "本地视频缺失或无法读取", code="video_source_unavailable"
        ) from None


class VideoAssetStore:
    """Persist complete videos under the workspace and project wire blocks."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        limits: VideoLimits | None = None,
    ) -> None:
        self.limits = limits or VideoLimits()
        try:
            self.workspace = Path(workspace).resolve()
        except (OSError, RuntimeError):
            raise VideoAssetError(
                "视频workspace路径无效", code="video_unsafe_source"
            ) from None
        self._lock = threading.RLock()

    async def ingest(
        self,
        source: str | Path | bytes,
        *,
        message_id: str,
        index: int,
        trusted_local: bool = False,
    ) -> dict[str, Any]:
        """Ingest a complete video and return a validated asset ref."""
        data = await self._source_bytes(
            source,
            trusted_local=trusted_local,
            limit=self.limits.max_video_bytes,
        )
        mime = detect_video_mime(data)
        if mime not in SUPPORTED_VIDEO_MIMES:
            raise VideoAssetError(
                "视频格式不受支持，仅支持MP4、WebM、MOV和MPEG",
                code="video_unsupported_format",
            )
        sha256 = hashlib.sha256(data).hexdigest()
        relative = await asyncio.to_thread(self._persist, data, sha256, mime)
        ref = {
            "sha256": sha256,
            "mime": mime,
            "byte_size": len(data),
            "path": relative,
            "message_id": message_id,
            "index": index,
            "transform_version": VIDEO_TRANSFORM_VERSION,
        }
        return validate_video_asset_ref(ref, limits=self.limits)

    async def project_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Copy-on-write projection of trusted video metadata."""
        if record.get("role") != "user":
            return record
        metadata = record.get("metadata")
        if not isinstance(metadata, Mapping) or "video_assets" not in metadata:
            return record
        assets = metadata["video_assets"]
        if not isinstance(assets, (list, tuple)):
            raise VideoAssetError("视频资产列表无效")
        if not assets:
            return record
        content = record.get("content", "")
        if not isinstance(content, (str, list)):
            raise VideoAssetError(
                "视频历史消息正文格式无效", code="video_projection_failed"
            )
        refs = [validate_video_asset_ref(asset, limits=self.limits) for asset in assets]
        video_parts = [await self.video_content(ref) for ref in refs]
        projected = dict(record)
        projected["metadata"] = copy.deepcopy(dict(metadata))
        projected["content"] = build_video_content_blocks(
            content, refs, video_parts
        )
        return projected

    async def project_records(
        self,
        records: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [await self.project_record(record) for record in records]

    async def video_content(self, asset: Mapping[str, Any]) -> dict[str, Any]:
        """Create an independent OpenAI-compatible video_url content block."""
        ref = validate_video_asset_ref(asset, limits=self.limits)
        data = await asyncio.to_thread(self._read_asset, ref)
        encoded = await asyncio.to_thread(_data_url, data, ref["mime"])
        return {
            "type": "video_url",
            "video_url": {"url": encoded.decode("ascii")},
        }

    async def _source_bytes(
        self,
        source: str | Path | bytes,
        *,
        trusted_local: bool,
        limit: int,
    ) -> bytes:
        if isinstance(source, bytes):
            return _check_size(source, limit)
        if isinstance(source, Path):
            if not trusted_local:
                raise VideoAssetError(
                    "视频来源不受信任", code="video_unsafe_source"
                )
            return await asyncio.to_thread(_read_local_video, source, limit)
        if not isinstance(source, str):
            raise VideoAssetError("视频来源无效", code="video_unsafe_source")
        text = source.strip()
        if not text:
            raise VideoAssetError("视频来源为空", code="video_source_unavailable")
        try:
            parsed = urlsplit(text)
        except ValueError:
            raise VideoAssetError(
                "视频来源无效", code="video_unsafe_source"
            ) from None
        if parsed.scheme in {"http", "https"}:
            from features.vision.image_assets import (
                ImageAssetError,
                download_remote_media_bytes,
            )

            try:
                data = await download_remote_media_bytes(
                    text,
                    max_bytes=limit,
                    request_timeout=60.0,
                    total_timeout=180.0,
                )
            except ImageAssetError as exc:
                if exc.code == "image_too_large":
                    raise VideoAssetError(
                        "视频超过字节限制", code="video_too_large"
                    ) from None
                if exc.code == "image_unsafe_source":
                    raise VideoAssetError(
                        "视频地址不允许访问本机或内网",
                        code="video_unsafe_source",
                    ) from None
                raise VideoAssetError(
                    "视频下载失败或超时", code="video_source_unavailable"
                ) from None
            return _check_size(data, limit)
        if not trusted_local:
            raise VideoAssetError(
                "视频来源不是可下载地址", code="video_source_unavailable"
            )
        path = _local_path(text)
        return await asyncio.to_thread(_read_local_video, path, limit)

    def _relative_path(self, sha256: str, mime: str) -> str:
        return video_asset_relative_path(sha256, mime)

    def _checked_path(self, relative: str | Path) -> Path:
        try:
            relative = Path(relative)
            if relative.is_absolute() or ".." in relative.parts:
                raise VideoAssetError("视频资产路径越出workspace")
            path = self.workspace / relative
            if (
                self.workspace.resolve() != self.workspace
                or not path.resolve().is_relative_to(self.workspace)
            ):
                raise VideoAssetError("视频资产路径越出workspace")
            current = self.workspace
            for part in relative.parts:
                current /= part
                if current.is_symlink():
                    raise VideoAssetError("视频资产路径不允许符号链接")
            return path
        except VideoAssetError:
            raise
        except (OSError, ValueError, RuntimeError):
            raise VideoAssetError("视频资产路径无效") from None

    @contextmanager
    def _root_directory(self, *, create: bool) -> Iterator[tuple[Path, int | None]]:
        relative = Path("incoming/videos") / VIDEO_TRANSFORM_VERSION
        root = self._checked_path(relative)
        if create:
            try:
                self.workspace.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise VideoAssetError(
                    "视频workspace无法创建", code="video_source_unavailable"
                ) from None

        fd: int | None = None
        base_fd: int | None = None
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            try:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                base_fd = os.open(self.workspace, flags)
                fd = base_fd
                for component in relative.parts:
                    try:
                        child = os.open(component, flags, dir_fd=fd)
                    except FileNotFoundError:
                        if not create:
                            raise
                        os.mkdir(component, 0o700, dir_fd=fd)
                        child = os.open(component, flags, dir_fd=fd)
                    if fd != base_fd:
                        os.close(fd)
                    fd = child
            except FileNotFoundError:
                if create:
                    raise VideoAssetError(
                        "视频资产目录不可用", code="video_source_unavailable"
                    ) from None
                raise
            except OSError:
                raise VideoAssetError(
                    "视频资产目录不可用", code="video_source_unavailable"
                ) from None
            try:
                yield root, fd
            finally:
                if fd is not None and fd != base_fd:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                if base_fd is not None:
                    try:
                        os.close(base_fd)
                    except OSError:
                        pass
            return
        if create:
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise VideoAssetError(
                    "视频资产目录不可用", code="video_source_unavailable"
                ) from None
        if not root.exists():
            raise VideoAssetError(
                "视频资产目录不存在", code="video_source_unavailable"
            )
        yield root, None

    def _persist(self, data: bytes, sha256: str, mime: str) -> str:
        relative = self._relative_path(sha256, mime)
        name = Path(relative).name
        with self._lock:
            with self._root_directory(create=True) as (root, root_fd):
                existing = self._read_at(
                    root,
                    root_fd,
                    name,
                    max_bytes=self.limits.max_video_bytes,
                    missing_ok=True,
                )
                if existing is not None:
                    if existing != data:
                        raise VideoAssetError(
                            "已存在的视频资产损坏，不能覆盖",
                            code="video_asset_corrupt",
                        )
                    return relative
                temp_name = f".video-{secrets.token_hex(16)}.tmp"
                flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                if root_fd is not None:
                    fd = os.open(temp_name, flags, 0o600, dir_fd=root_fd)
                else:
                    fd = os.open(root / temp_name, flags, 0o600)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    try:
                        if root_fd is not None:
                            os.link(
                                temp_name,
                                name,
                                src_dir_fd=root_fd,
                                dst_dir_fd=root_fd,
                            )
                        else:
                            os.link(root / temp_name, root / name)
                    except FileExistsError:
                        if (
                            self._read_at(
                                root,
                                root_fd,
                                name,
                                max_bytes=self.limits.max_video_bytes,
                                missing_ok=True,
                            )
                            != data
                        ):
                            raise VideoAssetError(
                                "已存在的视频资产损坏，不能覆盖",
                                code="video_asset_corrupt",
                            ) from None
                finally:
                    try:
                        if root_fd is not None:
                            os.unlink(temp_name, dir_fd=root_fd)
                        else:
                            os.unlink(root / temp_name)
                    except FileNotFoundError:
                        pass
            return relative

    def _read_asset(self, ref: Mapping[str, Any]) -> bytes:
        path = self._checked_path(ref["path"])
        with self._root_directory(create=False) as (root, root_fd):
            data = self._read_at(
                root,
                root_fd,
                path.name,
                max_bytes=self.limits.max_video_bytes,
            )
        if (
            len(data) != ref["byte_size"]
            or hashlib.sha256(data).hexdigest() != ref["sha256"]
            or detect_video_mime(data) != ref["mime"]
        ):
            raise VideoAssetError(
                "视频资产内容已损坏或被修改",
                code="video_asset_corrupt",
            )
        return data

    @staticmethod
    def _read_at(
        root: Path,
        root_fd: int | None,
        name: str,
        *,
        max_bytes: int,
        missing_ok: bool = False,
    ) -> bytes | None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            if root_fd is not None:
                fd = os.open(name, flags, dir_fd=root_fd)
            else:
                fd = os.open(root / name, flags)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise VideoAssetError(
                "视频资产文件缺失", code="video_source_unavailable"
            ) from None
        except OSError:
            raise VideoAssetError(
                "视频资产文件无法读取", code="video_source_unavailable"
            ) from None
        try:
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise VideoAssetError(
                        "视频资产不是普通文件", code="video_unsafe_source"
                    )
                if info.st_size > max_bytes:
                    raise VideoAssetError(
                        "视频资产超过字节限制", code="video_too_large"
                    )
                data = stream.read(max_bytes + 1)
        except VideoAssetError:
            raise
        except OSError:
            raise VideoAssetError(
                "视频资产文件无法读取", code="video_source_unavailable"
            ) from None
        if len(data) > max_bytes:
            raise VideoAssetError(
                "视频资产超过字节限制", code="video_too_large"
            )
        return data


__all__ = ["VideoAssetStore"]
