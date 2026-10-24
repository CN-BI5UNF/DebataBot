"""T14：D01 语义账本证据、精确恢复与墓碑重放。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from core.d01 import (
    D01Error,
    aggregate_ledger_evidence,
    append_record,
    apply_tombstone_replay,
    ledger_evidence,
    migrate_database,
    plan_restore_replay,
    tombstone_record,
)
from memory.debata_db import DebataDB


def _db(tmp_path):
    path = tmp_path / "debata.db"
    db = DebataDB(path)
    db.load()
    db.close()
    with sqlite3.connect(path) as conn:
        rows = [
            ("p1", 0, "user", "body-one"),
            ("p1", 1, "assistant", "body-two"),
            ("p2", 0, "user", "body-three"),
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
                    persona,
                    index,
                    role,
                    "h-" + content,
                    len(content),
                    json.dumps({"role": role, "content": content}),
                    "2026-10-24T00:00:00Z",
                ),
            )
        conn.commit()
    return path


def _snapshot(source, destination):
    """SQLite backup API：包含 WAL 中已提交的内容，避免只拷贝主文件。"""
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)


def _first_uuid(path, persona="p1"):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT record_uuid FROM history_records "
            "WHERE persona_id = ? ORDER BY record_seq LIMIT 1",
            (persona,),
        ).fetchone()[0]


def test_ledger_evidence_is_semantic_and_contains_no_content(tmp_path):
    path = _db(tmp_path)
    migrate_database(path)
    evidence = ledger_evidence(path, label="data/memory/demo/chat.db")
    assert evidence["aware"] is True
    assert evidence["complete"] is True
    assert evidence["schema_version"] == 2
    assert "M001" in evidence["migration_ids"]
    assert evidence["watermark"]["history_records"] == 3
    assert evidence["watermark"]["persona_count"] == 2
    assert evidence["personas"]["p1"]["next_seq"] == 3
    assert evidence["personas"]["p1"]["tombstones"] == 0
    # 证据只含计数/水位/指纹，不能出现聊天正文。
    encoded = json.dumps(evidence, ensure_ascii=False)
    for body in ("body-one", "body-two", "body-three"):
        assert body not in encoded

    aggregate = aggregate_ledger_evidence([evidence])
    assert aggregate["aware"] is True
    assert aggregate["complete"] is True
    assert aggregate["watermark"]["history_records"] == 3
    assert aggregate["databases"][0]["digest"] == evidence["digest"]


def test_ledger_evidence_fails_closed_for_v1_and_missing(tmp_path):
    path = _db(tmp_path)
    evidence = ledger_evidence(path, label="v1")
    assert evidence["aware"] is False
    assert evidence["complete"] is False
    assert any("schema_version_below_d01" in item for item in evidence["issues"])

    missing = ledger_evidence(tmp_path / "missing.db", label="missing")
    assert missing["aware"] is False
    assert "source_missing" in missing["issues"]
    aggregate = aggregate_ledger_evidence([])
    assert aggregate["aware"] is False
    assert aggregate["complete"] is False


def test_plan_exact_then_replay_then_new_record_block(tmp_path):
    path = _db(tmp_path)
    migrate_database(path)
    snapshot = tmp_path / "snapshot.db"
    _snapshot(path, snapshot)
    with sqlite3.connect(path) as conn:
        before_raw = {
            int(row[0]): row[1]
            for row in conn.execute(
                "SELECT id, record_json FROM history_records"
            )
        }

    assert plan_restore_replay(snapshot, path)["decision"] == "exact"
    noop = apply_tombstone_replay(snapshot, path)
    assert noop["decision"] == "exact"
    assert noop["replayed_tombstones"] == 0

    uuid = _first_uuid(path)
    tombstone_record(path, "p1", uuid, reason="test-delete")
    plan = plan_restore_replay(snapshot, path)
    assert plan["decision"] == "replay_tombstones"
    assert plan["personas"]["p1"]["added_tombstones"] == 1
    assert plan["summary_rebuild_required"] is True

    report = apply_tombstone_replay(snapshot, path)
    assert report["ok"] is True
    assert report["replayed_tombstones"] == 1
    assert report["verified"] is True
    assert report["projection_ops"] == 1

    snapshot_evidence = ledger_evidence(snapshot)
    source_evidence = ledger_evidence(path)
    assert (
        snapshot_evidence["personas"]["p1"]["tombstone_fingerprint"]
        == source_evidence["personas"]["p1"]["tombstone_fingerprint"]
    )
    assert snapshot_evidence["personas"]["p1"]["tombstones"] == 1
    with sqlite3.connect(snapshot) as conn:
        after_raw = {
            int(row[0]): row[1]
            for row in conn.execute(
                "SELECT id, record_json FROM history_records"
            )
        }
        visibility = conn.execute(
            "SELECT visibility FROM history_records "
            "WHERE persona_id='p1' AND record_uuid=?",
            (uuid,),
        ).fetchone()[0]
        current_summary_gen = conn.execute(
            "SELECT current_summary_gen FROM corpus_state "
            "WHERE persona_id='p1'"
        ).fetchone()[0]
    assert after_raw == before_raw
    assert visibility == "deleted"
    # 重放后派生摘要没有可指向的目标代次，必须要求重建。
    assert current_summary_gen == 0
    assert apply_tombstone_replay(snapshot, path)["decision"] == "exact"

    append_record(path, "p1", {"role": "user", "content": "new-after-snapshot"})
    blocked = plan_restore_replay(snapshot, path)
    assert blocked["decision"] == "blocked"
    assert any("new_records_after_snapshot" in item for item in blocked["reasons"])
    with pytest.raises(D01Error) as exc:
        apply_tombstone_replay(snapshot, path)
    assert exc.value.code == "replay_blocked"


def test_replay_refuses_tombstone_regression(tmp_path):
    path = _db(tmp_path)
    migrate_database(path)
    older = tmp_path / "older.db"
    _snapshot(path, older)
    uuid = _first_uuid(path)
    tombstone_record(path, "p1", uuid)
    snapshot = tmp_path / "snapshot.db"
    _snapshot(path, snapshot)

    plan = plan_restore_replay(snapshot, older)
    assert plan["decision"] == "blocked"
    assert any(
        "source_missing_tombstones" in item for item in plan["reasons"]
    )
    with pytest.raises(D01Error) as exc:
        apply_tombstone_replay(snapshot, older)
    assert exc.value.code == "replay_blocked"
