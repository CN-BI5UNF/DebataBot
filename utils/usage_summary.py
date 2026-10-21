"""模型 usage 聚合的轻量共享类型。

统计契约：
    - 全历史字段是所有明确纳入范围的记录总和，只用于总消耗展示；
    - 主缓存命中率只使用 ``usage_semantics_version == 2`` 的 V2 样本；
    - 缺失/0/1/无法解析为 legacy；大于 2 为 unknown，不自动并入 V2；
    - 无 V2 数据时主比例返回 ``None``，不能把 0 当作未命中率。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

UsageRange = Literal["today", "7d", "30d", "all"]


@dataclass(slots=True)
class UsageSummary:
    # ---- 全历史消耗（含 legacy/unknown，只用于总量展示）----
    request_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_miss_tokens: int = 0
    total_tokens: int = 0
    raw_cache_inconsistent_count: int = 0

    # ---- V2 当前口径 ----
    current_semantics_request_count: int = 0
    current_prompt_tokens: int = 0
    current_completion_tokens: int = 0
    current_reasoning_tokens: int = 0
    current_cached_tokens: int = 0
    current_cache_creation_tokens: int = 0
    current_cache_miss_tokens: int = 0
    current_total_tokens: int = 0
    current_raw_cache_inconsistent_count: int = 0
    current_estimated_prompt_tokens: int = 0
    current_estimate_actual_prompt_tokens: int = 0
    current_estimate_abs_error_tokens: int = 0
    current_estimate_samples: int = 0

    # ---- Legacy（缺失/0/1/不可解析）----
    legacy_request_count: int = 0
    legacy_prompt_tokens: int = 0
    legacy_completion_tokens: int = 0
    legacy_reasoning_tokens: int = 0
    legacy_cached_tokens: int = 0
    legacy_cache_creation_tokens: int = 0
    legacy_cache_miss_tokens: int = 0
    legacy_total_tokens: int = 0
    legacy_raw_cache_inconsistent_count: int = 0
    legacy_estimated_prompt_tokens: int = 0
    legacy_estimate_actual_prompt_tokens: int = 0
    legacy_estimate_abs_error_tokens: int = 0
    legacy_estimate_samples: int = 0

    # ---- Unknown / future（> 2）----
    unknown_semantics_request_count: int = 0
    unknown_semantics_prompt_tokens: int = 0
    unknown_semantics_completion_tokens: int = 0
    unknown_semantics_reasoning_tokens: int = 0
    unknown_semantics_cached_tokens: int = 0
    unknown_semantics_cache_creation_tokens: int = 0
    unknown_semantics_cache_miss_tokens: int = 0
    unknown_semantics_total_tokens: int = 0

    @property
    def has_current_semantics_data(self) -> bool:
        return (
            self.current_semantics_request_count > 0
            and self.current_prompt_tokens > 0
        )

    @property
    def cache_hit_rate(self) -> float | None:
        """V2-only 主缓存命中率；没有 V2 样本时为 None。"""
        return self.current_cache_hit_rate

    @property
    def current_cache_hit_rate(self) -> float | None:
        if not self.has_current_semantics_data:
            return None
        return max(
            0.0,
            min(
                1.0,
                self.current_cached_tokens / self.current_prompt_tokens,
            ),
        )

    @property
    def legacy_cache_hit_rate(self) -> None:
        """旧口径没有可与 V2 直接比较的比例。"""
        return None

    @property
    def uncached_prompt_tokens(self) -> int | None:
        """V2 未命中输入；没有 V2 样本时返回 None。"""
        return self.current_uncached_prompt_tokens

    @property
    def current_uncached_prompt_tokens(self) -> int | None:
        if not self.has_current_semantics_data:
            return None
        return max(0, self.current_prompt_tokens - self.current_cached_tokens)

    @property
    def estimate_error_rate(self) -> float:
        """全历史估算误差（兼容字段，调用方应同时检查 sample 数）。"""
        if self.estimate_samples <= 0 or self.estimate_actual_prompt_tokens <= 0:
            return 0.0
        return self.estimate_abs_error_tokens / self.estimate_actual_prompt_tokens

    @property
    def current_estimate_error_rate(self) -> float | None:
        if (
            self.current_estimate_samples <= 0
            or self.current_estimate_actual_prompt_tokens <= 0
        ):
            return None
        return (
            self.current_estimate_abs_error_tokens
            / self.current_estimate_actual_prompt_tokens
        )

    @property
    def legacy_estimate_error_rate(self) -> float | None:
        if (
            self.legacy_estimate_samples <= 0
            or self.legacy_estimate_actual_prompt_tokens <= 0
        ):
            return None
        return (
            self.legacy_estimate_abs_error_tokens
            / self.legacy_estimate_actual_prompt_tokens
        )

    # ---- 估算准确性（全历史兼容字段）----
    estimated_prompt_tokens: int = 0
    estimate_actual_prompt_tokens: int = 0
    estimate_abs_error_tokens: int = 0
    estimate_samples: int = 0


def _token_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def usage_semantics_bucket(record: dict) -> str:
    raw = record.get("usage_semantics_version")
    if raw is None:
        return "legacy"
    try:
        version = int(raw)
    except (TypeError, ValueError):
        return "legacy"
    if version == 2:
        return "current"
    if version > 2:
        return "unknown"
    return "legacy"


def _add_estimate(
    summary: UsageSummary,
    *,
    prefix: str,
    estimated: int,
    prompt: int,
) -> None:
    if estimated <= 0 or prompt <= 0:
        return
    setattr(
        summary,
        f"{prefix}estimated_prompt_tokens",
        getattr(summary, f"{prefix}estimated_prompt_tokens") + estimated,
    )
    setattr(
        summary,
        f"{prefix}estimate_actual_prompt_tokens",
        getattr(summary, f"{prefix}estimate_actual_prompt_tokens") + prompt,
    )
    setattr(
        summary,
        f"{prefix}estimate_abs_error_tokens",
        getattr(summary, f"{prefix}estimate_abs_error_tokens") + abs(estimated - prompt),
    )
    setattr(
        summary,
        f"{prefix}estimate_samples",
        getattr(summary, f"{prefix}estimate_samples") + 1,
    )


def add_record_to_summary(summary: UsageSummary, record: dict) -> None:
    """把一条 usage record 累加到全历史与对应口径 bucket。"""
    prompt = _token_int(record.get("prompt_tokens"))
    raw_cached = _token_int(record.get("cached_tokens"))
    cached = min(raw_cached, prompt)
    raw_creation = _token_int(record.get("cache_creation_tokens"))
    creation = min(raw_creation, prompt)
    completion = _token_int(record.get("completion_tokens"))
    reasoning = _token_int(record.get("reasoning_tokens"))
    cache_miss = _token_int(record.get("cache_miss_tokens"))
    total = _token_int(record.get("total_tokens")) or (prompt + completion)
    estimated = _token_int(record.get("estimated_prompt_tokens"))
    inconsistent = raw_cached > prompt or raw_creation > prompt

    summary.request_count += 1
    summary.prompt_tokens += prompt
    summary.completion_tokens += completion
    summary.reasoning_tokens += reasoning
    summary.cached_tokens += cached
    summary.cache_creation_tokens += creation
    summary.cache_miss_tokens += cache_miss
    summary.total_tokens += total
    if inconsistent:
        summary.raw_cache_inconsistent_count += 1
    _add_estimate(
        summary,
        prefix="",
        estimated=estimated,
        prompt=prompt,
    )

    bucket = usage_semantics_bucket(record)
    if bucket == "current":
        summary.current_semantics_request_count += 1
        summary.current_prompt_tokens += prompt
        summary.current_completion_tokens += completion
        summary.current_reasoning_tokens += reasoning
        summary.current_cached_tokens += cached
        summary.current_cache_creation_tokens += creation
        summary.current_cache_miss_tokens += cache_miss
        summary.current_total_tokens += total
        if inconsistent:
            summary.current_raw_cache_inconsistent_count += 1
        _add_estimate(
            summary,
            prefix="current_",
            estimated=estimated,
            prompt=prompt,
        )
        return

    if bucket == "legacy":
        summary.legacy_request_count += 1
        summary.legacy_prompt_tokens += prompt
        summary.legacy_completion_tokens += completion
        summary.legacy_reasoning_tokens += reasoning
        summary.legacy_cached_tokens += cached
        summary.legacy_cache_creation_tokens += creation
        summary.legacy_cache_miss_tokens += cache_miss
        summary.legacy_total_tokens += total
        if inconsistent:
            summary.legacy_raw_cache_inconsistent_count += 1
        _add_estimate(
            summary,
            prefix="legacy_",
            estimated=estimated,
            prompt=prompt,
        )
        return

    summary.unknown_semantics_request_count += 1
    summary.unknown_semantics_prompt_tokens += prompt
    summary.unknown_semantics_completion_tokens += completion
    summary.unknown_semantics_reasoning_tokens += reasoning
    summary.unknown_semantics_cached_tokens += cached
    summary.unknown_semantics_cache_creation_tokens += creation
    summary.unknown_semantics_cache_miss_tokens += cache_miss
    summary.unknown_semantics_total_tokens += total


def cutoff_timestamp(range_name: UsageRange) -> float | None:
    now = time.time()
    if range_name == "today":
        local = time.localtime(now)
        start = time.struct_time(
            (
                local.tm_year,
                local.tm_mon,
                local.tm_mday,
                0,
                0,
                0,
                local.tm_wday,
                local.tm_yday,
                local.tm_isdst,
            )
        )
        return time.mktime(start)
    if range_name == "7d":
        return now - 7 * 86400
    if range_name == "30d":
        return now - 30 * 86400
    return None


__all__ = [
    "UsageRange",
    "UsageSummary",
    "add_record_to_summary",
    "cutoff_timestamp",
    "usage_semantics_bucket",
]
