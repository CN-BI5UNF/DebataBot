"""AgentRunner provider/unknown 异常日志脱敏测试。"""

from __future__ import annotations

import logging

import pytest

from agents.runner import AgentRunner
from app_config.schema import AgentConfig
from providers.base import ProviderError

SENTINEL = "SYNTHETIC_SIGNED_TOKEN_DO_NOT_LOG"
SIGNED_URL = f"https://images.example.invalid/a.png?token={SENTINEL}"


def _runner(provider) -> AgentRunner:
    return AgentRunner(
        provider,
        AgentConfig(
            provider="fake",
            model="fake-1",
            temperature=0.6,
            max_tokens=64,
            max_loops=1,
            refocus_interval=0,
            first_token_timeout_seconds=1.0,
        ),
    )


async def _executor(tool_name, args, *, tool_call_id=None):
    return {"ok": True}


@pytest.mark.asyncio
async def test_runner_provider_error_log_is_redacted(caplog):
    class RaisingProvider:
        name = "fake"

        async def chat_completion(self, *args, **kwargs):
            raise ProviderError(f"provider failed at {SIGNED_URL}")

    statuses: list[dict] = []
    runner = _runner(RaisingProvider())
    with caplog.at_level(logging.ERROR, logger="agents.runner"):
        result = await runner.run(
            [{"role": "user", "content": "hi"}],
            tools=None,
            tool_executor=_executor,
            status_callback=statuses.append,
        )

    assert result.finish_reason == "api_error"
    assert SENTINEL not in caplog.text
    assert SENTINEL not in str(result)
    assert all(SENTINEL not in str(status) for status in statuses)
    assert any(status.get("text", "").endswith("调用失败") for status in statuses)


@pytest.mark.asyncio
async def test_runner_unknown_error_does_not_log_raw_traceback(caplog):
    class UnknownProvider:
        name = "fake"

        async def chat_completion(self, *args, **kwargs):
            try:
                raise ValueError(f"inner {SIGNED_URL}")
            except ValueError as inner:
                outer = RuntimeError(f"outer {SIGNED_URL}")
                raise outer from inner

    statuses: list[dict] = []
    runner = _runner(UnknownProvider())
    with caplog.at_level(logging.ERROR, logger="agents.runner"):
        result = await runner.run(
            [{"role": "user", "content": "hi"}],
            tools=None,
            tool_executor=_executor,
            status_callback=statuses.append,
        )

    assert result.finish_reason == "api_error"
    assert SENTINEL not in caplog.text
    assert "Traceback" not in caplog.text
    assert "inner" not in caplog.text
    assert all(SENTINEL not in str(status) for status in statuses)
    assert any(status.get("text", "").endswith("异常") for status in statuses)
