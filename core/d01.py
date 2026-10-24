"""D01 最小稳定账本：身份/seq/epoch、import 基线、outbox/tombstone。

本模块只对**显式传入的数据库文件**执行迁移，默认拒绝看起来像生产实例的
路径；测试使用 tmp AppPaths。它不启动 Runtime、不删除/重写旧 raw JSON、
不重放工具或外发消息。

迁移版本：debata.db v1 -> v2（migration id ``M001``）。旧 raw 字节保留，
只补身份列和账本表；重复执行幂等。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

D01_SCHEMA_VERSION = 2
D01_MIGRATION_ID = "M001"
D01_SCHEMA_NAME = "d01_stable_ledger"
D01_CONTRACT_EPOCH = 1
LEDGER_EVIDENCE_FORMAT = 1
DEFAULT_BUSY_TIMEOUT_MS = 30_000
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_EVIDENCE_ISSUES = 32
_REQUIRED_LEDGER_TABLES = (
    "corpus_state",
    "origin_messages",
    "record_origins",
    "record_copies",
    "summary_generations",
    "summary_sources",
    "projection_outbox",
    "legacy_import_ledger",
    "delete_ops",
    "tombstones",
    "turn_checkpoints",
    "action_ledger",
)
_REQUIRED_HISTORY_COLUMNS = {
    "record_uuid",
    "record_seq",
    "life_epoch",
    "turn_id",
    "raw_seq",
    "visibility",
    "deleted_at",
}

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS corpus_state (
        persona_id TEXT PRIMARY KEY,
        instance_id TEXT NOT NULL DEFAULT 'default',
        corpus_epoch INTEGER NOT NULL DEFAULT 0,
        next_seq INTEGER NOT NULL DEFAULT 1,
        append_revision INTEGER NOT NULL DEFAULT 0,
        mutation_gen INTEGER NOT NULL DEFAULT 0,
        current_summary_gen INTEGER NOT NULL DEFAULT 0,
        lifecycle_epoch INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS origin_messages (
        origin_id TEXT PRIMARY KEY,
        adapter TEXT NOT NULL,
        account_id TEXT,
        conversation_id TEXT,
        external_id TEXT,
        fingerprint TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(adapter, account_id, conversation_id, external_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS record_origins (
        persona_id TEXT NOT NULL,
        record_uuid TEXT NOT NULL,
        origin_id TEXT NOT NULL,
        mapping_version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, record_uuid, origin_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS record_copies (
        domain TEXT NOT NULL,
        copy_key TEXT NOT NULL,
        subindex INTEGER NOT NULL DEFAULT 0,
        persona_id TEXT NOT NULL,
        record_uuid TEXT NOT NULL,
        mapping_version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        PRIMARY KEY(domain, copy_key, subindex)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS summary_generations (
        persona_id TEXT NOT NULL,
        summary_gen INTEGER NOT NULL,
        corpus_epoch INTEGER NOT NULL DEFAULT 0,
        base_summary_gen INTEGER NOT NULL DEFAULT 0,
        coverage_start_seq INTEGER,
        coverage_end_seq INTEGER,
        digest TEXT,
        state TEXT NOT NULL DEFAULT 'staged',
        binding_version INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, summary_gen)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS summary_sources (
        persona_id TEXT NOT NULL,
        summary_gen INTEGER NOT NULL,
        record_uuid TEXT NOT NULL,
        PRIMARY KEY(persona_id, summary_gen, record_uuid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projection_outbox (
        op_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        persona_id TEXT,
        life_epoch INTEGER NOT NULL DEFAULT 0,
        target TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending',
        attempt INTEGER NOT NULL DEFAULT 0,
        retry_at REAL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS legacy_import_ledger (
        source TEXT NOT NULL,
        domain TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        status TEXT NOT NULL,
        detail_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        PRIMARY KEY(source, domain, fingerprint)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS delete_ops (
        op_id TEXT PRIMARY KEY,
        persona_id TEXT NOT NULL,
        corpus_epoch INTEGER NOT NULL DEFAULT 0,
        mutation_gen INTEGER NOT NULL DEFAULT 0,
        scope TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'tombstoned',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tombstones (
        tombstone_id TEXT PRIMARY KEY,
        persona_id TEXT NOT NULL,
        record_uuid TEXT NOT NULL,
        life_epoch INTEGER NOT NULL DEFAULT 0,
        mutation_gen INTEGER NOT NULL DEFAULT 0,
        reason TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(persona_id, record_uuid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS turn_checkpoints (
        persona_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        watermark_seq INTEGER NOT NULL DEFAULT 0,
        raw_seq INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'open',
        updated_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, turn_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS action_ledger (
        persona_id TEXT NOT NULL,
        action_id TEXT NOT NULL,
        turn_id TEXT,
        state TEXT NOT NULL DEFAULT 'prepared',
        receipt_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, action_id)
    )
    """,
)

_HISTORY_COLUMNS = {
    "record_uuid": "TEXT",
    "record_seq": "INTEGER",
    "life_epoch": "INTEGER NOT NULL DEFAULT 0",
    "turn_id": "TEXT",
    "raw_seq": "INTEGER",
    "visibility": "TEXT NOT NULL DEFAULT 'visible'",
    "deleted_at": "TEXT",
}
_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_history_persona_uuid "
    "ON history_records(persona_id, record_uuid) WHERE record_uuid IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_history_persona_seq "
    "ON history_records(persona_id, record_seq) WHERE record_seq IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_outbox_state ON projection_outbox(state, retry_at)",
    "CREATE INDEX IF NOT EXISTS idx_tombstones_persona ON tombstones(persona_id, record_uuid)",
)


