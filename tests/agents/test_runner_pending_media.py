"""Tool-loop pending records must project media for the model, not persist it."""

from __future__ import annotations

import pytest

from agents.runner import AgentRunner
from app_config.schema import AgentConfig
from providers.base import CompletionResult, IProvider, ToolCall
from tests.agents.helpers import basic_tool_schema


def _agent_cfg() -> AgentConfig:
    return AgentConfig(
        provider="fake",
        model="fake",
        max_loops=5,
        tool_loop_reminder_interval=20,
        tool_loop_final_warning_count=99,
    )


@pytest.mark.asyncio
async def test_pending_media_projector_only_changes_model_copy():
    pending_record = {
        "role": "user",
        "content": "刚来的图片",
        "conversation_id": "private:1",
        "metadata": {
            "messages": [{"message_id": "m1", "inbound_seq": 1}],
            "image_assets": [{"sha256": "a" * 64}],
        },
    }
    projected_calls: list[dict] = []

    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls: list[dict] = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            if len(self.calls) == 1:
                return CompletionResult(
                    tool_calls=[
                        ToolCall(id="tc-1", name="needs_feedback", arguments="{}")
                    ],
                    finish_reason="tool_calls",
                )
            return CompletionResult(
                tool_calls=[
                    ToolCall(id="tc-na", name="no_action", arguments="{}")
                ],
                finish_reason="tool_calls",
            )

        async def aclose(self) -> None:
            pass

    async def executor(name, _args):
        if name == "no_action":
            return {"ok": True, "no_action": True}
        return {"ok": True, "status": "partial"}

    async def pending_provider():
        if projected_calls:
            return []
        return [pending_record]

    async def projector(record):
        projected_calls.append(record)
        return {
            **record,
            "content": [
                {"type": "text", "text": "刚来的图片"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,AA",
                        "detail": "auto",
                    },
                },
            ],
        }

    provider = FakeProvider()
    runner = AgentRunner(provider, _agent_cfg())
    result = await runner.run(
        [{"role": "user", "content": "开始"}],
        tools=[
            basic_tool_schema(),
            basic_tool_schema("no_action"),
        ],
        tool_executor=executor,
        pending_context_provider=pending_provider,
        pending_media_projector=projector,
    )

    assert projected_calls == [pending_record]
    assert any(
        isinstance(message.get("content"), list)
        and any(
            block.get("type") == "image_url"
            for block in message["content"]
            if isinstance(block, dict)
        )
        for message in provider.calls[0]["messages"]
    )
    # 持久化的记录仍是原始文本 + refs，不能保存 data URL。
    persisted = [
        record
        for record in result.records
        if record.get("content") == "刚来的图片"
    ]
    assert len(persisted) == 1
    assert persisted[0]["metadata"]["image_assets"] == [
        {"sha256": "a" * 64}
    ]
    assert "data:image" not in str(persisted[0])


@pytest.mark.asyncio
async def test_pending_media_projector_failure_falls_back_to_text_only():
    pending_record = {
        "role": "user",
        "content": "坏图片",
        "metadata": {
            "image_assets": [{"sha256": "a" * 64}],
            "video_assets": [{"sha256": "b" * 64}],
        },
    }
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls: list[dict] = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages)})
            return CompletionResult(
                tool_calls=[
                    ToolCall(id="tc-na", name="no_action", arguments="{}")
                ],
                finish_reason="tool_calls",
            )

        async def aclose(self) -> None:
            pass

    async def executor(name, _args):
        return {"ok": True, "no_action": True}

    injected = False

    async def pending_provider():
        nonlocal injected
        if injected:
            return []
        injected = True
        return [pending_record]

    async def projector(_record):
        raise RuntimeError("boom")

    provider = FakeProvider()
    runner = AgentRunner(provider, _agent_cfg())
    await runner.run(
        [{"role": "user", "content": "开始"}],
        tools=[basic_tool_schema("no_action")],
        tool_executor=executor,
        pending_context_provider=pending_provider,
        pending_media_projector=projector,
    )

    observed = provider.calls[0]["messages"]
    injected_message = next(
        message for message in observed if message.get("content") == "坏图片"
    )
    assert "image_assets" not in injected_message["metadata"]
    assert "video_assets" not in injected_message["metadata"]
    assert pending_record["metadata"]["image_assets"]
    assert pending_record["metadata"]["video_assets"]
