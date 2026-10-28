"""S03 / C05: Raw Checkpoint、动作 Ledger 与循环内局部压缩。

职责：
1. 动作状态机管理（action_ledger）：
   prepared（工具调用已提取）
   → started（工具开始执行）
   → result_committed（结果已记录并追加）
   → unknown（超时或未知异常，保留无正文审计回执）。
2. 轮次检查点与水位追踪（turn_checkpoints）：
   记录每轮当前 watermark_seq 与 raw_seq。
3. 循环内局部压缩（LoopCompactor）：
   在长工具循环中，若单轮累积内容接近/超过上下文预算，
   对本轮已完成 checkpoint 的旧工具组执行局部紧缩，
   独立追踪局部 coverage，保留最新输入、pending 与未决工具。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memory.debata_db import DebataDB
from utils.token_budget import TokenEstimator

logger = logging.getLogger(__name__)

# 合法动作状态
ACTION_STATES = ("prepared", "started", "result_committed", "unknown")


class ActionLedgerError(RuntimeError):
    """动作 Ledger 错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


@dataclass(slots=True)
class ActionRecord:
    persona_id: str
    action_id: str
    turn_id: str | None
    state: str
    receipt: dict[str, Any]
    updated_at: str


@dataclass(slots=True)
class TurnCheckpoint:
    persona_id: str
    turn_id: str
    watermark_seq: int
    raw_seq: int
    state: str
    updated_at: str


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _has_table(conn: sqlite3.Connection, table_name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    )
    return cur.fetchone() is not None


class ActionLedgerManager:
    """管理 action_ledger 表的状态转换与持久化。"""

    def __init__(self, db: DebataDB | str | Path | sqlite3.Connection) -> None:
        self._db_or_conn = db

    def _connect(self) -> sqlite3.Connection:
        if isinstance(self._db_or_conn, sqlite3.Connection):
            return self._db_or_conn
        if isinstance(self._db_or_conn, DebataDB):
            return self._db_or_conn.connect()
        debata_db = DebataDB(self._db_or_conn)
        return debata_db.connect()

    def prepare_action(
        self,
        persona_id: str,
        action_id: str,
        *,
        turn_id: str | None = None,
        tool_name: str = "",
        args_summary: str = "",
    ) -> None:
        """记录动作已生成（prepared）。"""
        now = _now_iso()
        receipt = {"tool_name": tool_name, "args_summary": args_summary[:200]}
        conn = self._connect()
        if not _has_table(conn, "action_ledger"):
            return
        conn.execute(
            """
            INSERT INTO action_ledger (persona_id, action_id, turn_id, state, receipt_json, updated_at)
            VALUES (?, ?, ?, 'prepared', ?, ?)
            ON CONFLICT(persona_id, action_id) DO UPDATE SET
                state = 'prepared',
                turn_id = COALESCE(excluded.turn_id, action_ledger.turn_id),
                receipt_json = excluded.receipt_json,
                updated_at = excluded.updated_at
            """,
            (persona_id, action_id, turn_id, json.dumps(receipt), now),
        )
        conn.commit()

    def start_action(self, persona_id: str, action_id: str) -> None:
        """标记动作开始执行（started）。"""
        now = _now_iso()
        conn = self._connect()
        if not _has_table(conn, "action_ledger"):
            return
        conn.execute(
            """
            UPDATE action_ledger
            SET state = 'started', updated_at = ?
            WHERE persona_id = ? AND action_id = ?
            """,
            (now, persona_id, action_id),
        )
        conn.commit()

    def commit_action_result(
        self,
        persona_id: str,
        action_id: str,
        *,
        receipt: dict[str, Any] | None = None,
    ) -> None:
        """标记动作已成功提交结果（result_committed）。"""
        now = _now_iso()
        receipt_str = json.dumps(receipt or {"status": "ok"})
        conn = self._connect()
        if not _has_table(conn, "action_ledger"):
            return
        conn.execute(
            """
            UPDATE action_ledger
            SET state = 'result_committed', receipt_json = ?, updated_at = ?
            WHERE persona_id = ? AND action_id = ?
            """,
            (receipt_str, now, persona_id, action_id),
        )
        conn.commit()

    def mark_action_unknown(
        self,
        persona_id: str,
        action_id: str,
        *,
        error_detail: str = "",
    ) -> None:
        """标记动作处于未知状态（unknown）。

        防范 E33：动作超时或崩溃时，不得假装未发生，保留无正文审计回执。
        """
        now = _now_iso()
        receipt = {
            "status": "unknown",
            "error": error_detail[:500],
            "note": "external action outcome unknown, audited for safety",
        }
        conn = self._connect()
        if not _has_table(conn, "action_ledger"):
            return
        conn.execute(
            """
            INSERT INTO action_ledger (persona_id, action_id, state, receipt_json, updated_at)
            VALUES (?, ?, 'unknown', ?, ?)
            ON CONFLICT(persona_id, action_id) DO UPDATE SET
                state = 'unknown',
                receipt_json = excluded.receipt_json,
                updated_at = excluded.updated_at
            """,
            (persona_id, action_id, json.dumps(receipt), now),
        )
        conn.commit()

    def get_action(self, persona_id: str, action_id: str) -> ActionRecord | None:
        conn = self._connect()
        if not _has_table(conn, "action_ledger"):
            return None
        row = conn.execute(
            """
            SELECT persona_id, action_id, turn_id, state, receipt_json, updated_at
            FROM action_ledger WHERE persona_id = ? AND action_id = ?
            """,
            (persona_id, action_id),
        ).fetchone()
        if row is None:
            return None
        try:
            receipt = json.loads(row["receipt_json"])
        except Exception:
            receipt = {}
        return ActionRecord(
            persona_id=row["persona_id"],
            action_id=row["action_id"],
            turn_id=row["turn_id"],
            state=row["state"],
            receipt=receipt,
            updated_at=row["updated_at"],
        )


