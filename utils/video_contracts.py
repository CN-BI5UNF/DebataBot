"""视频资产与预算的纯数据契约。

和 ``utils.image_contracts`` 一样，本模块不导入 Pillow、httpx 或 core，
供配置、媒体管道、token 预算和协议层共享同一套字段、路径与限额规则。

视频按“完整文件”处理：不抽帧、不转码、不截断。能否真正发给某个模型
由协议层与 provider/model 能力表决定；不支持的模型只保留 workspace
占位，不会静默降级成图片帧。
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

MAX_VIDEO_BYTES = 64 * 1024 * 1024
MAX_VIDEOS_PER_MESSAGE = 2
MAX_VIDEOS_PER_REQUEST = 2
MAX_ENCODED_VIDEO_BYTES = 90 * 1024 * 1024
MAX_VIDEO_REQUEST_BODY_BYTES = 100 * 1024 * 1024
DEFAULT_VIDEO_TOKEN_RESERVE = 65536
MIN_VIDEO_TOKEN_RESERVE = 1024
MAX_VIDEO_TOKEN_RESERVE = 524288

VIDEO_TRANSFORM_VERSION = "video-native-v1"
"""完整视频不做转码；版本号只用于内容寻址路径和未来迁移。"""

SUPPORTED_VIDEO_MIMES: frozenset[str] = frozenset(
    {
        "video/mp4",
        "video/webm",
        "video/quicktime",
        "video/mpeg",
    }
)

_VIDEO_EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/mpeg": ".mpg",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_ASSET_REF_KEYS = frozenset(
    {
        "sha256",
        "mime",
        "byte_size",
        "path",
        "message_id",
        "index",
        "transform_version",
    }
)
_VIDEO_DATA_URL_WRAPPER_BYTES = 256
_BUDGET_VIDEO_URL = "<managed-video>"


class VideoAssetError(ValueError):
    """受控视频错误：safe message 可直接展示，不含 URL/密钥/base64。"""

    def __init__(self, message: str, *, code: str = "invalid_video_asset") -> None:
        super().__init__(message)
        self.code = code

    def safe_message(self) -> str:
        return str(self)


@dataclass(frozen=True, slots=True)
class VideoLimits:
    """项目视频资源护栏；默认值即硬上限，只允许向下收紧。"""

    max_video_bytes: int = MAX_VIDEO_BYTES
    max_videos_per_message: int = MAX_VIDEOS_PER_MESSAGE
    max_videos_per_request: int = MAX_VIDEOS_PER_REQUEST
    max_encoded_video_bytes: int = MAX_ENCODED_VIDEO_BYTES
    max_request_body_bytes: int = MAX_VIDEO_REQUEST_BODY_BYTES

    def __post_init__(self) -> None:
        _require_int("max_video_bytes", self.max_video_bytes, 1, MAX_VIDEO_BYTES)
        _require_int(
            "max_videos_per_message",
            self.max_videos_per_message,
            1,
            MAX_VIDEOS_PER_MESSAGE,
        )
        _require_int(
            "max_videos_per_request",
            self.max_videos_per_request,
            1,
            MAX_VIDEOS_PER_REQUEST,
        )
        _require_int(
            "max_encoded_video_bytes",
            self.max_encoded_video_bytes,
            1,
            MAX_ENCODED_VIDEO_BYTES,
        )
        _require_int(
            "max_request_body_bytes",
            self.max_request_body_bytes,
            1,
            MAX_VIDEO_REQUEST_BODY_BYTES,
        )


@dataclass(frozen=True, slots=True)
class VideoBudgetProfile:
    name: str
    per_video_tokens: int
    basis: str
    exact: bool


@dataclass(frozen=True, slots=True)
class VideoBudgetEstimate:
    video_count: int
    encoded_video_bytes: int
    video_tokens: int
    basis: str
    exact: bool


def _require_int(name: str, value: Any, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


def detect_video_mime(data: bytes) -> str | None:
    """按文件头识别受支持容器；不依赖扩展名或用户可控 MIME。"""
    if not isinstance(data, bytes) or len(data) < 12:
        return None
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand == b"qt  ":
            return "video/quicktime"
        return "video/mp4"
    if data.startswith(b"\x1aE\xdf\xa3"):
        # WebM 与 Matroska 共用 EBML 头；只接受 WebM，避免把 MKV 当作
        # 已知可用的 provider 格式。
        head = data[:256].lower()
        if b"webm" in head:
            return "video/webm"
        return None
    if data[:4] in {b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"}:
        return "video/mpeg"
    return None


def video_asset_relative_path(sha256: str, mime: str) -> str:
    if not _SHA256_RE.fullmatch(str(sha256 or "")):
        raise VideoAssetError("视频资产哈希无效")
    extension = _VIDEO_EXTENSIONS.get(mime)
    if extension is None:
        raise VideoAssetError("视频资产格式无效")
    return f"incoming/videos/{VIDEO_TRANSFORM_VERSION}/{sha256}{extension}"


def build_video_attribution_text(asset: Mapping[str, Any]) -> str:
    """稳定的视频归属文本；只使用可信 message_id/index。"""
    message_id = str(asset.get("message_id") or "")
    index = asset.get("index")
    return f"[视频所属消息 msg_id={message_id} index={index}]"


def build_video_content_blocks(
    content: str | list[Any],
    refs: Sequence[Mapping[str, Any]],
    video_parts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """在已有文本/图片块之后追加视频块，顺序与预算视图一致。"""
    if len(refs) != len(video_parts):
        raise VideoAssetError(
            "视频块数量与资产引用不一致", code="video_projection_failed"
        )
    if isinstance(content, str):
        blocks: list[dict[str, Any]] = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        blocks = list(content)
    else:
        raise VideoAssetError("视频历史消息正文格式无效", code="video_projection_failed")
    for ref, video in zip(refs, video_parts, strict=True):
        blocks.append(
            {"type": "text", "text": build_video_attribution_text(ref)}
        )
        blocks.append(dict(video))
    return blocks


def build_video_budget_messages(
    messages: Sequence[Mapping[str, Any]],
    refs_by_message: Mapping[int, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """从同一份文本/refs 源构造轻量预算视图。"""
    budget_messages: list[dict[str, Any]] = []
    for message_index, message in enumerate(messages):
        refs = list(refs_by_message.get(message_index, ()))
        content = message.get("content")
        if content is not None and _contains_video_block(content):
            raise VideoAssetError(
                "预算视图不能在视频已实体化后再次构造",
                code="video_projection_failed",
            )
        if not refs:
            budget_messages.append(dict(message))
            continue
        if message.get("role") != "user":
            raise VideoAssetError(
                "视频资产只能绑定真实用户消息",
                code="invalid_video_asset",
            )
        if isinstance(content, str):
            blocks: list[Any] = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            blocks = list(content)
        else:
            raise VideoAssetError(
                "视频历史消息正文格式无效", code="video_projection_failed"
            )
        for ref in refs:
            blocks.append(
                {
                    "type": "text",
                    "text": build_video_attribution_text(ref),
                }
            )
            blocks.append(_budget_video_block())
        projected = dict(message)
        metadata = message.get("metadata")
        if isinstance(metadata, Mapping):
            projected["metadata"] = copy.deepcopy(dict(metadata))
        projected["content"] = blocks
        budget_messages.append(projected)
    return budget_messages


def encode_video_ref_bytes_estimate(byte_size: int) -> int:
    """原始视频字节 -> data URL/base64 + JSON 包装的保守字节估算。"""
    if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 0:
        raise VideoAssetError("视频字节数无效")
    return 4 * math.ceil(byte_size / 3) + _VIDEO_DATA_URL_WRAPPER_BYTES


def _budget_video_block() -> dict[str, Any]:
    return {"type": "video_url", "video_url": {"url": _BUDGET_VIDEO_URL}}


def _contains_video_block(content: Any) -> bool:
    return isinstance(content, list) and any(
        isinstance(block, Mapping) and block.get("type") == "video_url"
        for block in content
    )


def _validate_message_id(value: Any) -> str:
    if not isinstance(value, str):
        raise VideoAssetError("视频资产消息归属无效")
    if len(value) > 256 or _CONTROL_CHARS_RE.search(value):
        raise VideoAssetError("视频资产消息归属无效")
    return value


def validate_video_asset_ref(
    asset: Any,
    *,
    limits: VideoLimits | None = None,
) -> dict[str, Any]:
    """严格校验一个持久化视频 asset ref，返回独立副本。"""
    if not isinstance(asset, Mapping):
        raise VideoAssetError("视频资产引用无效")
    unknown = set(asset) - _ASSET_REF_KEYS
    if unknown:
        raise VideoAssetError("视频资产引用包含未知字段")
    ref = dict(asset)

    sha256 = ref.get("sha256")
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise VideoAssetError("视频资产哈希无效")
    mime = ref.get("mime")
    if not isinstance(mime, str) or mime not in SUPPORTED_VIDEO_MIMES:
        raise VideoAssetError("视频资产格式无效")

    version = ref.get("transform_version")
    if version != VIDEO_TRANSFORM_VERSION:
        raise VideoAssetError("视频资产转换版本不受支持")
    if ref.get("path") != video_asset_relative_path(sha256, mime):
        raise VideoAssetError("视频资产路径不是受管的内容寻址路径")

    byte_size = ref.get("byte_size")
    max_bytes = (limits or VideoLimits()).max_video_bytes
    if (
        isinstance(byte_size, bool)
        or not isinstance(byte_size, int)
        or byte_size <= 0
        or byte_size > max_bytes
    ):
        raise VideoAssetError("视频资产字节数无效或超过安全限制")

    index = ref.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise VideoAssetError("视频资产消息索引无效")

    ref["message_id"] = _validate_message_id(ref.get("message_id", ""))
    return ref


def declared_video_assets(message: Mapping[str, Any]) -> list[Any] | None:
    """返回声明的原始视频资产列表；缺键为 None，类型错误抛错。"""
    metadata = message.get("metadata")
    if not isinstance(metadata, Mapping) or "video_assets" not in metadata:
        return None
    assets = metadata.get("video_assets")
    if assets is None:
        raise VideoAssetError("视频资产列表无效")
    if not isinstance(assets, (list, tuple)):
        raise VideoAssetError("视频资产列表无效")
    return list(assets)


def has_declared_video_assets(messages: list[Mapping[str, Any]]) -> bool:
    for message in messages:
        if declared_video_assets(message):
            return True
    return False


_ORIGIN_KIND_METADATA = "metadata"
_ORIGIN_KIND_LEGACY = "legacy_message_id"
_ORIGIN_KIND_LEGACY_RECORD = "legacy_record"


def _origin_conversation(message: Mapping[str, Any]) -> str:
    conversation_id = message.get("conversation_id")
    return conversation_id if isinstance(conversation_id, str) else ""


def _metadata_messages(message: Mapping[str, Any]) -> list[Any] | None:
    metadata = message.get("metadata")
    if not isinstance(metadata, Mapping) or "messages" not in metadata:
        return None
    entries = metadata.get("messages")
    if not isinstance(entries, list):
        raise VideoAssetError("视频资产消息来源列表无效")
    return entries


def _entry_message_id(entry: Mapping[str, Any]) -> str:
    value = entry.get("message_id")
    return value if isinstance(value, str) else ""


def _entry_inbound_seq(entry: Mapping[str, Any]) -> int | None:
    value = entry.get("inbound_seq")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _metadata_origin(
    message: Mapping[str, Any],
    ref: Mapping[str, Any],
    entries: list[Any],
) -> tuple[Any, ...]:
    conversation = _origin_conversation(message)
    ref_message_id = str(ref.get("message_id") or "")
    if not entries:
        raise VideoAssetError("视频资产缺少可信原始消息来源")
    valid_entries = [entry for entry in entries if isinstance(entry, Mapping)]
    if len(valid_entries) != len(entries):
        raise VideoAssetError("视频资产消息来源列表无效")

    if ref_message_id:
        matches = [
            entry
            for entry in valid_entries
            if _entry_message_id(entry) == ref_message_id
        ]
        if len(matches) != 1:
            raise VideoAssetError(
                "视频资产消息归属不存在或不唯一",
                code="invalid_video_asset",
            )
        entry = matches[0]
    else:
        if len(valid_entries) != 1:
            raise VideoAssetError(
                "视频资产消息归属为空且无法唯一绑定",
                code="invalid_video_asset",
            )
        entry = valid_entries[0]
        if not _entry_message_id(entry):
            raise VideoAssetError(
                "视频资产消息归属为空且无法唯一绑定",
                code="invalid_video_asset",
            )
    matched_message_id = _entry_message_id(entry)
    if not matched_message_id:
        raise VideoAssetError(
            "视频资产消息归属为空且无法唯一绑定",
            code="invalid_video_asset",
        )
    return (
        conversation,
        _ORIGIN_KIND_METADATA,
        matched_message_id,
        _entry_inbound_seq(entry),
    )


def _legacy_origin_for_record(
    message: Mapping[str, Any],
    message_index: int,
    refs: list[dict[str, Any]],
) -> tuple[Any, ...]:
    conversation = _origin_conversation(message)
    message_ids = {str(ref.get("message_id") or "") for ref in refs}
    if len(message_ids) != 1:
        raise VideoAssetError(
            "视频资产缺少可信原始消息归属",
            code="invalid_video_asset",
        )
    message_id = next(iter(message_ids))
    if message_id:
        return (conversation, _ORIGIN_KIND_LEGACY, message_id, None)
    return (
        conversation,
        _ORIGIN_KIND_LEGACY_RECORD,
        message_index,
        None,
    )


def validate_declared_video_assets(
    messages: list[Mapping[str, Any]],
    *,
    limits: VideoLimits | None = None,
    enforce_request_limits: bool = True,
) -> list[tuple[int, dict[str, Any]]]:
    """严格验证所有声明视频资产，并按可信原始消息执行数量限制。"""
    limits = limits or VideoLimits()
    validated: list[tuple[int, dict[str, Any]]] = []
    origin_counts: dict[tuple[Any, ...], int] = {}
    seen_origin_indices: set[tuple[tuple[Any, ...], int]] = set()
    total = 0
    for message_index, message in enumerate(messages):
        assets = declared_video_assets(message)
        if not assets:
            continue
        refs = [
            validate_video_asset_ref(asset, limits=limits)
            for asset in assets
        ]
        entries = _metadata_messages(message)
        legacy_origin: tuple[Any, ...] | None = None
        if entries is None:
            legacy_origin = _legacy_origin_for_record(
                message,
                message_index,
                refs,
            )
        for ref in refs:
            if entries is None:
                origin = legacy_origin
            else:
                origin = _metadata_origin(message, ref, entries)
            if origin is None:
                raise VideoAssetError(
                    "视频资产缺少可信原始消息归属",
                    code="invalid_video_asset",
                )
            origin_index = (origin, ref["index"])
            if origin_index in seen_origin_indices:
                raise VideoAssetError(
                    "同一原始消息的视频资产消息索引重复",
                    code="invalid_video_asset",
                )
            seen_origin_indices.add(origin_index)
            origin_count = origin_counts.get(origin, 0) + 1
            origin_counts[origin] = origin_count
            if origin_count > limits.max_videos_per_message:
                raise VideoAssetError(
                    "同一原始消息视频数量超过限制",
                    code="video_request_count_exceeded",
                )
            validated.append((message_index, ref))
            total += 1
            if enforce_request_limits and total > limits.max_videos_per_request:
                raise VideoAssetError(
                    "请求视频数量超过限制", code="video_request_count_exceeded"
                )
    return validated


def group_validated_video_refs(
    messages: Sequence[Mapping[str, Any]],
    *,
    limits: VideoLimits | None = None,
    enforce_request_limits: bool = False,
) -> dict[int, list[dict[str, Any]]]:
    validated = validate_declared_video_assets(
        list(messages),
        limits=limits,
        enforce_request_limits=enforce_request_limits,
    )
    grouped: dict[int, list[dict[str, Any]]] = {}
    for message_index, ref in validated:
        grouped.setdefault(message_index, []).append(ref)
    return grouped


__all__ = [
    "DEFAULT_VIDEO_TOKEN_RESERVE",
    "MAX_ENCODED_VIDEO_BYTES",
    "MAX_VIDEO_BYTES",
    "MAX_VIDEO_REQUEST_BODY_BYTES",
    "MAX_VIDEO_TOKEN_RESERVE",
    "MAX_VIDEOS_PER_MESSAGE",
    "MAX_VIDEOS_PER_REQUEST",
    "MIN_VIDEO_TOKEN_RESERVE",
    "SUPPORTED_VIDEO_MIMES",
    "VIDEO_TRANSFORM_VERSION",
    "VideoAssetError",
    "VideoBudgetEstimate",
    "VideoBudgetProfile",
    "VideoLimits",
    "build_video_attribution_text",
    "build_video_budget_messages",
    "build_video_content_blocks",
    "declared_video_assets",
    "detect_video_mime",
    "encode_video_ref_bytes_estimate",
    "group_validated_video_refs",
    "has_declared_video_assets",
    "validate_declared_video_assets",
    "validate_video_asset_ref",
    "video_asset_relative_path",
]
