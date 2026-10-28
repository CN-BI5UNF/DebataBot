"""T20: 范围删除预览、即时读屏障、受控隔离恢复（新 life_epoch）、Purge 与人格上下文重置测试。"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from core.d01 import migrate_database
from core.scoped_deletion import (
    DeletionScope,
    ScopedDeletionError,
    ScopedDeletionManager,
)
from memory.debata_db import DebataDB


@pytest.fixture
def test_env(tmp_path: Path) -> tuple[DebataDB, Path, ScopedDeletionManager]:
    db_file = tmp_path / "debata_del_test.db"
    quarantine_dir = tmp_path / "quarantine"
    db = DebataDB(db_file)
    db.load()

    # 执行 D01 迁移建立基础表
    migrate_database(db_file)

    mgr = ScopedDeletionManager(db, quarantine_dir=quarantine_dir)
    return db, quarantine_dir, mgr


def _insert_history(
    conn: sqlite3.Connection,
    *,
    persona_id: str,
    index: int,
    record: dict[str, Any],
    created_at: str,
) -> None:
    rec_json = json.dumps(record, ensure_ascii=False)
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
            record.get("conversation_id"),
            record.get("role", "user"),
            rec_json,
            record.get("record_uuid"),
            created_at,
        ),
    )


def test_preview_sanitization_and_empty_conversation_guard(test_env: tuple[DebataDB, Path, ScopedDeletionManager]) -> None:
    """测试范围删除预览脱敏输出，以及空 conversation_id 拒绝扩大删除（第 19 节）。"""
    db, _, mgr = test_env

    # 1. 尝试使用空会话 ID 执行会话级删除，必须被拦截
    with pytest.raises(ScopedDeletionError) as exc_info:
        mgr.preview_scope_deletion(
            DeletionScope(persona_id="p1", scope_type="session", conversation_id="")
        )
    assert exc_info.value.code == "invalid_scope"

    with pytest.raises(ScopedDeletionError) as exc_info2:
        mgr.preview_scope_deletion(
            DeletionScope(persona_id="p1", scope_type="session", conversation_id="   ")
        )
    assert exc_info2.value.code == "invalid_scope"

    # 2. 写入几条测试历史记录
    conn = db.connect()
    with conn:
        _insert_history(
            conn,
            persona_id="p1",
            index=0,
            record={
                "role": "user",
                "content": "机密敏感用户输入1",
                "record_uuid": "rec_uuid_001",
                "conversation_id": "conv_123",
            },
            created_at="2026-10-28T10:00:00Z",
        )
        _insert_history(
            conn,
            persona_id="p1",
            index=1,
            record={
                "role": "assistant",
                "content": "模型回答内容2",
                "record_uuid": "rec_uuid_002",
                "conversation_id": "conv_123",
            },
            created_at="2026-10-28T10:01:00Z",
        )

    # 3. 正常生成预览
    preview = mgr.preview_scope_deletion(
        DeletionScope(
            persona_id="p1",
            scope_type="session",
            conversation_id="conv_123",
        )
    )
    assert preview.history_count == 2
    assert preview.preview_token

    # 4. 严格断言脱敏字典：绝不能暴露任何正文文本或内部路径
    sanitized = preview.as_sanitized_dict()
    sanitized_str = json.dumps(sanitized, ensure_ascii=False)
    assert "机密敏感用户输入1" not in sanitized_str
    assert "模型回答内容2" not in sanitized_str
    assert "rec_uuid_001" not in sanitized_str
    assert "/home/" not in sanitized_str


def test_atomic_tombstone_and_instant_read_fence(test_env: tuple[DebataDB, Path, ScopedDeletionManager]) -> None:
    """测试原子墓碑提交、代次增加与即时读屏障即刻生效（E36）。"""
    db, _, mgr = test_env
    conn = db.connect()

    # 写入测试记录和摘要记录
    with conn:
        _insert_history(
            conn,
            persona_id="p1",
            index=0,
            record={
                "role": "user",
                "content": "用户问题A",
                "record_uuid": "uuid_a",
                "conversation_id": "conv_a",
            },
            created_at="2026-10-28T11:00:00Z",
        )
        _insert_history(
            conn,
            persona_id="p1",
            index=1,
            record={
                "role": "user",
                "content": "用户问题B",
                "record_uuid": "uuid_b",
                "conversation_id": "conv_b",
            },
            created_at="2026-10-28T11:05:00Z",
        )
        # 关联 summary_sources
        conn.execute(
            "INSERT INTO summary_generations(persona_id, summary_gen, state, created_at) VALUES (?, ?, ?, ?)",
            ("p1", 1, "committed", "2026-10-28T11:10:00Z"),
        )
        conn.execute(
            "INSERT INTO summary_sources(persona_id, summary_gen, record_uuid) VALUES (?, ?, ?)",
            ("p1", 1, "uuid_a"),
        )

    # 预览并执行删除 uuid_a
    preview = mgr.preview_scope_deletion(
        DeletionScope(persona_id="p1", scope_type="session", conversation_id="conv_a")
    )
    result = mgr.execute_scope_deletion(preview.preview_token, reason="用户撤回")

    assert result.tombstoned_count == 1
    assert result.invalidated_summaries_count == 1
    assert result.mutation_gen >= 1
    assert Path(result.quarantine_manifest_path).exists()

    # 验证即时读屏障：uuid_a 立即对所有 reader 隐形
    assert mgr.is_tombstoned("p1", "uuid_a")
    assert not mgr.is_tombstoned("p1", "uuid_b")

    mock_records = [
        {"record_uuid": "uuid_a", "content": "问题A"},
        {"record_uuid": "uuid_b", "content": "问题B"},
    ]
    visible = mgr.filter_visible_records("p1", mock_records)
    assert len(visible) == 1
    assert visible[0]["record_uuid"] == "uuid_b"

    # 验证 summary_generations 已标记为 invalidated，防止旧摘要死灰复燃
    s_row = conn.execute(
        "SELECT state FROM summary_generations WHERE persona_id = 'p1' AND summary_gen = 1"
    ).fetchone()
    assert s_row[0] == "invalidated"


def test_controlled_quarantine_restore_new_epoch(test_env: tuple[DebataDB, Path, ScopedDeletionManager]) -> None:
    """测试受控隔离期内恢复分配全新 lifecycle_epoch 与 mutation_gen（第 19 节，E36）。"""
    db, _, mgr = test_env
    conn = db.connect()

    with conn:
        _insert_history(
            conn,
            persona_id="p1",
            index=0,
            record={
                "role": "user",
                "content": "可恢复的问题",
                "record_uuid": "uuid_restore_test",
                "conversation_id": "conv_r",
            },
            created_at="2026-10-28T11:20:00Z",
        )

    preview = mgr.preview_scope_deletion(
        DeletionScope(persona_id="p1", scope_type="session", conversation_id="conv_r")
    )
    del_res = mgr.execute_scope_deletion(preview.preview_token)
    assert mgr.is_tombstoned("p1", "uuid_restore_test")

    # 执行隔离恢复
    restore_res = mgr.restore_scope_deletion(del_res.op_id)
    assert restore_res.status == "restored"
    assert restore_res.restored_count == 1
    # 必须分配全新的生命周期代次
    assert restore_res.new_life_epoch > del_res.lifecycle_epoch
    assert restore_res.new_mutation_gen > del_res.mutation_gen

    # 墓碑已清除，读屏障恢复可见
    assert not mgr.is_tombstoned("p1", "uuid_restore_test")
    visible = mgr.filter_visible_records(
        "p1", [{"record_uuid": "uuid_restore_test", "content": "可恢复的问题"}]
    )
    assert len(visible) == 1


def test_quarantine_expiration_rejects_restore(test_env: tuple[DebataDB, Path, ScopedDeletionManager]) -> None:
    """测试超过受控隔离期后拒绝常规恢复。"""
    _, quarantine_dir, mgr = test_env

    # 构造一个已超期的隔离清单
    op_id = "op_expired_test"
    manifest = {
        "op_id": op_id,
        "persona_id": "p1",
        "scope_type": "session",
        "record_uuids": ["uuid_expired"],
        "mutation_gen": 1,
        "lifecycle_epoch": 0,
        "quarantine_until": time.time() - 100.0,  # 已过期
        "state": "quarantined",
        "created_at": "2026-10-27T00:00:00Z",
    }
    (quarantine_dir / f"{op_id}.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ScopedDeletionError) as exc_info:
        mgr.restore_scope_deletion(op_id)
    assert exc_info.value.code == "quarantine_expired"


def test_purge_permanently_removes_records(test_env: tuple[DebataDB, Path, ScopedDeletionManager]) -> None:
    """测试 Purge 彻底物理清除历史与隔离清单。"""
    db, quarantine_dir, mgr = test_env
    conn = db.connect()

    with conn:
        _insert_history(
            conn,
            persona_id="p1",
            index=0,
            record={
                "role": "user",
                "content": "将要被物理彻底销毁的内容",
                "record_uuid": "uuid_to_purge",
                "conversation_id": "conv_purge",
            },
            created_at="2026-10-28T11:30:00Z",
        )

    preview = mgr.preview_scope_deletion(
        DeletionScope(persona_id="p1", scope_type="session", conversation_id="conv_purge")
    )
    del_res = mgr.execute_scope_deletion(preview.preview_token)

    # 物理销毁
    purge_res = mgr.purge_scope_deletion(del_res.op_id)
    assert purge_res.status == "purged"
    assert purge_res.purged_records_count == 1
    assert purge_res.freed_bytes_estimate > 0

    # 底层记录已被 DELETE
    row = conn.execute(
        "SELECT COUNT(*) FROM history_records WHERE record_uuid = 'uuid_to_purge' OR record_json LIKE '%uuid_to_purge%'"
    ).fetchone()
    assert int(row[0]) == 0

    # 隔离清单文件已被移除
    assert not (quarantine_dir / f"{del_res.op_id}.manifest.json").exists()


def test_persona_context_reset_preserves_records_advances_epoch(test_env: tuple[DebataDB, Path, ScopedDeletionManager]) -> None:
    """测试人格上下文重置（Context Reset）：推进代次与游标，绝不物理删除历史底册。"""
    db, _, mgr = test_env
    conn = db.connect()

    with conn:
        _insert_history(
            conn,
            persona_id="p1",
            index=0,
            record={
                "role": "user",
                "content": "人格历史问答1",
                "record_uuid": "uuid_hist_1",
            },
            created_at="2026-10-28T11:40:00Z",
        )
        _insert_history(
            conn,
            persona_id="p1",
            index=1,
            record={
                "role": "assistant",
                "content": "人格历史回答2",
                "record_uuid": "uuid_hist_2",
            },
            created_at="2026-10-28T11:41:00Z",
        )
        conn.execute(
            "INSERT INTO rolling_summary(persona_id, summary_text, active_start_index, updated_at) VALUES (?, ?, ?, ?)",
            ("p1", "旧的滚动摘要内容", 0, "2026-10-28T11:42:00Z"),
        )

    # 执行人格上下文重置
    reset_res = mgr.reset_persona_context("p1")
    assert reset_res.persona_id == "p1"
    assert reset_res.new_lifecycle_epoch > reset_res.previous_lifecycle_epoch
    assert reset_res.advanced_active_start == 2
    assert reset_res.cleared_summary

    # 验证历史记录完好无损（未物理删除）
    total_records = int(conn.execute("SELECT COUNT(*) FROM history_records").fetchone()[0])
    assert total_records == 2

    # 验证 rolling_summary 的活跃游标推进至末端，且摘要被清空
    sum_row = conn.execute(
        "SELECT summary_text, active_start_index FROM rolling_summary WHERE persona_id = 'p1'"
    ).fetchone()
    assert sum_row["summary_text"] == ""
    assert int(sum_row["active_start_index"]) == 2