class TurnCheckpointManager:
    """管理 turn_checkpoints 表的水位与检查点。"""

    def __init__(self, db: DebataDB | str | Path | sqlite3.Connection) -> None:
        self._db_or_conn = db

    def _connect(self) -> sqlite3.Connection:
        if isinstance(self._db_or_conn, sqlite3.Connection):
            return self._db_or_conn
        if isinstance(self._db_or_conn, DebataDB):
            return self._db_or_conn.connect()
        debata_db = DebataDB(self._db_or_conn)
        return debata_db.connect()

    def open_turn(
        self,
        persona_id: str,
        turn_id: str,
        *,
        watermark_seq: int = 0,
    ) -> None:
        now = _now_iso()
        conn = self._connect()
        if not _has_table(conn, "turn_checkpoints"):
            return
        conn.execute(
            """
            INSERT INTO turn_checkpoints (persona_id, turn_id, watermark_seq, raw_seq, state, updated_at)
            VALUES (?, ?, ?, 0, 'open', ?)
            ON CONFLICT(persona_id, turn_id) DO UPDATE SET
                watermark_seq = excluded.watermark_seq,
                state = 'open',
                updated_at = excluded.updated_at
            """,
            (persona_id, turn_id, watermark_seq, now),
        )
        conn.commit()

    def record_step(
        self,
        persona_id: str,
        turn_id: str,
        raw_seq: int,
    ) -> None:
        now = _now_iso()
        conn = self._connect()
        if not _has_table(conn, "turn_checkpoints"):
            return
        conn.execute(
            """
            UPDATE turn_checkpoints
            SET raw_seq = ?, updated_at = ?
            WHERE persona_id = ? AND turn_id = ?
            """,
            (raw_seq, now, persona_id, turn_id),
        )
        conn.commit()

    def close_turn(
        self,
        persona_id: str,
        turn_id: str,
    ) -> None:
        now = _now_iso()
        conn = self._connect()
        if not _has_table(conn, "turn_checkpoints"):
            return
        conn.execute(
            """
            UPDATE turn_checkpoints
            SET state = 'closed', updated_at = ?
            WHERE persona_id = ? AND turn_id = ?
            """,
            (now, persona_id, turn_id),
        )
        conn.commit()

    def get_checkpoint(
        self, persona_id: str, turn_id: str
    ) -> TurnCheckpoint | None:
        conn = self._connect()
        if not _has_table(conn, "turn_checkpoints"):
            return None
        row = conn.execute(
            """
            SELECT persona_id, turn_id, watermark_seq, raw_seq, state, updated_at
            FROM turn_checkpoints WHERE persona_id = ? AND turn_id = ?
            """,
            (persona_id, turn_id),
        ).fetchone()
        if row is None:
            return None
        return TurnCheckpoint(
            persona_id=row["persona_id"],
            turn_id=row["turn_id"],
            watermark_seq=int(row["watermark_seq"]),
            raw_seq=int(row["raw_seq"]),
            state=row["state"],
            updated_at=row["updated_at"],
        )


