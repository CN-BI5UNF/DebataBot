"""D01：隔离合成库的稳定身份/序、import 基线、append 与 tombstone。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from core.d01 import (
    D01_SCHEMA_VERSION,
    D01Error,
    add_origin,
    append_record,
    ledger_status,
    migrate_database,
    tombstone_record,
)
from memory.karen_db import KarenDB


def _db(tmp_path):
    path = tmp_path / "karen.db"
    db = KarenDB(path)
    db.load()
    db.close()
    with sqlite3.connect(path) as conn:
        rows = [
            ("p1", 0, "user", "same-content"),
            ("p1", 1, "assistant", "same-content"),
            ("p2", 0, "user", "other"),
        ]
        for persona, index, role, content in rows:
            conn.execute(
                """
                INSERT INTO history_records(
                    persona_id, history_index, role, content_hash,
                    content_length, record_json, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    persona, index, role, "h-" + content, len(content),
                    json.dumps({"role": role, "content": content}),
                    "2026-10-24T00:00:00Z",
                ),
            )
        conn.execute(
            """
            INSERT INTO history_records(
                persona_id, history_index, role, content_hash,
                content_length, record_json, timestamp
            ) VALUES ('p1', 2, 'user', 'h-bad', 4, '{bad json', NULL)
            """
        )
        conn.commit()
    return path


def test_migration_assigns_stable_identity_and_preserves_raw(tmp_path):
    path = _db(tmp_path)
    with sqlite3.connect(path) as conn:
        before = {
            int(row[0]): row[1]
            for row in conn.execute("SELECT id, record_json FROM history_records")
        }
    report = migrate_database(path)
    assert report["ok"] is True
    assert report["schema_version"] == D01_SCHEMA_VERSION
    assert report["corrupt_rows"] == 1
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = list(conn.execute(
            """
            SELECT id, persona_id, record_json, record_uuid, record_seq,
                   life_epoch, visibility
            FROM history_records ORDER BY persona_id, history_index
            """
        ))
        assert all(row["record_uuid"] for row in rows)
        assert all(row["record_seq"] for row in rows)
        assert {int(row["id"]): row["record_json"] for row in rows} == before
        p1 = [row for row in rows if row["persona_id"] == "p1"]
        assert [row["record_seq"] for row in p1] == [1, 2, 3]
        assert len({row["record_uuid"] for row in rows}) == len(rows)
        # 同正文不合并：两行同样 content 仍有各自身份/seq。
        assert p1[0]["record_uuid"] != p1[1]["record_uuid"]
        state = conn.execute(
            "SELECT next_seq, append_revision FROM corpus_state WHERE persona_id='p1'"
        ).fetchone()
        assert state["next_seq"] == 4
        assert state["append_revision"] == 3
        migrated = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=2"
        ).fetchone()[0]
        assert migrated == 1


def test_migration_is_idempotent(tmp_path):
    path = _db(tmp_path)
    first = migrate_database(path)
    with sqlite3.connect(path) as conn:
        before = list(conn.execute(
            "SELECT id, record_uuid, record_seq FROM history_records ORDER BY id"
        ))
    second = migrate_database(path)
    with sqlite3.connect(path) as conn:
        after = list(conn.execute(
            "SELECT id, record_uuid, record_seq FROM history_records ORDER BY id"
        ))
    assert first["ok"] is True and second["ok"] is True
    assert before == after
    assert ledger_status(path)["corpus_state"] == 2


def test_append_uses_next_seq_and_origin_mapping(tmp_path):
    path = _db(tmp_path)
    migrate_database(path)
    add_origin(
        path, origin_id="o1", adapter="qq", account_id="bot",
        conversation_id="c1", external_id="m1",
    )
    result = append_record(
        path, "p1", {"role": "user", "content": "new"},
        origin_id="o1",
    )
    assert result["record_seq"] == 4
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            """
            SELECT record_uuid, record_seq, life_epoch, visibility
            FROM history_records WHERE persona_id='p1' AND record_seq=4
            """
        ).fetchone()
        assert row == (result["record_uuid"], 4, 0, "visible")
        origin = conn.execute(
            "SELECT origin_id FROM record_origins WHERE record_uuid=?",
            (result["record_uuid"],),
        ).fetchone()
        assert origin == ("o1",)
        outbox = conn.execute(
            "SELECT target, state FROM projection_outbox WHERE source_id=?",
            (result["record_uuid"],),
        ).fetchone()
        assert outbox == ("chat_display", "pending")
    finally:
        conn.close()


def test_tombstone_is_transactional_and_blocks_reuse(tmp_path):
    path = _db(tmp_path)
    migrate_database(path)
    with sqlite3.connect(path) as conn:
        uuid = conn.execute(
            "SELECT record_uuid FROM history_records WHERE persona_id='p1' ORDER BY record_seq LIMIT 1"
        ).fetchone()[0]
    result = tombstone_record(path, "p1", uuid, reason="test")
    assert result["mutation_gen"] == 1
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT visibility, deleted_at FROM history_records WHERE record_uuid=?",
            (uuid,),
        ).fetchone()
        assert row[0] == "deleted"
        assert row[1]
        assert conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM delete_ops").fetchone()[0] == 1
        state = conn.execute(
            "SELECT next_seq, mutation_gen FROM corpus_state WHERE persona_id='p1'"
        ).fetchone()
        assert state == (4, 1)
    # 删除不回收 seq；下一次 append 仍用 4。
    assert append_record(path, "p1", {"content": "after delete"})["record_seq"] == 4


def test_production_path_requires_explicit_authorization(tmp_path):
    prod = tmp_path / ".local" / "share" / "KarenBot" / "instances" / "default"
    prod.mkdir(parents=True)
    path = prod / "karen.db"
    db = KarenDB(path)
    db.load()
    db.close()
    with pytest.raises(D01Error) as exc:
        migrate_database(path)
    assert exc.value.code == "production_blocked"


def test_migration_rejects_future_schema(tmp_path):
    path = _db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version={D01_SCHEMA_VERSION + 1}")
        conn.commit()
    with pytest.raises(D01Error) as exc:
        migrate_database(path)
    assert exc.value.code == "schema_too_new"
