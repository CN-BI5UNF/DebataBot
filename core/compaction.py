"""提前压缩的轻量状态机。

只负责“是否该调用摘要模型”的数值决策，不读取聊天正文、不调用模型。
摘要的实际执行、archive 与游标提交仍由 PipelineSummaryMixin 负责。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class CompactionDecision:
    should_run: bool
    reason: str
    predicted_tokens: int
    observed_tokens: int
    new_tokens: int


class CompactionPlanner:
    """同 persona 的软线/预测/冷却/退避状态。

    状态当前保存在 Runtime 进程内；重启后重置。C02 的持久化 job/checkpoint
    不在本类中伪造。
    """

    def __init__(self) -> None:
        self.last_success_at: float | None = None
        self.last_failure_at: float | None = None
        self.failure_count = 0
        self.backoff_until = 0.0
        self.last_post_tokens = 0
        self.last_active_records = 0
        self.last_observed_tokens = 0
        self.growth_ewma: float | None = None
        self.last_reason = ""
        self.last_skip_reason = ""
        self.last_action = "idle"

    def observe(self, active_tokens: int) -> None:
        active_tokens = max(0, int(active_tokens))
        if active_tokens < self.last_observed_tokens:
            # 压缩刚发生或窗口变短，旧增长样本不再适用。
            self.growth_ewma = None
        elif self.last_observed_tokens > 0:
            growth = max(0, active_tokens - self.last_observed_tokens)
            if self.growth_ewma is None:
                self.growth_ewma = float(growth)
            else:
                self.growth_ewma = self.growth_ewma * 0.7 + growth * 0.3
        self.last_observed_tokens = active_tokens

    def predict_growth(self, cold_tokens: int) -> int:
        if self.growth_ewma is None:
            return max(0, int(cold_tokens))
        return max(0, int(round(self.growth_ewma)))

    def decide(
        self,
        *,
        active_tokens: int,
        active_records: int,
        trigger_tokens: int,
        now: float,
        cooldown_seconds: int,
        min_new_tokens: int,
        cold_growth_tokens: int,
        forecast_enabled: bool = True,
        force: bool = False,
        media_pressure: bool = False,
    ) -> CompactionDecision:
        active_tokens = max(0, int(active_tokens))
        active_records = max(0, int(active_records))
        trigger_tokens = max(1, int(trigger_tokens))
        new_tokens = (
            max(0, active_tokens - self.last_post_tokens)
            if self.last_success_at is not None
            else active_tokens
        )
        predicted = self.predict_growth(cold_growth_tokens)
        if force or media_pressure:
            return CompactionDecision(
                True, "manual" if force else "media", predicted, active_tokens, new_tokens
            )
        if self.backoff_until > now:
            self.last_skip_reason = "backoff"
            return CompactionDecision(False, "backoff", predicted, active_tokens, new_tokens)
        if active_records < 2:
            self.last_skip_reason = "not_enough_history"
            return CompactionDecision(
                False, "not_enough_history", predicted, active_tokens, new_tokens
            )
        if self.last_success_at is not None:
            in_cooldown = (now - self.last_success_at) < max(0, cooldown_seconds)
            if in_cooldown and new_tokens < max(0, min_new_tokens):
                self.last_skip_reason = "cooldown"
                return CompactionDecision(
                    False, "cooldown", predicted, active_tokens, new_tokens
                )
            if new_tokens < max(0, min_new_tokens):
                self.last_skip_reason = "no_new_compaction_material"
                return CompactionDecision(
                    False,
                    "no_new_compaction_material",
                    predicted,
                    active_tokens,
                    new_tokens,
                )
        effective = active_tokens + (predicted if forecast_enabled else 0)
        if active_tokens < trigger_tokens and effective < trigger_tokens:
            self.last_skip_reason = "below_trigger"
            return CompactionDecision(
                False, "below_trigger", predicted, active_tokens, new_tokens
            )
        self.last_reason = "forecast" if active_tokens < trigger_tokens else "soft"
        return CompactionDecision(
            True, self.last_reason, predicted, active_tokens, new_tokens
        )

    def mark_success(
        self,
        *,
        now: float,
        active_tokens_before: int,
        active_records_before: int,
        archived_count: int,
        target_after_tokens: int | None,
    ) -> None:
        self.last_success_at = now
        self.last_failure_at = None
        self.failure_count = 0
        self.backoff_until = 0.0
        self.last_post_tokens = max(
            0,
            int(target_after_tokens)
            if target_after_tokens is not None
            else int(active_tokens_before) // 2,
        )
        self.last_active_records = max(0, int(active_records_before) - int(archived_count))
        self.last_action = "cooldown"
        self.last_reason = "success"
        self.last_skip_reason = ""

    def mark_failure(self, *, now: float, reason: str) -> None:
        self.last_failure_at = now
        self.failure_count += 1
        backoff = min(900.0, 30.0 * (2 ** min(5, self.failure_count - 1)))
        self.backoff_until = now + backoff
        self.last_action = "backoff"
        self.last_reason = reason
        self.last_skip_reason = "backoff"

    def mark_skipped(self, reason: str) -> None:
        self.last_skip_reason = reason
        if self.last_action != "cooldown":
            self.last_action = "idle"

    def as_dict(self) -> dict[str, Any]:
        return {
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "failure_count": self.failure_count,
            "backoff_until": self.backoff_until or None,
            "backoff_seconds_remaining": (
                max(0, int(self.backoff_until - time.monotonic()))
                if self.backoff_until
                else 0
            ),
            "last_post_tokens": self.last_post_tokens,
            "last_active_records": self.last_active_records,
            "growth_ewma": round(self.growth_ewma, 1)
            if self.growth_ewma is not None
            else None,
            "last_reason": self.last_reason,
            "last_skip_reason": self.last_skip_reason,
            "last_action": self.last_action,
        }


__all__ = ["CompactionDecision", "CompactionPlanner"]
