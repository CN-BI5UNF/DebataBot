"""上下文状态 API：展示有效窗口、输出预留和净输入预算。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app_config.schema import BehaviorConfig
from utils.token_budget import TokenBudget
from webui.api.context import status


def _request(rt):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                host=SimpleNamespace(rt=rt, state="running", last_error="")
            )
        )
    )


class _History:
    async def stats(self):
        return {
            "record_count": 1200,
            "active_start_index": 1000,
            "max_history_index": 1199,
            "page_cache_bytes": 4096,
            "page_cache_entries": 2,
            "full_cache_loaded": False,
        }


class _Rolling:
    def active_start_index(self):
        return 1000


@pytest.mark.asyncio
async def test_context_status_uses_model_limit_and_actual_output_reserve():
    behavior = BehaviorConfig()
    behavior.context.max_context_tokens = 131072
    behavior.context.reserve_output_tokens = 8192
    behavior.context.budget_policy_version = 2
    pipeline = SimpleNamespace(
        behavior_cfg=behavior,
        chat_agent=SimpleNamespace(
            cfg=SimpleNamespace(
                provider="si",
                model="deepseek-flash",
                max_tokens=5120,
            )
        ),
        model_context_length=64000,
        _context_budget=lambda: TokenBudget(131072, 8192, 4096, 4096),
        _fixed_prompt_overhead_tokens=lambda: 12000,
        _working_history_budget=lambda: 30000,
    )
    rt = SimpleNamespace(
        pipeline=pipeline,
        history=_History(),
        rolling_summary=_Rolling(),
    )

    data = await status(_request(rt))

    budget = data["budget"]
    assert budget["valid"] is True
    assert budget["window_requested"] == 131072
    assert budget["window_model"] == 64000
    assert budget["window_effective"] == 64000
    assert budget["output_reserve"] == 8192
    assert budget["safety_margin"] == 5120
    assert budget["input_budget"] == 50688
    assert budget["policy_version"] == 2
    assert data["history"]["active_start_index"] == 1000


@pytest.mark.asyncio
async def test_context_status_reports_invalid_budget_instead_of_fake_space():
    behavior = BehaviorConfig()
    behavior.context.max_context_tokens = 4096
    behavior.context.reserve_output_tokens = 8192
    pipeline = SimpleNamespace(
        behavior_cfg=behavior,
        chat_agent=SimpleNamespace(
            cfg=SimpleNamespace(provider="p", model="m", max_tokens=8192)
        ),
        model_context_length=None,
        _context_budget=lambda: TokenBudget(4096, 8192, 4096, 4096),
        _fixed_prompt_overhead_tokens=lambda: 12000,
        _working_history_budget=lambda: 1,
    )
    rt = SimpleNamespace(
        pipeline=pipeline,
        history=_History(),
        rolling_summary=_Rolling(),
    )

    data = await status(_request(rt))
    budget = data["budget"]
    assert budget["valid"] is False
    assert budget["input_budget"] == 0
    assert "output_reserve_exceeds_window" in budget["errors"]
    assert budget["window_model"] is None


@pytest.mark.asyncio
async def test_context_status_includes_compaction_state():
    behavior = BehaviorConfig()
    pipeline = SimpleNamespace(
        behavior_cfg=behavior,
        chat_agent=SimpleNamespace(
            cfg=SimpleNamespace(provider="p", model="m", max_tokens=5120)
        ),
        model_context_length=None,
        _context_budget=lambda: TokenBudget(131072, 8192, 4096, 4096),
        _fixed_prompt_overhead_tokens=lambda: 12000,
        _working_history_budget=lambda: 30000,
        compaction_status=lambda: {
            "last_action": "cooldown",
            "last_skip_reason": "",
            "background_enabled": True,
        },
    )
    rt = SimpleNamespace(
        pipeline=pipeline,
        history=_History(),
        rolling_summary=_Rolling(),
    )

    data = await status(_request(rt))

    assert data["compaction"]["last_action"] == "cooldown"
    assert any("C02" in note for note in data["notes"])


@pytest.mark.asyncio
async def test_manual_compact_returns_real_result_without_faking_archive():
    from core.pipeline_summary import SummaryAttemptResult
    from webui.api.context import compact

    calls = []

    async def runner():
        calls.append(True)
        return SummaryAttemptResult(
            status="success",
            reason="manual",
            archived_count=3,
            active_records=4,
            active_tokens=900,
            target_after_tokens=500,
        )

    pipeline = SimpleNamespace(
        compact_manual=runner,
        compaction_status=lambda: {"last_action": "cooldown"},
    )
    rt = SimpleNamespace(pipeline=pipeline, history=_History(), rolling_summary=_Rolling())

    data = await compact(_request(rt))

    assert calls == [True]
    assert data["ok"] is True
    assert data["result"]["archived_count"] == 3
    assert data["compaction"]["last_action"] == "cooldown"


@pytest.mark.asyncio
async def test_manual_compact_rejects_when_background_job_running():
    from webui.api.context import compact

    class RunningTask:
        def done(self):
            return False

    pipeline = SimpleNamespace(_summary_task=RunningTask())
    rt = SimpleNamespace(pipeline=pipeline, history=_History(), rolling_summary=_Rolling())

    with pytest.raises(HTTPException) as exc:
        await compact(_request(rt))
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "compaction_busy"
