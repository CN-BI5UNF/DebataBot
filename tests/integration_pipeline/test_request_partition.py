"""生产组合下的请求展示分区与稳定 S 前缀回归。"""

from __future__ import annotations

import json

import pytest

from agents.context_builder import first_tail_block_index
from providers.base import CompletionResult, ToolCall, normalize_messages
from tests.integration_pipeline.helpers import _drain_pipeline, _msg
from utils.model_records import runtime_kind


def _no_action_for(index: int) -> CompletionResult:
    return CompletionResult(
        tool_calls=[
            ToolCall(id=f"na-{index}", name="no_action", arguments="{}")
        ],
        finish_reason="tool_calls",
    )


def _failed_tool_pair() -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "stable-fail",
                    "type": "function",
                    "function": {"name": "demo_fail", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "stable-fail",
            "content": json.dumps(
                {
                    "ok": False,
                    "status": "failed",
                    "error": "stable failure",
                },
                ensure_ascii=False,
            ),
        },
    ]


@pytest.mark.asyncio
async def test_production_twelve_turns_keep_stable_s_prefix(build_pipeline):
    pipeline, provider, _, history, _ = await build_pipeline(
        [_no_action_for(index) for index in range(12)],
        model_view_enabled=True,
        tool_schema_profile="compact",
        auto_finish_after_send=True,
    )

    stable_prefixes: list[list[dict]] = []
    tool_name_orders: list[list[str]] = []
    failure_inserted = False

    for turn in range(12):
        if turn == 5:
            await history.add_records(_failed_tool_pair())
            failure_inserted = True

        await pipeline.enqueue(
            _msg(
                user_id="123",
                text=f"真实消息 {turn}",
                message_id=f"msg-{turn}",
            )
        )
        await _drain_pipeline(pipeline)

        messages = provider.calls[-1]["messages"]
        tail_index = first_tail_block_index(messages)
        stable = normalize_messages(messages[:tail_index])
        tail = messages[tail_index:]

        # 稳定区只能有真实聊天/普通记录，不能混入可信 runtime。
        assert all(runtime_kind(message) is None for message in messages[:tail_index])
        assert any(
            message.get("_prompt_section") == "volatile_tail"
            for message in tail
        ), "tail 必须有结构标记"

        if failure_inserted and turn >= 5:
            assert any(
                "stable failure" in str(message.get("content") or "")
                for message in stable
            ), "普通工具失败记录不能被稳定性优化丢掉"

        if stable_prefixes:
            previous = stable_prefixes[-1]
            assert stable[: len(previous)] == previous, (
                f"第 {turn} 轮稳定 S 不再是上一轮 S 的前缀"
            )
        stable_prefixes.append(stable)
        tool_name_orders.append(
            [
                str(tool["function"]["name"])
                for tool in provider.calls[-1]["tools"] or []
            ]
        )

    assert all(order == tool_name_orders[0] for order in tool_name_orders)

    records = await history.records()
    joined_history = "\n".join(str(record.get("content") or "") for record in records)
    for turn in range(12):
        assert f"真实消息 {turn}" in joined_history
    assert "stable failure" in joined_history


@pytest.mark.asyncio
async def test_production_partition_moves_runtime_to_tail(build_pipeline):
    pipeline, provider, _, _, _ = await build_pipeline(
        [_no_action_for(1), _no_action_for(2)],
        model_view_enabled=True,
        tool_schema_profile="compact",
        auto_finish_after_send=True,
    )

    await pipeline.enqueue(_msg(text="第一轮", message_id="m-1"))
    await _drain_pipeline(pipeline)
    await pipeline.enqueue(_msg(text="第二轮", message_id="m-2"))
    await _drain_pipeline(pipeline)

    messages = provider.calls[-1]["messages"]
    tail_index = first_tail_block_index(messages)
    tail_text = "\n".join(str(m.get("content") or "") for m in messages[tail_index:])

    assert "现在是" in tail_text, "当前 task_context 必须在尾区"
    assert any(
        "<historical_runtime_context>" in str(m.get("content") or "")
        for m in messages[tail_index:]
    ), "安全 no_action 必须转成历史 user 快照"
    assert all(m != {"role": "assistant", "content": ""} for m in messages[tail_index:])


@pytest.mark.asyncio
async def test_out_of_band_turn_uses_structured_tail(build_pipeline):
    pipeline, provider, _, _, _ = await build_pipeline(
        [_no_action_for(1)],
        model_view_enabled=True,
        tool_schema_profile="compact",
        auto_finish_after_send=True,
    )

    await pipeline.run_wakeup_turn(
        "30 秒到了，请处理提醒。",
        target={"target_type": "private", "target_id": 123},
        mode="wakeup",
    )

    messages = provider.calls[-1]["messages"]
    tail_index = first_tail_block_index(messages)
    assert 0 <= tail_index < len(messages)

    tail = messages[tail_index:]
    assert all(
        message.get("_prompt_section") == "volatile_tail"
        for message in tail
    )
    tail_text = "\n".join(str(message.get("content") or "") for message in tail)
    assert "定时唤醒轮" in tail_text
    assert "定时唤醒已到" in tail_text

    normalized = normalize_messages(messages)
    assert all("_prompt_section" not in message for message in normalized)
