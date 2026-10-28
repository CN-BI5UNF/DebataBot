"""T17 / S02 / C04 专项测试：摘要 / archive / cursor 原子 CAS 事务与冲突回滚。"""

import pytest

from core.d01 import _DDL
from core.summary_cas import (
    SummaryCASConflictError,
    SummaryCASPlan,
    SummaryCASResult,
    commit_summary_cas,
)
from memory.debata_db import DebataDB


@pytest.fixture
def memory_db(tmp_path):
    """初始化包含 D01 完整契约表结构的测试数据库。"""
    db_file = tmp_path / "debata_test.db"
    db = DebataDB(db_file)
    db.load()
    with db.connect() as conn:
        for stmt in _DDL:
            conn.execute(stmt)
        # 初始化基础 persona 数据
        conn.execute(
            """
            INSERT INTO corpus_state (
                persona_id, corpus_epoch, next_seq, append_revision,
                mutation_gen, current_summary_gen, lifecycle_epoch, updated_at
            ) VALUES ('test_p', 1, 10, 1, 1, 0, 0, '2026-10-28T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO rolling_summary (
                persona_id, summary_text, archived_until_json,
                active_start_index, summary_json, updated_at
            ) VALUES ('test_p', '初始摘要', '{}', 0, '{}', '2026-10-28T00:00:00Z')
            """
        )
        conn.commit()
    return db


def test_commit_summary_cas_happy_path(memory_db):
    """测试常规原子 CAS 提交：多表一致性原子写入。"""
    plan = SummaryCASPlan(
        persona_id="test_p",
        expected_base_summary_gen=0,
        expected_mutation_gen=1,
        expected_lifecycle_epoch=0,
        expected_active_start=0,
        new_active_start=5,
        summary_text="这是合并后的第一版测试摘要",
        structured_data={"facts": ["事实1"]},
        slice_records=[
            {
                "role": "user",
                "content": "消息1",
                "record_uuid": "uuid_1",
                "timestamp": "2026-10-28T00:01:00Z",
            },
            {
                "role": "user",
                "content": "消息2",
                "record_uuid": "uuid_2",
                "timestamp": "2026-10-28T00:02:00Z",
            },
        ],
        coverage_start_seq=1,
        coverage_end_seq=5,
    )

    res = commit_summary_cas(memory_db, plan)
    assert isinstance(res, SummaryCASResult)
    assert res.committed is True
    assert res.summary_gen == 1
    assert res.active_start_index == 5
    assert res.sources_count == 2
    assert res.archived_count == 2

    # 验证数据库真实落盘
    with memory_db.connect() as conn:
        # summary_generations
        gen_row = conn.execute(
            "SELECT summary_gen, state, coverage_start_seq, coverage_end_seq FROM summary_generations WHERE persona_id = 'test_p'"
        ).fetchone()
        assert gen_row["summary_gen"] == 1
        assert gen_row["state"] == "committed"
        assert gen_row["coverage_start_seq"] == 1
        assert gen_row["coverage_end_seq"] == 5

        # summary_sources
        sources = conn.execute(
            "SELECT record_uuid FROM summary_sources WHERE persona_id = 'test_p' ORDER BY record_uuid"
        ).fetchall()
        assert [s[0] for s in sources] == ["uuid_1", "uuid_2"]

        # rolling_summary
        summary_row = conn.execute(
            "SELECT summary_text, active_start_index FROM rolling_summary WHERE persona_id = 'test_p'"
        ).fetchone()
        assert summary_row["summary_text"] == "这是合并后的第一版测试摘要"
        assert summary_row["active_start_index"] == 5

        # corpus_state
        state_row = conn.execute(
            "SELECT current_summary_gen, append_revision FROM corpus_state WHERE persona_id = 'test_p'"
        ).fetchone()
        assert state_row["current_summary_gen"] == 1
        assert state_row["append_revision"] == 2


def test_commit_summary_cas_mutation_gen_conflict(memory_db):
    """测试 CAS 冲突：并发删除导致 mutation_gen 递增，拒绝并全量回滚。"""
    # 模拟并发删除将 mutation_gen 改为 2
    with memory_db.connect() as conn:
        conn.execute(
            "UPDATE corpus_state SET mutation_gen = 2 WHERE persona_id = 'test_p'"
        )
        conn.commit()

    plan = SummaryCASPlan(
        persona_id="test_p",
        expected_base_summary_gen=0,
        expected_mutation_gen=1,  # 预期仍为 1，冲突
        expected_lifecycle_epoch=0,
        expected_active_start=0,
        new_active_start=5,
        summary_text="冲突摘要",
        structured_data={},
        slice_records=[{"role": "user", "content": "1", "record_uuid": "u1"}],
    )

    with pytest.raises(SummaryCASConflictError) as exc_info:
        commit_summary_cas(memory_db, plan)
    assert exc_info.value.code == "mutation_gen_conflict"

    # 验证无任何脏数据写入
    with memory_db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM summary_generations WHERE persona_id = 'test_p'"
            ).fetchone()[0]
            == 0
        )
        cur_row = conn.execute(
            "SELECT active_start_index FROM rolling_summary WHERE persona_id = 'test_p'"
        ).fetchone()
        assert cur_row[0] == 0


def test_commit_summary_cas_active_start_desync(memory_db):
    """测试游标脱节：rolling_summary 已被并发推进，拒绝旧切片。"""
    # 模拟并发将活跃游标推进至 10
    with memory_db.connect() as conn:
        conn.execute(
            "UPDATE rolling_summary SET active_start_index = 10 WHERE persona_id = 'test_p'"
        )
        conn.commit()

    plan = SummaryCASPlan(
        persona_id="test_p",
        expected_base_summary_gen=0,
        expected_mutation_gen=1,
        expected_lifecycle_epoch=0,
        expected_active_start=0,  # 预期还是 0，但实际已是 10
        new_active_start=5,
        summary_text="脱节摘要",
        structured_data={},
        slice_records=[],
    )

    with pytest.raises(SummaryCASConflictError) as exc_info:
        commit_summary_cas(memory_db, plan)
    assert exc_info.value.code == "active_start_desync"


def test_commit_summary_cas_tombstone_rejection(memory_db):
    """测试包含墓碑记录时拒绝提交，防范已删内容复活。"""
    # 在 tombstones 表中插入已被删除的记录 uuid_deleted
    with memory_db.connect() as conn:
        conn.execute(
            """
            INSERT INTO tombstones (tombstone_id, persona_id, record_uuid, life_epoch, mutation_gen, reason, created_at)
            VALUES ('tomb_1', 'test_p', 'uuid_deleted', 0, 1, 'user deleted', '2026-10-28T00:00:00Z')
            """
        )
        conn.commit()

    plan = SummaryCASPlan(
        persona_id="test_p",
        expected_base_summary_gen=0,
        expected_mutation_gen=1,
        expected_lifecycle_epoch=0,
        expected_active_start=0,
        new_active_start=5,
        summary_text="包含已删记录的摘要",
        structured_data={},
        slice_records=[
            {"role": "user", "content": "已删消息", "record_uuid": "uuid_deleted"}
        ],
    )

    with pytest.raises(SummaryCASConflictError) as exc_info:
        commit_summary_cas(memory_db, plan)
    assert exc_info.value.code == "contains_tombstoned_records"
