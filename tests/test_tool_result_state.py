"""共享工具结果状态谓词测试。"""

from __future__ import annotations

from utils.tool_result_state import (
    no_action_succeeded,
    result_has_error,
    tool_result_blocks_completion,
)


def test_result_has_error_treats_empty_values_as_no_error():
    for empty in (None, "", [], {}):
        assert result_has_error({"error": empty}) is False
        assert result_has_error({"errors": empty}) is False
    assert result_has_error({"error": "boom"}) is True
    assert result_has_error({"errors": ["boom"]}) is True


def test_tool_result_blocks_completion_covers_common_failure_shapes():
    assert tool_result_blocks_completion({"ok": False})
    assert tool_result_blocks_completion({"ok": "false"})
    assert tool_result_blocks_completion({"ok": "0"})
    assert tool_result_blocks_completion({"success": False})
    assert tool_result_blocks_completion({"error": "boom"})
    assert tool_result_blocks_completion({"status": " FAILED "})
    assert tool_result_blocks_completion({"status": ["needs_review", "done"]})
    assert tool_result_blocks_completion({"status": "policy_rejected"})
    assert tool_result_blocks_completion({"turn_completion": {"allowed": False}})


def test_tool_result_blocks_completion_allows_normal_descriptive_status():
    for result in (
        {"ok": True, "status": "inline"},
        {"ok": True, "status": "sent"},
        {"ok": True, "status": "done"},
        {"ok": True, "status": "already_committed"},
        {"ok": True, "status": "found"},
        {"ok": True, "status": "empty"},
    ):
        assert tool_result_blocks_completion(result) is False


def test_no_action_succeeded_requires_explicit_success_and_no_blockers():
    assert no_action_succeeded({"ok": True, "no_action": True})
    assert no_action_succeeded(
        {"ok": True, "no_action": True, "status": "done"}
    )
    assert no_action_succeeded(
        {"ok": True, "no_action": True, "status": " DONE "}
    )

    assert not no_action_succeeded({"ok": True})
    assert not no_action_succeeded({"no_action": True})
    assert not no_action_succeeded({"ok": True, "no_action": True, "error": "x"})
    assert not no_action_succeeded(
        {"ok": True, "no_action": True, "status": "failed"}
    )
    assert not no_action_succeeded(
        {"ok": True, "no_action": True, "status": "unknown_state"}
    )
    assert not no_action_succeeded(
        {"ok": True, "no_action": True, "turn_completion": {"allowed": False}}
    )
