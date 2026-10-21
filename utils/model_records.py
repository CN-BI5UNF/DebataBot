"""模型视图的纯记录级策略、资格判断与投影逻辑。

这个模块是**低层纯逻辑**：只依赖标准库，不导入 ``core``/``agents``，避免
``agents`` → ``core`` → ``agents`` 的循环导入。工具结果的发送字段投影在函数内部
局部导入 ``tools.result_shrink``，不会在模块导入期触发工具包加载。

``core.model_view`` 从这里导入 ``ModelViewPolicy`` 等符号并继续对外导出，
因此旧的 ``from core.model_view import ModelViewPolicy`` 不会失效。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from utils.tool_result_state import no_action_succeeded

_TASK_CONTEXT_SNAPSHOT = "task_context_snapshot"
_SEND_RECEIPT = "send_receipt"
_SEND_DONE_SNAPSHOT = "send_done_snapshot"
RUNTIME_KINDS: tuple[str, ...] = (
    _TASK_CONTEXT_SNAPSHOT,
    _SEND_RECEIPT,
    _SEND_DONE_SNAPSHOT,
)

_TRUNCATION_MARKER = (
    "\n...[model view truncated: original {chars} chars; "
    "full record retained in history/archive]...\n"
)
_TRUNCATION_MARKER_HINT = "full record retained in history/archive"

_NO_ACTION_ALLOWED_KEYS = frozenset(
    {"ok", "no_action", "status", "tool", "result_format", "brief"}
)
# 只允许已知的固定无操作说明；任意业务 brief 默认视为有语义、不可删除。
_KNOWN_NO_ACTION_BRIEFS = frozenset({"", "本轮不执行操作。"})


@dataclass(frozen=True, slots=True)
class ModelViewPolicy:
    """模型视图裁剪策略。所有“保留”都是指最近 N 个。"""

    enabled: bool = True
    task_context_keep: int = 2
    send_receipt_keep: int = 2
    send_status_keep: int = 2
    no_action_keep: int = 4
    max_record_chars: int = 8000
    project_tool_results: bool = True


def runtime_kind(record: dict[str, Any]) -> str | None:
    """识别可信的内部运行时记录。

    只有 user 角色且 ``metadata.kind`` 属于受信内部种类时才成立。
    带 ``metadata.messages`` 的入站真实聊天永远优先视为真实聊天，即使用户文本
    恰好以 ``<task_context`` 等标签开头也绝不裁剪。没有可信 metadata 的旧记录
    保守保留，不再根据正文标签猜测。
    """
    if not isinstance(record, dict) or record.get("role") != "user":
        return None
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        return None
    if "messages" in metadata:
        # _build_user_record 生成的入站真实聊天标记。
        return None
    kind = metadata.get("kind")
    if isinstance(kind, str) and kind in RUNTIME_KINDS:
        return kind
    return None


def assistant_tool_call_names(record: dict[str, Any]) -> dict[str, str]:
    """返回可信的 tool_call ID→name 映射。

    任一条调用不合法（非 dict、ID 空/重复、function/name 不合法）时返回空字典，
    让调用方保守地跳过基于工具名的精简，而不是猜一个名字。
    """
    if record.get("role") != "assistant":
        return {}
    tool_calls = record.get("tool_calls")
    if not isinstance(tool_calls, list):
        return {}
    result: dict[str, str] = {}
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            return {}
        call_id = tool_call.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            return {}
        if call_id in result:
            return {}
        function = tool_call.get("function")
        if not isinstance(function, dict):
            return {}
        name = function.get("name")
        if not isinstance(name, str) or not name:
            return {}
        result[call_id] = name
    return result


def _tool_result_matches(record: dict[str, Any], call_id: str) -> bool:
    return (
        record.get("role") == "tool"
        and str(record.get("tool_call_id") or "") == call_id
    )


def _record_has_retained_reasoning(record: dict[str, Any]) -> bool:
    reasoning_content = record.get("reasoning_content")
    if isinstance(reasoning_content, str):
        if reasoning_content.strip():
            return True
    elif reasoning_content not in (None, ""):
        return True
    reasoning_blocks = record.get("reasoning_blocks")
    return bool(reasoning_blocks)


def _is_empty_tool_arguments(value: Any) -> bool:
    """no_action 只能接受合法空对象；坏 JSON/未知参数保守保留。"""
    if value is None:
        return True
    if isinstance(value, dict):
        return not value
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return True
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and not parsed


def _parse_json_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def is_safe_no_action_result(result: Any) -> bool:
    """判断一个已解析的 no_action 结果是否可安全精简。

    这比“执行成功”更严格：
      - 明确的执行成功与无阻断信息（复用 no_action_succeeded）；
      - allowlist 之外没有任何字段（error/loop_reminder/next/未知业务字段）；
      - 非空 brief 只接受已知固定模板，任意业务提醒一律保留。
    """
    if not isinstance(result, dict):
        return False
    if set(result) - _NO_ACTION_ALLOWED_KEYS:
        return False
    if not no_action_succeeded(result):
        return False

    tool = result.get("tool")
    if tool is not None and tool != "no_action":
        return False

    result_format = result.get("result_format")
    if result_format is not None and result_format != "structured_json":
        return False

    brief = result.get("brief")
    if brief is None:
        return True
    if not isinstance(brief, str):
        return False
    return brief.strip() in _KNOWN_NO_ACTION_BRIEFS


def no_action_pair_indices(
    records: list[dict[str, Any]],
    start: int,
) -> set[int]:
    """若 ``start`` 开始是一组**可安全压缩**的 no_action 调用，返回整组下标。

    第一版保守规则：只接受恰好一个 no_action 调用的闭合组。必须同时满足：
    assistant 无正文、无保留的 reasoning、唯一合法空参数调用、紧随其后同 ID
    的 tool 结果，且结果是明确成功、没有错误/提醒/未知语义字段的 envelope。
    任一条件不满足都返回空集合，绝不拆散或改写语义。
    """
    if start < 0 or start >= len(records):
        return set()
    assistant_record = records[start]
    if assistant_record.get("role") != "assistant":
        return set()
    if str(assistant_record.get("content") or "").strip():
        return set()
    if _record_has_retained_reasoning(assistant_record):
        return set()

    tool_calls = assistant_record.get("tool_calls")
    if not isinstance(tool_calls, list) or len(tool_calls) != 1:
        return set()
    tool_call = tool_calls[0]
    if not isinstance(tool_call, dict):
        return set()
    call_id = str(tool_call.get("id") or "")
    if not call_id:
        return set()
    function = tool_call.get("function")
    if not isinstance(function, dict):
        return set()
    if str(function.get("name") or "") != "no_action":
        return set()
    if not _is_empty_tool_arguments(function.get("arguments")):
        return set()

    result_index = start + 1
    if result_index >= len(records):
        return set()
    result_record = records[result_index]
    if not _tool_result_matches(result_record, call_id):
        return set()
    # 同一个 ID 后面还有额外结果，说明配对不干净，保守保留。
    if result_index + 1 < len(records) and _tool_result_matches(
        records[result_index + 1],
        call_id,
    ):
        return set()
    payload = _parse_json_object(result_record.get("content"))
    if payload is None or not is_safe_no_action_result(payload):
        return set()
    return {start, result_index}


def safe_no_action_groups(
    records: list[dict[str, Any]],
) -> list[tuple[int, ...]]:
    """在**原始时序 records** 上扫描所有可安全压缩的 no_action 组。

    返回值是原始下标元组，供 ModelViewResult sidecar 使用；调用方不得在
    已裁剪/已投影的 records 上重新推断资格。
    """
    groups: list[tuple[int, ...]] = []
    index = 0
    while index < len(records):
        pair = no_action_pair_indices(records, index)
        if pair:
            groups.append(tuple(sorted(pair)))
            index = max(pair) + 1
            continue
        index += 1
    return groups


def cap_content(content: str, limit: int) -> str:
    """对纯文本做确定性头尾截断；已带标记或未超限时原样返回。"""
    if limit <= 0 or len(content) <= limit:
        return content
    if _TRUNCATION_MARKER_HINT in content:
        return content
    marker = _TRUNCATION_MARKER.format(chars=len(content))
    room = max(0, limit - len(marker))
    if room <= 0:
        return marker.strip()
    head = max(0, int(room * 0.6))
    tail = max(0, room - head)
    prefix = content[:head].rstrip()
    suffix = content[-tail:].lstrip() if tail else ""
    return f"{prefix}{marker}{suffix}"


def _project_content_blocks(
    content: list[Any],
    policy: ModelViewPolicy,
) -> tuple[list[Any], bool]:
    """只截断 text 块；图片块/URL/顺序保持不变。"""
    projected: list[Any] = []
    changed = False
    for block in content:
        if isinstance(block, dict):
            block_type = str(block.get("type") or "")
            text = block.get("text")
            if (
                block_type in {"text", "input_text", "output_text"}
                and isinstance(text, str)
            ):
                capped = cap_content(text, policy.max_record_chars)
                if capped != text:
                    updated = dict(block)
                    updated["text"] = capped
                    projected.append(updated)
                    changed = True
                    continue
        projected.append(block)
    return projected, changed


def _project_tool_payload(
    tool_name: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if tool_name == "no_action":
        return {"ok": True, "no_action": True} if is_safe_no_action_result(payload) else None

    # 局部导入避免 utils → tools 包在模块加载期形成重依赖/循环。
    from tools.result_shrink import SEND_RESULT_TOOLS, project_send_result_payload

    if tool_name in SEND_RESULT_TOOLS:
        return project_send_result_payload(payload)
    return None


def _project_tool_record(
    record: dict[str, Any],
    policy: ModelViewPolicy,
    *,
    tool_name: str | None,
) -> dict[str, Any]:
    content = record.get("content")
    payload = _parse_json_object(content)
    if payload is None:
        return record
    resolved_name = str(tool_name or payload.get("tool") or "")
    projected_payload = _project_tool_payload(resolved_name, payload)
    if projected_payload is None:
        return record
    serialized = json.dumps(projected_payload, ensure_ascii=False, separators=(",", ":"))
    if serialized == content:
        return record
    projected = dict(record)
    projected["content"] = serialized
    return projected


def project_model_record(
    record: dict[str, Any],
    *,
    policy: ModelViewPolicy | None = None,
    tool_name: str | None = None,
    allow_tool_projection: bool = True,
) -> dict[str, Any]:
    """返回发给模型用的记录副本；不修改输入。

    - user 记录只截断文本（字符串或 text 块），不碰图片块/data URL。
    - tool 记录仅对可确认安全的 no_action 和发送类结果做精简；
      失败、坏 JSON、未知工具一律原样保留。
    - assistant/system 记录保持原样，避免丢失 reasoning/真实聊天。
    - 对同一记录重复调用是幂等的。
    """
    if not isinstance(record, dict):
        return record
    policy = policy or ModelViewPolicy()
    if not policy.enabled:
        return record

    role = record.get("role")
    if role == "user":
        content = record.get("content")
        if isinstance(content, str):
            capped = cap_content(content, policy.max_record_chars)
            if capped != content:
                projected = dict(record)
                projected["content"] = capped
                return projected
            return record
        if isinstance(content, list):
            projected_content, changed = _project_content_blocks(content, policy)
            if changed:
                projected = dict(record)
                projected["content"] = projected_content
                return projected
            return record
        return record

    if role == "tool" and policy.project_tool_results:
        if not allow_tool_projection:
            # 无法可信确定工具名/组配对时，不按猜测的名字精简。
            return record
        return _project_tool_record(record, policy, tool_name=tool_name)
    return record


__all__ = [
    "ModelViewPolicy",
    "RUNTIME_KINDS",
    "assistant_tool_call_names",
    "cap_content",
    "is_safe_no_action_result",
    "no_action_pair_indices",
    "project_model_record",
    "runtime_kind",
    "safe_no_action_groups",
]
