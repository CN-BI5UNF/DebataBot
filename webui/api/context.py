"""上下文预算只读状态。

本轮只展示统一预算快照与当前配置，不切换运行时压缩策略、不触发模型调用。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from utils.token_budget import build_budget_snapshot

from .deps import require_runtime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/context", tags=["context"])


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    rt = require_runtime(request)
    pipeline = getattr(rt, "pipeline", None)
    if pipeline is None:
        raise HTTPException(status_code=503, detail="消息管道尚未就绪")
    context_cfg = pipeline.behavior_cfg.context
    summarize_cfg = pipeline.behavior_cfg.summarize
    chat_cfg = getattr(getattr(pipeline, "chat_agent", None), "cfg", None)
    model_window = getattr(pipeline, "model_context_length", None)
    try:
        model_window = int(model_window) if model_window else None
    except (TypeError, ValueError):
        model_window = None
    try:
        requested = int(
            context_cfg.max_context_tokens
            or pipeline._context_budget().max_context_tokens
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"计算上下文预算失败：{type(exc).__name__}") from exc
    actual_max = getattr(chat_cfg, "max_tokens", None)
    snapshot = build_budget_snapshot(
        window_requested=requested,
        model_window=model_window,
        model_limit_verified=model_window is not None,
        window_source="configured" if context_cfg.max_context_tokens else "recommended",
        output_reserve=int(context_cfg.reserve_output_tokens),
        actual_max_tokens=actual_max if isinstance(actual_max, int) else None,
        safety_margin_percent=int(context_cfg.safety_margin_percent),
        safety_margin_min_tokens=int(context_cfg.safety_margin_min_tokens),
        policy_version=int(context_cfg.budget_policy_version),
        soft_percent=int(summarize_cfg.trigger_at_context_percent),
        target_percent=int(summarize_cfg.target_after_context_percent),
        retry_percent=int(summarize_cfg.retry_target_after_context_percent),
        urgent_percent=int(summarize_cfg.urgent_at_input_percent),
    )
    fixed_overhead = 0
    working_history_budget = 0
    try:
        fixed_overhead = int(pipeline._fixed_prompt_overhead_tokens())
        working_history_budget = int(pipeline._working_history_budget())
    except Exception:  # noqa: BLE001
        logger.debug("上下文状态：固定开销估算失败", exc_info=True)
    history_stats: dict[str, Any] = {}
    try:
        history_stats = await rt.history.stats()
    except Exception:  # noqa: BLE001
        logger.debug("上下文状态：历史统计失败", exc_info=True)
    active_start = 0
    try:
        rolling = getattr(rt, "rolling_summary", None)
        if rolling is not None:
            active_start = int(rolling.active_start_index())
    except Exception:  # noqa: BLE001
        active_start = 0
    legacy_trigger = max(
        1,
        int(
            snapshot.window_effective
            * int(summarize_cfg.trigger_at_context_percent)
            / 100
        )
        - fixed_overhead,
    )
    return {
        "provider": getattr(chat_cfg, "provider", ""),
        "model": getattr(chat_cfg, "model", ""),
        "config": {
            "max_context_tokens": context_cfg.max_context_tokens,
            "reserve_output_tokens": int(context_cfg.reserve_output_tokens),
            "budget_policy_version": int(context_cfg.budget_policy_version),
            "safety_margin_percent": int(context_cfg.safety_margin_percent),
            "safety_margin_min_tokens": int(context_cfg.safety_margin_min_tokens),
            "summary_token_budget": int(context_cfg.summary_token_budget),
            "memory_token_budget": int(context_cfg.memory_token_budget),
        },
        "budget": snapshot.as_dict(),
        "fixed_overhead_tokens": fixed_overhead,
        "working_history_budget": working_history_budget,
        "legacy_trigger_estimate": legacy_trigger,
        "history": {
            "record_count": int(history_stats.get("record_count") or 0),
            "active_start_index": active_start,
            "max_history_index": int(history_stats.get("max_history_index") or 0),
            "page_cache_bytes": int(history_stats.get("page_cache_bytes") or 0),
            "page_cache_entries": int(history_stats.get("page_cache_entries") or 0),
            "full_cache_loaded": bool(history_stats.get("full_cache_loaded")),
        },
        "notes": [
            "budget 为本地估算，不是供应商账单或硬保证",
            "window_effective = min(工作窗口, 已核实的模型上下文限制)",
            "本轮 budget_policy_version=2 仅展示净输入预算，不切换运行时阈值",
            "provider 鉴权/余额错误不属于上下文超限，不会被本地预算拦住",
        ],
    }


__all__ = ["router"]
