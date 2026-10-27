"""提前压缩的轻量状态机与持久 Job 模型。

只负责“是否该调用摘要模型”的数值决策与持久任务声明，不读取聊天正文、不直接调用模型。
摘要的实际执行、archive 与游标提交由 PipelineSummaryMixin 负责。
"""

from __future__ import annotations

import collections
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CompactionDecision:
    should_run: bool
    reason: str
    predicted_tokens: int
    observed_tokens: int
    new_tokens: int
    predicted_increment: int = 0
    predicted_total: int = 0

    def __post_init__(self) -> None:
        if self.predicted_increment == 0 and self.predicted_tokens != 0:
            self.predicted_increment = self.predicted_tokens
        if self.predicted_total == 0:
            self.predicted_total = self.observed_tokens + self.predicted_increment


@dataclass(frozen=True, slots=True)
class PressureSnapshot:
    """P01: 压力快照，区分正常、软线、紧急与超限四档状态。"""

    active_tokens: int
    view_tokens: int
    input_budget: int
    soft_tokens: int
    urgent_tokens: int
    pressure_level: str  # "normal" | "soft" | "urgent" | "over_budget"
    predicted_growth: int = 0
    needs_immediate_wait: bool = False
    cooldown_remaining: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "active_tokens": self.active_tokens,
            "view_tokens": self.view_tokens,
            "input_budget": self.input_budget,
            "soft_tokens": self.soft_tokens,
            "urgent_tokens": self.urgent_tokens,
            "pressure_level": self.pressure_level,
            "predicted_growth": self.predicted_growth,
            "needs_immediate_wait": self.needs_immediate_wait,
            "cooldown_remaining": self.cooldown_remaining,
        }


