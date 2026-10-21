"""AgentRunner：模型消息使用投影副本，运行记录保留完整原始结果。"""

from __future__ import annotations

import json

import pytest

from agents.runner import AgentRunner
from app_config.schema import AgentConfig
from providers.base import CompletionResult, IProvider, ToolCall
from tests.agents.helpers import basic_tool_schema
from utils.model_records import ModelViewPolicy


@pytest.mark.asyncio
async def test_runner_projects_tool_results_for_model_but_keeps_raw_records():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls: list[dict] = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            if len(self.calls) == 1:
                return CompletionResult(
                    tool_calls=[
                        ToolCall(
                            id="send-1",
                            name="send_private_messages",
                            arguments="{}",
                        )
                    ],
                    finish_reason="tool_calls",
                )
            return CompletionResult(
                tool_calls=[
                    ToolCall(id="na-1", name="no_action", arguments="{}")
                ],
                finish_reason="tool_calls",
            )

        async def aclose(self) -> None:
            pass

    long_text = "L" * 500
    send_payload = {
        "ok": True,
        "status": "sent",
        "qq_visible": True,
        "sent": [
            {
                "order": 1,
                "conversation_id": "private:1",
                "content": long_text,
                "msg_id": "m1",
            }
        ],
    }
    no_action_payload = {
        "ok": True,
        "status": "done",
        "no_action": True,
        "tool": "no_action",
        "result_format": "structured_json",
        "brief": "本轮不执行操作。",
    }

    async def executor(name, _args):
        if name == "send_private_messages":
            return send_payload
        if name == "no_action":
            return no_action_payload
        raise AssertionError(name)

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    result = await runner.run(
        [{"role": "user", "content": "发一条消息"}],
        tools=[
            basic_tool_schema("send_private_messages"),
            basic_tool_schema("no_action"),
        ],
        tool_executor=executor,
        model_view_policy=ModelViewPolicy(),
    )

    assert result.finish_reason == "no_action"
    assert len(provider.calls) == 2

    projected_send = json.loads(provider.calls[1]["messages"][-1]["content"])
    assert projected_send["sent"][0]["msg_id"] == "m1"
    assert len(projected_send["sent"][0]["content"]) <= 80
    assert projected_send["sent"][0]["content"] != long_text

    raw_send_records = [
        record
        for record in result.records
        if record.get("role") == "tool"
        and record.get("tool_call_id") == "send-1"
    ]
    assert raw_send_records
    raw_send = json.loads(raw_send_records[0]["content"])
    assert raw_send["sent"][0]["content"] == long_text

    raw_no_action_records = [
        record
        for record in result.records
        if record.get("role") == "tool"
        and record.get("tool_call_id") == "na-1"
    ]
    assert raw_no_action_records
    assert json.loads(raw_no_action_records[0]["content"]) == no_action_payload
