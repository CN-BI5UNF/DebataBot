"""Q01: 聊天 Read Model Keyset 稳定分页与游标（Cursor）契约。

核心契约（第二阶段接续方案 2.6 第 20 节）：
1. 稳定 Keyset 游标分页（杜绝全量历史/归档在内存 merge 后做 offset slice，杜绝截断最近 2000 条充当全历史）：
   - 单页限制：默认 page_size=50，硬上限 max_page_size=200。
   - 不透明游标（Opaque Cursor）：包含 (scope, conversation_id, timestamp, tie_breaker_id, version, direction)，带有防篡改签名。
   - SQL 级 keyset 条件过滤：利用 (timestamp, id) 联合索引实现 O(1) 游标推进。
2. 墓碑优先与读屏障即时生效：
   - 查询时自动过滤当前处于 tombstones 中的记录或 deleted_at 非空的行。
3. 工具组边界保护：
   - 跨页切分时，若末尾记录为携带 tool_calls 的 assistant 消息，自动补齐对应的 tool 响应，杜绝协议割裂。
4. 计数模式：
   - 区分精确计数（exact_count）与有界/估算计数（estimated_count）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memory.debata_db import DebataDB

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
_CURSOR_KEY = b"debata-chat-cursor-key-v2.6"


class ChatPaginationError(RuntimeError):
    """聊天分页错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


@dataclass(slots=True)
class ChatCursor:
    scope: str
    conversation_id: str | None
    timestamp: str
    tie_breaker_id: int
    version: int = 1
    direction: str = "backward"  # "backward" (历史往前翻) | "forward" (往后翻)


@dataclass(slots=True)
class ChatPageRequest:
    persona_id: str
    scope: str = "all"  # "session" | "all"
    conversation_id: str | None = None
    cursor: str | None = None
    limit: int = DEFAULT_PAGE_SIZE
    query: str | None = None
    direction: str = "backward"


@dataclass(slots=True)
class ChatPageResponse:
    items: list[dict[str, Any]]
    next_cursor: str | None
    prev_cursor: str | None
    has_more: bool
    count: int
    total_count: int
    is_exact_count: bool


