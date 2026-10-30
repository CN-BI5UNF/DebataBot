"""视频 token/字节预算与 wire 护栏的纯计算层。

不读文件、不解码 base64、不联网。视频成本使用显式 ``reserve_v1``：
每个视频使用可配置预留 token，标记 ``exact=False``，绝不冒充供应商账单。

预算视图中的视频块只含 ``<managed-video>`` 占位；真实 wire 视图只接受
受管的 ``data:video/...;base64,`` 数据 URL。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from utils.image_budget import estimate_wire_request_body_bytes
from utils.video_contracts import (
    DEFAULT_VIDEO_TOKEN_RESERVE,
    MAX_VIDEO_TOKEN_RESERVE,
    MIN_VIDEO_TOKEN_RESERVE,
    SUPPORTED_VIDEO_MIMES,
    VideoAssetError,
    VideoBudgetEstimate,
    VideoBudgetProfile,
    VideoLimits,
    declared_video_assets,
    encode_video_ref_bytes_estimate,
    validate_declared_video_assets,
)

RESERVE_PROFILE_NAME = "reserve_v1"
_VIDEO_DATA_URL_PREFIXES = tuple(
    f"data:{mime};base64," for mime in sorted(SUPPORTED_VIDEO_MIMES)
)


def _validate_reserve(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("video_token_reserve must be an integer")
    if not MIN_VIDEO_TOKEN_RESERVE <= value <= MAX_VIDEO_TOKEN_RESERVE:
        raise ValueError(
            "video_token_reserve must be between "
            f"{MIN_VIDEO_TOKEN_RESERVE} and {MAX_VIDEO_TOKEN_RESERVE}"
        )
    return value


def resolve_video_budget_profile(
    *,
    enabled: bool,
    reserve_tokens: int = DEFAULT_VIDEO_TOKEN_RESERVE,
) -> VideoBudgetProfile | None:
    """解析视频预算 profile；当前只有显式 reserve。"""
    if not enabled:
        return None
    reserve = _validate_reserve(reserve_tokens)
    return VideoBudgetProfile(
        name=RESERVE_PROFILE_NAME,
        per_video_tokens=reserve,
        basis="configured_reserve",
        exact=False,
    )


def estimate_video_refs_budget(
    refs: Iterable[Mapping[str, Any]],
    *,
    profile: VideoBudgetProfile | None,
) -> VideoBudgetEstimate:
    """按可信 refs 估算视频数量和编码字节；不读取视频内容。"""
    ref_list = list(refs)
    count = len(ref_list)
    encoded_bytes = sum(
        encode_video_ref_bytes_estimate(int(ref.get("byte_size") or 0))
        for ref in ref_list
    )
    if profile is None or count == 0:
        return VideoBudgetEstimate(
            video_count=count,
            encoded_video_bytes=encoded_bytes,
            video_tokens=0,
            basis="text_only",
            exact=True,
        )
    return VideoBudgetEstimate(
        video_count=count,
        encoded_video_bytes=encoded_bytes,
        video_tokens=count * profile.per_video_tokens,
        basis=profile.basis,
        exact=profile.exact,
    )


def count_video_inputs(messages: Sequence[Mapping[str, Any]]) -> int:
    """统计已声明/已实体化的视频数量，用于工具循环内的 token 预留。"""
    total = 0
    for message in messages:
        refs = declared_video_assets(message) or []
        content = message.get("content")
        block_count = (
            sum(1 for block in content if _is_video_block(block))
            if isinstance(content, list)
            else 0
        )
        total += max(len(refs), block_count)
    return total


def has_video_inputs(messages: Sequence[Mapping[str, Any]]) -> bool:
    """是否含声明视频或已实体化视频块（不验证合法性）。"""
    for message in messages:
        if declared_video_assets(message):
            return True
        content = message.get("content")
        if isinstance(content, list) and any(
            _is_video_block(block) for block in content
        ):
            return True
    return False


def wire_video_metrics(
    messages: Sequence[Mapping[str, Any]],
) -> tuple[int, int]:
    """统计真实 wire 视频数量和编码字节，并约束为受管 data URL。"""
    total_count = 0
    total_bytes = 0
    for message in messages:
        refs = declared_video_assets(message) or []
        content = message.get("content")
        block_count = 0
        if isinstance(content, list):
            for block in content:
                if not _is_video_block(block):
                    continue
                block_count += 1
                url = _video_block_url(block)
                if not isinstance(url, str) or not url.startswith(
                    _VIDEO_DATA_URL_PREFIXES
                ):
                    raise VideoAssetError(
                        "视频块不是受管data URL",
                        code="video_projection_failed",
                    )
                if not url.isascii():
                    raise VideoAssetError(
                        "视频块编码无效", code="video_projection_failed"
                    )
                total_bytes += len(url)
        if refs and block_count != len(refs):
            raise VideoAssetError(
                "视频块数量与资产引用不一致",
                code="video_projection_failed",
            )
        total_count += block_count
    return total_count, total_bytes


def request_body_limit(
    limits: VideoLimits,
    *,
    provider_limit_bytes: int | None = None,
) -> int:
    limit = limits.max_request_body_bytes
    if (
        isinstance(provider_limit_bytes, int)
        and not isinstance(provider_limit_bytes, bool)
        and provider_limit_bytes > 0
    ):
        limit = min(limit, provider_limit_bytes)
    return limit


def assert_wire_video_request_fits(
    messages: Sequence[Mapping[str, Any]],
    *,
    tools_schema: Sequence[Mapping[str, Any]] | None = None,
    limits: VideoLimits | None = None,
    provider_limit_bytes: int | None = None,
    encoded_image_bytes: int = 0,
) -> tuple[int, int]:
    """provider 调用前视频护栏；纯文字请求直接放行。

    返回 ``(video_count, encoded_video_bytes)``。``encoded_image_bytes`` 让
    图文/视频混合请求仍按完整请求体判断上限。
    """
    limits = limits or VideoLimits()
    if not has_video_inputs(messages):
        return 0, 0
    validate_declared_video_assets(
        list(messages),
        limits=limits,
        enforce_request_limits=True,
    )
    count, encoded_bytes = wire_video_metrics(messages)
    if count > limits.max_videos_per_request:
        raise VideoAssetError(
            "请求视频数量超过限制", code="video_request_count_exceeded"
        )
    if encoded_bytes > limits.max_encoded_video_bytes:
        raise VideoAssetError(
            "请求视频编码字节超过限制",
            code="video_request_bytes_exceeded",
        )
    body_limit = request_body_limit(
        limits, provider_limit_bytes=provider_limit_bytes
    )
    body_bytes = estimate_wire_request_body_bytes(
        messages,
        tools_schema,
        encoded_image_bytes=encoded_image_bytes,
        encoded_video_bytes=encoded_bytes,
    )
    if body_bytes > body_limit:
        raise VideoAssetError(
            "完整请求体超过限制", code="provider_request_body_exceeded"
        )
    return count, encoded_bytes


def _is_video_block(block: Any) -> bool:
    return isinstance(block, Mapping) and str(block.get("type") or "") == "video_url"


def _video_block_url(block: Mapping[str, Any]) -> Any:
    value = block.get("video_url")
    return value.get("url") if isinstance(value, Mapping) else None


__all__ = [
    "RESERVE_PROFILE_NAME",
    "assert_wire_video_request_fits",
    "count_video_inputs",
    "estimate_video_refs_budget",
    "has_video_inputs",
    "request_body_limit",
    "resolve_video_budget_profile",
    "wire_video_metrics",
]
