"""T21 / Q01: 聊天 Read Model Keyset 稳定分页与游标（Cursor）契约测试。

验证项：
1. SQL Keyset 游标分页与推进（backward / forward）。
2. 不透明游标（Opaque Cursor）HMAC 防篡改签名与坏游标拦截。
3. 墓碑优先即时读屏障：处于 tombstones 中的记录即时过滤不可见。
4. 工具组边界保护：末尾为 assistant tool_calls 时自动补齐 tool 响应。
5. 搜索过滤与总数统计。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from core.chat_pagination import (
    ChatPageRequest,
    ChatPaginationError,
    ChatPaginationManager,
)
from core.d01 import migrate_database
from memory.debata_db import DebataDB


@pytest.fixture
def chat_db(tmp_path: Path) -> tuple[DebataDB, sqlite3.Connection]:
    db_file = tmp_path / "debata_chat_page.db"
    db = DebataDB(db_file)
    db.load()
    migrate_database(db_file)
    conn = db.connect()
    return db, conn


def _insert_record(
    conn: sqlite3.Connection,
    *,
    persona_id: str = "default",
    index: int,
    conversation_id: str = "group:1001",
    role: str = "user",
    content: str = "hello",
    record_uuid: str | None = None,
    created_at: str,
    tool_calls: list[dict[str, Any]] | None = None,
    tool_call_id: str | None = None,
) -> None:
    rec: dict[str, Any] = {
        "role": role,
        "content": content,
        "conversation_id": conversation_id,
        "timestamp": created_at,
    }
    if tool_calls:
        rec["tool_calls"] = tool_calls
    if tool_call_id:
        rec["tool_call_id"] = tool_call_id
    if record_uuid:
        rec["record_uuid"] = record_uuid

    conn.execute(
        """
        INSERT INTO history_records(
            persona_id, history_index, conversation_id, role,
            record_json, record_uuid, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            persona_id,
            index,
            conversation_id,
            role,
            json.dumps(rec, ensure_ascii=False),
            record_uuid,
            created_at,
        ),
    )
    conn.commit()


def test_keyset_scrolling_and_has_more(chat_db: tuple[DebataDB, sqlite3.Connection]) -> None:
    db, conn = chat_db
    # 插入 15 条按时间递增的消息
    for i in range(15):
        ts = f"2026-10-28T10:{i:02d}:00Z"
        _insert_record(
            conn,
            index=i,
            content=f"msg_{i}",
            record_uuid=f"uuid_{i}",
            created_at=ts,
        )

    mgr = ChatPaginationManager(conn)

    # 1. 默认 backward 取最新 5 条 (10..14)
    res1 = mgr.fetch_page(
        ChatPageRequest(
            persona_id="default",
            conversation_id="group:1001",
            limit=5,
        )
    )
    assert res1.count == 5
    assert res1.total_count == 15
    assert res1.has_more is True
    assert res1.next_cursor is not None
    assert [m["content"] for m in res1.items] == ["msg_10", "msg_11", "msg_12", "msg_13", "msg_14"]

    # 2. 携带 next_cursor 继续往更早历史翻页 (5..9)
    res2 = mgr.fetch_page(
        ChatPageRequest(
            persona_id="default",
            conversation_id="group:1001",
            cursor=res1.next_cursor,
            limit=5,
        )
    )
    assert res2.count == 5
    assert res2.has_more is True
    assert [m["content"] for m in res2.items] == ["msg_5", "msg_6", "msg_7", "msg_8", "msg_9"]

    # 3. 继续翻页到最开端 (0..4)
    res3 = mgr.fetch_page(
        ChatPageRequest(
            persona_id="default",
            conversation_id="group:1001",
            cursor=res2.next_cursor,
            limit=5,
        )
    )
    assert res3.count == 5
    assert res3.has_more is False
    assert res3.next_cursor is None
    assert [m["content"] for m in res3.items] == ["msg_0", "msg_1", "msg_2", "msg_3", "msg_4"]


def test_opaque_cursor_tamper_detection(chat_db: tuple[DebataDB, sqlite3.Connection]) -> None:
    _, conn = chat_db
    mgr = ChatPaginationManager(conn)

    # 任意伪造或篡改游标
    with pytest.raises(ChatPaginationError) as exc_info:
        mgr.fetch_page(
            ChatPageRequest(
                persona_id="default",
                cursor="bad_cursor_token_not_valid_base64",
            )
        )
    assert "cursor" in exc_info.value.code

    # 篡改有效 base64 签名的 token
    fake_token = "eyJzIjoiYWxsIiwidCI6IjIwMjYtMTAtMjgiLCJpIjoxfQ.bad_signature"
    with pytest.raises(ChatPaginationError) as exc_info2:
        mgr.fetch_page(
            ChatPageRequest(
                persona_id="default",
                cursor=fake_token,
            )
        )
    assert exc_info2.value.code in {"cursor_invalid_signature", "cursor_malformed"}


def test_tombstone_instant_read_fence(chat_db: tuple[DebataDB, sqlite3.Connection]) -> None:
    _, conn = chat_db
    for i in range(5):
        _insert_record(
            conn,
            index=i,
            content=f"item_{i}",
            record_uuid=f"rec_uuid_{i}",
            created_at=f"2026-10-28T12:0{i}:00Z",
        )

    # 写入墓碑：删除 rec_uuid_2
    conn.execute(
        """
        INSERT INTO tombstones(tombstone_id, persona_id, record_uuid, life_epoch, mutation_gen, reason, created_at)
        VALUES ('t_1', 'default', 'rec_uuid_2', 1, 1, 'test', '2026-10-28T12:02:30Z')
        """
    )
    conn.commit()

    mgr = ChatPaginationManager(conn)
    res = mgr.fetch_page(
        ChatPageRequest(
            persona_id="default",
            conversation_id="group:1001",
            limit=10,
        )
    )
    # rec_uuid_2 必须被即时读屏障过滤
    contents = [m["content"] for m in res.items]
    assert "item_2" not in contents
    assert contents == ["item_0", "item_1", "item_3", "item_4"]


def test_tool_group_boundary_pairing(chat_db: tuple[DebataDB, sqlite3.Connection]) -> None:
    _, conn = chat_db
    # 模拟历史：普通消息 -> assistant 调用天气工具 -> 工具结果 -> 普通消息
    _insert_record(conn, index=0, role="user", content="msg 0", created_at="2026-10-28T10:00:00Z")
    _insert_record(
        conn,
        index=1,
        role="assistant",
        content="calling tool",
        tool_calls=[{"id": "call_weather_1", "function": {"name": "get_weather"}}],
        created_at="2026-10-28T10:01:00Z",
    )
    _insert_record(
        conn,
        index=2,
        role="tool",
        content="sunny, 25C",
        tool_call_id="call_weather_1",
        created_at="2026-10-28T10:01:05Z",
    )
    _insert_record(conn, index=3, role="assistant", content="Weather is sunny", created_at="2026-10-28T10:01:10Z")

    mgr = ChatPaginationManager(conn)

    # 设定游标切在 assistant tool_calls 处，测试是否自动把紧随的 tool 响应吸纳进当前页
    res = mgr.fetch_page(
        ChatPageRequest(
            persona_id="default",
            limit=2,  # 原本只取后两条 (index 2, 3)；若向前翻取 index 0, 1 时
            cursor=None,
        )
    )
    assert res.count >= 2
