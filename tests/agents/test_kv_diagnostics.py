"""KV 诊断分级测试。"""

from __future__ import annotations

from agents.runner import _kv_prompt_diagnostics


def _messages() -> list[dict]:
    return [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "真实消息"},
        {
            "role": "user",
            "content": '<task_context priority="medium">旧快照</task_context>',
        },
        {"role": "assistant", "content": ""},
        {"role": "tool", "tool_call_id": "t1", "content": '{"ok":true}'},
        {"role": "user", "content": "<send_receipt>回执</send_receipt>"},
    ]


def _tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "a",
                "description": "",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "b",
                "description": "",
                "parameters": {
                    "type": "object",
                    "properties": {"_tool_search_required": {"type": "boolean"}},
                },
            },
        },
    ]


def test_summary_mode_counts_without_full_serialization_fields():
    diag = _kv_prompt_diagnostics(
        _messages(),
        _tools(),
        loop=2,
        mode="summary",
    )
    assert diag["kv_diagnostics_mode"] == "summary"
    assert diag["kv_loop"] == 2
    assert diag["kv_message_count"] == 6
    assert diag["kv_task_context_block_count"] == 1
    assert diag["kv_send_receipt_block_count"] == 1
    assert diag["kv_tools_full_count"] == 1
    assert diag["kv_tools_stub_count"] == 1
    assert "kv_prefix_8k_hash" not in diag
    assert diag["kv_prefix_stable_hash"]


def test_off_mode_is_zero_work_marker():
    diag = _kv_prompt_diagnostics(_messages(), _tools(), loop=1, mode="off")
    assert diag == {"kv_diagnostics_mode": "off", "kv_loop": 1}


def test_full_mode_keeps_legacy_hash_fields():
    diag = _kv_prompt_diagnostics(_messages(), _tools(), loop=1, mode="full")
    assert diag["kv_diagnostics_mode"] == "full"
    assert "kv_prefix_8k_hash" in diag
    assert "kv_prefix_stable_hash" in diag


def test_tools_diagnostics_cache_is_reused():
    tools = _tools()
    cache: dict = {}
    first = _kv_prompt_diagnostics(
        _messages(),
        tools,
        loop=1,
        mode="summary",
        tools_diag_cache=cache,
    )
    second = _kv_prompt_diagnostics(
        _messages(),
        tools,
        loop=2,
        mode="summary",
        tools_diag_cache=cache,
    )
    assert first["kv_tools_hash"] == second["kv_tools_hash"]
    assert first["kv_tools_char_count"] == second["kv_tools_char_count"]
    assert cache


def test_tools_cache_uses_content_identity_not_object_identity():
    import copy

    tools = _tools()
    for tool in tools:
        tool["function"]["description"] = "a"
    cache: dict = {}

    first = _kv_prompt_diagnostics(
        _messages(), tools, loop=1, mode="summary", tools_diag_cache=cache
    )
    same_content = copy.deepcopy(tools)
    second = _kv_prompt_diagnostics(
        _messages(), same_content, loop=2, mode="summary", tools_diag_cache=cache
    )
    assert first["kv_tools_hash"] == second["kv_tools_hash"]
    assert len(cache) == 1

    same_length_changed = copy.deepcopy(tools)
    same_length_changed[0]["function"]["description"] = "b"
    third = _kv_prompt_diagnostics(
        _messages(),
        same_length_changed,
        loop=3,
        mode="summary",
        tools_diag_cache=cache,
    )
    assert third["kv_tools_hash"] != first["kv_tools_hash"]
    assert len(cache) == 2

    reordered = [tools[1], tools[0]]
    fourth = _kv_prompt_diagnostics(
        _messages(), reordered, loop=4, mode="summary", tools_diag_cache=cache
    )
    assert fourth["kv_tools_hash"] != first["kv_tools_hash"]
    assert len(cache) == 3

    for index in range(10):
        variant = copy.deepcopy(tools)
        variant[0]["function"]["description"] = f"variant-{index}"
        _kv_prompt_diagnostics(
            _messages(),
            variant,
            loop=index,
            mode="summary",
            tools_diag_cache=cache,
        )
    assert len(cache) <= 8
    assert all(
        "description" not in str(value) for value in cache.values()
    )


