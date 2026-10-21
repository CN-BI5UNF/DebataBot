"""工具结果的共享状态谓词。

低层纯逻辑，不导入 core/agents/tools，避免 runner 与 tools.base 各维护一份
不一致的状态列表。

注意区分两件事：
    - ``tool_result_blocks_completion``：这个结果是否要求本轮继续处理
      （失败、待审、需要纠正/重试、明确不允许完成）；
    - ``no_action_succeeded``：这个 no_action 是否明确执行成功。
后者比“可以安全压缩”更宽：成功但带业务 brief 的 no_action 仍算执行成功，
只是不适合删除/移入历史尾区。
"""

from __future__ import annotations

from typing import Any

# 明确的失败/拒绝/不可用状态。
_FAILURE_STATUSES = frozenset(
    {
        "denied",
        "error",
        "failed",
        "need_tool_search",
        "not_found",
        "stale",
        "timeout",
        "unavailable",
        "unsupported",
        "policy_rejected",
        "rejected",
        "blocked",
        "not_configured",
        "disabled",
        "insufficient_permission",
        "target_mismatch",
        "rate_limited",
        "expired",
        "cannot_commit_recalled_trigger",
        "need_group_id",
    }
)

# 尚未完成或需要继续处理的状态。
_PENDING_STATUSES = frozenset(
    {
        "needs_review",
        "needs_review_again",
        "partial",
        "running",
        "queued",
        "started",
        "correction",
        "retry",
    }
)

# no_action 明确成功时可接受的 status。
NO_ACTION_SUCCESS_STATUSES = frozenset(
    {"done", "ok", "success", "completed", "finished", "succeeded"}
)


def result_has_error(result: Any) -> bool:
    """error/errors 有非空内容即为错误；与 tools.base 的历史口径一致。"""
    if not isinstance(result, dict):
        return True
    for key in ("error", "errors"):
        value = result.get(key)
        if value not in (None, "", [], {}):
            return True
    return False


def _normalized_statuses(value: Any) -> list[str]:
    if isinstance(value, str):
        normalized = value.strip().lower()
        return [normalized] if normalized else []
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            text = str(item).strip().lower()
            if text:
                result.append(text)
        return result
    return []


def status_is_failure(status: str) -> bool:
    """状态是否表示失败/待处理。

    - 明确的失败/待审集合；
    - 含 failed/error 或结尾 _failed 的状态；
    - 其他普通描述状态（inline/artifact/sent/done 等）不算失败。
    """
    normalized = str(status or "").strip().lower()
    if not normalized:
        return False
    if normalized in _FAILURE_STATUSES or normalized in _PENDING_STATUSES:
        return True
    if (
        normalized.endswith("_failed")
        or "failed" in normalized
        or "error" in normalized
    ):
        return True
    return False


def _ok_is_explicit_false(value: Any) -> bool:
    if value is False:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"false", "0", "failed", "error", "no"}
    return False


def tool_result_blocks_completion(result: Any) -> bool:
    """结果是否明确要求继续处理，不能当作成功收尾。"""
    if not isinstance(result, dict):
        # 非 dict 结果无法证明成功，保守阻断。
        return True

    if _ok_is_explicit_false(result.get("ok")):
        return True
    if result.get("success") is False:
        return True
    if result_has_error(result):
        return True

    statuses = _normalized_statuses(result.get("status"))
    if any(status_is_failure(status) for status in statuses):
        return True

    completion = result.get("turn_completion")
    if isinstance(completion, dict) and completion.get("allowed") is False:
        return True
    return False


def no_action_succeeded(result: Any) -> bool:
    """no_action 是否明确执行成功。

    要求 ok/no_action 严格为 True、没有阻断信息；若带 status，必须是明确的
    成功状态。未知 status 保守按“未成功”处理，避免把失败或矛盾结果当成收尾。
    """
    if not isinstance(result, dict):
        return False
    if result.get("ok") is not True or result.get("no_action") is not True:
        return False
    if tool_result_blocks_completion(result):
        return False

    status = result.get("status")
    if status is None:
        return True
    if not isinstance(status, str):
        return False
    return status.strip().lower() in NO_ACTION_SUCCESS_STATUSES


__all__ = [
    "NO_ACTION_SUCCESS_STATUSES",
    "no_action_succeeded",
    "result_has_error",
    "status_is_failure",
    "tool_result_blocks_completion",
]
