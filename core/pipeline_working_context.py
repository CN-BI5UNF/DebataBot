"""Working-context helpers for MessagePipeline.

This module is a mechanical split from ``core.message_pipeline``. Keep behavior
equivalent; do not change token-budget, memory, RAG, or working-history
selection logic while moving methods.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from utils.image_budget import (
    assert_wire_request_fits,
    build_budget_messages,
    estimate_refs_budget,
    estimate_wire_request_body_bytes,
    group_validated_refs,
    request_body_limit,
    resolve_image_budget_profile,
)
from utils.image_contracts import (
    ImageAssetError,
    ImageBudgetProfile,
    ImageLimits,
)
from utils.token_budget import TokenBudget, TokenEstimator
from utils.video_budget import (
    assert_wire_video_request_fits,
    estimate_video_refs_budget,
    resolve_video_budget_profile,
)
from utils.video_contracts import (
    DEFAULT_VIDEO_TOKEN_RESERVE,
    VideoAssetError,
    VideoBudgetProfile,
    VideoLimits,
    build_video_budget_messages,
    group_validated_video_refs,
)

from .model_binding import (
    EffectiveModelBinding,
    ModelCallSnapshot,
    build_binding_budget,
    build_model_call_snapshot,
    resolve_agent_binding,
)
from .model_view import ModelViewPolicy, ModelViewResult, build_model_view
from .pipeline_context import _recommended_context_budget

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MainPromptBudgetResult:
    ok: bool
    messages: list[dict[str, Any]]
    estimated_tokens: int
    budget_tokens: int
    pre_summary_status: str = ""
    attempts: list[str] = field(default_factory=list)
    failure_reason: str = ""
    failure_code: str = ""
    contains_images: bool = False
    image_count: int = 0
    image_bytes: int = 0
    contains_videos: bool = False
    video_count: int = 0
    video_bytes: int = 0
    video_tokens: int = 0
    request_body_bytes: int = 0
    budget_basis: str = "text_only"
    model_call_snapshot: ModelCallSnapshot | None = None


@dataclass(slots=True)
class MainPromptBudgetUsage:
    estimated_tokens: int
    budget_tokens: int
    image_count: int
    encoded_image_bytes: int
    request_body_bytes: int
    basis: str
    contains_images: bool
    budget_messages: list[dict[str, Any]]
    contains_videos: bool = False
    video_count: int = 0
    encoded_video_bytes: int = 0
    video_tokens: int = 0


class PipelineWorkingContextMixin:
    def _context_budget(self) -> TokenBudget:
        cfg = self.behavior_cfg.context
        model = getattr(self.chat_agent.cfg, "model", "")
        max_context = cfg.max_context_tokens or _recommended_context_budget(
            model,
            self.model_context_length,
        )
        return TokenBudget(
            max_context_tokens=max_context,
            reserve_output_tokens=cfg.reserve_output_tokens,
            memory_token_budget=cfg.memory_token_budget,
            summary_token_budget=cfg.summary_token_budget,
        )

    def _active_model_binding(self, role: str = "chat") -> EffectiveModelBinding:
        config = getattr(self, "config", None)
        limit = getattr(self, "model_context_length", None)
        try:
            limit = int(limit) if limit else None
        except (TypeError, ValueError):
            limit = None
        if config is not None:
            resolved = resolve_agent_binding(config, role, model_context_length=limit)
            if resolved is not None:
                return resolved

        cfg = getattr(self.chat_agent, "cfg", None)
        reasoning = getattr(cfg, "reasoning", None) if cfg else None
        provider_name = str(getattr(cfg, "provider", "") or "")
        protocol = "openai_compat"
        if hasattr(self, "chat_agent") and hasattr(self.chat_agent, "provider"):
            protocol = getattr(self.chat_agent.provider, "protocol", "openai_compat") or "openai_compat"
        return EffectiveModelBinding(
            role=role,
            provider_id=provider_name,
            model=str(getattr(cfg, "model", "") or ""),
            protocol=protocol,
            context_limit=limit,
            context_source="runtime_model_meta" if limit else "unknown",
            context_verified=bool(limit),
            max_output_tokens=max(1, int(getattr(cfg, "max_tokens", 1) or 1)) if cfg else 1,
            reasoning_enabled=bool(getattr(reasoning, "enabled", False)),
            reasoning_budget=getattr(reasoning, "budget", None),
            reasoning_max_tokens=getattr(reasoning, "max_tokens", None),
        )

    def _active_budget_snapshot(self):
        cfg = self.behavior_cfg.context
        model = getattr(self.chat_agent.cfg, "model", "")
        requested = int(
            cfg.max_context_tokens
            or _recommended_context_budget(model, self.model_context_length)
        )
        return build_binding_budget(
            self._active_model_binding(),
            cfg,
            getattr(self.behavior_cfg, "summarize", None),
            requested_window=requested,
            actual_max_tokens=int(getattr(self.chat_agent.cfg, "max_tokens", 0) or 0),
        )

    def _guard_main_prompt_tokens(
        self,
        messages: list[dict[str, Any]],
        tools_schema: list[dict[str, Any]] | None,
    ) -> str:
        """在真正发送前用同一快照复核；返回空串表示通过。"""
        snapshot = self._active_budget_snapshot()
        if not snapshot.valid:
            return "invalid_context_budget"
        usage = self._main_prompt_budget_usage(
            messages,
            tools_schema,
            snapshot.input_budget,
        )
        code, _message = self._main_prompt_budget_failure(usage)
        return code

    def _main_prompt_image_limits(self) -> ImageLimits:
        limits = getattr(self, "image_limits", None)
        return limits if isinstance(limits, ImageLimits) else ImageLimits()

    def _main_prompt_image_profile(self) -> ImageBudgetProfile | None:
        enabled = bool(getattr(self, "main_model_images_enabled", False))
        if not enabled:
            return None
        profile = getattr(self, "_image_budget_profile", None)
        if isinstance(profile, ImageBudgetProfile):
            return profile
        profile = resolve_image_budget_profile(
            enabled=True,
            profile=str(getattr(self, "image_budget_profile_name", "auto")),
            reserve_tokens=int(getattr(self, "image_token_reserve", 8192)),
        )
        self._image_budget_profile = profile
        return profile

    def _main_prompt_video_limits(self) -> VideoLimits:
        limits = getattr(self, "video_limits", None)
        return limits if isinstance(limits, VideoLimits) else VideoLimits()

    def _main_prompt_video_profile(self) -> VideoBudgetProfile | None:
        enabled = bool(getattr(self, "main_model_videos_enabled", False))
        if not enabled:
            return None
        profile = getattr(self, "_video_budget_profile", None)
        if isinstance(profile, VideoBudgetProfile):
            return profile
        profile = resolve_video_budget_profile(
            enabled=True,
            reserve_tokens=int(
                getattr(self, "video_token_reserve", DEFAULT_VIDEO_TOKEN_RESERVE)
            ),
        )
        self._video_budget_profile = profile
        return profile

    def _provider_request_body_limit_bytes(self) -> int | None:
        provider = getattr(getattr(self, "chat_agent", None), "provider", None)
        for owner in (self, provider):
            value = getattr(owner, "request_body_limit_bytes", None)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
        return None

    def _token_estimator(self) -> TokenEstimator:
        return TokenEstimator(
            model=getattr(self.chat_agent.cfg, "model", ""),
            calib_ratio=self._token_calib_ratio,
            provider=getattr(self, "_token_calib_provider", ""),
            include_image_assets=bool(getattr(self, "main_model_images_enabled", False)),
            image_budget_profile=self._main_prompt_image_profile(),
        )

    def _calibrate_tokens(
        self,
        estimated: int,
        actual_prompt_tokens: int,
        *,
        contains_images: bool = False,
    ) -> None:
        if contains_images:
            # 图片/视频使用显式 reserve，与 provider 的真实 prompt_tokens 口径不同；
            # 把差值当文字误差会把校准系数带偏。只记录跳过，不伪造 0 误差样本。
            self._last_calibration_skipped_reason = "multimodal_prompt"
            logger.info(
                "跳过纯文字 token 校准：本轮请求含多媒体 estimated=%s actual=%s",
                estimated,
                actual_prompt_tokens,
            )
            return
        if estimated <= 0 or actual_prompt_tokens <= 0:
            return
        store = getattr(self, "token_calibration", None)
        if store is not None:
            state = store.update_sample(
                getattr(self, "_token_calib_provider", ""),
                getattr(self, "_token_calib_model", ""),
                estimated_prompt_tokens=estimated,
                actual_prompt_tokens=actual_prompt_tokens,
            )
            self._token_calib_ratio = state.ratio
            self._token_calib_samples = state.samples
            self._token_calib_mape = state.mape
        else:
            estimator = self._token_estimator()
            estimator.update_calibration(estimated, actual_prompt_tokens)
            self._token_calib_ratio = estimator.calib_ratio
        # 用 info：这个系数直接决定上下文预算与压缩时机，藏在 debug 里会让
        # "估算高估 2 倍导致压缩风暴" 这类故障变得不可观测。
        logger.info(
            "token 估算校准：estimated=%s actual=%s ratio=%.3f samples=%s mape=%.3f",
            estimated,
            actual_prompt_tokens,
            self._token_calib_ratio,
            getattr(self, "_token_calib_samples", 0),
            getattr(self, "_token_calib_mape", 0.0),
        )

    def _rolling_summary_text(self, estimator: TokenEstimator | None = None) -> str:
        if self.rolling_summary is None:
            return ""
        text = self.rolling_summary.text()
        if not text:
            return ""
        estimator = estimator or self._token_estimator()
        return self._trim_text_to_token_budget(
            text,
            self._context_budget().summary_token_budget,
            estimator,
        )

    async def _important_memory_text(
        self,
        conversation_id: str | None,
        *,
        token_budget: int | None = None,
        member_qqs: set[int] | None = None,
    ) -> str:
        """按当前会话选择重要记忆注入文本，不受 RAG 开关影响。"""
        estimator = self._token_estimator()
        budget = token_budget or self._context_budget().memory_token_budget
        return self.important.text_for_context(
            conversation_id,
            token_budget=budget,
            estimator=estimator,
            member_qqs=member_qqs,
        )

    async def _rag_context_text(
        self,
        conversation_id: str | None,
        *,
        query: str | None,
        before_ts: str | None = None,
        token_budget: int | None = None,
    ) -> str:
        """按当前 query 检索历史对话片段。RAG 关闭或不可用时返回空。"""
        if (
            self.features_cfg.long_term_memory.mode != "rag"
            or self.rag_memory is None
            or not query
        ):
            return ""
        estimator = self._token_estimator()
        budget = token_budget or self._context_budget().memory_token_budget
        return await self.rag_memory.retrieve_for_query(
            query,
            conversation_id=conversation_id,
            before_ts=before_ts,
            top_k=self.features_cfg.long_term_memory.rag_top_k,
            token_budget=budget,
            estimator=estimator,
        )

    @staticmethod
    def _trim_text_to_token_budget(
        text: str,
        budget: int,
        estimator: TokenEstimator,
    ) -> str:
        if not text or estimator.estimate_text(text) <= budget:
            return text
        marker = "\n...[滚动摘要因上下文预算截断]...\n"
        marker_cost = estimator.estimate_text(marker)
        if budget <= marker_cost + 16:
            return text[: max(1, budget * 2)]

        head_budget = max(1, (budget - marker_cost) // 2)
        tail_budget = max(1, budget - marker_cost - head_budget)

        def fit_prefix(limit: int) -> str:
            lo, hi = 0, len(text)
            best = ""
            while lo <= hi:
                mid = (lo + hi) // 2
                candidate = text[:mid]
                if estimator.estimate_text(candidate) <= limit:
                    best = candidate
                    lo = mid + 1
                else:
                    hi = mid - 1
            return best.rstrip()

        def fit_suffix(limit: int) -> str:
            lo, hi = 0, len(text)
            best = ""
            while lo <= hi:
                mid = (lo + hi) // 2
                candidate = text[len(text) - mid :]
                if estimator.estimate_text(candidate) <= limit:
                    best = candidate
                    lo = mid + 1
                else:
                    hi = mid - 1
            return best.lstrip()

        return f"{fit_prefix(head_budget)}{marker}{fit_suffix(tail_budget)}"

    def _model_view_policy(self) -> ModelViewPolicy:
        cfg = getattr(self.behavior_cfg.context, "model_view", None)
        if cfg is None:
            return ModelViewPolicy()
        return ModelViewPolicy(
            enabled=bool(getattr(cfg, "enabled", True)),
            task_context_keep=int(getattr(cfg, "task_context_keep", 2)),
            send_receipt_keep=int(getattr(cfg, "send_receipt_keep", 2)),
            send_status_keep=int(getattr(cfg, "send_status_keep", 2)),
            no_action_keep=int(getattr(cfg, "no_action_keep", 4)),
            max_record_chars=int(getattr(cfg, "max_record_chars", 8000)),
            project_tool_results=bool(
                getattr(cfg, "project_tool_results", True)
            ),
        )

    async def _active_model_view(
        self,
        conversation_id: str | None,
    ) -> ModelViewResult:
        records = await self._active_history_records()
        view = build_model_view(
            records,
            policy=self._model_view_policy(),
            conversation_id=conversation_id,
        )
        logger.debug(
            "模型视图：raw=%s view=%s dropped_runtime=%s dropped_no_action=%s "
            "capped=%s projected_tools=%s",
            len(records),
            len(view.records),
            view.dropped_runtime,
            view.dropped_no_action_records,
            view.capped_records,
            view.projected_tool_results,
        )
        return view

    async def _select_working_history(
        self,
        conversation_id: str | None,
    ) -> list[dict[str, Any]]:
        """返回 rolling summary 游标之后、经过模型视图裁剪的活跃时间线。

        只裁剪传给模型的内容；``_active_history_records()`` 仍返回原始记录，
        供摘要/归档/工具检索使用。
        """
        view = await self._active_model_view(conversation_id)
        return view.records

    async def _select_proactive_router_history(self) -> list[dict[str, Any]]:
        """主动路由专用小窗口；先裁运行时噪声，再按预算取最近后缀。"""
        records = await self._select_working_history(None)
        return self._select_recent_records_for_budget(
            records,
            working_budget=min(
                self._working_history_budget(),
                self.behavior_cfg.proactive_router_history_token_budget,
            ),
        )

    async def _active_history_records(self) -> list[dict[str, Any]]:
        helper = getattr(self, "_active_history_window", None)
        if callable(helper):
            active, _start = await helper()
            return active
        records = await self.history.records()
        active_start = self._active_start_index_for_prompt(records)
        return records[active_start:]

    def _active_start_index_for_prompt(self, records: list[dict[str, Any]]) -> int:
        if self.rolling_summary is None:
            return 0
        helper = getattr(self, "_active_start_index_for_records", None)
        if callable(helper):
            return helper(records)
        return max(0, min(self.rolling_summary.active_start_index(), len(records)))

    def _fixed_prompt_overhead_tokens(self) -> int:
        """复用 raw token 缓存，按当前人格、工具和校准估算固定开销。"""
        fallback = int(
            getattr(self.behavior_cfg.context, "prompt_overhead_estimate_tokens", 12000)
            or 0
        )
        try:
            from agents.context_builder import build_admin_info, build_combined_system_prompt

            system_content = build_combined_system_prompt(
                self.persona,
                memory_mode=self.features_cfg.long_term_memory.mode,
                eat_tool=bool(getattr(self, "eat_tool", False)),
                sleep_tool=bool(getattr(self, "sleep_tool", False)),
            )
            tools = (
                self.tool_registry.get_schemas()
                if getattr(self, "tool_registry", None) is not None
                else None
            )
            fixed_messages = [{"role": "system", "content": system_content}]
            admin_info = build_admin_info(self.persona)
            if admin_info:
                fixed_messages.append({"role": "system", "content": admin_info})
            overhead = self._token_estimator().estimate_prompt(fixed_messages, tools)
        except Exception:
            logger.debug("估算固定 prompt 开销失败，使用配置回退值", exc_info=True)
            overhead = fallback
        return int(overhead)

    def _working_history_budget(self) -> int:
        budget = self._context_budget()
        context_cfg = self.behavior_cfg.context
        overhead = self._fixed_prompt_overhead_tokens()
        if overhead <= 0:
            overhead = context_cfg.prompt_overhead_estimate_tokens
        snapshot = self._active_budget_snapshot()
        if not snapshot.valid:
            return 0
        return max(
            0,
            snapshot.input_budget
            - budget.memory_token_budget
            - budget.summary_token_budget
            - overhead,
        )

    def _warn_context_compaction_invariants(self) -> None:
        working_budget = self._working_history_budget()
        summarize = self.behavior_cfg.summarize
        if self.summary_agent is None or self.archive is None or self.rolling_summary is None:
            logger.warning(
                "未启用滚动摘要/归档压缩；长会话超过主模型输入预算后会显式跳过模型调用"
            )
            return
        trigger = summarize.trigger_at_tokens
        if trigger is None:
            trigger = int(
                self._context_budget().max_context_tokens
                * summarize.trigger_at_context_percent
                / 100
            ) - self._fixed_prompt_overhead_tokens()
        trigger = max(1, trigger)
        if trigger >= working_budget:
            logger.warning(
                "滚动摘要触发线高于活跃记录预算：trigger=%s working_budget=%s；"
                "长会话可能先触发主模型预算预检失败",
                trigger,
                working_budget,
            )

    def _estimate_main_prompt_tokens(
        self,
        messages: list[dict[str, Any]],
        tools_schema: list[dict[str, Any]] | None,
        estimator: TokenEstimator | None = None,
    ) -> int:
        estimator = estimator or self._token_estimator()
        # 用与 provider 实际发送口径一致的规范 JSON 估算 messages + tools，
        # 不再用 str(tools_schema)（Python repr 会引入双引号/空格差异）。
        return estimator.estimate_prompt(messages, tools_schema)

    def _missing_protected_input(
        self,
        messages: list[dict[str, Any]],
        conversation_id: str | None,
    ) -> bool:
        """本轮登记的用户输入是否仍在待发送快照中。"""
        guards = [
            guard
            for guard in getattr(self, "_active_input_guards", [])
            if conversation_id is None
            or getattr(guard, "conversation_id", None) == conversation_id
        ]
        if not guards:
            return False
        for guard in guards:
            found = False
            message_ids = set(getattr(guard, "message_ids", ()))
            inbound_seqs = set(getattr(guard, "inbound_seqs", ()))
            for message in messages:
                if message.get("conversation_id") != getattr(
                    guard, "conversation_id", None
                ):
                    continue
                metadata = message.get("metadata")
                entries = (
                    metadata.get("messages")
                    if isinstance(metadata, dict)
                    else None
                )
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    seq = entry.get("inbound_seq")
                    if str(entry.get("message_id") or "") in message_ids or (
                        isinstance(seq, int)
                        and not isinstance(seq, bool)
                        and seq in inbound_seqs
                    ):
                        found = True
                        break
                if found:
                    break
            if not found:
                return True
        return False

    def _main_prompt_budget_usage(
        self,
        messages: list[dict[str, Any]],
        tools_schema: list[dict[str, Any]] | None,
        budget_tokens: int,
    ) -> MainPromptBudgetUsage:
        """构造轻量预算视图；严格校验媒体 refs，但不读取文件。"""
        estimator = self._token_estimator()
        images_enabled = bool(getattr(self, "main_model_images_enabled", False))
        videos_enabled = bool(getattr(self, "main_model_videos_enabled", False))
        if not images_enabled and not videos_enabled:
            return MainPromptBudgetUsage(
                estimated_tokens=estimator.estimate_prompt(messages, tools_schema),
                budget_tokens=budget_tokens,
                image_count=0,
                encoded_image_bytes=0,
                request_body_bytes=0,
                basis="text_only",
                contains_images=False,
                budget_messages=messages,
            )

        budget_messages = messages
        image_refs: list[dict[str, Any]] = []
        image_estimate = None
        if images_enabled:
            limits = self._main_prompt_image_limits()
            profile = self._main_prompt_image_profile()
            grouped = group_validated_refs(
                budget_messages,
                limits=limits,
                enforce_request_limits=False,
            )
            image_refs = [ref for group in grouped.values() for ref in group]
            if image_refs:
                budget_messages = build_budget_messages(
                    budget_messages, grouped
                )
            image_estimate = estimate_refs_budget(image_refs, profile=profile)

        video_refs: list[dict[str, Any]] = []
        video_estimate = None
        if videos_enabled:
            video_limits = self._main_prompt_video_limits()
            video_profile = self._main_prompt_video_profile()
            video_grouped = group_validated_video_refs(
                budget_messages,
                limits=video_limits,
                enforce_request_limits=False,
            )
            video_refs = [
                ref for group in video_grouped.values() for ref in group
            ]
            if video_refs:
                budget_messages = build_video_budget_messages(
                    budget_messages, video_grouped
                )
            video_estimate = estimate_video_refs_budget(
                video_refs, profile=video_profile
            )

        estimated = estimator.estimate_prompt(budget_messages, tools_schema)
        if video_estimate is not None:
            estimated += video_estimate.video_tokens
        image_bytes = (
            image_estimate.encoded_image_bytes if image_estimate is not None else 0
        )
        video_bytes = (
            video_estimate.encoded_video_bytes if video_estimate is not None else 0
        )
        body_bytes = (
            estimate_wire_request_body_bytes(
                budget_messages,
                tools_schema,
                encoded_image_bytes=image_bytes,
                encoded_video_bytes=video_bytes,
            )
            if image_refs or video_refs
            else 0
        )
        if image_refs:
            basis = image_estimate.basis if image_estimate is not None else "configured_reserve"
        elif video_refs:
            basis = (
                video_estimate.basis
                if video_estimate is not None
                else "configured_reserve"
            )
        else:
            basis = "text_only"
        return MainPromptBudgetUsage(
            estimated_tokens=estimated,
            budget_tokens=budget_tokens,
            image_count=(
                image_estimate.image_count if image_estimate is not None else 0
            ),
            encoded_image_bytes=image_bytes,
            request_body_bytes=body_bytes,
            basis=basis,
            contains_images=bool(image_refs),
            budget_messages=budget_messages,
            contains_videos=bool(video_refs),
            video_count=(
                video_estimate.video_count if video_estimate is not None else 0
            ),
            encoded_video_bytes=video_bytes,
            video_tokens=(
                video_estimate.video_tokens if video_estimate is not None else 0
            ),
        )

    def _main_prompt_budget_failure(
        self,
        usage: MainPromptBudgetUsage,
    ) -> tuple[str, str]:
        """返回受控 (code, message)；空 tuple 表示所有维度均通过。"""
        limits = self._main_prompt_image_limits()
        if usage.contains_images:
            if usage.image_count > limits.max_images_per_request:
                return (
                    "image_request_count_exceeded",
                    "请求图片数量超过限制",
                )
            if usage.encoded_image_bytes > limits.max_encoded_image_bytes:
                return (
                    "image_request_bytes_exceeded",
                    "请求图片编码字节超过限制",
                )
        video_limits = self._main_prompt_video_limits()
        if usage.contains_videos:
            if usage.video_count > video_limits.max_videos_per_request:
                return (
                    "video_request_count_exceeded",
                    "请求视频数量超过限制",
                )
            if usage.encoded_video_bytes > video_limits.max_encoded_video_bytes:
                return (
                    "video_request_bytes_exceeded",
                    "请求视频编码字节超过限制",
                )
        if usage.contains_images or usage.contains_videos:
            body_limit = (
                request_body_limit(
                    video_limits,
                    provider_limit_bytes=self._provider_request_body_limit_bytes(),
                )
                if usage.contains_videos
                else request_body_limit(
                    limits,
                    provider_limit_bytes=self._provider_request_body_limit_bytes(),
                )
            )
            if usage.request_body_bytes > body_limit:
                return (
                    "provider_request_body_exceeded",
                    "完整请求体超过限制",
                )
        if usage.estimated_tokens > usage.budget_tokens:
            return (
                "prompt_token_budget_exceeded",
                "主模型输入超过上下文预算",
            )
        return "", ""

    def _new_budget_result(
        self,
        messages: list[dict[str, Any]],
        usage: MainPromptBudgetUsage,
        *,
        pre_summary_status: str = "",
        attempts: list[str] | None = None,
        tools_schema: list[dict[str, Any]] | None = None,
    ) -> MainPromptBudgetResult:
        call_snapshot: ModelCallSnapshot | None = None
        try:
            snapshot = self._active_budget_snapshot()
            binding = self._active_model_binding()
            guards = list(getattr(self, "_active_input_guards", []) or [])
            protected_ids: list[str] = []
            protected_seqs: list[int] = []
            for g in guards:
                protected_ids.extend(str(mid) for mid in getattr(g, "message_ids", ()))
                protected_seqs.extend(int(s) for s in getattr(g, "inbound_seqs", ()))
            call_snapshot = build_model_call_snapshot(
                binding,
                snapshot,
                tools_schema=tools_schema,
                estimator=self._token_estimator(),
                image_limits=self._main_prompt_image_limits(),
                request_body_limit_bytes=self._provider_request_body_limit_bytes(),
                calib_ratio=getattr(self, "_token_calib_ratio", 1.0),
                protected_message_ids=protected_ids,
                protected_inbound_seqs=protected_seqs,
            )
        except Exception:
            logger.debug("构建 ModelCallSnapshot 失败（忽略）", exc_info=True)

        return MainPromptBudgetResult(
            ok=True,
            messages=messages,
            estimated_tokens=usage.estimated_tokens,
            budget_tokens=usage.budget_tokens,
            pre_summary_status=pre_summary_status,
            attempts=list(attempts or []),
            contains_images=usage.contains_images,
            image_count=usage.image_count,
            image_bytes=usage.encoded_image_bytes,
            contains_videos=usage.contains_videos,
            video_count=usage.video_count,
            video_bytes=usage.encoded_video_bytes,
            video_tokens=usage.video_tokens,
            request_body_bytes=usage.request_body_bytes,
            budget_basis=usage.basis,
            model_call_snapshot=call_snapshot,
        )

    async def _failed_budget_result(
        self,
        *,
        code: str,
        message: str,
        messages: list[dict[str, Any]],
        usage: MainPromptBudgetUsage | None,
        conversation_id: str | None,
        phase: str,
        attempts: list[str],
        pre_summary_status: str = "",
    ) -> MainPromptBudgetResult:
        failure_code = code or "prompt_token_budget_exceeded"
        failed_attempts = [*attempts, f"failure:{failure_code}"]
        result = MainPromptBudgetResult(
            ok=False,
            messages=messages,
            estimated_tokens=usage.estimated_tokens if usage is not None else 0,
            budget_tokens=(
                usage.budget_tokens
                if usage is not None
                else self._context_budget().total_input_budget
            ),
            pre_summary_status=pre_summary_status,
            attempts=failed_attempts,
            failure_reason=failure_code,
            failure_code=failure_code,
            contains_images=usage.contains_images if usage is not None else False,
            image_count=usage.image_count if usage is not None else 0,
            image_bytes=usage.encoded_image_bytes if usage is not None else 0,
            contains_videos=usage.contains_videos if usage is not None else False,
            video_count=usage.video_count if usage is not None else 0,
            video_bytes=usage.encoded_video_bytes if usage is not None else 0,
            video_tokens=usage.video_tokens if usage is not None else 0,
            request_body_bytes=usage.request_body_bytes if usage is not None else 0,
            budget_basis=usage.basis if usage is not None else "text_only",
        )
        logger.error(
            "主模型输入预检失败 code=%s message=%s phase=%s estimated=%s budget=%s",
            failure_code,
            message,
            phase,
            result.estimated_tokens,
            result.budget_tokens,
        )
        await self._record_main_prompt_budget_failure(
            conversation_id=conversation_id,
            phase=phase,
            estimated_tokens=result.estimated_tokens,
            budget_tokens=result.budget_tokens,
            attempts=failed_attempts,
            failure_code=failure_code,
            image_count=result.image_count,
            image_bytes=result.image_bytes,
            video_count=result.video_count,
            video_bytes=result.video_bytes,
        )
        return result

    async def _project_pending_media_record(
        self,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        """Project media for a user record appended inside a running tool loop.

        Such records are built by ``_consume_send_receipts`` after the main prompt
        was already projected. They must be projected onto the model copy, while
        the raw record (with trusted refs) remains the history record. If one
        media kind fails or is disabled, only that kind is stripped from the
        model copy so the provider guard cannot see unmatched refs.
        """
        if record.get("role") != "user":
            return record
        metadata = record.get("metadata")
        if not isinstance(metadata, Mapping):
            return record
        if "image_assets" not in metadata and "video_assets" not in metadata:
            return record

        projected = record
        if metadata.get("image_assets"):
            if bool(getattr(self, "main_model_images_enabled", False)):
                try:
                    store = self._image_asset_store()
                    if store is None:
                        raise ImageAssetError(
                            "未配置图片 workspace，无法向主模型提供图片",
                            code="image_source_unavailable",
                        )
                    projected = await store.project_record(dict(projected))
                except Exception as exc:  # noqa: BLE001
                    code = (
                        exc.code
                        if isinstance(exc, ImageAssetError)
                        else "image_projection_failed"
                    )
                    safe = (
                        exc.safe_message()
                        if isinstance(exc, ImageAssetError)
                        else "图片投影失败"
                    )
                    logger.warning(
                        "待注入消息图片投影失败，仅保留文本占位 code=%s message=%s",
                        code,
                        safe,
                    )
                    projected = self._strip_pending_media(
                        projected, ("image_assets",)
                    )
            else:
                projected = self._strip_pending_media(
                    projected, ("image_assets",)
                )

        projected_metadata = projected.get("metadata")
        if (
            isinstance(projected_metadata, Mapping)
            and projected_metadata.get("video_assets")
        ):
            if bool(getattr(self, "main_model_videos_enabled", False)):
                try:
                    store = self._video_asset_store()
                    if store is None:
                        raise VideoAssetError(
                            "未配置视频 workspace，无法向主模型提供视频",
                            code="video_source_unavailable",
                        )
                    projected = await store.project_record(dict(projected))
                except Exception as exc:  # noqa: BLE001
                    code = (
                        exc.code
                        if isinstance(exc, VideoAssetError)
                        else "video_projection_failed"
                    )
                    safe = (
                        exc.safe_message()
                        if isinstance(exc, VideoAssetError)
                        else "视频投影失败"
                    )
                    logger.warning(
                        "待注入消息视频投影失败，仅保留文本占位 code=%s message=%s",
                        code,
                        safe,
                    )
                    projected = self._strip_pending_media(
                        projected, ("video_assets",)
                    )
            else:
                projected = self._strip_pending_media(
                    projected, ("video_assets",)
                )
        return projected

    @staticmethod
    def _strip_pending_media(
        record: dict[str, Any],
        keys: tuple[str, ...],
    ) -> dict[str, Any]:
        copied = dict(record)
        metadata = record.get("metadata")
        if isinstance(metadata, Mapping):
            clean = dict(metadata)
            for key in keys:
                clean.pop(key, None)
            copied["metadata"] = clean
        return copied

    async def _finalize_main_prompt_images(
        self,
        result: MainPromptBudgetResult,
        *,
        conversation_id: str | None,
        phase: str,
        tools_schema: list[dict[str, Any]] | None,
    ) -> MainPromptBudgetResult:
        """先实体化图片，再追加完整视频；任一步失败都显式拒绝本次请求。"""
        result = await self._finalize_main_prompt_images_only(
            result,
            conversation_id=conversation_id,
            phase=phase,
            tools_schema=tools_schema,
        )
        if not result.ok:
            return result
        return await self._finalize_main_prompt_videos(
            result,
            conversation_id=conversation_id,
            phase=phase,
            tools_schema=tools_schema,
        )

    async def _finalize_main_prompt_images_only(
        self,
        result: MainPromptBudgetResult,
        *,
        conversation_id: str | None,
        phase: str,
        tools_schema: list[dict[str, Any]] | None,
    ) -> MainPromptBudgetResult:
        """预算通过后才加载受管图片；失败显式拒绝本次请求，不静默删图。"""
        if not getattr(self, "main_model_images_enabled", False):
            return result
        if not result.contains_images:
            return result
        limits = self._main_prompt_image_limits()
        try:
            usage = self._main_prompt_budget_usage(
                result.messages,
                tools_schema,
                result.budget_tokens,
            )
            code, message = self._main_prompt_budget_failure(usage)
            if code:
                raise ImageAssetError(message, code=code)
            store = self._image_asset_store()
            if store is None:
                raise ImageAssetError(
                    "未配置图片 workspace，无法向主模型提供图片",
                    code="image_source_unavailable",
                )
            projected = await store.project_records(result.messages)
            image_count, image_bytes = assert_wire_request_fits(
                projected,
                tools_schema=tools_schema,
                limits=limits,
                provider_limit_bytes=self._provider_request_body_limit_bytes(),
            )
            if image_count != usage.image_count:
                raise ImageAssetError(
                    "图片投影数量与声明不一致",
                    code="image_projection_failed",
                )
            estimated = (
                self._estimate_main_prompt_tokens(projected, tools_schema)
                + result.video_tokens
            )
            if estimated > result.budget_tokens:
                raise ImageAssetError(
                    "图片消息组装后超过上下文预算",
                    code="prompt_token_budget_exceeded",
                )
            body_bytes = estimate_wire_request_body_bytes(
                projected,
                tools_schema,
                encoded_image_bytes=image_bytes,
                encoded_video_bytes=result.video_bytes,
            )
        except (ImageAssetError, VideoAssetError) as exc:
            result.ok = False
            result.failure_reason = exc.code
            result.failure_code = exc.code
            kind = "videos" if isinstance(exc, VideoAssetError) else "images"
            result.attempts.append(f"{kind}:{exc.code}:{exc.safe_message()}")
            logger.error(
                "主模型媒体最终投影失败 code=%s phase=%s", exc.code, phase
            )
            await self._record_main_prompt_budget_failure(
                conversation_id=conversation_id,
                phase=phase,
                estimated_tokens=result.estimated_tokens,
                budget_tokens=result.budget_tokens,
                attempts=result.attempts,
                failure_code=exc.code,
                image_count=result.image_count,
                image_bytes=result.image_bytes,
                video_count=result.video_count,
                video_bytes=result.video_bytes,
            )
            return result
        except Exception:
            logger.exception("主模型图片投影发生未预期错误 phase=%s", phase)
            result.ok = False
            result.failure_reason = "image_projection_failed"
            result.failure_code = "image_projection_failed"
            result.attempts.append("images:image_projection_failed")
            await self._record_main_prompt_budget_failure(
                conversation_id=conversation_id,
                phase=phase,
                estimated_tokens=result.estimated_tokens,
                budget_tokens=result.budget_tokens,
                attempts=result.attempts,
                failure_code="image_projection_failed",
                image_count=result.image_count,
                image_bytes=result.image_bytes,
                video_count=result.video_count,
                video_bytes=result.video_bytes,
            )
            return result
        result.messages = projected
        result.estimated_tokens = estimated
        result.image_count = image_count
        result.image_bytes = image_bytes
        result.request_body_bytes = body_bytes
        result.budget_basis = usage.basis
        result.contains_images = True
        return result

    async def _finalize_main_prompt_videos(
        self,
        result: MainPromptBudgetResult,
        *,
        conversation_id: str | None,
        phase: str,
        tools_schema: list[dict[str, Any]] | None,
    ) -> MainPromptBudgetResult:
        """图片实体化后追加完整视频；失败显式拒绝本次请求，不抽帧降级。"""
        if not getattr(self, "main_model_videos_enabled", False):
            return result
        if not result.contains_videos:
            return result
        limits = self._main_prompt_video_limits()
        profile = self._main_prompt_video_profile()
        try:
            store = self._video_asset_store()
            if store is None:
                raise VideoAssetError(
                    "未配置视频 workspace，无法向主模型提供视频",
                    code="video_source_unavailable",
                )
            projected = await store.project_records(result.messages)
            video_count, video_bytes = assert_wire_video_request_fits(
                projected,
                tools_schema=tools_schema,
                limits=limits,
                provider_limit_bytes=self._provider_request_body_limit_bytes(),
                encoded_image_bytes=result.image_bytes,
            )
            if video_count != result.video_count:
                raise VideoAssetError(
                    "视频投影数量与声明不一致",
                    code="video_projection_failed",
                )
            estimated = self._estimate_main_prompt_tokens(projected, tools_schema)
            if profile is not None:
                estimated += video_count * profile.per_video_tokens
            if estimated > result.budget_tokens:
                raise VideoAssetError(
                    "视频消息组装后超过上下文预算",
                    code="prompt_token_budget_exceeded",
                )
            body_bytes = estimate_wire_request_body_bytes(
                projected,
                tools_schema,
                encoded_image_bytes=result.image_bytes,
                encoded_video_bytes=video_bytes,
            )
        except VideoAssetError as exc:
            result.ok = False
            result.failure_reason = exc.code
            result.failure_code = exc.code
            result.attempts.append(f"videos:{exc.code}:{exc.safe_message()}")
            logger.error(
                "主模型视频最终投影失败 code=%s phase=%s", exc.code, phase
            )
            await self._record_main_prompt_budget_failure(
                conversation_id=conversation_id,
                phase=phase,
                estimated_tokens=result.estimated_tokens,
                budget_tokens=result.budget_tokens,
                attempts=result.attempts,
                failure_code=exc.code,
                image_count=result.image_count,
                image_bytes=result.image_bytes,
                video_count=result.video_count,
                video_bytes=result.video_bytes,
            )
            return result
        except Exception:
            logger.exception("主模型视频投影发生未预期错误 phase=%s", phase)
            result.ok = False
            result.failure_reason = "video_projection_failed"
            result.failure_code = "video_projection_failed"
            result.attempts.append("videos:video_projection_failed")
            await self._record_main_prompt_budget_failure(
                conversation_id=conversation_id,
                phase=phase,
                estimated_tokens=result.estimated_tokens,
                budget_tokens=result.budget_tokens,
                attempts=result.attempts,
                failure_code="video_projection_failed",
                image_count=result.image_count,
                image_bytes=result.image_bytes,
                video_count=result.video_count,
                video_bytes=result.video_bytes,
            )
            return result
        result.messages = projected
        result.estimated_tokens = estimated
        result.video_count = video_count
        result.video_bytes = video_bytes
        if profile is not None:
            result.video_tokens = video_count * profile.per_video_tokens
        result.request_body_bytes = body_bytes
        result.contains_videos = True
        return result

    async def _prepare_main_prompt_for_model(
        self,
        *,
        conversation_id: str | None,
        phase: str,
        tools_schema: list[dict[str, Any]] | None,
        rebuild_messages: Callable[[], Awaitable[list[dict[str, Any]]]],
    ) -> MainPromptBudgetResult:
        """统一预检：严格校验 → 预算视图 → 压缩重试 → 实体化 → 最终校验。"""
        snapshot = self._active_budget_snapshot()
        if not snapshot.valid:
            return await self._failed_budget_result(
                code="invalid_context_budget",
                message=f"上下文预算不合法：{','.join(snapshot.errors)}",
                messages=[],
                usage=None,
                conversation_id=conversation_id,
                phase=phase,
                attempts=[f"budget:{','.join(snapshot.errors)}"],
            )
        budget_tokens = snapshot.input_budget
        estimator = self._token_estimator()
        attempts: list[str] = []
        pre_summary_status = ""

        async def load_checked() -> tuple[list[dict[str, Any]], MainPromptBudgetUsage]:
            messages = await rebuild_messages()
            if self._missing_protected_input(messages, conversation_id):
                raise ImageAssetError(
                    "本轮用户输入已不在活跃上下文，停止本轮回调",
                    code="protected_input_missing",
                )
            usage = self._main_prompt_budget_usage(
                messages,
                tools_schema,
                budget_tokens,
            )
            return messages, usage

        try:
            messages, usage = await load_checked()
        except (ImageAssetError, VideoAssetError) as exc:
            return await self._failed_budget_result(
                code=exc.code,
                message=exc.safe_message(),
                messages=[],
                usage=None,
                conversation_id=conversation_id,
                phase=phase,
                attempts=attempts,
            )

        pre_summary = await self._maybe_summarize(reason=f"{phase}_preflight")
        pre_summary_status = f"{pre_summary.status}:{pre_summary.reason}"
        if pre_summary.status == "success":
            try:
                messages, usage = await load_checked()
            except (ImageAssetError, VideoAssetError) as exc:
                return await self._failed_budget_result(
                    code=exc.code,
                    message=exc.safe_message(),
                    messages=messages,
                    usage=usage,
                    conversation_id=conversation_id,
                    phase=phase,
                    attempts=attempts,
                    pre_summary_status=pre_summary_status,
                )
        elif pre_summary.status == "failed" and pre_summary.reason in {
            "invalid_asset_reference",
            "protected_input_missing",
            "protected_input_budget",
        }:
            return await self._failed_budget_result(
                code=pre_summary.reason,
                message="图片资产或受保护输入阻止了历史压缩",
                messages=messages,
                usage=usage,
                conversation_id=conversation_id,
                phase=phase,
                attempts=[
                    f"summary:{pre_summary.status}:{pre_summary.reason}"
                ],
                pre_summary_status=pre_summary_status,
            )

        failure_code, failure_message = self._main_prompt_budget_failure(usage)
        if not failure_code:
            return await self._finalize_main_prompt_images(
                self._new_budget_result(
                    messages,
                    usage,
                    pre_summary_status=pre_summary_status,
                    attempts=attempts,
                    tools_schema=tools_schema,
                ),
                conversation_id=conversation_id,
                phase=phase,
                tools_schema=tools_schema,
            )
        attempts.append(
            f"precheck:{failure_code}:tokens={usage.estimated_tokens}/"
            f"{usage.budget_tokens}:images={usage.image_count}:"
            f"bytes={usage.encoded_image_bytes}:body={usage.request_body_bytes}"
        )

        first_target = await self._first_budget_retry_target(estimator)
        if first_target is not None:
            first = await self._compact_active_history(
                target_after_tokens=first_target,
                reason=f"{phase}_budget_first",
            )
            attempts.append(
                f"first:{first.status}:{first.reason}:archived={first.archived_count}"
            )
            try:
                messages, usage = await load_checked()
            except (ImageAssetError, VideoAssetError) as exc:
                return await self._failed_budget_result(
                    code=exc.code,
                    message=exc.safe_message(),
                    messages=messages,
                    usage=usage,
                    conversation_id=conversation_id,
                    phase=phase,
                    attempts=attempts,
                    pre_summary_status=pre_summary_status,
                )
            failure_code, failure_message = self._main_prompt_budget_failure(usage)
            if not failure_code:
                return await self._finalize_main_prompt_images(
                    self._new_budget_result(
                        messages,
                        usage,
                        pre_summary_status=pre_summary_status,
                        attempts=attempts,
                    ),
                    conversation_id=conversation_id,
                    phase=phase,
                    tools_schema=tools_schema,
                )
            attempts.append(f"first_after:{failure_code}")
            retry_target = await self._retry_budget_target(
                estimator,
                first_target=first_target,
            )
        else:
            attempts.append("first:failed:no_compressible_active_history")
            retry_target = await self._retry_budget_target(
                estimator,
                first_target=None,
            )

        retry = await self._compact_active_history(
            target_after_tokens=retry_target,
            reason=f"{phase}_budget_retry_config_target",
        )
        attempts.append(
            f"retry:{retry.status}:{retry.reason}:archived={retry.archived_count}"
        )
        try:
            messages, usage = await load_checked()
        except (ImageAssetError, VideoAssetError) as exc:
            return await self._failed_budget_result(
                code=exc.code,
                message=exc.safe_message(),
                messages=messages,
                usage=usage,
                conversation_id=conversation_id,
                phase=phase,
                attempts=attempts,
                pre_summary_status=pre_summary_status,
            )
        failure_code, failure_message = self._main_prompt_budget_failure(usage)
        if not failure_code:
            return await self._finalize_main_prompt_images(
                self._new_budget_result(
                    messages,
                    usage,
                    pre_summary_status=pre_summary_status,
                    attempts=attempts,
                    tools_schema=tools_schema,
                ),
                conversation_id=conversation_id,
                phase=phase,
                tools_schema=tools_schema,
            )

        attempts.append(f"retry_after:{failure_code}")
        return await self._failed_budget_result(
            code=failure_code,
            message=failure_message,
            messages=messages,
            usage=usage,
            conversation_id=conversation_id,
            phase=phase,
            attempts=attempts,
            pre_summary_status=pre_summary_status,
        )

    async def _first_budget_retry_target(
        self,
        estimator: TokenEstimator,
    ) -> int | None:
        view = await self._active_model_view(None)
        if len(view.records) <= 1:
            return None
        active_tokens = estimator.estimate_messages(view.records)
        return self._summary_target_after_tokens(active_tokens=active_tokens)

    async def _retry_budget_target(
        self,
        estimator: TokenEstimator,
        *,
        first_target: int | None,
    ) -> int:
        view = await self._active_model_view(None)
        if not view.records:
            return 1
        active_tokens = estimator.estimate_messages(view.records)
        if active_tokens <= 1:
            return 1
        retry_percent = self.behavior_cfg.summarize.retry_target_after_context_percent
        retry_target_total = int(
            self._context_budget().max_context_tokens
            * retry_percent
            / 100
        )
        # 压缩目标是“模型可见历史”的 token；固定 system/tools 开销不参与压缩。
        retry_target = max(
            1,
            retry_target_total - self._fixed_prompt_overhead_tokens(),
        )
        retry_target = max(1, min(retry_target, active_tokens - 1))
        if first_target is not None:
            retry_target = min(retry_target, max(1, first_target))
        return retry_target

    async def _record_main_prompt_budget_failure(
        self,
        *,
        conversation_id: str | None,
        phase: str,
        estimated_tokens: int,
        budget_tokens: int,
        attempts: list[str],
        failure_code: str = "",
        image_count: int = 0,
        image_bytes: int = 0,
        video_count: int = 0,
        video_bytes: int = 0,
    ) -> None:
        note = (
            "[上下文预算] 主模型输入预检失败，已跳过本轮模型调用；"
            f"phase={phase}，code={failure_code or 'unknown'}，"
            f"estimated={estimated_tokens} tokens，"
            f"budget={budget_tokens} tokens，images={image_count}，"
            f"image_bytes={image_bytes}，videos={video_count}，"
            f"video_bytes={video_bytes}，attempts={'; '.join(attempts)}。"
        )
        logger.error(note)
        await self.history.add_system_note(
            note,
            conversation_id=conversation_id or "system:context_budget",
        )

    def _select_recent_records_for_budget(
        self,
        records: list[dict[str, Any]],
        *,
        working_budget: int,
    ) -> list[dict[str, Any]]:
        """从活跃记录尾部按 token 预算取最近后缀。"""
        if not records:
            return []
        estimator = self._token_estimator()
        selected: list[dict[str, Any]] = []
        used = 0
        for record in reversed(records):
            cost = estimator.estimate_messages([record])
            if selected and used + cost > working_budget:
                break
            selected.append(record)
            used += cost
        return list(reversed(selected))