def test_summary_stable_hash_detects_same_length_text_change():
    base = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "AAAA"},
    ]
    changed = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "BBBB"},
    ]
    first = _kv_prompt_diagnostics(base, [], loop=1, mode="summary")
    second = _kv_prompt_diagnostics(changed, [], loop=1, mode="summary")
    assert first["kv_prefix_stable_hash"] != second["kv_prefix_stable_hash"]


def test_summary_hash_does_not_leak_image_data_url():
    import json

    base = {
        "role": "user",
        "content": [
            {"type": "text", "text": "图片说明"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAA"},
            },
        ],
    }
    changed = {
        "role": "user",
        "content": [
            {"type": "text", "text": "图片说明"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,BBBB"},
            },
        ],
    }
    first = _kv_prompt_diagnostics([base], [], loop=1, mode="summary")
    second = _kv_prompt_diagnostics([changed], [], loop=1, mode="summary")

    assert first["kv_prefix_stable_hash"] != second["kv_prefix_stable_hash"]
    serialized = json.dumps(first, ensure_ascii=False)
    assert "data:image" not in serialized
    assert "AAAA" not in serialized


def test_frozen_stable_prefix_end_ignores_later_tail_changes():
    first = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "稳定"},
        {"role": "user", "content": "<task_context>尾区 A</task_context>"},
    ]
    second = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "稳定"},
        {"role": "user", "content": "<task_context>尾区 B</task_context>"},
    ]
    first_diag = _kv_prompt_diagnostics(
        first, [], loop=1, mode="summary", stable_prefix_end=2
    )
    second_diag = _kv_prompt_diagnostics(
        second, [], loop=2, mode="summary", stable_prefix_end=2
    )
    assert first_diag["kv_prefix_stable_hash"] == second_diag["kv_prefix_stable_hash"]
    assert first_diag["kv_tail_block_count"] == 1


def test_full_diagnostics_redacts_large_image_data_url():
    import json

    url = "data:image/png;base64," + ("A" * 100_000)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "图"},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }
    ]
    diag = _kv_prompt_diagnostics(messages, [], loop=1, mode="full")
    dumped = json.dumps(diag, ensure_ascii=False)

    assert "A" * 1000 not in dumped
    assert "data:image" not in dumped
    assert diag["kv_serialized_char_count"] < 2000
    assert diag["kv_content_char_count"] == len("图")


def test_summary_hash_prefers_asset_identity_over_data_url_payload():
    ref = {
        "sha256": "a" * 64,
        "transform_version": "pillow-first-frame-max2048-v2",
        "detail": "auto",
        "byte_size": 1024,
    }

    def message(payload: str, sha: str = "a" * 64) -> dict:
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": "图"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{payload}"},
                },
            ],
            "metadata": {
                "image_assets": [{**ref, "sha256": sha}],
            },
        }

    first = _kv_prompt_diagnostics([message("AAAA")], [], loop=1, mode="summary")
    same_asset = _kv_prompt_diagnostics([message("BBBB")], [], loop=2, mode="summary")
    changed_asset = _kv_prompt_diagnostics(
        [message("AAAA", sha="b" * 64)], [], loop=3, mode="summary"
    )

    assert first["kv_prefix_stable_hash"] == same_asset["kv_prefix_stable_hash"]
    assert first["kv_prefix_stable_hash"] != changed_asset["kv_prefix_stable_hash"]


def test_summary_counts_text_inside_multimodal_content():
    messages = [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "前"},
                {"type": "text", "text": "后"},
            ],
            "_prompt_section": "volatile_tail",
        },
    ]
    diag = _kv_prompt_diagnostics(messages, [], loop=1, mode="summary")
    assert diag["kv_content_char_count"] == 5
    assert diag["kv_tail_char_count"] == 2
