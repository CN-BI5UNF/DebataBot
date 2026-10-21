"""给模型看的非破坏性历史视图。

背景：
    运行时会把 ``task_context``、``send_receipt``、``send_done`` 等一次性状态
    写回 history。它们对当轮有用，但对后续轮次只是过期快照；永久 append-only
    会让每次请求重复发送上百个旧快照，成为最大的 token 黑洞。

这里的选择：
    - **只裁剪发给模型的视图**，历史/归档/事件库原样保留；
    - 真实聊天消息全部保留；
    - 同类运行时快照只保留最近 N 个（只认内部 metadata.kind，不猜正文标签）；
    - 仅明确成功、单调用、无 reasoning/额外语义的 ``no_action`` 组才可压缩，
      失败/提醒/未知字段一律原样保留；
    - 超长 user 记录做确定性头尾截断，multimodal 只截 text 块；
    - 发送类工具结果投影为定位/状态字段，但保留 error/loop_reminder 等控制信息。

视图必须是确定性的：同样的输入永远得到同样的字节，避免无意义地破坏 KV 前缀。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from utils.model_records import (
    RUNTIME_KINDS,
    ModelViewPolicy,
    assistant_tool_call_names,
    no_action_pair_indices,
    project_model_record,
    runtime_kind,
    safe_no_action_groups,
)

logger = logging.getLogger(__name__)

_TASK_CONTEXT_SNAPSHOT = "task_context_snapshot"
_SEND_RECEIPT = "send_receipt"
_SEND_DONE_SNAPSHOT = "send_done_snapshot"
_RUNTIME_KINDS = RUNTIME_KINDS


@dataclass(slots=True)
class ModelViewResult:
    records: list[dict[str, Any]]
    raw_indices: list[int]
    dropped_runtime: int = 0
    dropped_no_action_pairs: int = 0
    dropped_no_action_records: int = 0
    capped_records: int = 0
    projected_tool_results: int = 0
    safe_no_action_groups: list[tuple[int, ...]] = field(default_factory=list)
    """view.records 下标形式的安全 no_action 组；资格来自原始时序 records。"""

    @property
    def dropped_records(self) -> int:
        return self.dropped_runtime + self.dropped_no_action_records


def _drop_old_runtime_records(
    records: list[dict[str, Any]],
    policy: ModelViewPolicy,
) -> tuple[set[int], int]:
    positions: dict[str, list[int]] = {kind: [] for kind in _RUNTIME_KINDS}
    for index, record in enumerate(records):
        kind = runtime_kind(record)
        if kind is not None:
            positions[kind].append(index)

    limits = {
        _TASK_CONTEXT_SNAPSHOT: policy.task_context_keep,
        _SEND_RECEIPT: policy.send_receipt_keep,
        _SEND_DONE_SNAPSHOT: policy.send_status_keep,
    }
    dropped: set[int] = set()
    for kind, indices in positions.items():
        keep = max(0, int(limits[kind]))
        if keep <= 0:
            dropped.update(indices)
            continue
        dropped.update(indices[:-keep])
    return dropped, len(dropped)


def _drop_old_no_action_pairs(
    records: list[dict[str, Any]],
    policy: ModelViewPolicy,
    *,
    groups: list[tuple[int, ...]] | None = None,
) -> tuple[set[int], int, int]:
    safe_groups = groups if groups is not None else safe_no_action_groups(records)
    keep = max(0, int(policy.no_action_keep))
    dropped_groups = safe_groups if keep <= 0 else safe_groups[:-keep]
    dropped_records: set[int] = set()
    for group in dropped_groups:
        dropped_records.update(group)
    return dropped_records, len(dropped_groups), len(dropped_records)


def model_view_keep_indices(
    records: list[dict[str, Any]],
    *,
    policy: ModelViewPolicy,
) -> set[int]:
    keep = set(range(len(records)))
    if not policy.enabled:
        return keep
    runtime_dropped, _ = _drop_old_runtime_records(records, policy)
    no_action_dropped, _, _ = _drop_old_no_action_pairs(records, policy)
    keep.difference_update(runtime_dropped)
    keep.difference_update(no_action_dropped)
    return keep


def model_view_drop_indices(
    records: list[dict[str, Any]],
    *,
    policy: ModelViewPolicy,
) -> set[int]:
    keep = model_view_keep_indices(records, policy=policy)
    return set(range(len(records))) - keep


def _local_tool_names(records: list[dict[str, Any]]) -> dict[int, str]:
    """把每条 tool 结果映射到**紧邻其前**且配对可信的 assistant 调用名。

    不使用全 history 的 ID→name 字典，避免跨轮重复 tool_call ID 时后面的
    no_action 覆盖前面的发送工具。组内出现重复/未知/错配结果时，整组映射
    直接废弃，对应 tool 记录保守地不做基于工具名的精简。
    """
    names: dict[int, str] = {}
    for assistant_index, record in enumerate(records):
        call_names = assistant_tool_call_names(record)
        if not call_names:
            continue
        cursor = assistant_index + 1
        group: dict[int, str] = {}
        seen: set[str] = set()
        valid = True
        while cursor < len(records) and records[cursor].get("role") == "tool":
            call_id = str(records[cursor].get("tool_call_id") or "")
            if call_id not in call_names or call_id in seen:
                valid = False
                break
            group[cursor] = call_names[call_id]
            seen.add(call_id)
            cursor += 1
            if len(seen) >= len(call_names):
                # 紧随其后仍有同组 ID 的额外结果：配对不干净，整组不可信。
                if (
                    cursor < len(records)
                    and records[cursor].get("role") == "tool"
                    and str(records[cursor].get("tool_call_id") or "") in call_names
                ):
                    valid = False
                break
        if valid and group and len(seen) == len(call_names):
            names.update(group)
    return names


def build_model_view(
    records: list[dict[str, Any]],
    *,
    policy: ModelViewPolicy | None = None,
    conversation_id: str | None = None,
) -> ModelViewResult:
    """构造模型可见视图；不修改输入 records。"""
    policy = policy or ModelViewPolicy()
    _ = conversation_id  # 预留给未来按会话强制保留；当前真实消息全部保留。
    if not policy.enabled:
        return ModelViewResult(
            records=list(records),
            raw_indices=list(range(len(records))),
        )

    safe_groups_raw = safe_no_action_groups(records)
    runtime_dropped, dropped_runtime = _drop_old_runtime_records(records, policy)
    no_action_dropped, dropped_pairs, dropped_pair_records = (
        _drop_old_no_action_pairs(records, policy, groups=safe_groups_raw)
    )
    dropped = runtime_dropped | no_action_dropped
    local_tool_names = _local_tool_names(records)

    view: list[dict[str, Any]] = []
    raw_indices: list[int] = []
    output_positions: dict[int, int] = {}
    capped_records = 0
    projected_tool_results = 0
    for index, record in enumerate(records):
        if index in dropped:
            continue
        kept = project_model_record(
            record,
            policy=policy,
            tool_name=local_tool_names.get(index),
            allow_tool_projection=index in local_tool_names,
        )
        if kept is not record:
            if record.get("role") == "user":
                capped_records += 1
            elif record.get("role") == "tool":
                projected_tool_results += 1
        output_positions[index] = len(view)
        view.append(kept)
        raw_indices.append(index)

    # sidecar 资格来自原始 records；这里只做 raw→view 下标换算。
    safe_groups_in_view: list[tuple[int, ...]] = []
    for group in safe_groups_raw:
        if all(index in output_positions for index in group):
            safe_groups_in_view.append(
                tuple(output_positions[index] for index in group)
            )

    return ModelViewResult(
        records=view,
        raw_indices=raw_indices,
        dropped_runtime=dropped_runtime,
        dropped_no_action_pairs=dropped_pairs,
        dropped_no_action_records=dropped_pair_records,
        capped_records=capped_records,
        projected_tool_results=projected_tool_results,
        safe_no_action_groups=safe_groups_in_view,
    )


def apply_model_view(
    records: list[dict[str, Any]],
    *,
    policy: ModelViewPolicy | None = None,
    conversation_id: str | None = None,
) -> list[dict[str, Any]]:
    return build_model_view(
        records,
        policy=policy,
        conversation_id=conversation_id,
    ).records


__all__ = [
    "ModelViewPolicy",
    "ModelViewResult",
    "apply_model_view",
    "build_model_view",
    "model_view_drop_indices",
    "model_view_keep_indices",
    "no_action_pair_indices",
    "runtime_kind",
    "safe_no_action_groups",
]
