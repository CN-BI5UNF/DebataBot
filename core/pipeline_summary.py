"""Summary/compaction helpers for MessagePipeline.

This module is a mechanical split from ``core.message_pipeline``. Keep behavior
equivalent; do not change summary trigger, archive, rolling-summary, or
important-memory save logic while moving methods.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from utils import get_time
from utils.image_budget import estimate_refs_budget
from utils.image_contracts import (
    ImageAssetError,
    ImageBudgetProfile,
    ImageLimits,
    declared_image_assets,
    validate_declared_assets,
)
from utils.token_budget import TokenEstimator

from .compaction import CompactionPlanner
from .model_view import build_model_view, runtime_kind
from .pipeline_history import _record_timestamp
from .state import PendingMessageItem, ProtectedInput

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SummaryAttemptResult:
    status: str
    reason: str = ""
    active_start_before: int = 0
    active_start_after: int = 0
    active_records: int = 0
    active_tokens: int = 0
    raw_active_tokens: int = 0
    view_active_tokens: int = 0
    summarized_records: int = 0
    archived_count: int = 0
    target_after_tokens: int | None = None
    partial_archive_committed: bool = False
    archive_reused: bool = False

    @property
    def success(self) -> bool:
        return self.status == "success"


class PipelineSummaryMixin:
    def _compaction_planner(self) -> CompactionPlanner:
        planner = getattr(self, "_compaction_planner_obj", None)
        if planner is None:
            planner = CompactionPlanner()
            self._compaction_planner_obj = planner
        return planner

    def _record_compaction_outcome(self, result: SummaryAttemptResult) -> None:
        planner = self._compaction_planner()
        now = time.monotonic()
        if result.success:
            planner.mark_success(
                now=now,
                active_tokens_before=(
                    result.active_tokens or result.view_active_tokens or 0
                ),
                active_records_before=result.active_records,
                archived_count=result.archived_count,
                target_after_tokens=result.target_after_tokens,
            )
        elif result.status == "failed":
            planner.mark_failure(now=now, reason=result.reason or "failed")
        elif result.status == "not_needed":
            planner.mark_skipped(result.reason or "not_needed")

    def compaction_status(self) -> dict[str, Any]:
        planner = self._compaction_planner()
        cfg = getattr(getattr(self, "behavior_cfg", None), "summarize", None)
        return {
            **planner.as_dict(),
            "background_enabled": bool(getattr(cfg, "background_enabled", True)),
            "forecast_enabled": bool(getattr(cfg, "forecast_enabled", True)),
            "cooldown_seconds": int(getattr(cfg, "cooldown_seconds", 60)),
            "min_new_tokens": int(getattr(cfg, "min_new_tokens", 2048)),
            "cold_growth_tokens": int(getattr(cfg, "cold_growth_tokens", 2048)),
        }

    def _schedule_summarize(self) -> None:
        """后台触发 compaction，避免在回复热路径同步调用总结模型。"""
        if self.summary_agent is None or self.archive is None or self.rolling_summary is None:
            return
        if not bool(getattr(self.behavior_cfg.summarize, "background_enabled", True)):
            return
        if self._summary_task is not None and not self._summary_task.done():
            return

        async def _runner() -> None:
            try:
                await self._maybe_summarize()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"总结触发失败（忽略）: {e}")

        self._summary_task = asyncio.create_task(_runner())

    async def _maybe_summarize(
        self,
        *,
        force: bool = False,
        target_after_tokens: int | None = None,
        reason: str = "scheduled",
    ) -> SummaryAttemptResult:
        """按阈值压缩活跃 history：归档原文，只移动活跃窗口起点。"""
        async with self._summary_compaction_lock():
            result = await self._maybe_summarize_unlocked(
                force=force,
                target_after_tokens=target_after_tokens,
                reason=reason,
            )
        self._record_compaction_outcome(result)
        return result

    def _summary_compaction_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_summary_compaction_lock_obj", None)
        if lock is None:
            lock = asyncio.Lock()
            self._summary_compaction_lock_obj = lock
        return lock

    async def compact_manual(self) -> SummaryAttemptResult:
        """手动压缩：锁被占用时立即返回 busy，不排队重复调用摘要模型。"""
        lock = self._summary_compaction_lock()
        if lock.locked():
            return SummaryAttemptResult(status="not_needed", reason="compaction_busy")
        async with lock:
            result = await self._maybe_summarize_unlocked(
                force=True,
                target_after_tokens=None,
                reason="manual",
            )
        self._record_compaction_outcome(result)
        return result

    def _register_protected_input(
        self,
        items: list[PendingMessageItem],
    ) -> ProtectedInput | None:
        """登记本轮尚未回答的输入；必须由调用方在 finally 中释放。"""
        if not items:
            return None
        guard = ProtectedInput(
            conversation_id=str(items[-1].conversation_id or ""),
            message_ids=frozenset(
                str(item.message_id) for item in items if item.message_id
            ),
            inbound_seqs=frozenset(
                int(item.inbound_seq)
                for item in items
                if isinstance(item.inbound_seq, int)
                and not isinstance(item.inbound_seq, bool)
                and item.inbound_seq >= 0
            ),
        )
        guards = getattr(self, "_active_input_guards", None)
        if guards is None:
            guards = []
            self._active_input_guards = guards
        guards.append(guard)
        return guard

    def _release_protected_input(self, guard: ProtectedInput | None) -> None:
        if guard is None:
            return
        guards = getattr(self, "_active_input_guards", None)
        if not guards:
            return
        try:
            guards.remove(guard)
        except ValueError:
            logger.debug("受保护输入已被移除", exc_info=True)

    @staticmethod
    def _record_matches_guard(
        record: dict[str, Any],
        guard: ProtectedInput,
    ) -> bool:
        if record.get("conversation_id") != guard.conversation_id:
            return False
        metadata = record.get("metadata")
        entries = metadata.get("messages") if isinstance(metadata, dict) else None
        if not isinstance(entries, list):
            return False
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            message_id = str(entry.get("message_id") or "")
            seq = entry.get("inbound_seq")
            if message_id and message_id in guard.message_ids:
                return True
            if (
                isinstance(seq, int)
                and not isinstance(seq, bool)
                and seq in guard.inbound_seqs
            ):
                return True
        return False

    @staticmethod
    def _validated_image_refs(
        records: list[dict[str, Any]],
        *,
        limits: ImageLimits,
    ) -> list[dict[str, Any]]:
        """严格校验 refs 结构，但允许整请求数量超限由摘要压缩解决。"""
        validate_declared_assets(
            records,
            limits=limits,
            enforce_request_limits=False,
        )
        return [
            ref
            for record in records
            for ref in (declared_image_assets(record) or [])
        ]

    def _protected_max_view_cut(
        self,
        view: Any,
        active_records: list[dict[str, Any]],
    ) -> tuple[int | None, str]:
        """返回可安全归档的 view 前缀上限；None 表示没有保护登记。"""
        guards = list(getattr(self, "_active_input_guards", []) or [])
        if not guards:
            return None, ""
        view_index = {raw: index for index, raw in enumerate(view.raw_indices)}
        positions: list[int] = []
        for guard in guards:
            raw_pos = next(
                (
                    index
                    for index, record in enumerate(active_records)
                    if self._record_matches_guard(record, guard)
                ),
                None,
            )
            if raw_pos is None or raw_pos not in view_index:
                return None, "protected_input_missing"
            positions.append(view_index[raw_pos])
        return min(positions), ""

    async def _maybe_summarize_unlocked(
        self,
        *,
        force: bool,
        target_after_tokens: int | None,
        reason: str,
    ) -> SummaryAttemptResult:
        if self.summary_agent is None or self.archive is None or self.rolling_summary is None:
            return SummaryAttemptResult(status="not_configured", reason=reason)

        total_records = int(await self._history_length())
        if total_records <= 0:
            return SummaryAttemptResult(status="not_needed", reason="empty_history")
        active_records, active_start = await self._active_history_window()
        if not active_records:
            return SummaryAttemptResult(
                status="not_needed",
                reason="empty_active_history",
                active_start_before=active_start,
                active_start_after=active_start,
            )
        estimator = self._token_estimator()
        view = build_model_view(active_records, policy=self._model_view_policy())
        view_tokens = estimator.estimate_messages(view.records)
        raw_tokens = estimator.estimate_messages(active_records)
        image_limits: ImageLimits | None = None
        image_profile: ImageBudgetProfile | None = None
        image_count = 0
        image_bytes = 0
        image_triggered = False
        if getattr(self, "main_model_images_enabled", False):
            image_limits = self._image_limits()
            image_profile = self._main_prompt_image_profile()
            try:
                refs = self._validated_image_refs(
                    view.records,
                    limits=image_limits,
                )
            except ImageAssetError as exc:
                return SummaryAttemptResult(
                    status="failed",
                    reason=exc.code,
                    active_start_before=active_start,
                    active_start_after=active_start,
                    active_records=len(active_records),
                    active_tokens=view_tokens,
                    raw_active_tokens=raw_tokens,
                    view_active_tokens=view_tokens,
                )
            image_estimate = estimate_refs_budget(refs, profile=image_profile)
            image_count = image_estimate.image_count
            image_bytes = image_estimate.encoded_image_bytes
            image_triggered = (
                image_count > image_limits.max_images_per_request
                or image_bytes > image_limits.max_encoded_image_bytes
            )
        budget = self._context_budget()
        summarize_cfg = self.behavior_cfg.summarize
        trigger = summarize_cfg.trigger_at_tokens
        if trigger is None:
            # 百分比阈值按“整份 prompt”定义；固定 system/tools 开销不参与压缩，
            # 因此活跃视图的触发线要扣掉固定开销。
            trigger = int(
                budget.max_context_tokens
                * summarize_cfg.trigger_at_context_percent
                / 100
            ) - self._fixed_prompt_overhead_tokens()
            trigger = max(1, trigger)
        planner = self._compaction_planner()
        planner.observe(view_tokens)
        decision = planner.decide(
            active_tokens=view_tokens,
            active_records=len(active_records),
            trigger_tokens=trigger,
            now=time.monotonic(),
            cooldown_seconds=int(getattr(summarize_cfg, "cooldown_seconds", 60)),
            min_new_tokens=int(getattr(summarize_cfg, "min_new_tokens", 2048)),
            cold_growth_tokens=int(getattr(summarize_cfg, "cold_growth_tokens", 2048)),
            forecast_enabled=bool(getattr(summarize_cfg, "forecast_enabled", True)),
            force=force,
            media_pressure=image_triggered,
        )
        if not decision.should_run:
            planner.mark_skipped(decision.reason)
            return SummaryAttemptResult(
                status="not_needed",
                reason=decision.reason,
                active_start_before=active_start,
                active_start_after=active_start,
                active_records=len(active_records),
                active_tokens=view_tokens,
                raw_active_tokens=raw_tokens,
                view_active_tokens=view_tokens,
            )
        target_after = self._summary_target_after_tokens(
            active_tokens=view_tokens,
            override=target_after_tokens,
        )
        if target_after is None:
            return SummaryAttemptResult(
                status="failed",
                reason="invalid_target",
                active_start_before=active_start,
                active_start_after=active_start,
                active_records=len(active_records),
                active_tokens=view_tokens,
                raw_active_tokens=raw_tokens,
                view_active_tokens=view_tokens,
            )
        target_after = max(1, min(target_after, view_tokens - 1))

        max_view_cut, protected_reason = self._protected_max_view_cut(
            view,
            active_records,
        )
        if protected_reason:
            return SummaryAttemptResult(
                status="failed",
                reason=protected_reason,
                active_start_before=active_start,
                active_start_after=active_start,
                active_records=len(active_records),
                active_tokens=view_tokens,
                raw_active_tokens=raw_tokens,
                view_active_tokens=view_tokens,
                target_after_tokens=target_after,
            )
        if max_view_cut is not None and max_view_cut <= 0:
            return SummaryAttemptResult(
                status="failed",
                reason="protected_input_budget",
                active_start_before=active_start,
                active_start_after=active_start,
                active_records=len(active_records),
                active_tokens=view_tokens,
                raw_active_tokens=raw_tokens,
                view_active_tokens=view_tokens,
                target_after_tokens=target_after,
            )

        logger.info(
            "活跃历史触发滚动摘要 compaction reason=%s decision=%s view_tokens=%s "
            "raw_tokens=%s trigger=%s predicted=%s new_tokens=%s images=%s "
            "image_bytes=%s image_triggered=%s records=%s view_records=%s",
            reason,
            decision.reason,
            view_tokens,
            raw_tokens,
            trigger,
            decision.predicted_tokens,
            decision.new_tokens,
            image_count,
            image_bytes,
            image_triggered,
            len(active_records),
            len(view.records),
        )

        return await self._compact_active_history_unlocked(
            target_after_tokens=target_after,
            reason=reason,
            active_records=active_records,
            active_start_index=active_start,
            active_tokens=view_tokens,
            estimator=estimator,
        )

    async def _compact_active_history(
        self,
        *,
        target_after_tokens: int,
        reason: str,
        active_records: list[dict[str, Any]] | None = None,
        active_start_index: int | None = None,
        active_tokens: int | None = None,
        estimator: TokenEstimator | None = None,
    ) -> SummaryAttemptResult:
        async with self._summary_compaction_lock():
            result = await self._compact_active_history_unlocked(
                target_after_tokens=target_after_tokens,
                reason=reason,
                active_records=active_records,
                active_start_index=active_start_index,
                active_tokens=active_tokens,
                estimator=estimator,
            )
        self._record_compaction_outcome(result)
        return result

    async def _compact_active_history_unlocked(
        self,
        *,
        target_after_tokens: int,
        reason: str,
        active_records: list[dict[str, Any]] | None = None,
        active_start_index: int | None = None,
        active_tokens: int | None = None,
        estimator: TokenEstimator | None = None,
    ) -> SummaryAttemptResult:
        if self.summary_agent is None or self.archive is None or self.rolling_summary is None:
            return SummaryAttemptResult(status="not_configured", reason=reason)

        if active_records is not None:
            active = active_records
            if active_start_index is None:
                total = int(await self._history_length())
                active_start = max(0, total - len(active))
            else:
                active_start = max(0, int(active_start_index))
        else:
            active, active_start = await self._active_history_window()
        if not active:
            return SummaryAttemptResult(
                status="not_needed",
                reason="empty_active_history",
                active_start_before=active_start,
                active_start_after=active_start,
            )
        estimator = estimator or self._token_estimator()
        view = build_model_view(active, policy=self._model_view_policy())
        if not view.records:
            return SummaryAttemptResult(
                status="failed",
                reason="empty_slice",
                active_start_before=active_start,
                active_start_after=active_start,
                active_records=len(active),
                target_after_tokens=target_after_tokens,
            )
        view_tokens = estimator.estimate_messages(view.records)
        raw_tokens = estimator.estimate_messages(active)
        target_after = max(1, min(target_after_tokens, view_tokens - 1))

        image_limits: ImageLimits | None = None
        image_profile: ImageBudgetProfile | None = None
        if getattr(self, "main_model_images_enabled", False):
            image_limits = self._image_limits()
            image_profile = self._main_prompt_image_profile()
            try:
                self._validated_image_refs(active, limits=image_limits)
            except ImageAssetError as exc:
                return SummaryAttemptResult(
                    status="failed",
                    reason=exc.code,
                    active_start_before=active_start,
                    active_start_after=active_start,
                    active_records=len(active),
                    active_tokens=view_tokens,
                    raw_active_tokens=raw_tokens,
                    view_active_tokens=view_tokens,
                    target_after_tokens=target_after,
                )
        max_view_cut, protected_reason = self._protected_max_view_cut(view, active)
        if protected_reason:
            return SummaryAttemptResult(
                status="failed",
                reason=protected_reason,
                active_start_before=active_start,
                active_start_after=active_start,
                active_records=len(active),
                active_tokens=view_tokens,
                raw_active_tokens=raw_tokens,
                view_active_tokens=view_tokens,
                target_after_tokens=target_after,
            )
        if max_view_cut is not None and max_view_cut <= 0:
            return SummaryAttemptResult(
                status="failed",
                reason="protected_input_budget",
                active_start_before=active_start,
                active_start_after=active_start,
                active_records=len(active),
                active_tokens=view_tokens,
                raw_active_tokens=raw_tokens,
                view_active_tokens=view_tokens,
                target_after_tokens=target_after,
            )

        important_text = self.important.text()
        # 摘要只读“模型可见”的切片，避免把旧 task_context/send_receipt 噪声
        # 再喂给总结 Agent。归档仍然写原始切片。
        summary_slice = self._select_compaction_slice(
            view.records,
            active_tokens=view_tokens,
            target_after_tokens=target_after,
            estimator=estimator,
            image_limits=image_limits,
            image_profile=image_profile,
            max_view_cut=max_view_cut,
        )
        if not summary_slice:
            logger.warning("compaction 未选出可归档切片，跳过")
            return SummaryAttemptResult(
                status="failed",
                reason="empty_slice",
                active_start_before=active_start,
                active_start_after=active_start,
                active_records=len(active),
                active_tokens=view_tokens,
                raw_active_tokens=raw_tokens,
                view_active_tokens=view_tokens,
                target_after_tokens=target_after,
            )

        raw_cut = view.raw_indices[len(summary_slice) - 1] + 1
        raw_cut = self._provider_safe_prefix_len(active, raw_cut)
        if max_view_cut is not None and max_view_cut > 0:
            protected_raw_cut = view.raw_indices[max_view_cut - 1] + 1
            raw_cut = min(raw_cut, protected_raw_cut)
        slice_records = active[:raw_cut]
        if not slice_records:
            logger.warning("compaction 原始切片为空，跳过")
            return SummaryAttemptResult(
                status="failed",
                reason="empty_slice",
                active_start_before=active_start,
                active_start_after=active_start,
                active_records=len(active),
                active_tokens=view_tokens,
                raw_active_tokens=raw_tokens,
                view_active_tokens=view_tokens,
                target_after_tokens=target_after,
            )

        # 总结 Agent 只读真实聊天；旧运行时快照只影响游标推进，不参与摘要。
        summary_input = [
            record for record in summary_slice if runtime_kind(record) is None
        ]
        try:
            if summary_input:
                result = await self.summary_agent.summarize_rolling(
                    summary_input,
                    self.rolling_summary.text(),
                    important_text,
                )
            else:
                result = {
                    "summary_text": self.rolling_summary.text()
                    or "（仅清理运行时快照，无新增摘要）",
                    "new_important": [],
                }
        except Exception as e:
            logger.warning("滚动摘要 Agent 调用失败: %s", e)
            return SummaryAttemptResult(
                status="failed",
                reason="summary_agent_error",
                active_start_before=active_start,
                active_start_after=active_start,
                active_records=len(active),
                active_tokens=view_tokens,
                raw_active_tokens=raw_tokens,
                view_active_tokens=view_tokens,
                target_after_tokens=target_after,
            )
        if not result:
            logger.warning("滚动摘要 Agent 返回 None，跳过本次压缩")
            return SummaryAttemptResult(
                status="failed",
                reason="empty_result",
                active_start_before=active_start,
                active_start_after=active_start,
                active_records=len(active),
                active_tokens=view_tokens,
                raw_active_tokens=raw_tokens,
                view_active_tokens=view_tokens,
                target_after_tokens=target_after,
            )

        budget = self._context_budget()
        summary_text = self._trim_text_to_token_budget(
            str(result.get("summary_text") or "").strip(),
            budget.summary_token_budget,
            estimator,
        )
        new_important_items = result.get("new_important", [])
        if not summary_text:
            logger.warning("滚动摘要为空，跳过本次压缩")
            return SummaryAttemptResult(
                status="failed",
                reason="empty_summary_text",
                active_start_before=active_start,
                active_start_after=active_start,
                active_records=len(active),
                active_tokens=view_tokens,
                raw_active_tokens=raw_tokens,
                view_active_tokens=view_tokens,
                target_after_tokens=target_after,
            )

        new_active_start = active_start + raw_cut
        archive_key = self._compaction_archive_key(active_start, slice_records)
        partial_archives = self._partial_compaction_archives()
        archive_reused = archive_key in partial_archives
        try:
            if archive_reused:
                logger.warning(
                    "检测到上次滚动摘要 archive 已提交但游标未更新，"
                    "本次重试跳过重复 archive append: reason=%s active_start=%s count=%s",
                    reason,
                    active_start,
                    len(slice_records),
                )
            else:
                await self.archive.append_many(slice_records)
                partial_archives[archive_key] = {
                    "active_start": active_start,
                    "count": len(slice_records),
                    "reason": reason,
                }
            await self.rolling_summary.update(
                summary_text,
                archived_until={
                    "last_compaction_count": len(slice_records),
                    "last_timestamp": _record_timestamp(slice_records[-1]),
                },
                updated_at=get_time(),
                active_start_index=new_active_start,
            )
        except Exception as e:
            logger.warning(
                "滚动摘要提交失败，未移动活跃起点；如果 archive 已提交，"
                "同进程重试会跳过重复归档: %s",
                e,
            )
            return SummaryAttemptResult(
                status="failed",
                reason="commit_error",
                active_start_before=active_start,
                active_start_after=active_start,
                active_records=len(active),
                active_tokens=view_tokens,
                raw_active_tokens=raw_tokens,
                view_active_tokens=view_tokens,
                target_after_tokens=target_after,
                partial_archive_committed=archive_key in partial_archives,
                archive_reused=archive_reused,
            )
        partial_archives.pop(archive_key, None)

        saved_important_count = 0
        if isinstance(new_important_items, list) and new_important_items:
            for item in new_important_items:
                content = (item.get("content") or "").strip() if isinstance(item, dict) else ""
                if content:
                    scope = item.get("scope") if isinstance(item.get("scope"), str) else None
                    pinned = item.get("pinned") if isinstance(item.get("pinned"), bool) else False
                    try:
                        save_result = await self.important.save(
                            content,
                            scope=scope,
                            pinned=pinned,
                        )
                    except Exception as e:
                        logger.warning("滚动摘要提取重要记忆保存失败: %s", e)
                        continue
                    if save_result.get("saved"):
                        saved_important_count += 1

        await self.history.add_system_note(
            f"[滚动摘要] 已归档并移动活跃窗口起点 {len(slice_records)} 条；"
            f"新增重要记忆 {saved_important_count} 条"
        )
        logger.info(
            "滚动摘要完成：raw_active=%s view_active=%s summarized=%s "
            "summary_input=%s archived=%s target_after=%s",
            raw_tokens,
            view_tokens,
            len(summary_slice),
            len(summary_input),
            len(slice_records),
            target_after,
        )
        return SummaryAttemptResult(
            status="success",
            reason=reason,
            active_start_before=active_start,
            active_start_after=new_active_start,
            active_records=len(active),
            active_tokens=view_tokens,
            raw_active_tokens=raw_tokens,
            view_active_tokens=view_tokens,
            summarized_records=len(summary_input),
            archived_count=len(slice_records),
            target_after_tokens=target_after,
            archive_reused=archive_reused,
        )

    def _partial_compaction_archives(self) -> dict[str, dict[str, Any]]:
        partial = getattr(self, "_summary_partial_archives", None)
        if partial is None:
            partial = {}
            self._summary_partial_archives = partial
        return partial

    @staticmethod
    def _compaction_archive_key(
        active_start: int,
        slice_records: list[dict[str, Any]],
    ) -> str:
        payload = json.dumps(
            slice_records,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"{active_start}:{len(slice_records)}:{digest}"

    def _summary_target_after_tokens(
        self,
        *,
        active_tokens: int,
        override: int | None = None,
    ) -> int | None:
        if active_tokens <= 1:
            return None
        summarize_cfg = self.behavior_cfg.summarize
        target_after = override
        if target_after is None:
            target_after = summarize_cfg.target_after_tokens
        if target_after is None:
            # 百分比目标按整份 prompt 定义；减去固定开销后才是活跃视图目标。
            target_after = int(
                self._context_budget().max_context_tokens
                * summarize_cfg.target_after_context_percent
                / 100
            ) - self._fixed_prompt_overhead_tokens()
        return max(1, min(int(target_after), active_tokens - 1))

    def _active_start_index_for_records(self, records: list[dict[str, Any]]) -> int:
        if self.rolling_summary is None:
            return 0
        active_start = max(0, min(self.rolling_summary.active_start_index(), len(records)))
        return self._provider_safe_active_start_index(records, active_start)

    async def _history_length(self) -> int:
        length = getattr(self.history, "length", None)
        if callable(length):
            return int(await length())
        records = await self.history.records()
        return len(records)

    async def _active_history_window(self) -> tuple[list[dict[str, Any]], int]:
        """分页读取活跃历史，避免为了取后缀而加载全部归档记录。

        返回 ``(活跃记录, 活跃起点绝对位置)``。游标修正需要少量向前回看，
        找不到安全工具组边界时逐步扩大回看窗口。测试/旧 runtime 的
        SimpleNamespace history 没有分页接口时退回全量读取。
        """
        fetcher = getattr(self.history, "fetch_suffix", None)
        if not callable(fetcher):
            records = await self.history.records()
            start = self._active_start_index_for_records(records)
            return records[start:], start
        total = int(await self._history_length())
        if total <= 0:
            return [], 0
        cursor = 0
        if self.rolling_summary is not None:
            cursor = int(self.rolling_summary.active_start_index())
        cursor = max(0, min(cursor, total))
        lookback = 32
        while True:
            start = max(0, cursor - lookback)
            suffix = await self.history.fetch_suffix(start)
            window = list(getattr(suffix, "records", suffix) or [])
            actual_start = int(getattr(suffix, "start_index", start))
            if not window:
                return [], total
            local = max(0, min(cursor - actual_start, len(window)))
            adjusted = self._provider_safe_active_start_index(window, local)
            if adjusted > 0 or actual_start == 0:
                return window[adjusted:], actual_start + adjusted
            if start == 0:
                return window, 0
            # 活跃游标落在工具组中间：继续向前取，直到能拿到组起点。
            lookback = min(max(lookback * 2, lookback + 1), max(cursor, 1))

    @staticmethod
    def _image_totals(
        records: list[dict[str, Any]],
        *,
        profile: ImageBudgetProfile | None,
    ) -> tuple[int, int]:
        count = 0
        encoded_bytes = 0
        for record in records:
            estimate = estimate_refs_budget(
                declared_image_assets(record) or [],
                profile=profile,
            )
            count += estimate.image_count
            encoded_bytes += estimate.encoded_image_bytes
        return count, encoded_bytes

    @staticmethod
    def _select_compaction_slice(
        records: list[dict[str, Any]],
        *,
        active_tokens: int,
        target_after_tokens: int,
        estimator: TokenEstimator,
        image_limits: ImageLimits | None = None,
        image_profile: ImageBudgetProfile | None = None,
        max_view_cut: int | None = None,
    ) -> list[dict[str, Any]]:
        """从最旧记录开始选择需要归档的切片。

        token、图片编码字节和图片数量任一超限都继续向后扩展切点；图片随整条
        记录退出活跃窗口，不单独删除某张图。``max_view_cut`` 是本轮输入保护
        上限，切点绝不越过它。
        """
        # 至少保留最后一条活跃记录，避免 history 被清空后 task context 太孤立。
        maximum = max(0, len(records) - 1)
        if max_view_cut is not None:
            maximum = min(maximum, max(0, max_view_cut))
        if maximum <= 0:
            return []

        need_remove = max(0, active_tokens - target_after_tokens)
        remaining_count = 0
        remaining_bytes = 0
        if image_limits is not None:
            remaining_count, remaining_bytes = (
                PipelineSummaryMixin._image_totals(
                    records,
                    profile=image_profile,
                )
            )

        def over_budget() -> bool:
            return image_limits is not None and (
                remaining_count > image_limits.max_images_per_request
                or remaining_bytes > image_limits.max_encoded_image_bytes
            )

        selected: list[dict[str, Any]] = []
        removed = 0
        for record in records[:maximum]:
            selected.append(record)
            removed += estimator.estimate_messages([record])
            if image_limits is not None:
                record_count, record_bytes = (
                    PipelineSummaryMixin._image_totals(
                        [record],
                        profile=image_profile,
                    )
                )
                remaining_count -= record_count
                remaining_bytes -= record_bytes
            if removed >= need_remove and not over_budget():
                break
        safe_len = PipelineSummaryMixin._provider_safe_prefix_len(records, len(selected))
        safe_len = min(safe_len, maximum)
        return records[:safe_len]

    @staticmethod
    def _provider_safe_prefix_len(records: list[dict[str, Any]], cut: int) -> int:
        """调整切点，避免把 assistant/tool 调用组切开。"""
        cut = max(0, min(cut, max(0, len(records) - 1)))
        if cut <= 0:
            return cut

        for start, end in PipelineSummaryMixin._assistant_tool_group_ranges(records):
            if start < cut < end:
                # 优先向后扩到工具调用组末尾；如果会清空活跃区，再退回组开始。
                return end if end < len(records) else start
        return cut

    @staticmethod
    def _provider_safe_active_start_index(
        records: list[dict[str, Any]],
        active_start: int,
    ) -> int:
        """修正旧游标，避免活跃窗口从孤立 tool 结果开始。"""
        active_start = max(0, min(active_start, len(records)))
        if active_start <= 0 or active_start >= len(records):
            return active_start

        for start, end in PipelineSummaryMixin._assistant_tool_group_ranges(records):
            if start < active_start < end:
                return start
        return active_start

    @staticmethod
    def _assistant_tool_group_ranges(records: list[dict[str, Any]]) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        idx = 0
        while idx < len(records):
            record = records[idx]
            if record.get("role") != "assistant":
                idx += 1
                continue
            tool_call_ids = PipelineSummaryMixin._assistant_tool_call_ids(record)
            if not tool_call_ids:
                idx += 1
                continue

            seen: set[str] = set()
            end = idx + 1
            while end < len(records) and records[end].get("role") == "tool":
                tool_call_id = str(records[end].get("tool_call_id") or "")
                if tool_call_id not in tool_call_ids:
                    break
                seen.add(tool_call_id)
                end += 1
                if seen >= tool_call_ids:
                    break
            ranges.append((idx, end))
            idx = max(end, idx + 1)
        return ranges

    @staticmethod
    def _assistant_tool_call_ids(record: dict[str, Any]) -> set[str]:
        result: set[str] = set()
        for tool_call in record.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            call_id = str(tool_call.get("id") or "")
            if call_id:
                result.add(call_id)
        return result
