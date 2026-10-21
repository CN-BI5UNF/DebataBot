"""图片 token/字节预算的纯计算层。

不读文件、不解码 base64、不联网。当前只实现显式的 ``reserve_v1``：
每图使用可配置预留 token，标记 ``exact=False``，绝不冒充供应商计费上界。

预算视图与真实 wire 视图共用 ``build_image_content_blocks`` 的正文/归属/图片
顺序；预算视图中的图片仅替换为轻量占位，绝不发送给 provider。
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

try:  # 项目依赖；隔离测试缺失时退回标准库
    import orjson as _orjson
except Exception:  # pragma: no cover - 依赖缺失兜底
    _orjson = None

from utils.image_contracts import (
    DEFAULT_IMAGE_TOKEN_RESERVE,
    MAX_IMAGE_TOKEN_RESERVE,
    MIN_IMAGE_TOKEN_RESERVE,
    ImageAssetError,
    ImageBudgetEstimate,
    ImageBudgetProfile,
    ImageLimits,
    build_image_content_blocks,
    declared_image_assets,
    encode_ref_bytes_estimate,
    validate_declared_assets,
)

RESERVE_PROFILE_NAME = "reserve_v1"
_BUDGET_IMAGE_URL = "<managed-image>"
_MANAGED_DATA_URL_PREFIXES = (
    "data:image/jpeg;base64,",
    "data:image/png;base64,",
)


def _validate_reserve(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("image_token_reserve must be an integer")
    if not MIN_IMAGE_TOKEN_RESERVE <= value <= MAX_IMAGE_TOKEN_RESERVE:
        raise ValueError(
            "image_token_reserve must be between "
            f"{MIN_IMAGE_TOKEN_RESERVE} and {MAX_IMAGE_TOKEN_RESERVE}"
        )
    return value


def resolve_image_budget_profile(
    *,
    enabled: bool,
    profile: str = "auto",
    reserve_tokens: int = DEFAULT_IMAGE_TOKEN_RESERVE,
) -> ImageBudgetProfile | None:
    """解析图片预算 profile。

    - 主模型原生关闭：None；
    - ``auto`` / ``reserve_v1``：当前均使用显式 reserve；
    - 未知 profile 明确报错，不静默退化成某个固定上界。
    """
    if not enabled:
        return None
    selected = str(profile or "auto").strip().lower()
    if selected not in {"auto", RESERVE_PROFILE_NAME}:
        raise ValueError(f"unknown image budget profile: {profile!r}")
    reserve = _validate_reserve(reserve_tokens)
    return ImageBudgetProfile(
        name=RESERVE_PROFILE_NAME,
        per_image_tokens=reserve,
        basis="configured_reserve",
        exact=False,
    )


def estimate_refs_budget(
    refs: Iterable[Mapping[str, Any]],
    *,
    profile: ImageBudgetProfile | None,
) -> ImageBudgetEstimate:
    """按可信 refs 估算图片数量和编码字节；不读取图片内容。"""
    ref_list = list(refs)
    count = len(ref_list)
    encoded_bytes = sum(
        encode_ref_bytes_estimate(int(ref.get("byte_size") or 0))
        for ref in ref_list
    )
    if profile is None or count == 0:
        return ImageBudgetEstimate(
            image_count=count,
            encoded_image_bytes=encoded_bytes,
            image_tokens=0,
            basis="text_only",
            exact=True,
        )
    return ImageBudgetEstimate(
        image_count=count,
        encoded_image_bytes=encoded_bytes,
        image_tokens=count * profile.per_image_tokens,
        basis=profile.basis,
        exact=profile.exact,
    )


def estimate_request_body_bytes(
    *,
    text_serialized_bytes: int,
    encoded_image_bytes: int,
) -> int:
    """保守估算完整请求体；是否超限由调用方按项目/provider 上限判断。"""
    if text_serialized_bytes < 0 or encoded_image_bytes < 0:
        raise ValueError("byte counts must be non-negative")
    return text_serialized_bytes + encoded_image_bytes


def group_validated_refs(
    messages: Sequence[Mapping[str, Any]],
    *,
    limits: ImageLimits | None = None,
    allowed_versions: frozenset[str] | None = None,
    enforce_request_limits: bool = False,
) -> dict[int, list[dict[str, Any]]]:
    """严格校验并按消息分组 refs，保持消息内原始顺序。"""
    limits = limits or ImageLimits()
    validated = validate_declared_assets(
        list(messages),
        limits=limits,
        allowed_versions=allowed_versions,
        enforce_request_limits=enforce_request_limits,
    )
    grouped: dict[int, list[dict[str, Any]]] = {}
    for message_index, ref in validated:
        grouped.setdefault(message_index, []).append(ref)
    return grouped


def build_budget_messages(
    messages: Sequence[Mapping[str, Any]],
    refs_by_message: Mapping[int, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """从同一份文本+refs 源构造轻量预算视图。

    - 只复制会被修改的 content/metadata，绝不回写 history；
    - 正文、归属文本、图片顺序与真实 wire 视图一致；
    - 图片块使用 ``<managed-image>`` 占位，绝不包含 base64；
    - 源消息若已经实体化图片块，明确失败，避免二次投影/重复计费。
    """
    budget_messages: list[dict[str, Any]] = []
    for message_index, message in enumerate(messages):
        refs = list(refs_by_message.get(message_index, ()))
        content = message.get("content")
        if content is not None and _contains_image_block(content):
            raise ImageAssetError(
                "预算视图不能在图片已实体化后再次构造",
                code="image_projection_failed",
            )
        if not refs:
            budget_messages.append(dict(message))
            continue
        if message.get("role") != "user":
            raise ImageAssetError(
                "图片资产只能绑定真实用户消息",
                code="invalid_asset_reference",
            )
        if not isinstance(content, str):
            raise ImageAssetError(
                "图片历史消息正文格式无效", code="image_projection_failed"
            )
        metadata = message.get("metadata")
        if not isinstance(metadata, Mapping):
            raise ImageAssetError(
                "图片资产缺少消息metadata", code="invalid_asset_reference"
            )
        image_parts = [
            {
                "type": "image_url",
                "image_url": {
                    "url": _BUDGET_IMAGE_URL,
                    "detail": str(ref.get("detail") or "auto"),
                },
            }
            for ref in refs
        ]
        projected = dict(message)
        projected["metadata"] = copy.deepcopy(dict(metadata))
        projected["content"] = build_image_content_blocks(content, refs, image_parts)
        budget_messages.append(projected)
    return budget_messages


def wire_image_metrics(
    messages: Sequence[Mapping[str, Any]],
) -> tuple[int, int]:
    """统计真实 wire 图片数量和编码字节，并约束为受管 data URL。

    返回 ``(image_count, encoded_image_bytes)``。metadata 与图片块数量不一致、
    非 data URL、未知 MIME 或包含非 ASCII 数据时明确失败。
    """
    total_count = 0
    total_bytes = 0
    for message in messages:
        refs = declared_image_assets(message) or []
        content = message.get("content")
        block_count = 0
        if isinstance(content, list):
            for block in content:
                if not _is_image_block(block):
                    continue
                block_count += 1
                url = _image_block_url(block)
                if not isinstance(url, str) or not url.startswith(
                    _MANAGED_DATA_URL_PREFIXES
                ):
                    raise ImageAssetError(
                        "图片块不是受管data URL",
                        code="image_projection_failed",
                    )
                if not url.isascii():
                    raise ImageAssetError(
                        "图片块编码无效", code="image_projection_failed"
                    )
                total_bytes += len(url)
        if refs and block_count != len(refs):
            raise ImageAssetError(
                "图片块数量与资产引用不一致",
                code="image_projection_failed",
            )
        total_count += block_count
    return total_count, total_bytes


def estimate_wire_request_body_bytes(
    messages: Sequence[Mapping[str, Any]],
    tools_schema: Sequence[Mapping[str, Any]] | None,
    *,
    encoded_image_bytes: int,
) -> int:
    """估算完整请求体：redacted messages+tools 的 JSON 字节 + 实际图片编码。"""
    text_bytes = len(
        _canonical_json(
            {
                "messages": _redacted_messages(messages),
                "tools": list(tools_schema or ()),
            }
        ).encode("utf-8")
    )
    return estimate_request_body_bytes(
        text_serialized_bytes=text_bytes,
        encoded_image_bytes=encoded_image_bytes,
    )


def request_body_limit(
    limits: ImageLimits,
    *,
    provider_limit_bytes: int | None = None,
) -> int:
    """项目上限与已知 provider 更小上限取最小值。"""
    limit = limits.max_request_body_bytes
    if (
        isinstance(provider_limit_bytes, int)
        and not isinstance(provider_limit_bytes, bool)
        and provider_limit_bytes > 0
    ):
        limit = min(limit, provider_limit_bytes)
    return limit


def assert_wire_request_fits(
    messages: Sequence[Mapping[str, Any]],
    *,
    tools_schema: Sequence[Mapping[str, Any]] | None = None,
    limits: ImageLimits | None = None,
    provider_limit_bytes: int | None = None,
) -> tuple[int, int]:
    """provider 调用前最后护栏：只拦截含图请求的图片数量/结构/大小。

    返回 ``(image_count, encoded_image_bytes)``。纯文字请求直接放行，避免把
    本轮改动扩成所有文本预算策略重写。
    """
    limits = limits or ImageLimits()
    has_refs = any(declared_image_assets(message) for message in messages)
    if not has_refs and not any(
        _contains_image_block(message.get("content")) for message in messages
    ):
        return 0, 0
    validate_declared_assets(
        list(messages),
        limits=limits,
        enforce_request_limits=True,
    )
    count, encoded_bytes = wire_image_metrics(messages)
    if count > limits.max_images_per_request:
        raise ImageAssetError(
            "请求图片数量超过限制", code="image_request_count_exceeded"
        )
    if encoded_bytes > limits.max_encoded_image_bytes:
        raise ImageAssetError(
            "请求图片编码字节超过限制",
            code="image_request_bytes_exceeded",
        )
    body_limit = request_body_limit(limits, provider_limit_bytes=provider_limit_bytes)
    body_bytes = estimate_wire_request_body_bytes(
        messages,
        tools_schema,
        encoded_image_bytes=encoded_bytes,
    )
    if body_bytes > body_limit:
        raise ImageAssetError(
            "完整请求体超过限制", code="provider_request_body_exceeded"
        )
    return count, encoded_bytes


def _is_image_block(block: Any) -> bool:
    return isinstance(block, Mapping) and str(block.get("type") or "") in {
        "image_url",
        "image",
    }


def _contains_image_block(content: Any) -> bool:
    return isinstance(content, list) and any(
        _is_image_block(block) for block in content
    )


def _image_block_url(block: Mapping[str, Any]) -> Any:
    if str(block.get("type") or "") == "image_url":
        value = block.get("image_url")
        return value.get("url") if isinstance(value, Mapping) else None
    return block.get("url")


def _redacted_messages(
    messages: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """替换 data URL 后再序列化，避免为预算复制第二份大 base64。"""
    redacted: list[dict[str, Any]] = []
    for message in messages:
        copied = dict(message)
        content = message.get("content")
        if isinstance(content, list):
            blocks: list[Any] = []
            for block in content:
                if not _is_image_block(block):
                    blocks.append(block)
                    continue
                if str(block.get("type") or "") == "image_url":
                    value = block.get("image_url")
                    blocks.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": _BUDGET_IMAGE_URL,
                                "detail": (
                                    value.get("detail")
                                    if isinstance(value, Mapping)
                                    else "auto"
                                ),
                            },
                        }
                    )
                else:
                    blocks.append({"type": "image", "url": _BUDGET_IMAGE_URL})
            copied["content"] = blocks
        redacted.append(copied)
    return redacted


def _canonical_json(value: Any) -> str:
    if _orjson is not None:
        return _orjson.dumps(
            value,
            option=_orjson.OPT_SORT_KEYS,
            default=str,
        ).decode("utf-8")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


__all__ = [
    "RESERVE_PROFILE_NAME",
    "assert_wire_request_fits",
    "build_budget_messages",
    "estimate_refs_budget",
    "estimate_request_body_bytes",
    "estimate_wire_request_body_bytes",
    "group_validated_refs",
    "request_body_limit",
    "resolve_image_budget_profile",
    "wire_image_metrics",
]
