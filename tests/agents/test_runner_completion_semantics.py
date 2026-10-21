"""AgentRunner 完成语义回归：no_action 不能掩盖同批失败。"""

from __future__ import annotations

import json

import pytest

from agents.runner import AgentRunner
from app_config.schema import AgentConfig
from providers.base import CompletionResult, IProvider, ToolCall
from tests.agents.helpers import basic_tool_schema


def _no_action(call_id: str) -> ToolCall:
    return ToolCall(id=call_id, name="no_action", arguments="{}")


def _tool(call_id: str, name: str) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments="{}")


class _ScriptedProvider(IProvider):
    def __init__(self, script: list[CompletionResult]) -> None:
        super().__init__("fake")
        self._script = list(script)
        self.calls: list[dict] = []

    async def chat_completion(self, messages, *, tools=None, **kwargs):
        self.calls.append({"messages": list(messages), "tools": tools})
        if self._script:
            return self._script.pop(0)
        return CompletionResult(
            tool_calls=[_no_action(f"fallback-{len(self.calls)}")],
            finish_reason="tool_calls",
        )

    async def aclose(self) -> None:
        pass


def _runner(provider: IProvider) -> AgentRunner:
    return AgentRunner(
        provider,
        AgentConfig(
            provider="fake",
            model="fake",
            max_loops=20,
            refocus_interval=0,
            tool_loop_reminder_interval=100,
            tool_loop_final_warning_count=100,
            tool_loop_final_grace_loops=0,
        ),
    )


def _tools(*names: str) -> list[dict]:
    return [basic_tool_schema(name) for name in names]


@pytest.mark.asyncio
async def test_no_action_success_does_not_mask_failed_tool_in_same_batch():
    provider = _ScriptedProvider(
        [
            CompletionResult(
                tool_calls=[_no_action("na-1"), _tool("fail-1", "demo_fail")],
                finish_reason="tool_calls",
            ),
            CompletionResult(
                tool_calls=[_no_action("na-2")],
                finish_reason="tool_calls",
            ),
        ]
    )

    async def executor(name, _args):
        if name == "no_action":
            return {"ok": True, "status": "done", "no_action": True}
        if name == "demo_fail":
            return {"ok": False, "status": "failed", "error": "synthetic failure"}
        raise AssertionError(name)

    result = await _runner(provider).run(
        [{"role": "user", "content": "执行"}],
        tools=_tools("no_action", "demo_fail"),
        tool_executor=executor,
    )

    assert len(provider.calls) == 2, "失败工具必须给模型后续纠错机会"
    assert result.finish_reason == "no_action"
    assert any(
        "synthetic failure" in str(message.get("content") or "")
        for message in provider.calls[1]["messages"]
    )
    assert any(
        "synthetic failure" in str(record.get("content") or "")
        for record in result.records
        if record.get("role") == "tool"
    )


@pytest.mark.asyncio
async def test_no_action_singular_error_blocks_completion():
    provider = _ScriptedProvider(
        [
            CompletionResult(
                tool_calls=[_no_action("na-err")],
                finish_reason="tool_calls",
            ),
            CompletionResult(
                tool_calls=[_no_action("na-ok")],
                finish_reason="tool_calls",
            ),
        ]
    )

    async def executor(name, _args):
        assert name == "no_action"
        if len(provider.calls) == 1:
            return {
                "ok": True,
                "no_action": True,
                "error": "synthetic contradiction",
            }
        return {"ok": True, "status": "done", "no_action": True}

    result = await _runner(provider).run(
        [{"role": "user", "content": "不操作"}],
        tools=_tools("no_action"),
        tool_executor=executor,
    )

    assert len(provider.calls) == 2
    assert result.finish_reason == "no_action"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "contradictory_result",
    [
        {"ok": True, "no_action": True, "success": False},
        {"ok": True, "no_action": True, "status": " FAILED "},
        {"ok": True, "no_action": True, "status": ["done", "needs_review"]},
        {"ok": True, "no_action": True, "turn_completion": {"allowed": False}},
    ],
)
async def test_no_action_contradictory_state_blocks_completion(contradictory_result):
    provider = _ScriptedProvider(
        [
            CompletionResult(
                tool_calls=[_no_action("na-bad")],
                finish_reason="tool_calls",
            ),
            CompletionResult(
                tool_calls=[_no_action("na-good")],
                finish_reason="tool_calls",
            ),
        ]
    )

    async def executor(name, _args):
        assert name == "no_action"
        if len(provider.calls) == 1:
            return contradictory_result
        return {"ok": True, "status": "done", "no_action": True}

    result = await _runner(provider).run(
        [{"role": "user", "content": "不操作"}],
        tools=_tools("no_action"),
        tool_executor=executor,
    )

    assert len(provider.calls) == 2
    assert result.finish_reason == "no_action"


