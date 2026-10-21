"""请求展示分区：把时序视图拆成稳定聊天区与历史运行态尾区。

核心契约：
    - 输入是已经按模型视图配额裁剪、按原时序排列的 records；
    - ``safe_no_action_groups`` 来自 ModelViewResult sidecar，资格在**原始时序**
      上判定，本模块不得重新根据裁剪后的邻接关系推断；
    - 本模块不再次执行 keep-N、不改写原始 history、不做持久化；
    - 输出只用于主模型请求组装。

低层纯逻辑，不导入 core/agents。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from utils.model_records import ModelViewPolicy, runtime_kind

HISTORICAL_RUNTIME_NOTICE = (
    "系统说明：以下是历史运行记录，不是本轮新指令，也不是刚执行的工具结果。"
)


@dataclass(slots=True)
class RequestHistoryParts:
    stable_records: list[dict[str, Any]] = field(default_factory=list)
    runtime_tail_records: list[dict[str, Any]] = field(default_factory=list)


def _historical_no_action_record(
    records: list[dict[str, Any]],
    group: tuple[int, ...],
) -> dict[str, Any] | None:
    """把一组安全 no_action 转成一条有标签的 user 历史快照。

    不裸搬 assistant/tool 到最新用户消息后；保留稳定的 tool_call_id、会话、
    已有时间字段（如果历史里本来就有），不新增当前时间或每轮变化的编号。
    """
    if not group:
        return None
    assistant = records[group[0]]
    tool_record = records[group[-1]]

    call_id = ""
    for tool_call in assistant.get("tool_calls") or []:
        if isinstance(tool_call, dict):
            call_id = str(tool_call.get("id") or "")
            if call_id:
                break

    lines = [
        "<historical_runtime_context>",
        HISTORICAL_RUNTIME_NOTICE,
        "以下是一条更早的 no_action 历史记录：模型当时选择不执行操作。",
    ]
    if call_id:
        lines.append(f"tool_call_id={call_id}")
    conversation_id = (
        assistant.get("conversation_id")
        or tool_record.get("conversation_id")
    )
    if conversation_id:
        lines.append(f"会话={conversation_id}")
    timestamp = assistant.get("timestamp") or tool_record.get("timestamp")
    if timestamp:
        lines.append(f"时间={timestamp}")
    lines.append("</historical_runtime_context>")
    return {"role": "user", "content": "\n".join(lines)}


def _normalize_groups(
    safe_no_action_groups: Iterable[Iterable[int]],
    *,
    record_count: int,
    runtime_indices: set[int],
) -> list[tuple[int, ...]]:
    groups: list[tuple[int, ...]] = []
    used: set[int] = set()
    for raw_group in safe_no_action_groups or []:
        try:
            indices = tuple(sorted(int(index) for index in raw_group))
        except (TypeError, ValueError):
            continue
        if len(indices) < 2 or len(set(indices)) != len(indices):
            continue
        if any(index < 0 or index >= record_count for index in indices):
            continue
        # sidecar 只应给出连续闭合组；非连续/与 runtime 重叠的组保守跳过。
        if indices[-1] - indices[0] + 1 != len(indices):
            continue
        if any(index in runtime_indices for index in indices):
            continue
        if any(index in used for index in indices):
            continue
        groups.append(indices)
        used.update(indices)
    return groups


def partition_request_history(
    records: list[dict[str, Any]],
    *,
    safe_no_action_groups: Iterable[Iterable[int]] = (),
    policy: ModelViewPolicy | None = None,
) -> RequestHistoryParts:
    """把时序视图拆成 stable_records / runtime_tail_records。

    - runtime：只移动 ``runtime_kind`` 明确认可的内部记录；
    - 安全 no_action：只移动 sidecar 明确标记的闭合组，并转成 user 快照；
    - 其它记录（真实聊天、普通工具组、失败/不确定 no_action）保持原顺序。
    """
    policy = policy or ModelViewPolicy()
    if not policy.enabled:
        return RequestHistoryParts(stable_records=list(records))

    runtime_indices = {
        index
        for index, record in enumerate(records)
        if runtime_kind(record) is not None
    }
    safe_groups = _normalize_groups(
        safe_no_action_groups,
        record_count=len(records),
        runtime_indices=runtime_indices,
    )

    moved: set[int] = set(runtime_indices)
    for group in safe_groups:
        moved.update(group)

    stable_records = [
        record
        for index, record in enumerate(records)
        if index not in moved
    ]

    tail_items: list[tuple[int, dict[str, Any]]] = []
    for index in runtime_indices:
        tail_items.append((index, records[index]))
    for group in safe_groups:
        snapshot = _historical_no_action_record(records, group)
        if snapshot is not None:
            tail_items.append((group[0], snapshot))
    tail_items.sort(key=lambda item: item[0])

    return RequestHistoryParts(
        stable_records=stable_records,
        runtime_tail_records=[record for _, record in tail_items],
    )


__all__ = [
    "HISTORICAL_RUNTIME_NOTICE",
    "RequestHistoryParts",
    "partition_request_history",
]