def _encode_cursor(cur: ChatCursor) -> str:
    payload = json.dumps(
        {
            "s": cur.scope,
            "c": cur.conversation_id,
            "t": cur.timestamp,
            "i": cur.tie_breaker_id,
            "v": cur.version,
            "d": cur.direction,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    sig = hmac.new(_CURSOR_KEY, payload, hashlib.sha256).digest()[:12]
    token_bytes = payload + b"." + sig
    return base64.urlsafe_b64encode(token_bytes).decode("ascii")


def _decode_cursor(token: str) -> ChatCursor:
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        payload, sig = raw.rsplit(b".", 1)
        expected_sig = hmac.new(_CURSOR_KEY, payload, hashlib.sha256).digest()[:12]
        if not hmac.compare_digest(sig, expected_sig):
            raise ChatPaginationError("cursor_invalid_signature", "游标签名无效或已被篡改")
        data = json.loads(payload.decode("utf-8"))
        return ChatCursor(
            scope=str(data["s"]),
            conversation_id=data.get("c"),
            timestamp=str(data["t"]),
            tie_breaker_id=int(data["i"]),
            version=int(data.get("v", 1)),
            direction=str(data.get("d", "backward")),
        )
    except (ValueError, KeyError, IndexError) as err:
        raise ChatPaginationError("cursor_malformed", "游标格式损坏") from err


class ChatPaginationManager:
    """基于 Keyset 的高效、有界聊天 Read Model 分页服务。"""

    def __init__(self, db: DebataDB | str | Path | sqlite3.Connection) -> None:
        self._db_or_conn = db

    def _get_connection(self) -> sqlite3.Connection:
        if isinstance(self._db_or_conn, sqlite3.Connection):
            return self._db_or_conn
        if isinstance(self._db_or_conn, DebataDB):
            return self._db_or_conn.connect()
        debata_db = DebataDB(self._db_or_conn)
        return debata_db.connect()

    def fetch_page(self, req: ChatPageRequest) -> ChatPageResponse:
        """从 SQLite 获取 Keyset 分页，墓碑优先过滤并保证工具组完整。"""
        limit = max(1, min(int(req.limit), MAX_PAGE_SIZE))
        conn = self._get_connection()
        persona_id = req.persona_id

        # 检查表是否存在
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='history_records'"
        )
        if not cur.fetchone():
            return ChatPageResponse(
                items=[],
                next_cursor=None,
                prev_cursor=None,
                has_more=False,
                count=0,
                total_count=0,
                is_exact_count=True,
            )

        col_names = {col[1] for col in conn.execute("PRAGMA table_info(history_records)").fetchall()}
        json_col = "record_json" if "record_json" in col_names else "payload"

        # 解析 Cursor
        cursor_obj: ChatCursor | None = None
        if req.cursor:
            cursor_obj = _decode_cursor(req.cursor)

        direction = cursor_obj.direction if cursor_obj else req.direction

        # 构建基础条件
        base_conditions = ["1=1"]
        base_params: list[Any] = []

        if "persona_id" in col_names:
            base_conditions.append("persona_id = ?")
            base_params.append(persona_id)

        if req.conversation_id:
            base_conditions.append("conversation_id = ?")
            base_params.append(req.conversation_id)

        conditions = list(base_conditions)
        params: list[Any] = list(base_params)

        # 墓碑过滤：过滤在 tombstones 表中的记录
        has_tombstones = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tombstones'"
            ).fetchone()
        )
        if has_tombstones:
            conditions.append(
                """
                (record_uuid IS NULL OR record_uuid NOT IN (
                    SELECT record_uuid FROM tombstones WHERE persona_id = ?
                ))
                """
            )
            params.append(persona_id)

        # 软删除/隐藏过滤
        if "deleted_at" in col_names:
            conditions.append("deleted_at IS NULL")
        if "visibility" in col_names:
            conditions.append("(visibility IS NULL OR visibility != 'hidden')")

        # 文本搜索
        if req.query and req.query.strip():
            conditions.append(f"{json_col} LIKE ?")
            params.append(f"%{req.query.strip()}%")

        # Keyset 游标过滤 (created_at / id)
        time_col = "created_at"
        if cursor_obj:
            if direction == "backward":
                conditions.append(f"({time_col} < ? OR ({time_col} = ? AND id < ?))")
                params.extend([cursor_obj.timestamp, cursor_obj.timestamp, cursor_obj.tie_breaker_id])
            else:
                conditions.append(f"({time_col} > ? OR ({time_col} = ? AND id > ?))")
                params.extend([cursor_obj.timestamp, cursor_obj.timestamp, cursor_obj.tie_breaker_id])

        # 排序：backward 取更早记录（DESC），获取后再反转成时间正序显示
        order_sql = "DESC" if direction == "backward" else "ASC"
        # 多取一条用于探测 has_more
        fetch_limit = limit + 1

        sql = f"""
            SELECT id, {time_col}, conversation_id, {json_col}
            FROM history_records
            WHERE {' AND '.join(conditions)}
            ORDER BY {time_col} {order_sql}, id {order_sql}
            LIMIT {fetch_limit}
        """

        rows = conn.execute(sql, params).fetchall()
        has_more = len(rows) > limit
        actual_rows = rows[:limit]

        if direction == "backward":
            actual_rows.reverse()

        items: list[dict[str, Any]] = []
        for r in actual_rows:
            raw_json = str(r[json_col] or "{}")
            try:
                item_dict = json.loads(raw_json)
                if not isinstance(item_dict, dict):
                    item_dict = {"content": raw_json}
            except Exception:
                item_dict = {"content": raw_json}
            item_dict["_id"] = int(r["id"])
            item_dict["_created_at"] = str(r[time_col] or "")
            item_dict["conversation_id"] = r["conversation_id"]
            items.append(item_dict)

        # 工具组边界保护：如果最后一项包含 tool_calls，检查是否跨页割裂
        if items and items[-1].get("role") == "assistant" and items[-1].get("tool_calls"):
            last_tc_ids = {
                str(tc.get("id"))
                for tc in items[-1].get("tool_calls", [])
                if isinstance(tc, dict) and tc.get("id")
            }
            # 补齐紧随其后的 tool 响应
            if last_tc_ids:
                last_id = items[-1]["_id"]
                tool_rows = conn.execute(
                    f"""
                    SELECT id, {time_col}, conversation_id, {json_col}
                    FROM history_records
                    WHERE id > ? AND {' AND '.join(base_conditions)}
                    ORDER BY id ASC LIMIT 10
                    """,
                    [last_id] + base_params,
                ).fetchall()
                for tr in tool_rows:
                    t_dict = json.loads(str(tr[json_col] or "{}"))
                    if t_dict.get("role") == "tool" and str(t_dict.get("tool_call_id")) in last_tc_ids:
                        t_dict["_id"] = int(tr["id"])
                        t_dict["_created_at"] = str(tr[time_col] or "")
                        items.append(t_dict)
                        last_tc_ids.discard(str(t_dict.get("tool_call_id")))
                        if not last_tc_ids:
                            break

        # 生成游标
        next_cursor = None
        prev_cursor = None
        if items:
            first_item = items[0]
            last_item = items[-1]
            if has_more:
                next_cursor = _encode_cursor(
                    ChatCursor(
                        scope=req.scope,
                        conversation_id=req.conversation_id,
                        timestamp=first_item["_created_at"],
                        tie_breaker_id=first_item["_id"],
                        direction="backward",
                    )
                )
            prev_cursor = _encode_cursor(
                ChatCursor(
                    scope=req.scope,
                    conversation_id=req.conversation_id,
                    timestamp=last_item["_created_at"],
                    tie_breaker_id=last_item["_id"],
                    direction="forward",
                )
            )

        # 计算总条数（在限制范围内使用快速 count）
        total_count = conn.execute(
            f"SELECT COUNT(*) FROM history_records WHERE {' AND '.join(base_conditions)}",
            base_params,
        ).fetchone()[0]

        return ChatPageResponse(
            items=items,
            next_cursor=next_cursor,
            prev_cursor=prev_cursor,
            has_more=has_more,
            count=len(items),
            total_count=int(total_count),
            is_exact_count=True,
        )
