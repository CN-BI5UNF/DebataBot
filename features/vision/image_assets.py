"""Validated, content-addressed inbound images and request-only vision projection.

Only the media pipeline may turn adapter-resolved local paths into ``trusted_local``
sources. Display text, adapter ``raw``/``extra`` dictionaries and remote content types
are never asset references. History contains small JSON metadata, never data URLs.

新图统一使用 ``pillow-first-frame-max<side>-v2``：
    - 只取首帧；
    - 应用 EXIF 方向；
    - 固定最长边缩放（不放大小图）；
    - 去多余 metadata；
    - 固定 JPEG/PNG 编码。
旧 v1 资产只读兼容，不在回放时重编码或覆盖。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import hashlib
import io
import ipaddress
import os
import re
import secrets
import socket
import stat
import warnings
from collections import OrderedDict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from utils.image_contracts import (
    TRANSFORM_VERSION_V1,
    TRANSFORM_VERSION_V2,
    ImageAssetError,
    ImageLimits,
    asset_relative_path,
    build_image_content_blocks,
    transform_version_for_limits,
    validate_image_asset_ref,
)

TRANSFORM_VERSION = TRANSFORM_VERSION_V2
_SUPPORTED_FORMATS = frozenset({"JPEG", "PNG", "GIF", "WEBP"})
_OUTPUT_FORMATS = {"image/jpeg": ("JPEG", ".jpg"), "image/png": ("PNG", ".png")}
_MAX_REDIRECTS = 3
_HTTP_TOTAL_TIMEOUT = 30.0


class _LimitedBuffer(io.BytesIO):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = limit

    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > self._limit:
            raise ImageAssetError(
                f"图片规范化后超过{self._limit}字节限制",
                code="image_too_large",
            )
        return super().write(data)


def _check_size(data: bytes, limit: int) -> bytes:
    if not data:
        raise ImageAssetError("图片内容为空", code="image_asset_corrupt")
    if len(data) > limit:
        raise ImageAssetError("图片超过字节限制", code="image_too_large")
    return data


def _check_dimensions(size: tuple[int, int], limits: ImageLimits) -> None:
    width, height = size
    if width <= 0 or height <= 0:
        raise ImageAssetError("图片尺寸无效", code="image_asset_corrupt")
    if width * height > limits.max_image_pixels:
        raise ImageAssetError(
            "图片超过像素限制",
            code="image_pixel_limit",
        )


def _normalise_image(data: bytes, limits: ImageLimits) -> tuple[bytes, str, int, int]:
    """Freeze frame zero, apply EXIF orientation, scale, discard metadata.

    JPEG stays RGB JPEG (quality=85, 4:2:0, no optimize/progressive); all other
    supported inputs become RGB/RGBA PNG (compression=6). Source and output must
    fit the configured byte limit; output is scaled to at most max_image_side
    without upscaling.
    """
    _check_size(data, limits.max_image_bytes)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                if probe.format not in _SUPPORTED_FORMATS:
                    raise ImageAssetError(
                        "图片格式不受支持，仅支持JPEG、PNG、GIF和WebP",
                        code="image_unsupported_format",
                    )
                _check_dimensions(probe.size, limits)
                image_format = probe.format
                probe.verify()
            with Image.open(io.BytesIO(data)) as source:
                _check_dimensions(source.size, limits)
                source.seek(0)
                source.load()
                oriented = ImageOps.exif_transpose(source)
                try:
                    _check_dimensions(oriented.size, limits)
                    oriented.thumbnail(
                        (limits.max_image_side, limits.max_image_side),
                        Image.Resampling.LANCZOS,
                    )
                    _check_dimensions(oriented.size, limits)
                    alpha = (
                        "A" in oriented.getbands()
                        or "transparency" in oriented.info
                    )
                    mode = "RGBA" if alpha and image_format != "JPEG" else "RGB"
                    converted = oriented.convert(mode)
                    try:
                        converted.info.clear()
                        output = _LimitedBuffer(limits.max_image_bytes)
                        if image_format == "JPEG":
                            converted.save(
                                output,
                                format="JPEG",
                                quality=85,
                                subsampling=2,
                                optimize=False,
                                progressive=False,
                            )
                            mime = "image/jpeg"
                        else:
                            converted.save(
                                output,
                                format="PNG",
                                compress_level=6,
                                optimize=False,
                            )
                            mime = "image/png"
                        return (
                            _check_size(output.getvalue(), limits.max_image_bytes),
                            mime,
                            converted.width,
                            converted.height,
                        )
                    finally:
                        converted.close()
                finally:
                    oriented.close()
    except ImageAssetError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageAssetError(
            "图片解压尺寸超过安全限制",
            code="image_pixel_limit",
        ) from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError):
        raise ImageAssetError(
            "图片内容无效或已损坏",
            code="image_asset_corrupt",
        ) from None


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return _is_public_ip(str(address.ipv4_mapped))
        # Transition addresses can embed an otherwise forbidden IPv4 destination.
        if address.sixtofour is not None or address.teredo is not None:
            return False
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_unspecified
    )


async def _public_addresses(host: str, port: int) -> list[str]:
    if host == "localhost" or host.endswith(
        (".localhost", ".local", ".internal", ".lan", ".home")
    ):
        raise ImageAssetError("图片地址不允许访问本机或内网", code="image_unsafe_source")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        try:
            records = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(
                    host,
                    port,
                    type=socket.SOCK_STREAM,
                ),
                timeout=5.0,
            )
        except (OSError, asyncio.TimeoutError):
            raise ImageAssetError(
                "图片地址解析失败",
                code="image_source_unavailable",
            ) from None
        addresses = list(dict.fromkeys(record[4][0] for record in records))
    # Reject mixed public/private DNS answers too, not merely the first address.
    if not addresses or not all(_is_public_ip(address) for address in addresses):
        raise ImageAssetError("图片地址不允许访问本机或内网", code="image_unsafe_source")
    return addresses


async def _checked_remote_url(source: str) -> tuple[httpx.URL, str, int, str]:
    try:
        if any(ord(char) < 33 or ord(char) == 127 for char in source) or "\\" in source:
            raise ValueError
        parsed = urlsplit(source)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "%" in parsed.hostname
        ):
            raise ValueError
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not host or not 1 <= port <= 65535:
            raise ValueError
        url = httpx.URL(source).copy_with(host=host, fragment=None)
    except (ValueError, UnicodeError, httpx.InvalidURL):
        raise ImageAssetError(
            "图片地址无效或来源不受支持",
            code="image_unsafe_source",
        ) from None
    addresses = await _public_addresses(host, port)
    return url, host, port, addresses[0]


async def _download_image(source: str, limits: ImageLimits) -> bytes:
    """Check every hop and pin its public IP, including HTTPS SNI/Host.

    The transport API avoids AsyncClient's INFO logging of signed request URLs.
    No environment proxy, cookies, automatic redirects or HTTP decompression are
    used. DNS cannot be rebound between validation and connection.
    """

    async def download() -> bytes:
        current = source
        for hop in range(_MAX_REDIRECTS + 1):
            url, host, port, address = await _checked_remote_url(current)
            authority = f"[{host}]" if ":" in host else host
            if port != (443 if url.scheme == "https" else 80):
                authority = f"{authority}:{port}"
            request = httpx.Request(
                "GET",
                url.copy_with(host=address),
                headers={
                    "Host": authority,
                    "Accept": "image/jpeg,image/png,image/gif,image/webp",
                    "Accept-Encoding": "identity",
                },
                extensions={
                    "sni_hostname": host,
                    "timeout": httpx.Timeout(20.0, connect=5.0).as_dict(),
                },
            )
            # A transport per hop also prevents TLS pooling across different
            # original hosts which happen to share a pinned IP.
            async with httpx.AsyncHTTPTransport(
                trust_env=False,
                retries=0,
            ) as transport:
                response = await transport.handle_async_request(request)
                try:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or hop >= _MAX_REDIRECTS:
                            raise ImageAssetError(
                                "图片下载重定向无效或次数过多",
                                code="image_source_unavailable",
                            )
                        current = urljoin(str(url), location)
                        continue
                    if not 200 <= response.status_code < 300:
                        raise ImageAssetError(
                            "图片下载失败",
                            code="image_source_unavailable",
                        )
                    if response.headers.get(
                        "content-encoding",
                        "identity",
                    ).lower() not in {"", "identity"}:
                        raise ImageAssetError(
                            "图片下载不接受压缩传输",
                            code="image_source_unavailable",
                        )
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            declared_size = int(content_length)
                        except ValueError:
                            raise ImageAssetError(
                                "图片下载大小无效",
                                code="image_source_unavailable",
                            ) from None
                        if declared_size < 0 or declared_size > limits.max_image_bytes:
                            raise ImageAssetError(
                                "图片超过字节限制",
                                code="image_too_large",
                            )
                    buffer = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=64 * 1024):
                        if len(buffer) + len(chunk) > limits.max_image_bytes:
                            raise ImageAssetError(
                                "图片超过字节限制",
                                code="image_too_large",
                            )
                        buffer.extend(chunk)
                    return _check_size(bytes(buffer), limits.max_image_bytes)
                finally:
                    await response.aclose()
        raise ImageAssetError(
            "图片下载重定向次数过多",
            code="image_source_unavailable",
        )

    try:
        return await asyncio.wait_for(download(), timeout=_HTTP_TOTAL_TIMEOUT)
    except ImageAssetError:
        raise
    except (httpx.HTTPError, OSError, ValueError, asyncio.TimeoutError):
        raise ImageAssetError(
            "图片下载失败或超时",
            code="image_source_unavailable",
        ) from None


def _local_path(source: str | Path) -> Path:
    if isinstance(source, Path):
        return source
    if re.match(r"^[A-Za-z]:[\\/]", source):
        return Path(source)
    try:
        parsed = urlsplit(source)
        if parsed.scheme == "file":
            if parsed.netloc not in {"", "localhost"} or parsed.query or parsed.fragment:
                raise ImageAssetError("本地图片来源无效", code="image_unsafe_source")
            value = unquote(parsed.path)
            if re.match(r"^/[A-Za-z]:/", value):
                value = value[1:]
            return Path(value)
        if parsed.scheme:
            raise ImageAssetError("图片来源不受支持", code="image_unsafe_source")
    except ValueError:
        raise ImageAssetError("本地图片来源无效", code="image_unsafe_source") from None
    return Path(source)


def _read_local_image(path: Path, limits: ImageLimits) -> bytes:
    try:
        # Local sources are adapter-resolved paths, not arbitrary inbound file IDs.
        # Non-regular files (including pipes/devices) must never block the worker.
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ImageAssetError(
                    "本地图片不是普通文件",
                    code="image_unsafe_source",
                )
            if info.st_size > limits.max_image_bytes:
                raise ImageAssetError("图片超过字节限制", code="image_too_large")
            return _check_size(
                stream.read(limits.max_image_bytes + 1),
                limits.max_image_bytes,
            )
    except ImageAssetError:
        raise
    except (OSError, ValueError):
        raise ImageAssetError(
            "本地图片缺失或无法读取",
            code="image_source_unavailable",
        ) from None


def _decode_image_data_url(value: str, limits: ImageLimits) -> bytes:
    """显式解析 data URL；不把它当路径，也不授予 trusted_local。"""
    text = unescape(str(value or "").strip())
    header, separator, payload = text.partition(",")
    if not separator or not header.lower().startswith("data:"):
        raise ImageAssetError("图片data URL格式无效", code="image_asset_corrupt")
    parts = [part.strip().lower() for part in header[5:].split(";") if part.strip()]
    if not parts or not parts[0].startswith("image/") or "base64" not in parts[1:]:
        raise ImageAssetError("图片data URL格式无效", code="image_asset_corrupt")
    max_encoded = 4 * ((limits.max_image_bytes + 2) // 3) + 16
    if not payload or len(payload) > max_encoded or not payload.isascii():
        raise ImageAssetError("图片data URL超过限制", code="image_too_large")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error):
        raise ImageAssetError("图片data URL编码无效", code="image_asset_corrupt") from None
    return _check_size(raw, limits.max_image_bytes)


async def _source_bytes(
    source: str | Path | bytes,
    *,
    trusted_local: bool,
    limits: ImageLimits,
) -> bytes:
    if isinstance(source, bytes):
        return _check_size(source, limits.max_image_bytes)
    if isinstance(source, str):
        source = unescape(source.strip())
        if source.lower().startswith("data:"):
            return _decode_image_data_url(source, limits)
        if source.lower().startswith(("http://", "https://")):
            return await _download_image(source, limits)
    if not isinstance(source, (str, Path)):
        raise ImageAssetError("图片来源不受支持", code="image_unsafe_source")
    if not trusted_local:
        raise ImageAssetError(
            "未受信任的本地图片来源",
            code="image_unsafe_source",
        )
    path = _local_path(source)
    if not path.is_absolute():
        raise ImageAssetError(
            "本地图片来源必须是已验证的绝对路径",
            code="image_unsafe_source",
        )
    return await asyncio.to_thread(_read_local_image, path, limits)


def _data_url(data: bytes, mime: str) -> bytes:
    return f"data:{mime};base64,".encode("ascii") + base64.b64encode(data)


async def prepare_image_data_url(
    source: str | Path | bytes,
    *,
    trusted_local: bool = False,
    limits: ImageLimits | None = None,
) -> str:
    """Standalone vision loader, sharing the same validation without a workspace.

    Accepts normalized-capable bytes, http(s) URLs, explicit image data URLs, or
    trusted absolute local paths. ``bytes`` must be decoded image bytes, not base64
    text. Callers accepting local user paths must enforce their own workspace
    boundary before granting trust.
    """
    limits = limits or ImageLimits()
    raw = await _source_bytes(
        source,
        trusted_local=trusted_local,
        limits=limits,
    )
    data, mime, _width, _height = await asyncio.to_thread(
        _normalise_image,
        raw,
        limits,
    )
    return (await asyncio.to_thread(_data_url, data, mime)).decode("ascii")


class ImageAssetStore:
    """Persist immutable images; cache at most configured encoded URL bytes.

    Eviction only removes an in-memory encoding. Every projection rechecks the
    managed file and its hash, even on a cache hit; missing/corrupt history assets
    therefore fail explicitly rather than using stale bytes or placeholders.
    """

    def __init__(
        self,
        workspace: str | Path,
        *,
        cache_max_bytes: int | None = None,
        limits: ImageLimits | None = None,
    ) -> None:
        self.limits = limits or ImageLimits()
        if cache_max_bytes is None:
            cache_max_bytes = self.limits.cache_max_bytes
        if isinstance(cache_max_bytes, bool) or not isinstance(cache_max_bytes, int):
            raise ValueError("cache_max_bytes must be an integer")
        if not 0 <= cache_max_bytes <= self.limits.cache_max_bytes:
            raise ValueError(
                "image cache must be between 0 and the configured cache limit"
            )
        try:
            self.workspace = Path(workspace).resolve()
        except (OSError, ValueError, RuntimeError):
            raise ImageAssetError("图片workspace目录无效") from None
        self.cache_max_bytes = cache_max_bytes
        self.transform_version = transform_version_for_limits(self.limits)
        self._asset_directory = (
            Path("incoming") / "images" / self.transform_version
        )
        self._cache: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self._cache_bytes = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._cache_evictions = 0
        self._ingest_count = 0
        self._read_count = 0
        self._encode_count = 0
        self._cache_lock = asyncio.Lock()
        self._ingest_lock = asyncio.Lock()

    @property
    def cache_bytes(self) -> int:
        return self._cache_bytes

    @property
    def cache_entries(self) -> int:
        return len(self._cache)

    def cache_info(self) -> dict[str, int]:
        return {
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "evictions": self._cache_evictions,
            "entries": len(self._cache),
            "current_bytes": self._cache_bytes,
            "max_bytes": self.cache_max_bytes,
            "ingests": self._ingest_count,
            "reads": self._read_count,
            "encodes": self._encode_count,
        }

    async def ingest(
        self,
        source: str | Path | bytes,
        *,
        message_id: str = "",
        index: int = 0,
        trusted_local: bool = False,
    ) -> dict[str, Any]:
        """Validate once and return a detached, JSON-safe metadata reference.

        ``index`` is the zero-based image position in the original inbound message,
        including earlier failed images. A trusted source flag never bypasses HTTP
        restrictions. The per-message count limit belongs to the media pipeline.
        """
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ImageAssetError("图片消息索引无效")
        async with self._ingest_lock:
            raw = await _source_bytes(
                source,
                trusted_local=trusted_local,
                limits=self.limits,
            )
            data, mime, width, height = await asyncio.to_thread(
                _normalise_image,
                raw,
                self.limits,
            )
            sha256 = hashlib.sha256(data).hexdigest()
            path = await asyncio.to_thread(self._persist, data, sha256, mime)
        ref = {
            "sha256": sha256,
            "mime": mime,
            "width": width,
            "height": height,
            "byte_size": len(data),
            "path": path,
            "detail": "auto",
            "message_id": str(message_id),
            "index": index,
            "transform_version": self.transform_version,
        }
        self._ingest_count += 1
        return validate_image_asset_ref(ref)

    async def image_content(self, asset: Mapping[str, Any]) -> dict[str, Any]:
        """Create an independent OpenAI-compatible image_url content block."""
        ref = self._checked_reference(asset)
        data = await asyncio.to_thread(self._read_asset, ref)
        self._read_count += 1
        key = (ref["transform_version"], ref["sha256"])
        async with self._cache_lock:
            encoded = self._cache.get(key)
            if encoded is None:
                self._cache_misses += 1
                self._encode_count += 1
                encoded = await asyncio.to_thread(_data_url, data, ref["mime"])
                if self.cache_max_bytes > 0 and len(encoded) <= self.cache_max_bytes:
                    while (
                        self._cache
                        and self._cache_bytes + len(encoded) > self.cache_max_bytes
                    ):
                        _old_key, previous = self._cache.popitem(last=False)
                        self._cache_bytes -= len(previous)
                        self._cache_evictions += 1
                    self._cache[key] = encoded
                    self._cache_bytes += len(encoded)
            else:
                self._cache_hits += 1
                self._cache.move_to_end(key)
        return {
            "type": "image_url",
            "image_url": {
                "url": encoded.decode("ascii"),
                "detail": ref["detail"],
            },
        }

    async def project_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Copy-on-write projection of trusted metadata, never display markers."""
        if record.get("role") != "user":
            return record
        metadata = record.get("metadata")
        if not isinstance(metadata, Mapping) or "image_assets" not in metadata:
            return record
        assets = metadata["image_assets"]
        if not isinstance(assets, (list, tuple)):
            raise ImageAssetError("图片资产列表无效")
        if not assets:
            return record
        text = record.get("content", "")
        if not isinstance(text, str):
            raise ImageAssetError("图片历史消息正文格式无效")
        refs = [self._checked_reference(asset) for asset in assets]
        image_parts = [await self.image_content(ref) for ref in refs]
        blocks = build_image_content_blocks(text, refs, image_parts)
        projected = dict(record)
        projected["metadata"] = copy.deepcopy(dict(metadata))
        projected["content"] = blocks
        return projected

    async def project_records(
        self,
        records: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Project in order without modifying history or dropping failed assets."""
        return [await self.project_record(record) for record in records]

    def _relative_path(self, sha256: str, mime: str) -> str:
        return asset_relative_path(self.transform_version, sha256, mime)

    def _checked_reference(self, asset: Mapping[str, Any]) -> dict[str, Any]:
        return validate_image_asset_ref(asset, limits=self.limits)

    def _checked_path(self, relative: str | Path) -> Path:
        try:
            relative = Path(relative)
            if relative.is_absolute() or ".." in relative.parts:
                raise ImageAssetError("图片资产路径越出workspace")
            path = self.workspace / relative
            if (
                self.workspace.resolve() != self.workspace
                or not path.resolve().is_relative_to(self.workspace)
            ):
                raise ImageAssetError("图片资产路径越出workspace")
            current = self.workspace
            for part in relative.parts:
                current /= part
                if current.is_symlink():
                    raise ImageAssetError("图片资产路径不允许符号链接")
            return path
        except ImageAssetError:
            raise
        except (OSError, ValueError, RuntimeError):
            raise ImageAssetError("图片资产路径无效") from None

    @contextmanager
    def _directory(
        self,
        directory: Path,
        *,
        create: bool,
    ) -> Iterator[tuple[Path, int | None]]:
        """Resolve-check on all platforms; no-follow descriptor walk on POSIX."""
        directory = Path(directory)
        directory_path = self._checked_path(directory)
        fd: int | None = None
        try:
            if create:
                self.workspace.mkdir(parents=True, exist_ok=True)
            if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                fd = os.open(self.workspace, flags)
                for component in directory.parts:
                    if create:
                        try:
                            os.mkdir(component, dir_fd=fd)
                        except FileExistsError:
                            pass
                    next_fd = os.open(component, flags, dir_fd=fd)
                    os.close(fd)
                    fd = next_fd
                yield directory_path, fd
            else:
                if create:
                    directory_path.mkdir(parents=True, exist_ok=True)
                self._checked_path(directory)
                yield directory_path, None
        except ImageAssetError:
            raise
        except (OSError, ValueError):
            raise ImageAssetError(
                "图片资产文件缺失、损坏或路径不安全"
            ) from None
        finally:
            if fd is not None:
                os.close(fd)

    @staticmethod
    def _read_at(
        directory: Path,
        directory_fd: int | None,
        name: str,
        *,
        max_bytes: int,
    ) -> bytes:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        if directory_fd is not None:
            fd = os.open(name, flags, dir_fd=directory_fd)
        else:
            fd = os.open(directory / name, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                raise ImageAssetError("图片资产文件无效或大小超限")
            return _check_size(
                stream.read(max_bytes + 1),
                max_bytes,
            )

    def _persist(self, data: bytes, sha256: str, mime: str) -> str:
        relative = self._relative_path(sha256, mime)
        directory = Path(relative).parent
        path = self._checked_path(relative)
        with self._directory(directory, create=True) as (directory_path, directory_fd):
            try:
                existing = self._read_at(
                    directory_path,
                    directory_fd,
                    path.name,
                    max_bytes=self.limits.max_image_bytes,
                )
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if existing != data:
                    raise ImageAssetError("已存在的图片资产损坏，不能覆盖")
                return relative
            temp_name = f".image-{secrets.token_hex(16)}.tmp"
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
            )
            if directory_fd is not None:
                fd = os.open(temp_name, flags, 0o600, dir_fd=directory_fd)
            else:
                fd = os.open(directory_path / temp_name, flags, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    if directory_fd is not None:
                        os.link(
                            temp_name,
                            path.name,
                            src_dir_fd=directory_fd,
                            dst_dir_fd=directory_fd,
                        )
                    else:
                        os.link(directory_path / temp_name, path)
                except FileExistsError:
                    if (
                        self._read_at(
                            directory_path,
                            directory_fd,
                            path.name,
                            max_bytes=self.limits.max_image_bytes,
                        )
                        != data
                    ):
                        raise ImageAssetError(
                            "已存在的图片资产损坏，不能覆盖"
                        ) from None
                except OSError:
                    # Windows / filesystems without hard links: rename is
                    # no-clobber enough here because the destination was checked
                    # absent above; a race still fails explicitly below.
                    try:
                        if directory_fd is not None:
                            os.rename(
                                temp_name,
                                path.name,
                                src_dir_fd=directory_fd,
                                dst_dir_fd=directory_fd,
                            )
                        else:
                            os.rename(directory_path / temp_name, path)
                    except FileExistsError:
                        if (
                            self._read_at(
                                directory_path,
                                directory_fd,
                                path.name,
                                max_bytes=self.limits.max_image_bytes,
                            )
                            != data
                        ):
                            raise ImageAssetError(
                                "已存在的图片资产损坏，不能覆盖"
                            ) from None
            finally:
                if directory_fd is not None:
                    try:
                        os.unlink(temp_name, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
                else:
                    try:
                        (directory_path / temp_name).unlink()
                    except FileNotFoundError:
                        pass
        return relative

    def _read_asset(self, ref: Mapping[str, Any]) -> bytes:
        path = self._checked_path(ref["path"])
        directory = Path(ref["path"]).parent
        with self._directory(directory, create=False) as (
            directory_path,
            directory_fd,
        ):
            data = self._read_at(
                directory_path,
                directory_fd,
                path.name,
                max_bytes=self.limits.max_image_bytes,
            )
        if (
            len(data) != ref["byte_size"]
            or hashlib.sha256(data).hexdigest() != ref["sha256"]
        ):
            raise ImageAssetError(
                "图片资产内容已损坏或被修改",
                code="image_asset_corrupt",
            )
        try:
            with Image.open(io.BytesIO(data)) as image:
                if (
                    image.format != _OUTPUT_FORMATS[ref["mime"]][0]
                    or image.size != (ref["width"], ref["height"])
                ):
                    raise ImageAssetError(
                        "图片资产内容与元数据不一致",
                        code="image_asset_corrupt",
                    )
                image.verify()
        except ImageAssetError:
            raise
        except (
            OSError,
            ValueError,
            SyntaxError,
            Image.DecompressionBombError,
        ):
            raise ImageAssetError(
                "图片资产内容已损坏",
                code="image_asset_corrupt",
            ) from None
        return data


__all__ = [
    "ImageAssetError",
    "ImageAssetStore",
    "prepare_image_data_url",
    "TRANSFORM_VERSION",
    "TRANSFORM_VERSION_V1",
    "TRANSFORM_VERSION_V2",
]
