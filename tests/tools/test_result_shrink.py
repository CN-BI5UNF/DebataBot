"""工具结果精简的失败语义保护。

重点：no_action 失败/带额外语义时不能被改写成成功；发送结果精简不能
丢掉 error、loop_reminder 等控制字段。
"""

from __future__ import annotations

import json

import pytest

from tests.tools.helpers import _make_config
from tools import ToolContext, build_default_registry
from tools.base import _ensure_tool_result_envelope
from tools.result_shrink import project_send_result_payload, shrink_tool_result
from utils.model_records import no_action_pair_indices


def test_failed_no_action_is_not_rewritten_to_success():
    failed = {
        "ok": False,
        "status": "policy_rejected",
        "error": "策略拒绝",
    }

    shrunk = shrink_tool_result("no_action", failed, ToolContext())
    assert shrunk["ok"] is False
    assert shrunk["status"] == "policy_rejected"
    assert shrunk["error"] == "策略拒绝"

    enveloped = _ensure_tool_result_envelope("no_action", shrunk)
    assert enveloped["ok"] is False
    assert enveloped["status"] == "policy_rejected"
    assert enveloped["error"] == "策略拒绝"
    assert enveloped.get("no_action") is not True


def test_status_only_policy_rejection_is_inferred_as_failure():
    result = {"status": "policy_rejected"}
    shrunk = shrink_tool_result("no_action", result, ToolContext())
    enveloped = _ensure_tool_result_envelope("no_action", shrunk)
    assert enveloped["ok"] is False
    assert enveloped["status"] == "policy_rejected"
    assert enveloped.get("no_action") is not True


def test_successful_no_action_stays_minimal():
    success = {
        "ok": True,
        "status": "done",
        "no_action": True,
        "tool": "no_action",
        "result_format": "structured_json",
        "brief": "本轮不执行操作。",
    }
    assert shrink_tool_result("no_action", success, ToolContext()) == {
        "ok": True,
        "no_action": True,
    }
    assert _ensure_tool_result_envelope("no_action", success) == {
        "ok": True,
        "no_action": True,
    }


def test_no_action_with_loop_reminder_keeps_reminder_field():
    result = {
        "ok": True,
        "status": "done",
        "no_action": True,
        "loop_reminder": {"level": "reminder", "message": "不要重复同一错误"},
    }
    shrunk = shrink_tool_result("no_action", result, ToolContext())
    assert shrunk["loop_reminder"] == {
        "level": "reminder",
        "message": "不要重复同一错误",
    }
    assert shrunk.get("no_action") is True


def test_send_result_projection_keeps_error_and_loop_reminder():
    payload = {
        "ok": False,
        "status": "failed",
        "error": "network down",
        "loop_reminder": {"level": "reminder", "message": "停止重试"},
        "brief": "发送失败",
        "sent": [],
    }
    projected = project_send_result_payload(payload)
    assert projected["ok"] is False
    assert projected["status"] == "failed"
    assert projected["error"] == "network down"
    assert projected["loop_reminder"] == {"level": "reminder", "message": "停止重试"}


@pytest.mark.asyncio
async def test_real_registry_success_is_eligible_for_compression():
    registry = build_default_registry(_make_config())
    executor = registry.get_executor(ToolContext())

    result = await executor("no_action", {})
    assert result == {"ok": True, "no_action": True}

    records = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "real-na",
                    "type": "function",
                    "function": {"name": "no_action", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "real-na",
            "content": json.dumps(result, ensure_ascii=False),
        },
    ]
    assert no_action_pair_indices(records, 0) == {0, 1}
