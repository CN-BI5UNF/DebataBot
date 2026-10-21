"""图片资产与预算的纯数据契约。

这个模块不导入 Pillow、httpx、core 或 agents，供配置、token 预算、媒体管道
和协议层共享同一套字段/路径/限额规则。

注意：这里的限额是**项目资源护栏**，不是供应商法定上限；图片 token 预算是
可配置预留量，不是供应商账单真值。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_SIDE = 2048
MAX_IMAGES_PER_MESSAGE = 4
MAX_IMAGES_PER_REQUEST = 20
MAX_ENCODED_IMAGE_BYTES = 24 * 1024 * 1024
MAX_REQUEST_BODY_BYTES = 30 * 1024 * 1024
MAX_CACHE_BYTES = 32 * 1024 * 1024
DEFAULT_IMAGE_TOKEN_RESERVE = 8192
MIN_IMAGE_TOKEN_RESERVE = 256
MAX_IMAGE_TOKEN_RESERVE = 65536

TRANSFORM_VERSION_V1 = "pillow-first-frame-v1"
TRANSFORM_VERSION_V2 = "pillow-first-frame-max2048-v2"
SUPPORTED_OUTPUT_MIMES = frozenset({"image/jpeg", "image/png"})
SUPPORTED_DETAILS = frozenset({"auto", "low", "high"})

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_V2_VERSION_RE = re.compile(r"^pillow-first-frame-max(\d+)-v2$")
_OUTPUT_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png"}
_ASSET_REF_KEYS = frozenset(
    {
        "sha256",
        "mime",
        "width",
        "height",
        "byte_size",
        "path",
        "detail",
        "message_id",
        "index",
        "transform_version",
    }
)
_IMAGE_DATA_URL_WRAPPER_BYTES = 256


class ImageAssetError(ValueError):
    """受控图片错误：safe message 可直接展示，不含 URL/密钥/base64。"""

    def __init__(self, message: str, *, code: str = "invalid_asset_reference") -> None:
        super().__init__(message)
        self.code = code

    def safe_message(self) -> str:
        return str(self)


@dataclass(frozen=True, slots=True)
class ImageLimits:
    """项目图片资源护栏；默认值即硬上限，第一版只允许向下收紧。"""

    max_image_bytes: int = MAX_IMAGE_BYTES
    max_image_pixels: int = MAX_IMAGE_PIXELS
    max_image_side: int = MAX_IMAGE_SIDE
    max_images_per_message: int = MAX_IMAGES_PER_MESSAGE
    max_images_per_request: int = MAX_IMAGES_PER_REQUEST
    max_encoded_image_bytes: int = MAX_ENCODED_IMAGE_BYTES
    max_request_body_bytes: int = MAX_REQUEST_BODY_BYTES
    cache_max_bytes: int = MAX_CACHE_BYTES

    def __post_init__(self) -> None:
        _require_int("max_image_bytes", self.max_image_bytes, 1, MAX_IMAGE_BYTES)
        _require_int("max_image_pixels", self.max_image_pixels, 1, MAX_IMAGE_PIXELS)
        _require_int("max_image_side", self.max_image_side, 1, MAX_IMAGE_SIDE)
        _require_int(
            "max_images_per_message",
            self.max_images_per_message,
            1,
            MAX_IMAGES_PER_MESSAGE,
        )
        _require_int(
            "max_images_per_request",
            self.max_images_per_request,
            1,
            MAX_IMAGES_PER_REQUEST,
        )
        _require_int(
            "max_encoded_image_bytes",
            self.max_encoded_image_bytes,
            1,
            MAX_ENCODED_IMAGE_BYTES,
        )
        _require_int(
            "max_request_body_bytes",
            self.max_request_body_bytes,
            1,
            MAX_REQUEST_BODY_BYTES,
        )
        _require_int("cache_max_bytes", self.cache_max_bytes, 0, MAX_CACHE_BYTES)


@dataclass(frozen=True, slots=True)
class ImageBudgetEstimate:
    image_count: int
    encoded_image_bytes: int
    image_tokens: int
    basis: str
    exact: bool


@dataclass(frozen=True, slots=True)
class ImageBudgetUsage:
    estimated_tokens: int
    encoded_image_bytes: int
    image_count: int
    request_body_bytes_estimate: int
    image_estimation_basis: str


@dataclass(frozen=True, slots=True)
class ImageBudgetProfile:
    name: str
    per_image_tokens: int
    basis: str
    exact: bool


def _require_int(name: str, value: Any, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


def transform_version_for_limits(limits: ImageLimits) -> str:
    """新图规范化版本包含最大边，避免不同缩放策略共用内容寻址路径。"""
    return f"pillow-first-frame-max{limits.max_image_side}-v2"


def supported_transform_versions() -> frozenset[str]:
    return frozenset({TRANSFORM_VERSION_V1, TRANSFORM_VERSION_V2})


def is_supported_transform_version(version: str) -> bool:
    if version == TRANSFORM_VERSION_V1:
        return True
    match = _V2_VERSION_RE.fullmatch(version)
    if match is None:
        return False
    side = int(match.group(1))
    return 1 <= side <= MAX_IMAGE_SIDE


def asset_relative_path(version: str, sha256: str, mime: str) -> str:
    if not is_supported_transform_version(version):
        raise ImageAssetError("图片资产转换版本不受支持")
    if not _SHA256_RE.fullmatch(str(sha256 or "")):
        raise ImageAssetError("图片资产哈希无效")
    extension = _OUTPUT_EXTENSIONS.get(mime)
    if extension is None:
        raise ImageAssetError("图片资产格式无效")
    return f"incoming/images/{version}/{sha256}{extension}"


def build_image_attribution_text(asset: Mapping[str, Any]) -> str:
    """稳定的图片归属文本；只使用可信 message_id/index。"""
    message_id = str(asset.get("message_id") or "")
    index = asset.get("index")
    return f"[图片所属消息 msg_id={message_id} index={index}]"


def build_image_content_blocks(
    text: str,
    refs: Sequence[Mapping[str, Any]],
    image_parts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """按同一顺序构造 text/归属/image 块，供预算视图与真实 wire 视图共用。"""
    if len(refs) != len(image_parts):
        raise ImageAssetError("图片块数量与资产引用不一致", code="image_projection_failed")
    blocks: list[dict[str, Any]] = [{"type": "text", "text": str(text)}]
    for ref, image in zip(refs, image_parts, strict=True):
        blocks.append(
            {"type": "text", "text": build_image_attribution_text(ref)}
        )
        blocks.append(dict(image))
    return blocks


def encode_ref_bytes_estimate(byte_size: int) -> int:
    """规范化字节 -> data URL/base64 + JSON 包装的保守字节估算。"""
    if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 0:
        raise ImageAssetError("图片字节数无效")
    return 4 * math.ceil(byte_size / 3) + _IMAGE_DATA_URL_WRAPPER_BYTES


def _validate_message_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ImageAssetError("图片资产消息归属无效")
    if len(value) > 256 or _CONTROL_CHARS_RE.search(value):
        raise ImageAssetError("图片资产消息归属无效")
    return value


def _version_side_limit(version: str) -> int:
    if version == TRANSFORM_VERSION_V1:
        # v1 未做固定缩放，旧资产仍可能有较大的合法边长。
        return MAX_IMAGE_PIXELS
    match = _V2_VERSION_RE.fullmatch(version)
    return int(match.group(1)) if match is not None else MAX_IMAGE_SIDE


def _validate_dimension(name: str, value: Any, *, hard_max: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ImageAssetError(f"图片资产{name}无效")
    if value > hard_max:
        raise ImageAssetError(f"图片资产{name}超过安全限制")
    return value


def validate_image_asset_ref(
    asset: Any,
    *,
    allowed_versions: frozenset[str] | None = None,
    limits: ImageLimits | None = None,
) -> dict[str, Any]:
    """严格校验一个持久化 asset ref，返回独立副本。

    - 只校验元数据，不读磁盘、不解码图片、不联网；
    - 任一字段非法即抛 ImageAssetError，不做“过滤坏项后继续”；
    - 单图尺寸/字节使用项目硬上限；当前 limits 的请求级限制在
      validate_declared_assets 中应用。
    """
    if not isinstance(asset, Mapping):
        raise ImageAssetError("图片资产引用无效")
    unknown = set(asset) - _ASSET_REF_KEYS
    if unknown:
        raise ImageAssetError("图片资产引用包含未知字段")
    ref = dict(asset)

    sha256 = ref.get("sha256")
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise ImageAssetError("图片资产哈希无效")
    mime = ref.get("mime")
    if not isinstance(mime, str) or mime not in SUPPORTED_OUTPUT_MIMES:
        raise ImageAssetError("图片资产格式无效")

    version = ref.get("transform_version")
    if not isinstance(version, str) or not is_supported_transform_version(version):
        raise ImageAssetError("图片资产转换版本不受支持")
    if allowed_versions is not None and version not in allowed_versions:
        raise ImageAssetError("图片资产转换版本不受支持")

    if ref.get("path") != asset_relative_path(version, sha256, mime):
        raise ImageAssetError("图片资产路径不是受管的内容寻址路径")

    side_limit = _version_side_limit(version)
    width = _validate_dimension("宽度", ref.get("width"), hard_max=side_limit)
    height = _validate_dimension("高度", ref.get("height"), hard_max=side_limit)
    if width * height > MAX_IMAGE_PIXELS:
        raise ImageAssetError("图片资产像素超过安全限制")

    byte_size = ref.get("byte_size")
    if (
        isinstance(byte_size, bool)
        or not isinstance(byte_size, int)
        or byte_size <= 0
        or byte_size > MAX_IMAGE_BYTES
    ):
        raise ImageAssetError("图片资产字节数无效或超过安全限制")

    index = ref.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ImageAssetError("图片资产消息索引无效")

    detail = ref.get("detail", "auto")
    if not isinstance(detail, str) or detail not in SUPPORTED_DETAILS:
        raise ImageAssetError("图片资产 detail 无效")

    message_id = _validate_message_id(ref.get("message_id", ""))
    ref["detail"] = detail
    ref["message_id"] = message_id
    return ref


def declared_image_assets(message: Mapping[str, Any]) -> list[Any] | None:
    """返回声明的原始资产列表。

    - metadata 没有 image_assets 键：None（旧文本历史）；
    - 空 list/tuple：[]（明确无图）；
    - 键存在但类型错误：ImageAssetError，不当作“没有图片”。
    """
    metadata = message.get("metadata")
    if not isinstance(metadata, Mapping) or "image_assets" not in metadata:
        return None
    assets = metadata.get("image_assets")
    if assets is None:
        raise ImageAssetError("图片资产列表无效")
    if not isinstance(assets, (list, tuple)):
        raise ImageAssetError("图片资产列表无效")
    return list(assets)


def has_declared_image_assets(messages: list[Mapping[str, Any]]) -> bool:
    """是否声明了非空图片资产（不验证每一项是否合法）。"""
    for message in messages:
        assets = declared_image_assets(message)
        if assets:
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
        raise ImageAssetError("图片资产消息来源列表无效")
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
    """从合并 record 的 metadata.messages 解析唯一原始消息归属。"""
    conversation = _origin_conversation(message)
    ref_message_id = str(ref.get("message_id") or "")
    if not entries:
        raise ImageAssetError("图片资产缺少可信原始消息来源")
    valid_entries = [entry for entry in entries if isinstance(entry, Mapping)]
    if len(valid_entries) != len(entries):
        raise ImageAssetError("图片资产消息来源列表无效")

    if ref_message_id:
        matches = [
            entry
            for entry in valid_entries
            if _entry_message_id(entry) == ref_message_id
        ]
        if len(matches) != 1:
            raise ImageAssetError(
                "图片资产消息归属不存在或不唯一",
                code="invalid_asset_reference",
            )
        entry = matches[0]
    else:
        # 旧 ref 可能没有 message_id；只有来源唯一时才允许绑定。
        if len(valid_entries) != 1:
            raise ImageAssetError(
                "图片资产消息归属为空且无法唯一绑定",
                code="invalid_asset_reference",
            )
        entry = valid_entries[0]
        if not _entry_message_id(entry):
            raise ImageAssetError(
                "图片资产消息归属为空且无法唯一绑定",
                code="invalid_asset_reference",
            )
    matched_message_id = _entry_message_id(entry)
    if not matched_message_id:
        raise ImageAssetError(
            "图片资产消息归属为空且无法唯一绑定",
            code="invalid_asset_reference",
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
    """没有 metadata.messages 时的保守兼容归属。

    - 所有 refs 共享一个非空 message_id：视为同一条旧消息；
    - 所有 refs 都没有 message_id：只绑定到当前顶层 record；
    - 多个不同 message_id 或空/非空混合：无法验证，拒绝。
    """
    conversation = _origin_conversation(message)
    message_ids = {str(ref.get("message_id") or "") for ref in refs}
    if len(message_ids) != 1:
        raise ImageAssetError(
            "图片资产缺少可信原始消息归属",
            code="invalid_asset_reference",
        )
    message_id = next(iter(message_ids))
    if message_id:
        return (
            conversation,
            _ORIGIN_KIND_LEGACY,
            message_id,
            None,
        )
    return (
        conversation,
        _ORIGIN_KIND_LEGACY_RECORD,
        message_index,
        None,
    )


def validate_declared_assets(
    messages: list[Mapping[str, Any]],
    *,
    limits: ImageLimits | None = None,
    allowed_versions: frozenset[str] | None = None,
    enforce_request_limits: bool = True,
) -> list[tuple[int, dict[str, Any]]]:
    """严格验证所有声明资产，返回 (message_index, ref) 顺序列表。

    图片数量限制按“可信原始入站消息”分组，而不是按模型可见的合并 user
    record 分组。合并 record 通过 metadata.messages + ref.message_id 解析归属；
    无 metadata.messages 的旧记录只允许单 message_id 或单 record 兼容绑定。

    - 任意一条非法：整条请求失败；
    - 同一原始消息的 index 不得重复；
    - 同一原始消息图片数 / 整请求数量受 limits 约束；
    - ``enforce_request_limits=False`` 只跳过整请求数量上限，原始消息上限与
      重复/归属校验仍严格执行。
    """
    limits = limits or ImageLimits()
    validated: list[tuple[int, dict[str, Any]]] = []
    origin_counts: dict[tuple[Any, ...], int] = {}
    seen_origin_indices: set[tuple[tuple[Any, ...], int]] = set()
    total = 0
    for message_index, message in enumerate(messages):
        assets = declared_image_assets(message)
        if not assets:
            continue
        refs = [
            validate_image_asset_ref(
                asset,
                allowed_versions=allowed_versions,
                limits=limits,
            )
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
                raise ImageAssetError(
                    "图片资产缺少可信原始消息归属",
                    code="invalid_asset_reference",
                )
            origin_index = (origin, ref["index"])
            if origin_index in seen_origin_indices:
                raise ImageAssetError(
                    "同一原始消息的图片资产消息索引重复",
                    code="invalid_asset_reference",
                )
            seen_origin_indices.add(origin_index)
            origin_count = origin_counts.get(origin, 0) + 1
            origin_counts[origin] = origin_count
            if origin_count > limits.max_images_per_message:
                raise ImageAssetError(
                    "同一原始消息图片数量超过限制",
                    code="image_request_count_exceeded",
                )
            validated.append((message_index, ref))
            total += 1
            if enforce_request_limits and total > limits.max_images_per_request:
                raise ImageAssetError(
                    "请求图片数量超过限制", code="image_request_count_exceeded"
                )
    return validated


__all__ = [
    "DEFAULT_IMAGE_TOKEN_RESERVE",
    "ImageAssetError",
    "ImageBudgetEstimate",
    "ImageBudgetProfile",
    "ImageBudgetUsage",
    "ImageLimits",
    "MAX_CACHE_BYTES",
    "MAX_ENCODED_IMAGE_BYTES",
    "MAX_IMAGE_BYTES",
    "MAX_IMAGE_PIXELS",
    "MAX_IMAGE_SIDE",
    "MAX_IMAGES_PER_MESSAGE",
    "MAX_IMAGES_PER_REQUEST",
    "MAX_IMAGE_TOKEN_RESERVE",
    "MAX_REQUEST_BODY_BYTES",
    "MIN_IMAGE_TOKEN_RESERVE",
    "SUPPORTED_DETAILS",
    "SUPPORTED_OUTPUT_MIMES",
    "TRANSFORM_VERSION_V1",
    "TRANSFORM_VERSION_V2",
    "asset_relative_path",
    "build_image_attribution_text",
    "build_image_content_blocks",
    "declared_image_assets",
    "encode_ref_bytes_estimate",
    "has_declared_image_assets",
    "is_supported_transform_version",
    "supported_transform_versions",
    "transform_version_for_limits",
    "validate_declared_assets",
    "validate_image_asset_ref",
]
