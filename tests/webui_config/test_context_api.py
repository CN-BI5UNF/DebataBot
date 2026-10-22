"""上下文状态 API：展示有效窗口、输出预留和净输入预算。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

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
