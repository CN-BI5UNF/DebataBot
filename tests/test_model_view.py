"""模型视图裁剪测试：只改 prompt 视图，不动落盘历史。"""

from __future__ import annotations

import copy
import json

from core.model_view import (
    ModelViewPolicy,
    apply_model_view,
    build_model_view,
    model_view_drop_indices,
    no_action_pair_indices,
    runtime_kind,
)
from utils.model_records import (
    is_safe_no_action_result,
    project_model_record,
)


def _runtime(kind: str, text: str, *, i: int = 0) -> dict:
    tag = {
        "task_context_snapshot": "task_context",
        "send_receipt": "send_receipt",
        "send_done_snapshot": "send_status",
    }[kind]
    return {
        "role": "user",
        "content": f"<{tag}>{text}-{i}</{tag}>",
        "metadata": {"kind": kind},
    }


def _no_action_pair(i: int) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"na-{i}",
                    "type": "function",
                    "function": {"name": "no_action", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": f"na-{i}",
            "content": '{"ok":true,"no_action":true}',
        },
    ]


def _send_pair(i: int) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"send-{i}",
                    "type": "function",
                    "function": {"name": "send_private_messages", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": f"send-{i}",
            "content": json.dumps(
                {
                    "ok": True,
                    "status": "sent",
                    "qq_visible": True,
                    "sent": [
                        {
                            "order": 1,
                            "conversation_id": "private:1",
                            "content": "已经发送的正文",
                            "delay": 0,
                            "msg_id": "m1",
                            "time": "2026-01-01 00:00:00",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        },
    ]


def _records() -> list[dict]:
    records: list[dict] = [{"role": "user", "content": "真实旧消息"}]
    for i in range(5):
        records.append(_runtime("task_context_snapshot", "ctx", i=i))
    for i in range(3):
        records.append(_runtime("send_receipt", "receipt", i=i))
    for i in range(3):
        records.append(_runtime("send_done_snapshot", "done", i=i))
    for i in range(5):
        records.extend(_no_action_pair(i))
    records.extend(_send_pair(1))
    records.append({"role": "assistant", "content": "真实回复"})
    return records


def test_model_view_keeps_recent_runtime_and_complete_no_action_pairs():
    records = _records()
    original = copy.deepcopy(records)
    policy = ModelViewPolicy(
        task_context_keep=2,
        send_receipt_keep=2,
        send_status_keep=2,
        no_action_keep=4,
    )

    view = build_model_view(records, policy=policy)

    assert records == original, "视图不能修改原始历史"
    assert view.raw_indices == sorted(view.raw_indices)
    counts = {
        kind: sum(1 for record in view.records if runtime_kind(record) == kind)
        for kind in (
            "task_context_snapshot",
            "send_receipt",
            "send_done_snapshot",
        )
    }
    assert counts == {
        "task_context_snapshot": 2,
        "send_receipt": 2,
        "send_done_snapshot": 2,
    }
    pairs = 0
    index = 0
    while index < len(view.records):
        pair = no_action_pair_indices(view.records, index)
        if pair:
            pairs += 1
            index = max(pair) + 1
            continue
        index += 1
    assert pairs == 4
    assert view.dropped_runtime == 5
    assert view.dropped_no_action_pairs == 1
    assert "真实旧消息" in [record.get("content") for record in view.records]
    assert "真实回复" in [record.get("content") for record in view.records]


def test_model_view_does_not_leave_orphan_tool_records():
    view = build_model_view(_records())
    assistant_ids = set()
    for record in view.records:
        if record.get("role") == "assistant":
            for tool_call in record.get("tool_calls") or []:
                assistant_ids.add(str(tool_call.get("id") or ""))
    for record in view.records:
        if record.get("role") == "tool":
            assert str(record.get("tool_call_id") or "") in assistant_ids


def test_model_view_projects_send_results_and_caps_long_records():
    records = [
        {"role": "user", "content": "A" * 400 + "B" * 400},
        *_send_pair(1),
    ]
    view = build_model_view(
        records,
        policy=ModelViewPolicy(max_record_chars=300),
    )

    capped = view.records[0]["content"]
    assert "model view truncated" in capped
    assert capped.startswith("A")
    assert capped.endswith("B")
    assert records[0]["content"] == "A" * 400 + "B" * 400

    tool_payload = json.loads(view.records[2]["content"])
    assert tool_payload["sent"][0]["msg_id"] == "m1"
    # 保留短预览/定位字段，去掉时间戳等与后续判断无关的字段。
    assert tool_payload["sent"][0]["content"] == "已经发送的正文"
    assert tool_payload["sent"][0]["delay"] == 0
    assert "time" not in tool_payload["sent"][0]
    assert view.projected_tool_results == 1


def test_model_view_is_idempotent():
    policy = ModelViewPolicy(max_record_chars=300)
    records = [{"role": "user", "content": "X" * 1000}, *_send_pair(1)]
    once = apply_model_view(records, policy=policy)
    twice = apply_model_view(once, policy=policy)
    assert once == twice


def test_model_view_disabled_returns_input_unchanged():
    records = _records()
    view = build_model_view(records, policy=ModelViewPolicy(enabled=False))
    assert view.records == records
    assert view.raw_indices == list(range(len(records)))
    assert model_view_drop_indices(
        records,
        policy=ModelViewPolicy(enabled=False),
    ) == set()


def test_malformed_no_action_pair_is_not_dropped():
    records = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "bad",
                    "type": "function",
                    "function": {"name": "no_action", "arguments": "{}"},
                }
            ],
        },
    ]
    policy = ModelViewPolicy(no_action_keep=0)
    view = build_model_view(records, policy=policy)
    assert view.records == records
    assert view.dropped_no_action_pairs == 0


