"""Token 预算估算工具。

热路径只做本地估算，不调用模型。优先使用 tiktoken；不可用时退回粗略字符估算。

口径说明：
    - 文本 token 数来自 tiktoken（或字符兜底），再乘以按 provider/model 持久化的
      校准系数。校准样本来自 provider 返回的真实 ``prompt_tokens``。
    - **raw token 缓存**是进程级、有界、按内容寻址的 LRU：键包含 model、实际
      tokenizer 身份、UTF-8 字节长度和内容 SHA-256；缓存只保存 raw token 整数，
      不保存原文、messages 或 schema 对象。相同内容的新对象/新 estimator 也能命中。
    - 校准系数不进入 raw 缓存：同一 raw 值在系数变化后立刻得到新的最终估算。
    - messages 按 provider 实际发送口径（``normalize_messages``）序列化为规范 JSON，
      图片块的数据 URL/base64 不参与文字 token 计算；图片成本单独估算。
    - 图片 token 使用显式 ``reserve_v1`` 预留量（默认 8192/图），只用于本地预算，
      不代表供应商账单或计费上界。图片成本不乘纯文字校准系数。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from utils.image_contracts import (
    DEFAULT_IMAGE_TOKEN_RESERVE,
    ImageBudgetProfile,
)

try:  # orjson 是项目依赖；测试环境缺失时退回标准库
    import orjson as _orjson
except Exception:  # pragma: no cover - 依赖缺失兜底
    _orjson = None


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


# 单条消息的角色/分隔符/协议开销。OpenAI 兼容协议通常按 3-4 token/消息计。
_MESSAGE_OVERHEAD_TOKENS = 4
# 单个工具定义的协议开销（name/type/parameters 包装）。
_TOOL_OVERHEAD_TOKENS = 8

# 进程级 raw-token 缓存。只保存整数，不保存原文。
_RAW_TEXT_CACHE_MAXSIZE = 2048
_RAW_TEXT_CACHE: OrderedDict[tuple[str, str, int, str], int] = OrderedDict()
_RAW_TEXT_CACHE_LOCK = threading.Lock()
_RAW_TEXT_CACHE_HITS = 0
_RAW_TEXT_CACHE_MISSES = 0

# 没有已核实供应商公式时使用的显式资源预留。它既不是精确计费，也不是上界。
_DEFAULT_IMAGE_BUDGET_PROFILE = ImageBudgetProfile(
    name="reserve_v1",
    per_image_tokens=DEFAULT_IMAGE_TOKEN_RESERVE,
    basis="configured_reserve",
    exact=False,
)

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_IMAGE_MIME_PREFIX = "image/"


@dataclass(slots=True)
class PromptTokenParts:
    """一次 prompt 估算的组成部分。

    ``message_tokens_raw``/``tool_tokens_raw``/``overhead_tokens`` 是校准前的
    文字/schema raw token；``image_tokens`` 是独立的保守图片预算，不参与文字
    校准。旧的三字段构造继续可用，``image_tokens`` 默认为 0。
    """

    message_tokens_raw: int
    tool_tokens_raw: int
    overhead_tokens: int
    image_tokens: int = 0

    @property
    def raw_total(self) -> int:
        return max(
            1,
            self.message_tokens_raw
            + self.tool_tokens_raw
            + self.overhead_tokens
            + self.image_tokens,
        )


@dataclass(slots=True)
class TokenBudget:
    max_context_tokens: int
    reserve_output_tokens: int
    memory_token_budget: int
    summary_token_budget: int

    @property
    def total_input_budget(self) -> int:
        # 窗口装不下输出预留时返回 0，由上层明确报错；不能用 1024 伪造空间。
        return max(0, self.max_context_tokens - self.reserve_output_tokens)


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """一次请求预算的统一只读快照。

    只做算术与校验，不读取数据库、不调用模型。``input_budget`` 是扣除实际
    输出预留和安全余量后的净输入预算；任何一项不合法时 ``errors`` 非空，
    调用方应拒绝发送而不是把预算钳成一个小正数。
    """

    window_requested: int
    window_model: int | None
    window_effective: int
    window_source: str
    model_limit_verified: bool
    output_reserve: int
    safety_margin: int
    input_budget: int
    policy_version: int
    threshold_basis: str
    soft_trigger: int
    target_after: int
    retry_target: int
    urgent_trigger: int
    errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_requested": self.window_requested,
            "window_model": self.window_model,
            "window_effective": self.window_effective,
            "window_source": self.window_source,
            "model_limit_verified": self.model_limit_verified,
            "output_reserve": self.output_reserve,
            "safety_margin": self.safety_margin,
            "input_budget": self.input_budget,
            "policy_version": self.policy_version,
            "threshold_basis": self.threshold_basis,
            "soft_trigger": self.soft_trigger,
            "target_after": self.target_after,
            "retry_target": self.retry_target,
            "urgent_trigger": self.urgent_trigger,
            "valid": self.valid,
            "errors": list(self.errors),
        }


def build_budget_snapshot(
    *,
    window_requested: int,
    model_window: int | None = None,
    model_limit_verified: bool = False,
    window_source: str = "configured",
    output_reserve: int = 8192,
    actual_max_tokens: int | None = None,
    safety_margin_percent: int = 8,
    safety_margin_min_tokens: int = 1024,
    policy_version: int = 1,
    soft_percent: int = 50,
    target_percent: int = 30,
    retry_percent: int = 20,
    urgent_percent: int = 85,
) -> BudgetSnapshot:
    """构造并校验统一预算快照（纯函数）。"""

    def _int(name: str, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        return value

    errors: list[str] = []
    requested = _int("window_requested", window_requested)
    if requested <= 0:
        errors.append("invalid_window_requested")
        requested = max(0, requested)
    model_limit = None
    if model_window is not None:
        model_limit = _int("model_window", model_window)
        if model_limit <= 0:
            errors.append("invalid_model_window")
            model_limit = None
    effective = requested
    if model_limit is not None and model_limit > 0:
        effective = min(effective, model_limit)
    if model_limit is not None and model_limit < requested:
        window_source = "model_limit"
    reserve = max(0, _int("output_reserve", output_reserve))
    if actual_max_tokens is not None:
        reserve = max(reserve, max(0, _int("actual_max_tokens", actual_max_tokens)))
    margin_percent = max(0, min(50, _int("safety_margin_percent", safety_margin_percent)))
    margin_min = max(0, _int("safety_margin_min_tokens", safety_margin_min_tokens))
    safety = max(margin_min, (effective * margin_percent + 99) // 100)
    if effective <= 0:
        errors.append("invalid_effective_window")
    if reserve <= 0:
        errors.append("invalid_output_reserve")
    if reserve >= effective:
        errors.append("output_reserve_exceeds_window")
    if reserve + safety >= effective:
        errors.append("safety_margin_exceeds_window")
    input_budget = max(0, effective - reserve - safety)
    if effective > 0 and input_budget <= 0:
        errors.append("input_budget_not_positive")

    policy = 2 if int(policy_version) == 2 else 1
    base = input_budget if policy == 2 else max(0, effective)
    basis = "full_input_v2" if policy == 2 else "legacy_window_v1"
    soft = base * max(1, min(100, _int("soft_percent", soft_percent))) // 100
    target = base * max(1, min(100, _int("target_percent", target_percent))) // 100
    retry = base * max(1, min(100, _int("retry_percent", retry_percent))) // 100
    urgent = base * max(1, min(100, _int("urgent_percent", urgent_percent))) // 100
    if not (0 <= retry < target < soft < urgent <= max(1, base)):
        errors.append("invalid_threshold_order")
    return BudgetSnapshot(
        window_requested=requested,
        window_model=model_limit,
        window_effective=effective,
        window_source=window_source,
        model_limit_verified=bool(model_limit_verified),
        output_reserve=reserve,
        safety_margin=safety,
        input_budget=input_budget,
        policy_version=policy,
        threshold_basis=basis,
        soft_trigger=soft,
        target_after=target,
        retry_target=retry,
        urgent_trigger=urgent,
        errors=tuple(dict.fromkeys(errors)),
    )


class TokenEstimator:
    """带简单校准系数的 token 估算器。

    ``calib_ratio`` 会乘到文字/schema 的 raw token 结果上，但**不会**乘到图片
    预算上。校准应该只反映 tokenizer 与 provider 实际计费的差异。

    ``include_image_assets=True`` 时，history 记录中可信的
    ``metadata.image_assets`` 也会计入预算；实际发送的 image content block
    无论该开关如何都始终计图。

    ``image_budget_profile`` 为 ``None`` 时，实际图片块仍按默认
    ``reserve_v1`` 计图；history 字符串里的 refs 仅在
    ``include_image_assets=True`` 时计图。
    """

    def __init__(
        self,
        model: str = "",
        calib_ratio: float = 1.0,
        *,
        provider: str = "",
        provider_overhead_tokens: int = 0,
        include_image_assets: bool = False,
        image_budget_profile: ImageBudgetProfile | None = None,
    ) -> None:
        self.model = model
        self.provider = provider
        self.calib_ratio = max(0.05, float(calib_ratio or 1.0))
        self.provider_overhead_tokens = max(0, int(provider_overhead_tokens))
        self.include_image_assets = bool(include_image_assets)
        self.image_budget_profile = image_budget_profile
        self._effective_image_profile = (
            image_budget_profile or _DEFAULT_IMAGE_BUDGET_PROFILE
        )

    # ------------------------------------------------------------------
    # 文本
    # ------------------------------------------------------------------
    def estimate_text(self, text: str) -> int:
        raw = raw_text_tokens(text, self.model)
        return max(1, int(round(raw * self.calib_ratio)))

    def raw_text_tokens(self, text: str) -> int:
        return raw_text_tokens(text, self.model)

    # ------------------------------------------------------------------
    # messages / tools / prompt
    # ------------------------------------------------------------------
    def raw_message_parts(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """返回 ``(文字/schema raw token, 图片 token)``。

        图片块中的 data URL/base64 不进入文字 token 计算；history 里的
        metadata 图片引用只在 ``include_image_assets=True`` 时计图。
        """
        pairs = _normalized_message_pairs(messages)
        text_total = 0
        image_total = 0
        for original, normalized in pairs:
            text_total += _MESSAGE_OVERHEAD_TOKENS
            content = normalized.get("content")
            if isinstance(content, list):
                normalized = dict(normalized)
                normalized["content"] = _sanitize_content_blocks(content)
            text_total += raw_text_tokens(_canonical_json(normalized), self.model)
            image_total += _message_image_tokens(
                original,
                content,
                include_history_assets=self.include_image_assets,
                profile=self._effective_image_profile,
            )
        return max(1, text_total) if pairs else 0, image_total

    def raw_message_tokens(self, messages: list[dict[str, Any]]) -> int:
        """只返回文字/schema 部分的 raw token（不含图片）。"""
        text_raw, _ = self.raw_message_parts(messages)
        return text_raw

    def image_tokens(self, messages: list[dict[str, Any]]) -> int:
        """返回 messages 的图片资源预留 token（非供应商计费真值）。"""
        _, image_raw = self.raw_message_parts(messages)
        return image_raw

    def estimate_messages(self, messages: list[dict[str, Any]]) -> int:
        text_raw, image_raw = self.raw_message_parts(messages)
        text_estimate = int(round(text_raw * self.calib_ratio))
        # 图片预算不乘文字校准系数；空 messages 保持旧的最小 1。
        return max(1, text_estimate + image_raw)

    def estimate_tools_schema(self, tools_schema: list[dict[str, Any]] | None) -> int:
        raw = self.raw_tools_tokens(tools_schema)
        return max(1, int(round(raw * self.calib_ratio))) if raw > 0 else 0

    def raw_tools_tokens(self, tools_schema: list[dict[str, Any]] | None) -> int:
        if not tools_schema:
            return 0
        # 不做 id+len 快捷命中：内容相同的新 list 走共享 raw 缓存，
        # 同长度但内容变化的 schema 会因内容哈希不同而 miss。
        raw = raw_text_tokens(_canonical_json(tools_schema), self.model)
        raw += _TOOL_OVERHEAD_TOKENS * len(tools_schema)
        return raw

    def estimate_prompt_parts(
        self,
        messages: list[dict[str, Any]],
        tools_schema: list[dict[str, Any]] | None = None,
        *,
        provider_overhead_tokens: int | None = None,
    ) -> PromptTokenParts:
        overhead = (
            self.provider_overhead_tokens
            if provider_overhead_tokens is None
            else max(0, int(provider_overhead_tokens))
        )
        message_raw, image_raw = self.raw_message_parts(messages)
        return PromptTokenParts(
            message_tokens_raw=message_raw,
            tool_tokens_raw=self.raw_tools_tokens(tools_schema),
            overhead_tokens=overhead,
            image_tokens=image_raw,
        )

    def estimate_prompt(
        self,
        messages: list[dict[str, Any]],
        tools_schema: list[dict[str, Any]] | None = None,
        *,
        provider_overhead_tokens: int | None = None,
    ) -> int:
        parts = self.estimate_prompt_parts(
            messages,
            tools_schema,
            provider_overhead_tokens=provider_overhead_tokens,
        )
        text_raw = (
            parts.message_tokens_raw
            + parts.tool_tokens_raw
            + parts.overhead_tokens
        )
        # 公式：round(文字与 schema raw * calib) + 图片预算。
        return max(1, int(round(text_raw * self.calib_ratio)) + parts.image_tokens)

    # ------------------------------------------------------------------
    # 校准
    # ------------------------------------------------------------------
    def set_calibration(self, ratio: float) -> None:
        self.calib_ratio = min(2.0, max(0.35, float(ratio or 1.0)))

    def update_calibration(self, estimated: int, actual_prompt_tokens: int) -> None:
        """用一次真实调用的 (估算, 实际) 反馈修正系数。

        两道护栏，防止一次异常样本把系数带偏（估算器决定上下文预算与压缩时机，
        一旦跑飞会让压缩策略整体失准）：
          - 单次 ratio 先夹到 [0.3, 3.0]，挡掉口径错误或含工具循环累加值的样本；
          - 累计 calib_ratio 再夹到 [0.35, 2.0]。
        """
        if estimated <= 0 or actual_prompt_tokens <= 0:
            return
        ratio = actual_prompt_tokens / estimated
        ratio = min(3.0, max(0.3, ratio))
        blended = self.calib_ratio * 0.8 + ratio * 0.2
        self.calib_ratio = min(2.0, max(0.35, blended))


# ----------------------------------------------------------------------
# 共享 raw-token 内容缓存
# ----------------------------------------------------------------------
def _coarse_text_tokens(text: str) -> int:
    # 中文约 1.5 字/token，英文约 4 chars/token；用保守混合估算。
    return max(1, len(text) // 2)


def _tokenizer_backend_identity(encoder: Any) -> str:
    """稳定标识实际编码器/版本，避免 id+len 一类不可验证的身份捷径。"""
    name = str(getattr(encoder, "name", "") or type(encoder).__name__)
    try:
        import tiktoken

        version = str(getattr(tiktoken, "__version__", ""))
    except Exception:  # pragma: no cover - tiktoken 缺失时仍可用 fake encoder 测试
        version = ""
    return f"{name}@{version}"


def raw_text_tokens(text: str, model: str = "") -> int:
    """返回未经校准的 token 数（进程级内容缓存）。

    大文本只为求哈希临时编码一次，缓存不保留原文。tokenizer 加载/编码失败时
    使用粗估，但粗估**不会**写入正常编码器键，tokenizer 恢复后会重新计算。
    """
    global _RAW_TEXT_CACHE_HITS, _RAW_TEXT_CACHE_MISSES

    if not text:
        return 1
    try:
        encoder = _encoding_for_model(model)
        identity = _tokenizer_backend_identity(encoder)
    except Exception:
        return _coarse_text_tokens(text)

    data = text.encode("utf-8")
    cache_key = (
        model,
        identity,
        len(data),
        hashlib.sha256(data).hexdigest(),
    )
    # 持锁覆盖 get/compute/set，保证并发同键只冷算一次。此处是同步估算路径，
    # 没有 await；单次编码期间串行化是可接受的代价，换来确定性。
    with _RAW_TEXT_CACHE_LOCK:
        cached = _RAW_TEXT_CACHE.get(cache_key)
        if cached is not None:
            _RAW_TEXT_CACHE.move_to_end(cache_key)
            _RAW_TEXT_CACHE_HITS += 1
            return cached
        _RAW_TEXT_CACHE_MISSES += 1
        try:
            tokens = max(1, len(encoder.encode(text)))
        except Exception:
            # 编码失败不缓存粗估，避免临时故障永久污染预算。
            return _coarse_text_tokens(text)
        _RAW_TEXT_CACHE[cache_key] = tokens
        while len(_RAW_TEXT_CACHE) > _RAW_TEXT_CACHE_MAXSIZE:
            _RAW_TEXT_CACHE.popitem(last=False)
        return tokens


def raw_text_cache_info() -> dict[str, int]:
    """返回共享 raw-token 缓存的可观测计数。"""
    with _RAW_TEXT_CACHE_LOCK:
        return {
            "hits": _RAW_TEXT_CACHE_HITS,
            "misses": _RAW_TEXT_CACHE_MISSES,
            "maxsize": int(_RAW_TEXT_CACHE_MAXSIZE),
            "currsize": len(_RAW_TEXT_CACHE),
        }


def raw_text_cache_approx_bytes() -> int:
    """raw-token 缓存键值的近似内存占用；不读取任何原文。"""
    with _RAW_TEXT_CACHE_LOCK:
        return sum(
            sys.getsizeof(key) + sys.getsizeof(value)
            for key, value in _RAW_TEXT_CACHE.items()
        )


def clear_raw_text_cache() -> None:
    """清空共享 raw-token 缓存并重置计数（测试/运维观测用）。"""
    global _RAW_TEXT_CACHE_HITS, _RAW_TEXT_CACHE_MISSES

    with _RAW_TEXT_CACHE_LOCK:
        _RAW_TEXT_CACHE.clear()
        _RAW_TEXT_CACHE_HITS = 0
        _RAW_TEXT_CACHE_MISSES = 0


def warm_token_estimator(model: str = "") -> None:
    """预热并缓存 tokenizer，避免首条真实消息触发同步加载。"""
    _encoding_for_model(model)
    raw_text_tokens("warmup", model)


@lru_cache(maxsize=32)
def _encoding_for_model(model: str = ""):
    import tiktoken

    try:
        return tiktoken.encoding_for_model(model) if model else tiktoken.get_encoding("o200k_base")
    except Exception:
        try:
            return tiktoken.get_encoding("o200k_base")
        except Exception:
            return tiktoken.get_encoding("cl100k_base")


# ----------------------------------------------------------------------
# 图片预算（不读文件、不解码 base64、不访问网络）
# ----------------------------------------------------------------------
def has_image_inputs(messages: list[dict[str, Any]]) -> bool:
    """检测 messages 是否含实际图片块或可信 history 图片引用。"""
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list) and any(
            _is_image_block(block) for block in content
        ):
            return True
        if _valid_image_assets(message.get("metadata")):
            return True
    return False


def _is_image_block(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    return str(block.get("type") or "") in {"image_url", "image"}


def _is_video_block(block: Any) -> bool:
    return isinstance(block, dict) and str(block.get("type") or "") == "video_url"


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_image_assets(metadata: Any) -> list[dict[str, Any]]:
    """返回可信的图片 refs；字段类型不合法的一律忽略。

    token_budget 只做类型校验，不解析 path、不读文件。
    """
    if not isinstance(metadata, dict):
        return []
    raw_assets = metadata.get("image_assets")
    if not isinstance(raw_assets, list):
        return []
    valid: list[dict[str, Any]] = []
    for asset in raw_assets:
        if not isinstance(asset, dict):
            continue
        digest = asset.get("sha256")
        mime = asset.get("mime")
        path = asset.get("path")
        byte_size = asset.get("byte_size")
        if not (
            isinstance(digest, str)
            and _SHA256_RE.match(digest)
            and isinstance(mime, str)
            and mime.strip().lower().startswith(_IMAGE_MIME_PREFIX)
            and isinstance(path, str)
            and path.strip()
            and _positive_int(byte_size)
        ):
            continue
        valid.append(asset)
    return valid


def _image_block_detail(block: dict[str, Any]) -> str:
    if str(block.get("type") or "") == "image_url":
        value = block.get("image_url")
        raw_detail = value.get("detail") if isinstance(value, dict) else None
    else:
        raw_detail = block.get("detail")
    detail = str(raw_detail or "auto").strip().lower()
    return detail if detail in {"auto", "low", "high"} else "auto"


def _image_token_cost(profile: ImageBudgetProfile) -> int:
    """每张图使用显式预留量；不根据模型名猜供应商公式。"""
    return max(0, int(profile.per_image_tokens))


def _sanitize_content_blocks(content: list[Any]) -> list[Any]:
    """把图片/视频块替换成极小的占位，避免 base64/data URL 进入文字 tokenizer。"""
    sanitized: list[Any] = []
    for block in content:
        if _is_video_block(block):
            sanitized.append(
                {"type": "video_url", "video_url": {"url": "<video>"}}
            )
            continue
        if not _is_image_block(block):
            sanitized.append(block)
            continue
        detail = _image_block_detail(block)
        sanitized.append(
            {
                "type": "image_url",
                "image_url": {"url": "<image>", "detail": detail},
            }
        )
    return sanitized


def _message_image_tokens(
    original: dict[str, Any],
    content: Any,
    *,
    include_history_assets: bool,
    profile: ImageBudgetProfile,
) -> int:
    """计算单条消息的图片预算。

    content 为数组时只按实际图片块计数，metadata 只用于归属/对齐，不再重复
    计费。content 为字符串（history）时只在 ``include_history_assets=True``
    下按可信 refs 计数。每张图固定使用 profile 的预留 token。
    """
    assets = _valid_image_assets(original.get("metadata"))
    per_image = _image_token_cost(profile)
    if isinstance(content, list):
        return sum(1 for block in content if _is_image_block(block)) * per_image
    if include_history_assets:
        return len(assets) * per_image
    return 0


def _normalized_message_pairs(
    messages: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """把原始消息与 provider 规整后的消息按顺序配对（保留 metadata 来源）。"""
    originals = [
        message
        for message in messages
        if isinstance(message, dict) and "role" in message
    ]
    normalized = _normalize_messages_for_estimate(messages)
    return list(zip(originals, normalized, strict=False))


def _normalize_messages_for_estimate(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 provider 默认发送口径规整消息，避免把本地存储字段计入预算。"""
    from providers.base import normalize_messages

    return normalize_messages(messages)


__all__ = [
    "PromptTokenParts",
    "TokenBudget",
    "TokenEstimator",
    "clear_raw_text_cache",
    "has_image_inputs",
    "raw_text_cache_info",
    "raw_text_tokens",
    "warm_token_estimator",
]