@dataclass(slots=True)
class CompactionJob:
    """P01: 持久压缩任务状态。

    记录 phase、来源范围、base summary、fencing token、UTC 重试时间及实际前后 token。
    运行期间测时间差只用 monotonic；跨重启持久化使用 UTC，并在恢复时夹取防时钟跳变。
    """

    job_id: str
    persona_id: str = ""
    phase: str = "idle"  # idle | claimed | running | committed | failed | cancelled
    source_range_start: int = 0
    source_range_end: int = 0
    base_summary_hash: str = ""
    config_version: int = 1
    mutation_gen: int = 0
    claim_token: str = ""
    boot_id: str = ""
    fencing_token: int = 0
    checkpoint: dict[str, Any] = field(default_factory=dict)
    created_at_utc: str = ""
    retry_after_utc: str | None = None
    cooldown_seconds: int = 60
    tokens_before: int = 0
    actual_after_tokens: int = 0
    archived_count: int = 0
    fee_tokens_reserved: int = 0
    already_committed: bool = False
    failure_reason: str = ""
    failure_class: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class CompactionPlanner:
    """同 persona 的软线/预测/冷却/退避状态。

    P01 增强：
    - mark_success 优先记录 actual_after_tokens 与真实 coverage；
    - active-view 与 full-input 计数明确分开；
    - 每 (request_id, watermark, view_version) 只 observe 一次，retry 不重复加零增量；
    - model/policy/calib 变更时重新对齐基准；
    - 增长预测限制在最近 128 个有限样本内。
    """

    def __init__(self) -> None:
        self.last_success_at: float | None = None
        self.last_failure_at: float | None = None
        self.failure_count = 0
        self.backoff_until = 0.0
        self.last_post_tokens = 0
        self.last_active_records = 0
        self.last_observed_tokens = 0
        self.last_full_input_tokens = 0
        self.last_active_view_tokens = 0
        self.growth_ewma: float | None = None
        self._growth_samples: collections.deque[int] = collections.deque(maxlen=128)
        self._observed_keys: set[tuple[str, int, int]] = set()
        self._observed_key_order: collections.deque[tuple[str, int, int]] = collections.deque(
            maxlen=256
        )
        self._baseline_key: str | None = None
        self.last_reason = ""
        self.last_skip_reason = ""
        self.last_action = "idle"
        self.last_coverage = 0
        self.last_actual_after_tokens: int | None = None

    def observe(
        self,
        active_tokens: int,
        *,
        request_id: str | None = None,
        source_watermark: int | None = None,
        view_version: int | None = None,
        full_input_tokens: int | None = None,
        baseline_tag: str | None = None,
    ) -> None:
        """记录活跃 token 并更新增长 EWMA。

        P01 规则：
        1. baseline_tag（模型/策略/校准）变化时重置旧样本；
        2. 同一 request/watermark/view_version 只记录一次，重试不重复累加 0 增量；
        3. 严格限制至多 128 个数值样本。
        """
        if baseline_tag is not None:
            if self._baseline_key is not None and self._baseline_key != baseline_tag:
                self.growth_ewma = None
                self._growth_samples.clear()
                self.last_observed_tokens = 0
            self._baseline_key = baseline_tag

        if request_id or source_watermark is not None or view_version is not None:
            key = (
                str(request_id or ""),
                int(source_watermark or 0),
                int(view_version or 0),
            )
            if key in self._observed_keys:
                return
            if len(self._observed_key_order) >= 256:
                oldest = self._observed_key_order.popleft()
                self._observed_keys.discard(oldest)
            self._observed_keys.add(key)
            self._observed_key_order.append(key)

        active_tokens = max(0, int(active_tokens))
        if full_input_tokens is not None:
            self.last_full_input_tokens = max(0, int(full_input_tokens))
        self.last_active_view_tokens = active_tokens

        if active_tokens < self.last_observed_tokens:
            # 压缩刚发生或窗口变短，旧增长样本不再适用
            self.growth_ewma = None
        elif self.last_observed_tokens > 0:
            growth = max(0, active_tokens - self.last_observed_tokens)
            if growth > 0:
                self._growth_samples.append(growth)
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
                True,
                "manual" if force else "media",
                predicted,
                active_tokens,
                new_tokens,
                predicted_increment=predicted,
                predicted_total=active_tokens + predicted,
            )
        if self.backoff_until > now:
            self.last_skip_reason = "backoff"
            return CompactionDecision(
                False,
                "backoff",
                predicted,
                active_tokens,
                new_tokens,
                predicted_increment=predicted,
                predicted_total=active_tokens + predicted,
            )
        if active_records < 2:
            self.last_skip_reason = "not_enough_history"
            return CompactionDecision(
                False,
                "not_enough_history",
                predicted,
                active_tokens,
                new_tokens,
                predicted_increment=predicted,
                predicted_total=active_tokens + predicted,
            )
        if self.last_success_at is not None:
            in_cooldown = (now - self.last_success_at) < max(0, cooldown_seconds)
            if in_cooldown and new_tokens < max(0, min_new_tokens):
                self.last_skip_reason = "cooldown"
                return CompactionDecision(
                    False,
                    "cooldown",
                    predicted,
                    active_tokens,
                    new_tokens,
                    predicted_increment=predicted,
                    predicted_total=active_tokens + predicted,
                )
            if new_tokens < max(0, min_new_tokens):
                self.last_skip_reason = "no_new_compaction_material"
                return CompactionDecision(
                    False,
                    "no_new_compaction_material",
                    predicted,
                    active_tokens,
                    new_tokens,
                    predicted_increment=predicted,
                    predicted_total=active_tokens + predicted,
                )
        effective = active_tokens + (predicted if forecast_enabled else 0)
        if active_tokens < trigger_tokens and effective < trigger_tokens:
            self.last_skip_reason = "below_trigger"
            return CompactionDecision(
                False,
                "below_trigger",
                predicted,
                active_tokens,
                new_tokens,
                predicted_increment=predicted,
                predicted_total=effective,
            )
        self.last_reason = "forecast" if active_tokens < trigger_tokens else "soft"
        return CompactionDecision(
            True,
            self.last_reason,
            predicted,
            active_tokens,
            new_tokens,
            predicted_increment=predicted,
            predicted_total=effective,
        )

    def mark_success(
        self,
        *,
        now: float,
        active_tokens_before: int,
        active_records_before: int,
        archived_count: int,
        target_after_tokens: int | None = None,
        actual_after_tokens: int | None = None,
        actual_coverage: int | None = None,
    ) -> None:
        """记录成功压缩。

        P01: 必须记录真实 actual_after_tokens，不拿目标值或 active_tokens // 2 充数。
        """
        self.last_success_at = now
        self.last_failure_at = None
        self.failure_count = 0
        self.backoff_until = 0.0
        self.last_coverage = max(
            0,
            int(actual_coverage if actual_coverage is not None else archived_count),
        )
        if actual_after_tokens is not None:
            self.last_actual_after_tokens = max(0, int(actual_after_tokens))
            self.last_post_tokens = self.last_actual_after_tokens
        elif target_after_tokens is not None:
            self.last_actual_after_tokens = None
            self.last_post_tokens = max(0, int(target_after_tokens))
        else:
            self.last_actual_after_tokens = None
            self.last_post_tokens = max(0, int(active_tokens_before) // 2)

        self.last_active_records = max(
            0, int(active_records_before) - int(archived_count)
        )
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

    def build_pressure_snapshot(
        self,
        *,
        active_tokens: int,
        input_budget: int,
        soft_tokens: int,
        urgent_tokens: int,
        now: float,
        cooldown_seconds: int = 60,
    ) -> PressureSnapshot:
        """适配压力快照，给出当前的压力等级和是否必须等待。"""
        active_tokens = max(0, int(active_tokens))
        input_budget = max(1, int(input_budget))
        soft_tokens = max(1, int(soft_tokens))
        urgent_tokens = max(soft_tokens, int(urgent_tokens))

        predicted = self.predict_growth(0)
        effective = active_tokens + predicted

        cooldown_rem = 0.0
        if self.last_success_at is not None:
            elapsed = now - self.last_success_at
            if elapsed < cooldown_seconds:
                cooldown_rem = max(0.0, float(cooldown_seconds - elapsed))

        if effective > input_budget or active_tokens > input_budget:
            level = "over_budget"
            wait = True
        elif active_tokens >= urgent_tokens or effective >= urgent_tokens:
            level = "urgent"
            wait = True
        elif active_tokens >= soft_tokens or effective >= soft_tokens:
            level = "soft"
            wait = False
        else:
            level = "normal"
            wait = False

        return PressureSnapshot(
            active_tokens=active_tokens,
            view_tokens=self.last_active_view_tokens or active_tokens,
            input_budget=input_budget,
            soft_tokens=soft_tokens,
            urgent_tokens=urgent_tokens,
            pressure_level=level,
            predicted_growth=predicted,
            needs_immediate_wait=wait,
            cooldown_remaining=cooldown_rem,
        )

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
            "last_actual_after_tokens": self.last_actual_after_tokens,
            "last_active_records": self.last_active_records,
            "last_full_input_tokens": self.last_full_input_tokens,
            "last_active_view_tokens": self.last_active_view_tokens,
            "last_coverage": self.last_coverage,
            "growth_ewma": (
                round(self.growth_ewma, 1) if self.growth_ewma is not None else None
            ),
            "growth_samples_count": len(self._growth_samples),
            "last_reason": self.last_reason,
            "last_skip_reason": self.last_skip_reason,
            "last_action": self.last_action,
        }