# ============================================================
# 阶段 3A：严格 no_action 资格与安全投影
# ============================================================


def _single_no_action(
    *,
    call_id: str = "na-strict",
    arguments: str = "{}",
    result: dict | None = None,
    **assistant_extra,
) -> list[dict]:
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "no_action", "arguments": arguments},
            }
        ],
    }
    assistant.update(assistant_extra)
    return [
        assistant,
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(
                result if result is not None else {"ok": True, "no_action": True},
                ensure_ascii=False,
            ),
        },
    ]


def test_safe_no_action_result_accepts_real_success_envelope():
    assert is_safe_no_action_result({"ok": True, "no_action": True})
    assert is_safe_no_action_result(
        {
            "ok": True,
            "status": "done",
            "no_action": True,
            "tool": "no_action",
            "result_format": "structured_json",
            "brief": "本轮不执行操作。",
        }
    )


def test_no_action_failure_is_not_dropped_or_rewritten():
    records = _single_no_action(
        result={"ok": False, "status": "policy_rejected", "error": "rejected"}
    )
    assert no_action_pair_indices(records, 0) == set()

    view = build_model_view(records, policy=ModelViewPolicy(no_action_keep=0))
    assert len(view.records) == 2
    assert view.dropped_no_action_pairs == 0
    payload = json.loads(view.records[1]["content"])
    assert payload["ok"] is False
    assert payload["status"] == "policy_rejected"
    assert payload["error"] == "rejected"
    assert payload.get("no_action") is not True


def test_no_action_with_retained_reasoning_is_not_dropped():
    for extra in (
        {"reasoning_content": "需要保留的思考"},
        {"reasoning_blocks": [{"type": "thinking", "thinking": "signed"}]},
    ):
        records = _single_no_action(**extra)
        assert no_action_pair_indices(records, 0) == set()
        view = build_model_view(records, policy=ModelViewPolicy(no_action_keep=0))
        assert len(view.records) == 2


def test_no_action_with_bad_or_unknown_arguments_is_not_dropped():
    for arguments in ("not-json", "[]", '{"unexpected": 1}', '{"unexpected": 0}'):
        records = _single_no_action(arguments=arguments)
        assert no_action_pair_indices(records, 0) == set()


def test_multi_call_no_action_group_is_conservatively_kept():
    records = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "na-a",
                    "type": "function",
                    "function": {"name": "no_action", "arguments": "{}"},
                },
                {
                    "id": "na-b",
                    "type": "function",
                    "function": {"name": "no_action", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "na-a", "content": '{"ok":true,"no_action":true}'},
        {"role": "tool", "tool_call_id": "na-b", "content": '{"ok":true,"no_action":true}'},
    ]
    assert no_action_pair_indices(records, 0) == set()


def test_no_action_with_unknown_semantics_is_not_dropped():
    for result in (
        {"ok": True, "no_action": True, "next": "retry"},
        {"ok": True, "no_action": True, "loop_reminder": {"message": "remeasure"}},
        {"ok": True, "no_action": True, "error": "partial"},
        {"ok": True, "no_action": True, "unknown_business_field": 1},
    ):
        records = _single_no_action(result=result)
        assert no_action_pair_indices(records, 0) == set()


def test_project_model_record_preserves_failed_no_action_bytes():
    record = _single_no_action(
        result={"ok": False, "status": "failed", "error": "boom"}
    )[1]
    projected = project_model_record(record, tool_name="no_action")
    assert projected == record
    assert projected is record