@dataclass(slots=True)
class LoopCompactionResult:
    """循环内局部紧缩结果。"""

    compacted: bool
    compacted_records_count: int
    saved_tokens: int
    remaining_messages: list[dict[str, Any]]
    covered_tool_call_ids: set[str] = field(default_factory=set)


def compact_turn_loop(
    messages: list[dict[str, Any]],
    *,
    input_budget: int,
    estimator: TokenEstimator,
    turn_id: str = "",
) -> LoopCompactionResult:
    """对单轮内积累的多步工具调用执行局部紧缩。

    规则（S03 / C05）：
    1. 仅对本轮内**已闭合**的历史工具组（assistant[tool_calls] + matching tool[tool_call_id]）
       进行紧缩，将其替换为紧凑的工具调用摘要描述。
    2. 绝不修改或截断最新一条用户输入、pending 消息及尚未闭合的工具调用。
    3. 局部 coverage 独立追踪，不直接推进全局持久 sequence。
    """
    if not messages:
        return LoopCompactionResult(False, 0, 0, messages)

    current_tokens = estimator.estimate_messages(messages)
    if current_tokens <= input_budget:
        return LoopCompactionResult(False, 0, 0, messages)

    # 寻找本轮内部已闭合的工具组
    # 工具组结构：assistant(index_a) 携带 tool_calls -> 后续跟随对应的 tool(index_t)
    tool_call_to_result: dict[str, int] = {}
    for i, m in enumerate(messages):
        if m.get("role") == "tool" and m.get("tool_call_id"):
            tool_call_to_result[str(m["tool_call_id"])] = i

    compacted_messages: list[dict[str, Any]] = []
    covered_ids: set[str] = set()
    compacted_count = 0
    i = 0
    n = len(messages)

    while i < n:
        msg = messages[i]
        # 保护最后一项或非工具消息
        if i == n - 1 or msg.get("role") != "assistant" or not msg.get("tool_calls"):
            compacted_messages.append(msg)
            i += 1
            continue

        # 检查该 assistant 包含的所有 tool_calls 是否均已在后续闭合
        tcs = msg.get("tool_calls", [])
        tc_ids = [
            str(tc.get("id"))
            for tc in tcs
            if isinstance(tc, dict) and tc.get("id")
        ]
        all_closed = bool(tc_ids and all(tid in tool_call_to_result for tid in tc_ids))
        if not all_closed:
            compacted_messages.append(msg)
            i += 1
            continue

        # 如果是本轮最新一次工具调用，保留原样
        is_latest_tool_round = all(
            tool_call_to_result[tid] >= n - len(tc_ids) - 1 for tid in tc_ids
        )
        if is_latest_tool_round:
            compacted_messages.append(msg)
            i += 1
            continue

        # 该工具组已闭合且属于早期轮次，可进行局部紧缩
        # 将 verbose 的 tool 响应紧缩为简短的 text note
        tool_names = [
            tc.get("function", {}).get("name", "tool")
            if isinstance(tc, dict)
            else "tool"
            for tc in tcs
        ]
        compacted_asst = dict(msg)
        # 移除庞大的 reasoning 或长说明，保留必要指示
        compacted_asst["content"] = (
            str(msg.get("content") or "").strip()
            or f"（已调用工具：{', '.join(tool_names)}）"
        )
        compacted_messages.append(compacted_asst)
        covered_ids.update(tc_ids)
        compacted_count += 1
        i += 1

        # 紧跟的处理匹配的 tool 记录
        while i < n and messages[i].get("role") == "tool":
            tool_msg = messages[i]
            tid = str(tool_msg.get("tool_call_id") or "")
            if tid in covered_ids:
                # 紧缩 tool 结果
                raw_content = str(tool_msg.get("content") or "")
                summary_content = (
                    raw_content[:150] + "..."
                    if len(raw_content) > 150
                    else raw_content
                )
                compacted_tool = dict(tool_msg)
                compacted_tool["content"] = f"[工具结果摘要] {summary_content}"
                compacted_messages.append(compacted_tool)
                compacted_count += 1
            else:
                compacted_messages.append(tool_msg)
            i += 1

    after_tokens = estimator.estimate_messages(compacted_messages)
    saved = max(0, current_tokens - after_tokens)

    return LoopCompactionResult(
        compacted=saved > 0,
        compacted_records_count=compacted_count,
        saved_tokens=saved,
        remaining_messages=compacted_messages,
        covered_tool_call_ids=covered_ids,
    )
