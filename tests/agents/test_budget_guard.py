"""统一预算硬 guard：已知超限时不得调用 provider。"""

from __future__ import annotations

import pytest

from agents.runner import AgentRunner
from agents.summary_agent import SummaryAgent
from app_config.schema import AgentConfig, SummarizeConfig
from providers.base import CompletionResult, Usage
from utils.token_budget import TokenEstimator


class _RecordingProvider:
    name = "recording"

    def __init__(self) -> None:
        self.calls = 0

    async def chat_completion(self, *args, **kwargs):
        self.calls += 1
        return CompletionResult(content="unexpected", usage=Usage())


def _agent_cfg() -> AgentConfig:
    return AgentConfig(provider="p", model="fake-model", max_tokens=256)


def _estimator() -> TokenEstimator:
    return TokenEstimator(model="fake-model")


async def _executor(*args, **kwargs):
    return {"ok": True, "result": "ok"}


@pytest.mark.asyncio
async def test_runner_blocks_over_budget_before_provider_call():
    provider = _RecordingProvider()
    runner = AgentRunner(provider, _agent_cfg())

    result = await runner.run(
        [{"role": "user", "content": "长" * 5000}],
        tools=None,
        tool_executor=_executor,
        prompt_budget_tokens=10,
        token_estimator=_estimator(),
    )

    assert provider.calls == 0
    assert result.finish_reason == "budget_blocked"
    assert result.loop_count == 1


@pytest.mark.asyncio
async def test_finalizer_also_blocks_over_budget():
    provider = _RecordingProvider()
    runner = AgentRunner(provider, _agent_cfg())

    result = await runner._finalize_tool_loop(
        [{"role": "user", "content": "长" * 5000}],
        usage_recorder=None,
        status_callback=None,
        status_label="主模型",
        prompt_budget_tokens=10,
        token_estimator=_estimator(),
    )

    assert result is None
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_summary_agent_blocks_over_its_own_budget():
    provider = _RecordingProvider()
    agent = SummaryAgent(
        provider,
        _agent_cfg(),
        SummarizeConfig(),
        prompt_budget_tokens=10,
    )

    result = await agent.summarize_rolling(
        [{"role": "user", "content": "长" * 5000}],
        "",
        "",
    )

    assert result is None
    assert provider.calls == 0