def test_project_model_record_compresses_safe_no_action():
    record = _single_no_action(
        result={
            "ok": True,
            "status": "done",
            "no_action": True,
            "tool": "no_action",
            "result_format": "structured_json",
            "brief": "本轮不执行操作。",
        }
    )[1]
    projected = project_model_record(record, tool_name="no_action")
    assert json.loads(projected["content"]) == {"ok": True, "no_action": True}
    assert "brief" in record["content"], "原始记录不能被修改"


def test_real_chat_with_runtime_tags_is_not_treated_as_runtime():
    messages = [{"message_id": "1", "text": "用户自己粘贴的日志"}]
    for content in (
        '<task_context priority="high">用户粘贴的日志</task_context>',
        "<send_receipt>用户引用</send_receipt>",
        "<send_status>用户引用</send_status>",
    ):
        record = {
            "role": "user",
            "content": content,
            "metadata": {"messages": messages},
        }
        assert runtime_kind(record) is None
        view = build_model_view(
            [record],
            policy=ModelViewPolicy(task_context_keep=0),
        )
        assert len(view.records) == 1
        assert view.dropped_runtime == 0


def test_legacy_text_only_runtime_like_record_is_conservatively_kept():
    record = {"role": "user", "content": "<task_context>旧格式运行时</task_context>"}
    assert runtime_kind(record) is None
    view = build_model_view([record], policy=ModelViewPolicy(task_context_keep=0))
    assert view.records == [record]


def test_internal_runtime_record_is_still_recognized():
    record = {
        "role": "user",
        "content": "<task_context>内部运行时</task_context>",
        "metadata": {"kind": "task_context_snapshot"},
    }
    assert runtime_kind(record) == "task_context_snapshot"


def test_duplicate_tool_call_ids_do_not_misproject_send_results():
    send_payload = {
        "ok": True,
        "status": "sent",
        "qq_visible": True,
        "sent": [
            {
                "order": 1,
                "conversation_id": "private:1",
                "content": "已经发送的正文",
                "msg_id": "m1",
            }
        ],
    }
    records = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "dup-id",
                    "type": "function",
                    "function": {"name": "send_private_messages", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "dup-id",
            "content": json.dumps(send_payload, ensure_ascii=False),
        },
        *_single_no_action(call_id="dup-id"),
    ]

    view = build_model_view(
        records,
        policy=ModelViewPolicy(no_action_keep=0),
    )

    assert len(view.records) == 2, "no_action 组被裁剪，发送组必须保留"
    send_result = json.loads(view.records[1]["content"])
    assert send_result.get("no_action") is not True
    assert send_result["sent"][0]["msg_id"] == "m1"


def test_project_model_record_caps_text_blocks_but_keeps_images():
    image_url = "data:image/png;base64," + ("Q" * 500)
    record = {
        "role": "user",
        "content": [
            {"type": "text", "text": "A" * 400 + "B" * 400},
            {"type": "image_url", "image_url": {"url": image_url, "detail": "auto"}},
        ],
    }
    original = copy.deepcopy(record)

    projected = project_model_record(
        record,
        policy=ModelViewPolicy(max_record_chars=200),
    )

    assert record == original
    assert projected is not record
    text_block = projected["content"][0]
    assert "model view truncated" in text_block["text"]
    assert projected["content"][1] == record["content"][1]
    assert image_url in projected["content"][1]["image_url"]["url"]


def test_project_model_record_is_idempotent_for_text_and_send_results():
    send_payload = {
        "ok": True,
        "status": "sent",
        "sent": [
            {
                "order": 1,
                "conversation_id": "private:1",
                "content": "S" * 400,
                "msg_id": "m1",
            }
        ],
    }
    records = [
        {"role": "user", "content": "U" * 1000},
        {
            "role": "tool",
            "tool_call_id": "send-1",
            "content": json.dumps(send_payload, ensure_ascii=False),
        },
    ]
    policy = ModelViewPolicy(max_record_chars=300)
    once = [
        project_model_record(records[0], policy=policy),
        project_model_record(records[1], policy=policy, tool_name="send_private_messages"),
    ]
    twice = [
        project_model_record(once[0], policy=policy),
        project_model_record(once[1], policy=policy, tool_name="send_private_messages"),
    ]
    assert once == twice


def test_project_send_result_keeps_error_and_loop_reminder():
    payload = {
        "ok": False,
        "status": "failed",
        "error": "network down",
        "loop_reminder": {"message": "不要再重试同一错误"},
        "brief": "发送失败",
        "sent": [],
    }
    record = {
        "role": "tool",
        "tool_call_id": "send-fail",
        "content": json.dumps(payload, ensure_ascii=False),
    }
    projected = json.loads(
        project_model_record(
            record,
            tool_name="send_private_messages",
        )["content"]
    )
    assert projected["ok"] is False
    assert projected["error"] == "network down"
    assert projected["loop_reminder"] == {"message": "不要再重试同一错误"}