class CompactionJobStore:
    """P01: 压缩任务持久化协调器。

    保证：
    1. 单 persona 压缩并发度为 1（H618 默认 summary 并发 1）；
    2. 尝试执行前先登记 attempt，网络崩溃也记录现场；
    3. 已 commit 任务标记 already_committed，防重放重打；
    4. 跨重启恢复支持 UTC 夹取，防止系统时钟跳变产生负等待或无限锁死。
    """

    def __init__(self, jobs_path: Path | str, *, boot_id: str = "") -> None:
        self.path = Path(jobs_path)
        self.boot_id = boot_id or f"boot-{os.getpid()}-{int(time.time())}"
        self._jobs: dict[str, CompactionJob] = {}
        self._active_job_id: str | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for job_id, item in data.items():
                    if isinstance(item, dict):
                        # UTC 恢复夹取，防 clock-jump
                        created_str = item.get("created_at_utc", "")
                        if created_str:
                            try:
                                dt = datetime.fromisoformat(created_str)
                                now_dt = datetime.now(timezone.utc)
                                if dt > now_dt:
                                    logger.warning(
                                        "检测到未来创建时间的 compaction job %s，夹取至当前时间",
                                        job_id,
                                    )
                                    item["created_at_utc"] = now_dt.isoformat()
                            except ValueError:
                                pass
                        job = CompactionJob(**item)
                        self._jobs[job_id] = job
                        if job.phase in {"claimed", "running"}:
                            # 旧进程中断，置为 failed
                            job.phase = "failed"
                            job.failure_reason = "interrupted_by_restart"
                            job.failure_class = "process_restart"
        except Exception as e:
            logger.warning("读取 compaction_jobs.json 失败: %s", e)

    def persist(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(".tmp")
            data = {job_id: job.as_dict() for job_id, job in self._jobs.items()}
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            tmp_path.replace(self.path)
        except Exception as e:
            logger.error("持久化 compaction_jobs.json 失败: %s", e)

    def claim_job(
        self,
        job_id: str,
        *,
        persona_id: str = "",
        source_range_start: int = 0,
        source_range_end: int = 0,
        base_summary_hash: str = "",
        config_version: int = 1,
        mutation_gen: int = 0,
        cooldown_seconds: int = 60,
        tokens_before: int = 0,
        fee_tokens_reserved: int = 0,
    ) -> tuple[CompactionJob | None, str]:
        """尝试认领一个压缩任务。

        如果已有进行中的活跃任务，拒绝并返回现有 job_id（job busy）。
        """
        if self._active_job_id and self._active_job_id in self._jobs:
            active = self._jobs[self._active_job_id]
            if active.phase in {"claimed", "running"}:
                return None, active.job_id

        now_utc = datetime.now(timezone.utc).isoformat()
        job = CompactionJob(
            job_id=job_id,
            persona_id=persona_id,
            phase="claimed",
            source_range_start=source_range_start,
            source_range_end=source_range_end,
            base_summary_hash=base_summary_hash,
            config_version=config_version,
            mutation_gen=mutation_gen,
            boot_id=self.boot_id,
            created_at_utc=now_utc,
            cooldown_seconds=cooldown_seconds,
            tokens_before=tokens_before,
            fee_tokens_reserved=fee_tokens_reserved,
        )
        self._jobs[job_id] = job
        self._active_job_id = job_id
        self.persist()
        return job, ""

    def mark_running(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.phase = "running"
            self.persist()

    def commit_job(
        self,
        job_id: str,
        *,
        actual_after_tokens: int,
        archived_count: int,
    ) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.phase = "committed"
            job.actual_after_tokens = actual_after_tokens
            job.archived_count = archived_count
            job.already_committed = True
            if self._active_job_id == job_id:
                self._active_job_id = None
            self.persist()

    def fail_job(
        self,
        job_id: str,
        *,
        reason: str,
        failure_class: str = "general",
    ) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.phase = "failed"
            job.failure_reason = reason
            job.failure_class = failure_class
            if self._active_job_id == job_id:
                self._active_job_id = None
            self.persist()

    def get_job(self, job_id: str) -> CompactionJob | None:
        return self._jobs.get(job_id)


__all__ = [
    "CompactionDecision",
    "CompactionJob",
    "CompactionJobStore",
    "CompactionPlanner",
    "PressureSnapshot",
]
