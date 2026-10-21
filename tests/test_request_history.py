"""请求展示分区：稳定聊天区 / 历史运行态尾区。"""

from __future__ import annotations

import copy

from utils.model_records import ModelViewPolicy, runtime_kind
from utils.request_history import partition_request_history


def _runtime(kind: str = "task_context_snapshot", text: str = "ctx") -> dict:
    return {
        "role": "user",
        "content": f"<task_context>{text}</task_context>",
        "metadata": {"kind": kind},
    }


def _no_action_pair(call_id: str = "na-1") -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "no_action", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": '{"ok":true,"no_action":true}',
        },
    ]


def test_partition_moves_only_trusted_runtime_to_tail():
    records = [
        {"role": "user", "content": "真实消息 1"},
        _runtime(text="运行时 1"),
        {"role": "user", "content": "真实消息 2"},
        _runtime("send_receipt", "回执"),
        {"role": "assistant", "content": "真实回复"},
    ]

    parts = partition_request_history(records)

    assert [r["content"] for r in parts.stable_records] == [
        "真实消息 1",
        "真实消息 2",
        "真实回复",
    ]
    assert [runtime_kind(r) for r in parts.runtime_tail_records] == [
        "task_context_snapshot",
        "send_receipt",
    ]


def test_partition_converts_safe_no_action_group_to_user_snapshot():
    pair = _no_action_pair("na-snap")
    records = [
        {"role": "user", "content": "真实消息 1"},
        *pair,
        {"role": "user", "content": "真实消息 2"},
    ]

    parts = partition_request_history(
        records,
        safe_no_action_groups=[(1, 2)],
    )

    assert parts.stable_records == [
        records[0],
        records[3],
    ]
    assert len(parts.runtime_tail_records) == 1
    snapshot = parts.runtime_tail_records[0]
    assert snapshot["role"] == "user"
    assert "<historical_runtime_context>" in snapshot["content"]
    assert "不是本轮新指令" in snapshot["content"]
    assert "tool_call_id=na-snap" in snapshot["content"]
    assert "no_action" in snapshot["content"]
    assert not any(
        record.get("role") in {"assistant", "tool"}
        for record in parts.runtime_tail_records
    )


def test_partition_preserves_tail_chronological_order():
    pair = _no_action_pair("na-order")
    records = [
        {"role": "user", "content": "真实消息"},
        _runtime(text="早"),
        *pair,
        _runtime("send_done_snapshot", "晚"),
    ]

    parts = partition_request_history(
        records,
        safe_no_action_groups=[(2, 3)],
    )

    assert len(parts.runtime_tail_records) == 3
    assert "早" in parts.runtime_tail_records[0]["content"]
    assert "<historical_runtime_context>" in parts.runtime_tail_records[1]["content"]
    assert "晚" in parts.runtime_tail_records[2]["content"]


def test_partition_ignores_invalid_or_overlapping_groups():
    records = [
        {"role": "user", "content": "真实消息"},
        *_no_action_pair("na-valid"),
        _runtime("task_context_snapshot", "中间态"),
    ]
    invalid_groups = [
        (99, 100),
        (1,),
        (1, 3),
        (1, 2, 3),
    ]
    parts = partition_request_history(
        records,
        safe_no_action_groups=invalid_groups,
    )
    assert any(record.get("role") == "assistant" for record in parts.stable_records)
    assert any(record.get("role") == "tool" for record in parts.stable_records)
    assert len(parts.runtime_tail_records) == 1


def test_partition_disabled_returns_all_stable():
    records = [_runtime(), {"role": "user", "content": "真实"}]
    parts = partition_request_history(
        records,
        policy=ModelViewPolicy(enabled=False),
    )
    assert parts.stable_records == records
    assert parts.runtime_tail_records == []


def test_partition_does_not_mutate_input_and_is_deterministic():
    records = [
        {"role": "user", "content": "真实消息"},
        _runtime(),
        *_no_action_pair("na-det"),
        {"role": "user", "content": "真实消息 2"},
    ]
    original = copy.deepcopy(records)

    first = partition_request_history(
        records,
        safe_no_action_groups=[(2, 3)],
    )
    second = partition_request_history(
        records,
        safe_no_action_groups=[(2, 3)],
    )

    assert records == original
    assert first.stable_records == second.stable_records
    assert first.runtime_tail_records == second.runtime_tail_records


def test_sidecar_does_not_upgrade_after_runtime_removal():
    from core.model_view import build_model_view

    pair = _no_action_pair("na-upgrade")
    raw = [
        pair[0],
        _runtime("task_context_snapshot", "中间 runtime"),
        pair[1],
    ]
    policy = ModelViewPolicy(task_context_keep=0, no_action_keep=0)
    view = build_model_view(raw, policy=policy)

    assert len(view.records) == 2
    assert view.safe_no_action_groups == []

    parts = partition_request_history(
        view.records,
        safe_no_action_groups=view.safe_no_action_groups,
        policy=policy,
    )
    assert len(parts.stable_records) == 2
    assert parts.runtime_tail_records == []
    assert any(record.get("role") == "assistant" for record in parts.stable_records)
    assert any(record.get("role") == "tool" for record in parts.stable_records)