@pytest.mark.asyncio
async def test_only_successful_no_action_still_finishes_in_one_call():
    provider = _ScriptedProvider(
        [
            CompletionResult(
                tool_calls=[_no_action("na-only")],
                finish_reason="tool_calls",
            )
        ]
    )

    async def executor(name, _args):
        assert name == "no_action"
        return {"ok": True, "status": "done", "no_action": True}

    result = await _runner(provider).run(
        [{"role": "user", "content": "不操作"}],
        tools=_tools("no_action"),
        tool_executor=executor,
    )

    assert len(provider.calls) == 1
    assert result.finish_reason == "no_action"


@pytest.mark.asyncio
async def test_mixed_no_action_with_unpermitted_success_tool_continues():
    provider = _ScriptedProvider(
        [
            CompletionResult(
                tool_calls=[_no_action("na-1"), _tool("ok-1", "demo_ok")],
                finish_reason="tool_calls",
            ),
            CompletionResult(
                tool_calls=[_no_action("na-2")],
                finish_reason="tool_calls",
            ),
        ]
    )

    async def executor(name, _args):
        if name == "no_action":
            return {"ok": True, "status": "done", "no_action": True}
        if name == "demo_ok":
            # 成功但没有 finish_after_success 许可，不能借 no_action 结束。
            return {"ok": True, "status": "done"}
        raise AssertionError(name)

    result = await _runner(provider).run(
        [{"role": "user", "content": "执行"}],
        tools=_tools("no_action", "demo_ok"),
        tool_executor=executor,
    )

    assert len(provider.calls) == 2
    assert result.finish_reason == "no_action"


@pytest.mark.asyncio
async def test_successful_send_with_failed_auto_finish_tool_does_not_finish():
    provider = _ScriptedProvider(
        [
            CompletionResult(
                tool_calls=[
                    _tool("send-1", "send_private_messages"),
                    _tool("mem-1", "save_important_memory"),
                ],
                finish_reason="tool_calls",
            ),
            CompletionResult(
                tool_calls=[_no_action("na-2")],
                finish_reason="tool_calls",
            ),
        ]
    )

    async def executor(name, _args):
        if name == "send_private_messages":
            return {"ok": True, "status": "sent", "qq_visible": True}
        if name == "save_important_memory":
            return {"ok": False, "status": "failed", "error": "memory write failed"}
        if name == "no_action":
            return {"ok": True, "status": "done", "no_action": True}
        raise AssertionError(name)

    result = await _runner(provider).run(
        [{"role": "user", "content": "发送并保存"}],
        tools=_tools("no_action", "send_private_messages", "save_important_memory"),
        tool_executor=executor,
        auto_finish_after_send=True,
    )

    assert len(provider.calls) == 2, "同批失败不能被自动发送收尾掩盖"
    assert result.finish_reason == "no_action"


@pytest.mark.asyncio
async def test_stop_after_tool_with_failure_keeps_tool_stop_and_raw_error():
    provider = _ScriptedProvider(
        [
            CompletionResult(
                tool_calls=[_tool("stop-1", "demo_stop")],
                finish_reason="tool_calls",
            )
        ]
    )

    async def executor(name, _args):
        assert name == "demo_stop"
        return {
            "ok": False,
            "status": "failed",
            "error": "stop path failure",
            "stop_after_tool": True,
        }

    result = await _runner(provider).run(
        [{"role": "user", "content": "停止"}],
        tools=_tools("demo_stop"),
        tool_executor=executor,
    )

    assert len(provider.calls) == 1
    assert result.finish_reason == "tool_stop"
    raw_tool_contents = [
        json.loads(record["content"])
        for record in result.records
        if record.get("role") == "tool"
    ]
    assert raw_tool_contents[0]["error"] == "stop path failure"
    assert raw_tool_contents[0]["ok"] is False