class D01Error(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _looks_like_production(path: Path) -> bool:
    """拒绝默认生产数据根和项目内 data/，测试必须使用显式临时路径。"""
    resolved = path.resolve()
    text = resolved.as_posix()
    # 不绑定具体用户名：任何 .../.local/share/Debata_Agent 都视为生产数据根。
    if "/.local/share/Debata_Agent/" in text or text.endswith(
        "/.local/share/Debata_Agent"
    ):
        return True
    project_data = Path(__file__).resolve().parents[1] / "data"
    try:
        project_data = project_data.resolve()
    except OSError:
        return False
    return resolved == project_data or project_data in resolved.parents


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    else:
        conn = sqlite3.connect(path, timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_schema(conn: sqlite3.Connection) -> None:
    tables = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "history_records" not in tables:
        raise D01Error("history_missing", "源库缺少 history_records")
    for statement in _DDL:
        conn.execute(statement)
    existing = _columns(conn, "history_records")
    for name, ddl in _HISTORY_COLUMNS.items():
        if name not in existing:
            if "DEFAULT" in ddl or "NOT NULL" in ddl:
                conn.execute(
                    f"ALTER TABLE history_records ADD COLUMN {name} {ddl}"
                )
            else:
                conn.execute(
                    f"ALTER TABLE history_records ADD COLUMN {name} {ddl}"
                )
    for statement in _INDEXES:
        conn.execute(statement)


def _row_identity_payload(row: sqlite3.Row) -> str:
    return json.dumps(
        {
            "id": int(row["id"]),
            "history_index": int(row["history_index"]),
            "content_hash": row["content_hash"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _backfill_history(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = list(
        conn.execute(
            """
            SELECT id, persona_id, history_index, content_hash, record_json,
                   record_uuid, record_seq
            FROM history_records
            ORDER BY persona_id, history_index, id
            """
        )
    )
    per_persona: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        per_persona.setdefault(str(row["persona_id"]), []).append(row)
    report: dict[str, Any] = {"personas": {}, "corrupt_rows": 0}
    for persona_id, items in per_persona.items():
        existing_seq = [
            int(row["record_seq"]) for row in items
            if row["record_seq"] is not None
        ]
        next_seq = max(existing_seq, default=0) + 1
        assigned = 0
        bad = 0
        fingerprints: list[str] = []
        for row in items:
            fingerprints.append(_row_identity_payload(row))
            raw = row["record_json"]
            try:
                json.loads(raw) if isinstance(raw, str) else None
            except json.JSONDecodeError:
                bad += 1
            if row["record_uuid"] is not None and row["record_seq"] is not None:
                continue
            record_uuid = row["record_uuid"] or uuid.uuid4().hex
            seq = row["record_seq"] if row["record_seq"] is not None else next_seq
            if row["record_seq"] is None:
                next_seq = max(next_seq, int(seq) + 1)
            conn.execute(
                """
                UPDATE history_records
                SET record_uuid = ?, record_seq = ?,
                    life_epoch = COALESCE(life_epoch, 0),
                    visibility = COALESCE(visibility, 'visible')
                WHERE id = ?
                """,
                (record_uuid, int(seq), int(row["id"])),
            )
            assigned += 1
        max_seq = conn.execute(
            "SELECT COALESCE(MAX(record_seq), 0) FROM history_records WHERE persona_id = ?",
            (persona_id,),
        ).fetchone()[0]
        count = conn.execute(
            "SELECT COUNT(*) FROM history_records WHERE persona_id = ?",
            (persona_id,),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO corpus_state(
                persona_id, instance_id, corpus_epoch, next_seq,
                append_revision, mutation_gen, current_summary_gen,
                lifecycle_epoch, updated_at
            ) VALUES (?, 'default', 0, ?, ?, 0, 0, 0, ?)
            ON CONFLICT(persona_id) DO UPDATE SET
                next_seq = MAX(corpus_state.next_seq, excluded.next_seq),
                append_revision = MAX(corpus_state.append_revision, excluded.append_revision),
                updated_at = excluded.updated_at
            """,
            (persona_id, int(max_seq) + 1, int(count), _now()),
        )
        fingerprint = hashlib.sha256(
            "\n".join(fingerprints).encode("utf-8")
        ).hexdigest()
        status = "imported" if count else "valid_empty"
        conn.execute(
            """
            INSERT OR IGNORE INTO legacy_import_ledger(
                source, domain, fingerprint, status, detail_json, created_at
            ) VALUES ('history_records', 'history', ?, ?, ?, ?)
            """,
            (
                fingerprint,
                status,
                json.dumps(
                    {"persona_id": persona_id, "rows": int(count),
                     "assigned": assigned, "corrupt_rows": bad},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                _now(),
            ),
        )
        report["personas"][persona_id] = {
            "rows": int(count), "assigned": assigned, "corrupt_rows": bad,
            "next_seq": int(max_seq) + 1, "fingerprint": fingerprint,
        }
        report["corrupt_rows"] += bad
    return report


def migrate_database(
    path: str | Path,
    *,
    allow_production: bool = False,
    instance_id: str = "default",
) -> dict[str, Any]:
    """把 debata.db v1 增量迁移到 D01 v2；不覆盖生产路径除非显式授权。"""
    db_path = Path(path)
    if not db_path.is_file():
        raise D01Error("source_missing", f"数据库不存在：{db_path}")
    if _looks_like_production(db_path) and not allow_production:
        raise D01Error(
            "production_blocked",
            "拒绝迁移生产路径；需要用户明确生产迁移授权并设置 allow_production",
        )
    conn = _connect(db_path)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > D01_SCHEMA_VERSION:
            raise D01Error("schema_too_new", f"源库版本 v{version} 高于 D01 v{D01_SCHEMA_VERSION}")
        if version < 1:
            raise D01Error("schema_invalid", "源库没有 v1 schema")
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_schema(conn)
            report = _backfill_history(conn)
            conn.execute(f"PRAGMA user_version = {D01_SCHEMA_VERSION}")
            conn.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, migration_id, applied_at)
                VALUES (?, ?, ?)
                """,
                (D01_SCHEMA_VERSION, D01_MIGRATION_ID, _now()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    return {
        "ok": True,
        "migration_id": D01_MIGRATION_ID,
        "schema_version": D01_SCHEMA_VERSION,
        "source_version": version,
        "instance_id": instance_id,
        **report,
    }


def append_record(
    path: str | Path,
    persona_id: str,
    record: dict[str, Any],
    *,
    origin_id: str | None = None,
    allow_production: bool = False,
) -> dict[str, Any]:
    """同库短事务追加：history + corpus_state + 可选 origin/outbox。"""
    db_path = Path(path)
    if _looks_like_production(db_path) and not allow_production:
        raise D01Error("production_blocked", "拒绝写生产路径")
    payload = json.dumps(record, ensure_ascii=False)
    if len(payload.encode("utf-8")) > MAX_JSON_BYTES:
        raise D01Error("record_too_large", "记录过大")
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        state = conn.execute(
            "SELECT next_seq, append_revision, corpus_epoch, mutation_gen FROM corpus_state WHERE persona_id = ?",
            (persona_id,),
        ).fetchone()
        if state is None:
            raise D01Error("not_migrated", "persona 没有 corpus_state，先执行 D01 迁移")
        seq = int(state["next_seq"])
        max_index = conn.execute(
            "SELECT COALESCE(MAX(history_index), -1) FROM history_records WHERE persona_id = ?",
            (persona_id,),
        ).fetchone()[0]
        record_uuid = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO history_records(
                persona_id, history_index, role, content_hash, content_length,
                record_json, timestamp, record_uuid, record_seq, life_epoch,
                visibility
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'visible')
            """,
            (
                persona_id, int(max_index) + 1, record.get("role"),
                hashlib.sha256(str(record.get("content", "")).encode("utf-8")).hexdigest(),
                len(str(record.get("content", ""))), payload,
                record.get("timestamp"), record_uuid, seq,
            ),
        )
        conn.execute(
            """
            UPDATE corpus_state SET next_seq = ?, append_revision = append_revision + 1,
                updated_at = ? WHERE persona_id = ?
            """,
            (seq + 1, _now(), persona_id),
        )
        if origin_id:
            conn.execute(
                """
                INSERT OR IGNORE INTO record_origins(
                    persona_id, record_uuid, origin_id, mapping_version, created_at
                ) VALUES (?, ?, ?, 1, ?)
                """,
                (persona_id, record_uuid, origin_id, _now()),
            )
        op_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO projection_outbox(
                op_id, source_id, persona_id, life_epoch, target, state,
                attempt, retry_at, created_at, updated_at
            ) VALUES (?, ?, ?, 0, 'chat_display', 'pending', 0, NULL, ?, ?)
            """,
            (op_id, record_uuid, persona_id, _now(), _now()),
        )
        conn.commit()
        return {
            "record_uuid": record_uuid, "record_seq": seq,
            "op_id": op_id, "next_seq": seq + 1,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_origin(
    path: str | Path,
    *,
    origin_id: str,
    adapter: str,
    account_id: str = "",
    conversation_id: str = "",
    external_id: str = "",
    allow_production: bool = False,
) -> None:
    db_path = Path(path)
    if _looks_like_production(db_path) and not allow_production:
        raise D01Error("production_blocked", "拒绝写生产路径")
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT OR IGNORE INTO origin_messages(
                origin_id, adapter, account_id, conversation_id, external_id,
                fingerprint, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (origin_id, adapter, account_id, conversation_id, external_id,
             hashlib.sha256(f"{adapter}|{account_id}|{conversation_id}|{external_id}".encode()).hexdigest(),
             _now()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def tombstone_record(
    path: str | Path,
    persona_id: str,
    record_uuid: str,
    *,
    reason: str = "",
    allow_production: bool = False,
) -> dict[str, Any]:
    """逻辑删除：tombstone + visibility + mutation_gen + outbox 同事务。"""
    db_path = Path(path)
    if _looks_like_production(db_path) and not allow_production:
        raise D01Error("production_blocked", "拒绝写生产路径")
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT record_seq, life_epoch FROM history_records WHERE persona_id = ? AND record_uuid = ?",
            (persona_id, record_uuid),
        ).fetchone()
        if row is None:
            raise D01Error("record_missing", "记录不存在")
        op_id = uuid.uuid4().hex
        mutation_gen = int(conn.execute(
            "SELECT mutation_gen FROM corpus_state WHERE persona_id = ?",
            (persona_id,),
        ).fetchone()[0]) + 1
        conn.execute(
            "UPDATE history_records SET visibility='deleted', deleted_at=? WHERE persona_id=? AND record_uuid=?",
            (_now(), persona_id, record_uuid),
        )
        conn.execute(
            "UPDATE corpus_state SET mutation_gen=?, updated_at=? WHERE persona_id=?",
            (mutation_gen, _now(), persona_id),
        )
        conn.execute(
            """
            INSERT INTO delete_ops(op_id, persona_id, corpus_epoch, mutation_gen, scope, state, created_at)
            VALUES (?, ?, 0, ?, 'record', 'tombstoned', ?)
            """,
            (op_id, persona_id, mutation_gen, _now()),
        )
        conn.execute(
            """
            INSERT INTO tombstones(
                tombstone_id, persona_id, record_uuid, life_epoch, mutation_gen, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (uuid.uuid4().hex, persona_id, record_uuid, int(row["life_epoch"]),
             mutation_gen, reason[:500], _now()),
        )
        conn.execute(
            """
            INSERT INTO projection_outbox(
                op_id, source_id, persona_id, life_epoch, target, state,
                attempt, retry_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'read_filter', 'pending', 0, NULL, ?, ?)
            """,
            (uuid.uuid4().hex, record_uuid, persona_id, int(row["life_epoch"]), _now(), _now()),
        )
        conn.commit()
        return {"op_id": op_id, "mutation_gen": mutation_gen}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ledger_status(path: str | Path) -> dict[str, Any]:
    conn = _connect(Path(path), read_only=True)
    try:
        tables = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        result: dict[str, Any] = {"tables": sorted(tables), "schema_version": int(
            conn.execute("PRAGMA user_version").fetchone()[0]
        )}
        for name in ("corpus_state", "legacy_import_ledger", "tombstones", "projection_outbox"):
            if name in tables:
                result[name] = int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
        return result
    finally:
        conn.close()


def _canonical_digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _add_issue(issues: list[str], message: str) -> None:
    if len(issues) < MAX_EVIDENCE_ISSUES and message not in issues:
        issues.append(message)


def _ledger_structure_issues(
    conn: sqlite3.Connection,
    tables: set[str],
) -> tuple[list[str], list[str]]:
    """返回（结构问题，schema_migrations 中的 migration id）。"""
    issues: list[str] = []
    missing = [name for name in _REQUIRED_LEDGER_TABLES if name not in tables]
    if missing:
        _add_issue(issues, "ledger_tables_missing:" + ",".join(missing))
    if "history_records" not in tables:
        _add_issue(issues, "history_table_missing")
    else:
        missing_columns = sorted(
            _REQUIRED_HISTORY_COLUMNS - _columns(conn, "history_records")
        )
        if missing_columns:
            _add_issue(
                issues,
                "history_columns_missing:" + ",".join(missing_columns),
            )
    migration_ids: list[str] = []
    if "schema_migrations" not in tables:
        _add_issue(issues, "schema_migrations_missing")
    else:
        migration_ids = [
            str(row["migration_id"])
            for row in conn.execute(
                "SELECT migration_id FROM schema_migrations ORDER BY version"
            )
        ]
        if D01_MIGRATION_ID not in migration_ids:
            _add_issue(issues, "d01_migration_not_recorded")
    return issues, migration_ids


def _tombstone_fingerprints(
    conn: sqlite3.Connection,
) -> tuple[dict[str, str], dict[str, int], dict[str, int]]:
    """流式 tombstone 身份/代次指纹；内存只随 persona 数增长。"""
    hashers: dict[str, Any] = {}
    counts: dict[str, int] = {}
    max_gen: dict[str, int] = {}
    for row in conn.execute(
        """
        SELECT persona_id, record_uuid, life_epoch, mutation_gen
        FROM tombstones ORDER BY persona_id, record_uuid
        """
    ):
        persona_id = str(row["persona_id"])
        hasher = hashers.get(persona_id)
        if hasher is None:
            hasher = hashlib.sha256()
            hashers[persona_id] = hasher
        hasher.update(
            (
                f"{row['record_uuid']}|{int(row['life_epoch'])}|"
                f"{int(row['mutation_gen'])}\n"
            ).encode()
        )
        counts[persona_id] = counts.get(persona_id, 0) + 1
        max_gen[persona_id] = max(
            max_gen.get(persona_id, 0), int(row["mutation_gen"])
        )
    fingerprints = {
        persona_id: hasher.hexdigest()
        for persona_id, hasher in hashers.items()
    }
    return fingerprints, counts, max_gen


def ledger_evidence(path: str | Path, *, label: str = "") -> dict[str, Any]:
    """只读 D01 ledger 语义证据：schema、迁移、水位和不变量。

    证据只包含计数、ID/seq/epoch、tombstone 指纹，不含聊天正文、摘要或密钥。
    ``aware`` 表示 D01 schema/表/迁移标记存在；``complete`` 还要求迁移后
    不变量成立。聚合 digest 绑定 ``label``（归档内路径），便于恢复时复验。
    """
    db_path = Path(path)
    evidence: dict[str, Any] = {
        "format": LEDGER_EVIDENCE_FORMAT,
        "label": str(label),
        "schema_name": "",
        "schema_version": 0,
        "migration_ids": [],
        "aware": False,
        "complete": False,
        "issues": [],
        "personas": {},
        "watermark": {},
        "digest": "",
    }

    def finalize() -> dict[str, Any]:
        evidence["digest"] = _canonical_digest(
            {key: value for key, value in evidence.items() if key != "digest"}
        )
        return evidence

    if not db_path.is_file() or db_path.is_symlink():
        _add_issue(evidence["issues"], "source_missing")
        return finalize()
    try:
        conn = _connect(db_path, read_only=True)
    except Exception as exc:  # noqa: BLE001 - 证据失败必须可诊断且 fail-closed
        _add_issue(evidence["issues"], f"open_failed:{type(exc).__name__}")
        return finalize()

    try:
        table_names = _table_names(conn)
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        evidence["schema_version"] = version
        structure_issues, migration_ids = _ledger_structure_issues(
            conn, table_names
        )
        evidence["migration_ids"] = migration_ids
        if version < D01_SCHEMA_VERSION:
            _add_issue(structure_issues, f"schema_version_below_d01:v{version}")
        evidence["aware"] = bool(
            version >= D01_SCHEMA_VERSION and not structure_issues
        )
        evidence["schema_name"] = (
            D01_SCHEMA_NAME if evidence["aware"] else ""
        )
        for issue in structure_issues:
            _add_issue(evidence["issues"], issue)
        if not evidence["aware"]:
            evidence["complete"] = False
            return finalize()

        corpus_rows = {
            str(row["persona_id"]): row
            for row in conn.execute(
                """
                SELECT persona_id, instance_id, corpus_epoch, next_seq,
                       append_revision, mutation_gen, current_summary_gen,
                       lifecycle_epoch
                FROM corpus_state ORDER BY persona_id
                """
            )
        }
        history_rows = {
            str(row["persona_id"]): row
            for row in conn.execute(
                """
                SELECT persona_id, COUNT(*) AS total,
                       SUM(CASE WHEN visibility = 'visible' THEN 1 ELSE 0 END) AS visible,
                       SUM(CASE WHEN visibility = 'deleted' THEN 1 ELSE 0 END) AS deleted,
                       SUM(CASE WHEN visibility IS NULL
                                      OR visibility NOT IN ('visible', 'deleted')
                                THEN 1 ELSE 0 END) AS invalid_visibility,
                       SUM(CASE WHEN record_uuid IS NULL THEN 1 ELSE 0 END) AS null_uuid,
                       SUM(CASE WHEN record_seq IS NULL THEN 1 ELSE 0 END) AS null_seq,
                       COALESCE(MAX(record_seq), 0) AS max_seq,
                       COALESCE(MIN(record_seq), 0) AS min_seq
                FROM history_records GROUP BY persona_id
                """
            )
        }
        duplicate_rows = list(
            conn.execute(
                """
                SELECT persona_id, record_seq, COUNT(*) AS duplicates
                FROM history_records
                WHERE record_seq IS NOT NULL
                GROUP BY persona_id, record_seq
                HAVING COUNT(*) > 1
                ORDER BY persona_id, record_seq LIMIT 20
                """
            )
        )
        for row in duplicate_rows:
            _add_issue(
                evidence["issues"],
                f"duplicate_record_seq:{row['persona_id']}:{row['record_seq']}",
            )

        tomb_fingerprints, tomb_counts, tomb_max_gen = _tombstone_fingerprints(conn)
        delete_ops = {
            str(row["persona_id"]): {
                "count": int(row["count"]),
                "max_mutation_gen": int(row["max_mutation_gen"] or 0),
            }
            for row in conn.execute(
                """
                SELECT persona_id, COUNT(*) AS count,
                       COALESCE(MAX(mutation_gen), 0) AS max_mutation_gen
                FROM delete_ops GROUP BY persona_id
                """
            )
        }
        outbox_pending = {
            str(row["persona_id"]): int(row["count"])
            for row in conn.execute(
                """
                SELECT persona_id, COUNT(*) AS count
                FROM projection_outbox WHERE state = 'pending'
                GROUP BY persona_id
                """
            )
        }
        summaries = {
            str(row["persona_id"]): {
                "count": int(row["count"]),
                "max_gen": int(row["max_gen"] or 0),
            }
            for row in conn.execute(
                """
                SELECT persona_id, COUNT(*) AS count,
                       COALESCE(MAX(summary_gen), 0) AS max_gen
                FROM summary_generations GROUP BY persona_id
                """
            )
        }
        checkpoints = {
            str(row["persona_id"]): int(row["count"])
            for row in conn.execute(
                "SELECT persona_id, COUNT(*) AS count "
                "FROM turn_checkpoints GROUP BY persona_id"
            )
        }
        actions = {
            str(row["persona_id"]): int(row["count"])
            for row in conn.execute(
                "SELECT persona_id, COUNT(*) AS count "
                "FROM action_ledger GROUP BY persona_id"
            )
        }
        import_status: dict[str, int] = {}
        for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM legacy_import_ledger "
            "GROUP BY status"
        ):
            import_status[str(row["status"])] = int(row["count"])

        persona_ids = sorted(set(corpus_rows) | set(history_rows))
        if not persona_ids:
            _add_issue(evidence["issues"], "corpus_state_empty")
        for persona_id in persona_ids:
            corpus = corpus_rows.get(persona_id)
            history = history_rows.get(persona_id)
            if corpus is None:
                _add_issue(
                    evidence["issues"], f"corpus_state_missing:{persona_id}"
                )
                corpus_values = {
                    "instance_id": "",
                    "corpus_epoch": 0,
                    "next_seq": 0,
                    "append_revision": 0,
                    "mutation_gen": 0,
                    "current_summary_gen": 0,
                    "lifecycle_epoch": 0,
                }
            else:
                corpus_values = {
                    "instance_id": str(corpus["instance_id"]),
                    "corpus_epoch": int(corpus["corpus_epoch"]),
                    "next_seq": int(corpus["next_seq"]),
                    "append_revision": int(corpus["append_revision"]),
                    "mutation_gen": int(corpus["mutation_gen"]),
                    "current_summary_gen": int(corpus["current_summary_gen"]),
                    "lifecycle_epoch": int(corpus["lifecycle_epoch"]),
                }
            if history is None:
                history_values = {
                    "history_records": 0,
                    "visible_records": 0,
                    "deleted_records": 0,
                    "max_record_seq": 0,
                    "min_record_seq": 0,
                }
            else:
                history_values = {
                    "history_records": int(history["total"]),
                    "visible_records": int(history["visible"] or 0),
                    "deleted_records": int(history["deleted"] or 0),
                    "max_record_seq": int(history["max_seq"] or 0),
                    "min_record_seq": int(history["min_seq"] or 0),
                }
                invalid_visibility = int(history["invalid_visibility"] or 0)
                null_uuid = int(history["null_uuid"] or 0)
                null_seq = int(history["null_seq"] or 0)
                if invalid_visibility:
                    _add_issue(
                        evidence["issues"],
                        f"invalid_visibility:{persona_id}:{invalid_visibility}",
                    )
                if null_uuid:
                    _add_issue(
                        evidence["issues"],
                        f"record_uuid_missing:{persona_id}:{null_uuid}",
                    )
                if null_seq:
                    _add_issue(
                        evidence["issues"],
                        f"record_seq_missing:{persona_id}:{null_seq}",
                    )
            if corpus is not None:
                if int(corpus["next_seq"]) < 1:
                    _add_issue(
                        evidence["issues"], f"next_seq_invalid:{persona_id}"
                    )
                if int(corpus["next_seq"]) <= history_values["max_record_seq"]:
                    _add_issue(
                        evidence["issues"],
                        f"next_seq_not_above_max_seq:{persona_id}",
                    )
            persona_watermark = {
                **corpus_values,
                **history_values,
                "tombstones": int(tomb_counts.get(persona_id, 0)),
                "tombstone_fingerprint": tomb_fingerprints.get(persona_id, ""),
                "max_tombstone_mutation_gen": int(
                    tomb_max_gen.get(persona_id, 0)
                ),
                "delete_ops": int(
                    delete_ops.get(persona_id, {}).get("count", 0)
                ),
                "outbox_pending": int(outbox_pending.get(persona_id, 0)),
                "summary_generations": int(
                    summaries.get(persona_id, {}).get("count", 0)
                ),
                "max_summary_gen": int(
                    summaries.get(persona_id, {}).get("max_gen", 0)
                ),
                "turn_checkpoints": int(checkpoints.get(persona_id, 0)),
                "action_ledger": int(actions.get(persona_id, 0)),
            }
            evidence["personas"][persona_id] = persona_watermark
        evidence["watermark"] = {
            "persona_count": len(evidence["personas"]),
            "history_records": sum(
                item["history_records"] for item in evidence["personas"].values()
            ),
            "visible_records": sum(
                item["visible_records"] for item in evidence["personas"].values()
            ),
            "tombstones": sum(
                item["tombstones"] for item in evidence["personas"].values()
            ),
            "delete_ops": sum(
                item["delete_ops"] for item in evidence["personas"].values()
            ),
            "outbox_pending": sum(
                item["outbox_pending"] for item in evidence["personas"].values()
            ),
            "max_record_seq": max(
                (
                    item["max_record_seq"]
                    for item in evidence["personas"].values()
                ),
                default=0,
            ),
            "next_seq_sum": sum(
                item["next_seq"] for item in evidence["personas"].values()
            ),
            "append_revision_sum": sum(
                item["append_revision"]
                for item in evidence["personas"].values()
            ),
            "mutation_gen_sum": sum(
                item["mutation_gen"] for item in evidence["personas"].values()
            ),
            "lifecycle_epoch_sum": sum(
                item["lifecycle_epoch"]
                for item in evidence["personas"].values()
            ),
            "import_status": import_status,
        }
        evidence["complete"] = not evidence["issues"]
        return finalize()
    except Exception as exc:  # noqa: BLE001
        _add_issue(evidence["issues"], f"probe_failed:{type(exc).__name__}")
        evidence["aware"] = False
        evidence["complete"] = False
        return finalize()
    finally:
        conn.close()


def _aggregate_watermark(evidence: dict[str, Any]) -> dict[str, Any]:
    watermark = dict(evidence.get("watermark") or {})
    return watermark


def aggregate_ledger_evidence(items: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合同一快照内全部 persona DB 的 ledger 证据。"""
    databases = sorted(
        (dict(item) for item in items if isinstance(item, dict)),
        key=lambda item: str(item.get("label", "")),
    )
    issues: list[str] = []
    persona_sources: dict[str, str] = {}
    for item in databases:
        label = str(item.get("label") or "?")
        for issue in item.get("issues") or []:
            _add_issue(issues, f"{label}:{issue}")
        for persona_id in item.get("personas") or {}:
            key = str(persona_id)
            if key in persona_sources:
                _add_issue(
                    issues,
                    f"duplicate_persona_across_databases:{key}",
                )
            else:
                persona_sources[key] = label
    aware = bool(databases) and all(bool(item.get("aware")) for item in databases)
    complete = (
        bool(databases)
        and all(bool(item.get("complete")) for item in databases)
        and not issues
    )
    migration_ids = sorted(
        {
            str(migration_id)
            for item in databases
            for migration_id in item.get("migration_ids") or []
        }
    )
    aggregate: dict[str, Any] = {
        "format": LEDGER_EVIDENCE_FORMAT,
        "schema_name": D01_SCHEMA_NAME if aware else "",
        "schema_version": (
            max(
                int(item.get("schema_version") or 0)
                for item in databases
            )
            if databases
            else 0
        ),
        "migration_ids": migration_ids,
        "aware": aware,
        "complete": complete,
        "databases": [
            {
                "label": str(item.get("label") or ""),
                "schema_version": int(item.get("schema_version") or 0),
                "migration_ids": list(item.get("migration_ids") or []),
                "aware": bool(item.get("aware")),
                "complete": bool(item.get("complete")),
                "issues": list(item.get("issues") or []),
                "digest": str(item.get("digest") or ""),
                "watermark": _aggregate_watermark(item),
                "personas": dict(item.get("personas") or {}),
            }
            for item in databases
        ],
        "issues": issues,
        "personas": {
            str(persona_id): dict(values)
            for item in databases
            for persona_id, values in (item.get("personas") or {}).items()
        },
        "watermark": {
            "persona_count": sum(
                int((item.get("watermark") or {}).get("persona_count") or 0)
                for item in databases
            ),
            "history_records": sum(
                int((item.get("watermark") or {}).get("history_records") or 0)
                for item in databases
            ),
            "visible_records": sum(
                int((item.get("watermark") or {}).get("visible_records") or 0)
                for item in databases
            ),
            "tombstones": sum(
                int((item.get("watermark") or {}).get("tombstones") or 0)
                for item in databases
            ),
            "delete_ops": sum(
                int((item.get("watermark") or {}).get("delete_ops") or 0)
                for item in databases
            ),
            "outbox_pending": sum(
                int((item.get("watermark") or {}).get("outbox_pending") or 0)
                for item in databases
            ),
            "max_record_seq": max(
                (
                    int(
                        (item.get("watermark") or {}).get("max_record_seq")
                        or 0
                    )
                    for item in databases
                ),
                default=0,
            ),
            "next_seq_sum": sum(
                int((item.get("watermark") or {}).get("next_seq_sum") or 0)
                for item in databases
            ),
            "append_revision_sum": sum(
                int(
                    (item.get("watermark") or {}).get("append_revision_sum")
                    or 0
                )
                for item in databases
            ),
            "mutation_gen_sum": sum(
                int(
                    (item.get("watermark") or {}).get("mutation_gen_sum")
                    or 0
                )
                for item in databases
            ),
            "lifecycle_epoch_sum": sum(
                int(
                    (item.get("watermark") or {}).get("lifecycle_epoch_sum")
                    or 0
                )
                for item in databases
            ),
        },
    }
    aggregate["digest"] = _canonical_digest(aggregate)
    return aggregate


def ledger_evidence_digest(evidence: dict[str, Any]) -> str:
    return _canonical_digest(
        {key: value for key, value in evidence.items() if key != "digest"}
    )


def _tombstone_id_set(
    conn: sqlite3.Connection, persona_id: str
) -> set[str]:
    return {
        str(row["record_uuid"])
        for row in conn.execute(
            "SELECT record_uuid FROM tombstones WHERE persona_id = ?",
            (persona_id,),
        )
    }


def plan_restore_replay(
    snapshot_path: str | Path,
    source_path: str | Path,
) -> dict[str, Any]:
    """比较 D01 快照与当前源，判断能否精确恢复或只重放墓碑。

    - ``exact``：无新消息/新墓碑，恢复快照即可；
    - ``replay_tombstones``：快照后只新增删除意图，恢复后必须重放；
    - ``blocked``：出现新消息、墓碑回退、epoch/ledger 分叉等，不能裸恢复。
    本函数只读，不修改任何数据库。
    """
    snapshot_evidence = ledger_evidence(snapshot_path, label="snapshot")
    source_evidence = ledger_evidence(source_path, label="source")
    result: dict[str, Any] = {
        "ok": True,
        "decision": "exact",
        "reasons": [],
        "personas": {},
        "snapshot_digest": snapshot_evidence["digest"],
        "source_digest": source_evidence["digest"],
        "summary_rebuild_required": False,
        "projection_reconcile_required": False,
    }
    if not snapshot_evidence["complete"] or not source_evidence["complete"]:
        result.update(
            {
                "ok": False,
                "decision": "blocked",
                "reasons": ["ledger_evidence_incomplete"],
                "snapshot_evidence": snapshot_evidence,
                "source_evidence": source_evidence,
            }
        )
        return result

    snapshot_personas = snapshot_evidence["personas"]
    source_personas = source_evidence["personas"]
    snapshot_conn = _connect(Path(snapshot_path), read_only=True)
    source_conn = _connect(Path(source_path), read_only=True)
    try:
        for persona_id in sorted(set(snapshot_personas) | set(source_personas)):
            snapshot = snapshot_personas.get(persona_id)
            source = source_personas.get(persona_id)
            entry: dict[str, Any] = {
                "decision": "exact",
                "added_tombstones": 0,
                "new_records": 0,
                "reasons": [],
            }
            if snapshot is None:
                entry["decision"] = "blocked_new_records"
                entry["reasons"].append("persona_not_in_snapshot")
                entry["new_records"] = int(source.get("history_records") or 0) if source else 0
            elif source is None:
                entry["decision"] = "blocked_ledger_divergence"
                entry["reasons"].append("persona_missing_from_source")
            else:
                if source["corpus_epoch"] != snapshot["corpus_epoch"]:
                    entry["decision"] = "blocked_epoch"
                    entry["reasons"].append("corpus_epoch_changed")
                elif (
                    source["lifecycle_epoch"] != snapshot["lifecycle_epoch"]
                ):
                    entry["decision"] = "blocked_epoch"
                    entry["reasons"].append("lifecycle_epoch_changed")
                elif (
                    source["next_seq"] < snapshot["next_seq"]
                    or source["history_records"] < snapshot["history_records"]
                ):
                    entry["decision"] = "blocked_source_older"
                    entry["reasons"].append("source_older_than_snapshot")
                elif (
                    source["next_seq"] > snapshot["next_seq"]
                    or source["history_records"] > snapshot["history_records"]
                    or source["append_revision"] > snapshot["append_revision"]
                ):
                    entry["decision"] = "blocked_new_records"
                    entry["new_records"] = max(
                        0,
                        source["history_records"]
                        - snapshot["history_records"],
                    )
                    entry["reasons"].append("new_records_after_snapshot")
                else:
                    snapshot_tombs = _tombstone_id_set(
                        snapshot_conn, persona_id
                    )
                    source_tombs = _tombstone_id_set(source_conn, persona_id)
                    if snapshot_tombs - source_tombs:
                        entry["decision"] = "blocked_tombstone_divergence"
                        entry["reasons"].append("source_missing_tombstones")
                    else:
                        added = source_tombs - snapshot_tombs
                        delete_ops_added = (
                            int(source["delete_ops"])
                            > int(snapshot["delete_ops"])
                        )
                        entry["added_tombstones"] = len(added)
                        if added or delete_ops_added:
                            entry["decision"] = "replay_tombstones"
                            entry["reasons"].append(
                                "tombstones_after_snapshot"
                            )
                            result["summary_rebuild_required"] = True
                        elif (
                            source["mutation_gen"] != snapshot["mutation_gen"]
                        ):
                            entry["decision"] = "blocked_ledger_divergence"
                            entry["reasons"].append(
                                "mutation_gen_changed_without_tombstones"
                            )
                        elif (
                            source["append_revision"]
                            != snapshot["append_revision"]
                        ):
                            entry["decision"] = "blocked_ledger_divergence"
                            entry["reasons"].append("append_revision_changed")
                        elif (
                            source["max_summary_gen"]
                            != snapshot["max_summary_gen"]
                        ):
                            entry["reasons"].append("derived_summary_newer")
                            result["summary_rebuild_required"] = True
                        if (
                            source["outbox_pending"]
                            > snapshot["outbox_pending"]
                        ):
                            result["projection_reconcile_required"] = True
            result["personas"][persona_id] = entry
            if entry["decision"] != "exact":
                result["reasons"].extend(
                    f"{persona_id}:{reason}" for reason in entry["reasons"]
                )
    finally:
        snapshot_conn.close()
        source_conn.close()

    decisions = {
        entry["decision"] for entry in result["personas"].values()
    }
    if any(decision.startswith("blocked") for decision in decisions):
        result["decision"] = "blocked"
        result["ok"] = False
    elif "replay_tombstones" in decisions:
        result["decision"] = "replay_tombstones"
    else:
        result["decision"] = "exact"
    return result


def apply_tombstone_replay(
    snapshot_path: str | Path,
    source_path: str | Path,
    *,
    allow_production: bool = False,
) -> dict[str, Any]:
    """在恢复前把快照之后新增的墓碑/删除操作重放到快照库。

    仅在 ``plan_restore_replay`` 判定 exact/replay_tombstones 时执行；有新消息、
    墓碑回退或 ledger 分叉时拒绝。重放只改 visibility/tombstone/delete_ops/
    outbox/corpus_state，不改写历史 raw JSON；相关 summary 标记为 stale。
    """
    target = Path(snapshot_path)
    source = Path(source_path)
    plan = plan_restore_replay(target, source)
    if plan["decision"] == "blocked":
        raise D01Error(
            "replay_blocked",
            "恢复重放被拒绝：" + "; ".join(plan["reasons"][:5]),
        )
    if _looks_like_production(target) and not allow_production:
        raise D01Error(
            "production_blocked",
            "拒绝重放生产路径；需要用户明确恢复授权并设置 allow_production",
        )
    if plan["decision"] == "exact":
        return {
            "ok": True,
            "decision": "exact",
            "replayed_tombstones": 0,
            "projection_ops": 0,
            "summary_invalidated": 0,
            "verified": True,
        }

    conn = _connect(target)
    source_conn = _connect(source, read_only=True)
    replayed_tombstones = 0
    projection_ops = 0
    summary_invalidated = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for persona_id, entry in plan["personas"].items():
            if entry["decision"] != "replay_tombstones":
                continue
            source_state = source_conn.execute(
                """
                SELECT corpus_epoch, next_seq, append_revision, mutation_gen,
                       lifecycle_epoch
                FROM corpus_state WHERE persona_id = ?
                """,
                (persona_id,),
            ).fetchone()
            target_state = conn.execute(
                """
                SELECT corpus_epoch, next_seq, append_revision, mutation_gen,
                       lifecycle_epoch
                FROM corpus_state WHERE persona_id = ?
                """,
                (persona_id,),
            ).fetchone()
            if source_state is None or target_state is None:
                raise D01Error(
                    "replay_blocked", f"persona 状态缺失：{persona_id}"
                )
            target_tombs = _tombstone_id_set(conn, persona_id)
            source_tombs_rows = list(
                source_conn.execute(
                    """
                    SELECT tombstone_id, record_uuid, life_epoch,
                           mutation_gen, reason, created_at
                    FROM tombstones WHERE persona_id = ?
                    """,
                    (persona_id,),
                )
            )
            for row in source_tombs_rows:
                record_uuid = str(row["record_uuid"])
                if record_uuid in target_tombs:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO tombstones(
                        tombstone_id, persona_id, record_uuid, life_epoch,
                        mutation_gen, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(row["tombstone_id"]),
                        persona_id,
                        record_uuid,
                        int(row["life_epoch"]),
                        int(row["mutation_gen"]),
                        str(row["reason"]),
                        str(row["created_at"]),
                    ),
                )
                conn.execute(
                    """
                    UPDATE history_records
                    SET visibility = 'deleted', deleted_at = ?
                    WHERE persona_id = ? AND record_uuid = ?
                    """,
                    (str(row["created_at"]), persona_id, record_uuid),
                )
                op_id = hashlib.sha256(
                    f"replay:{persona_id}:{record_uuid}".encode()
                ).hexdigest()[:32]
                existing = conn.execute(
                    """
                    SELECT 1 FROM projection_outbox
                    WHERE persona_id = ? AND source_id = ?
                      AND target = 'read_filter' AND state = 'pending'
                    LIMIT 1
                    """,
                    (persona_id, record_uuid),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO projection_outbox(
                            op_id, source_id, persona_id, life_epoch, target,
                            state, attempt, retry_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'read_filter', 'pending', 0,
                                  NULL, ?, ?)
                        """,
                        (
                            op_id,
                            record_uuid,
                            persona_id,
                            int(row["life_epoch"]),
                            str(row["created_at"]),
                            str(row["created_at"]),
                        ),
                    )
                    projection_ops += 1
                replayed_tombstones += 1

            source_delete_ops = list(
                source_conn.execute(
                    """
                    SELECT op_id, persona_id, corpus_epoch, mutation_gen,
                           scope, state, created_at
                    FROM delete_ops WHERE persona_id = ?
                    """,
                    (persona_id,),
                )
            )
            for row in source_delete_ops:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO delete_ops(
                        op_id, persona_id, corpus_epoch, mutation_gen,
                        scope, state, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(row["op_id"]),
                        str(row["persona_id"]),
                        int(row["corpus_epoch"]),
                        int(row["mutation_gen"]),
                        str(row["scope"]),
                        str(row["state"]),
                        str(row["created_at"]),
                    ),
                )
            conn.execute(
                """
                UPDATE corpus_state SET
                    corpus_epoch = ?, next_seq = ?, append_revision = ?,
                    mutation_gen = ?, current_summary_gen = 0,
                    lifecycle_epoch = ?, updated_at = ?
                WHERE persona_id = ?
                """,
                (
                    int(source_state["corpus_epoch"]),
                    int(source_state["next_seq"]),
                    int(source_state["append_revision"]),
                    int(source_state["mutation_gen"]),
                    int(source_state["lifecycle_epoch"]),
                    _now(),
                    persona_id,
                ),
            )
            cursor = conn.execute(
                "UPDATE summary_generations SET state = 'stale' "
                "WHERE persona_id = ? AND state != 'stale'",
                (persona_id,),
            )
            summary_invalidated += max(0, int(cursor.rowcount or 0))

        # 事务内复验：墓碑集合/账本水位必须与源一致，历史条数/最大 seq 不变。
        for persona_id, entry in plan["personas"].items():
            if entry["decision"] != "replay_tombstones":
                continue
            source_state = source_conn.execute(
                """
                SELECT next_seq, append_revision, mutation_gen, lifecycle_epoch
                FROM corpus_state WHERE persona_id = ?
                """,
                (persona_id,),
            ).fetchone()
            target_state = conn.execute(
                """
                SELECT next_seq, append_revision, mutation_gen, lifecycle_epoch
                FROM corpus_state WHERE persona_id = ?
                """,
                (persona_id,),
            ).fetchone()
            if (
                source_state is None
                or target_state is None
                or tuple(source_state) != tuple(target_state)
            ):
                raise D01Error(
                    "replay_verify_failed",
                    f"重放后 corpus_state 不一致：{persona_id}",
                )
            if _tombstone_id_set(conn, persona_id) != _tombstone_id_set(
                source_conn, persona_id
            ):
                raise D01Error(
                    "replay_verify_failed",
                    f"重放后墓碑集合不一致：{persona_id}",
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        source_conn.close()
        conn.close()
    return {
        "ok": True,
        "decision": "replay_tombstones",
        "replayed_tombstones": replayed_tombstones,
        "projection_ops": projection_ops,
        "summary_invalidated": summary_invalidated,
        "verified": True,
        "summary_rebuild_required": bool(plan["summary_rebuild_required"]),
        "projection_reconcile_required": bool(
            plan["projection_reconcile_required"]
        ),
    }


__all__ = [
    "D01_CONTRACT_EPOCH",
    "D01_MIGRATION_ID",
    "D01_SCHEMA_VERSION",
    "D01Error",
    "LEDGER_EVIDENCE_FORMAT",
    "add_origin",
    "aggregate_ledger_evidence",
    "append_record",
    "apply_tombstone_replay",
    "ledger_evidence",
    "ledger_evidence_digest",
    "ledger_status",
    "migrate_database",
    "plan_restore_replay",
    "tombstone_record",
]