# ============================================================
# 第 2 轮 2A：sidecar 资格、brief、组内重复 ID
# ============================================================


def test_safe_no_action_sidecar_uses_raw_adjacency():
    pair = _single_no_action(call_id="na-raw")
    records = [
        pair[0],
        _runtime("task_context_snapshot", "中间插入的运行时"),
        pair[1],
    ]
    original = copy.deepcopy(records)
    policy = ModelViewPolicy(task_context_keep=0, no_action_keep=0)

    first = build_model_view(records, policy=policy)
    second = build_model_view(records, policy=policy)

    assert records == original
    assert len(first.records) == 2, "runtime 被删除，但 a/tool 仍保留"
    assert first.safe_no_action_groups == [], (
        "原始时序中调用与结果不相邻，不能因为删掉 runtime 就升级为安全组"
    )
    assert second.records == first.records
    assert second.raw_indices == first.raw_indices
    assert second.safe_no_action_groups == first.safe_no_action_groups


def test_sidecar_maps_kept_safe_group_to_view_indices():
    records = [
        {"role": "user", "content": "真实消息"},
        *_no_action_pair(1),
        *_no_action_pair(2),
    ]
    view = build_model_view(
        records,
        policy=ModelViewPolicy(no_action_keep=1),
    )

    # 第一组被裁掉，第二组保留：view = [user, assistant, tool]
    assert view.safe_no_action_groups == [(1, 2)]
    assert view.raw_indices == [0, 3, 4]


def test_model_view_result_sidecar_default_is_compatible():
    from core.model_view import ModelViewResult

    view = ModelViewResult(records=[], raw_indices=[])
    assert view.safe_no_action_groups == []


def test_disabled_model_view_has_no_sidecar():
    records = [*_no_action_pair(1)]
    view = build_model_view(
        records,
        policy=ModelViewPolicy(enabled=False, no_action_keep=0),
    )
    assert view.safe_no_action_groups == []


def test_business_brief_is_not_safe_and_is_preserved():
    business = {
        "ok": True,
        "no_action": True,
        "brief": "后续需要确认的业务提醒",
    }
    assert is_safe_no_action_result(business) is False

    record = _single_no_action(result=business)[1]
    projected = project_model_record(record, tool_name="no_action")
    assert projected is record
    assert json.loads(projected["content"])["brief"] == "后续需要确认的业务提醒"

    records = _single_no_action(result=business)
    assert no_action_pair_indices(records, 0) == set()


def test_known_no_action_brief_remains_compressible():
    result = {
        "ok": True,
        "no_action": True,
        "brief": "本轮不执行操作。",
    }
    assert is_safe_no_action_result(result) is True
    record = _single_no_action(result=result)[1]
    projected = project_model_record(record, tool_name="no_action")
    assert json.loads(projected["content"]) == {"ok": True, "no_action": True}


def test_duplicate_ids_inside_one_assistant_disable_tool_projection():
    from utils.model_records import assistant_tool_call_names

    send_payload = {
        "ok": True,
        "status": "sent",
        "qq_visible": True,
        "sent": [{"order": 1, "content": "正文" * 100, "msg_id": "m1"}],
    }
    records = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "dup",
                    "type": "function",
                    "function": {"name": "send_private_messages", "arguments": "{}"},
                },
                {
                    "id": "dup",
                    "type": "function",
                    "function": {"name": "no_action", "arguments": "{}"},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "dup",
            "content": json.dumps(send_payload, ensure_ascii=False),
        },
    ]
    assert assistant_tool_call_names(records[0]) == {}

    view = build_model_view(records)
    assert json.loads(view.records[1]["content"]) == send_payload, (
        "组内重复 ID 不能猜一个工具名去精简结果"
    )


def test_unknown_result_id_is_not_projected_from_payload_tool_field():
    payload = {
        "tool": "send_private_messages",
        "ok": True,
        "status": "sent",
        "sent": [{"order": 1, "content": "正文" * 100, "msg_id": "m1"}],
    }
    records = [
        {"role": "user", "content": "孤儿结果"},
        {
            "role": "tool",
            "tool_call_id": "orphan-id",
            "content": json.dumps(payload, ensure_ascii=False),
        },
    ]
    view = build_model_view(records)
    assert json.loads(view.records[1]["content"]) == payload
