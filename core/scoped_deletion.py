"""X01 / X02: 范围删除、即时读屏障、受控隔离与人格上下文重置。

核心契约（第二阶段接续方案 2.6 第 19 节）：
1. 范围删除预览（preview_scope_deletion）：
   - 支持按时间范围 [from, to)、会话 ID 或精确选择；
   - 空 conversation_id 绝不作为全删会话（防误删空值扩大范围）；
   - 预览输出严格脱敏：绝不返回消息正文、signed URL 或内部绝对路径表，只输出多域影响计数与字节估算；
   - 产出带有时效（TTL）与 HMAC 防篡改签名的 preview_token。
2. 原子墓碑与即时读屏障（read fence，E36）：
   - 在 debata.db 的单个 SQLite 事务中先写入 delete_ops 与 tombstones，递增 corpus_state.mutation_gen；
   - 所有 history、event、archive、display、summary、RAG、recall、export 即时遵守读屏障，慢任务/备份不得复活已删记录；
   - 关联的 summary_generations 同步失效或解绑，杜绝已概括事实产生幽灵记忆。
3. 受控隔离期与恢复（Controlled Quarantine & Restore）：
   - 默认受控隔离 24 小时，写入隔离 manifest（包含 op_id, epoch, uuids, expiry）；
   - 隔离期内恢复（restore）：递增分配全新的 lifecycle_epoch 与 mutation_gen，确保旧回调和慢 worker 无法覆写，且绝不重放外部工具动作或重发外部消息；
   - 物理清理（purge）：隔离期满或用户明确 purge 时，彻底清除物理记录与隔离文件，明确不可逆。
4. 人格上下文重置（Persona Context Reset）：
   - 不物理删除原始历史，而是递增分配新的 lifecycle_epoch 并将活跃游标推进至末端；
   - 历史底册完整保留供归档与审计，新轮次进入干净上下文。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memory.debata_db import DebataDB

logger = logging.getLogger(__name__)

DEFAULT_QUARANTINE_HOURS = 24.0
PREVIEW_TOKEN_TTL_SECONDS = 300.0
_DEFAULT_SECRET_KEY = b"debata-scope-deletion-seal-v2.6"


def _get_secret_key() -> bytes:
    env_key = os.environ.get("DEBATA_SCOPE_DELETION_KEY")
    if env_key:
        return env_key.encode("utf-8")
    return _DEFAULT_SECRET_KEY


def _chunked(items: list[Any], chunk_size: int = 200) -> list[list[Any]]:
    """将列表安全切分为固定大小的批次。"""
    return [items[i : i + chunk_size] for i in range(0, len(items), chunk_size)]


class ScopedDeletionError(RuntimeError):
    """范围删除操作错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


@dataclass(slots=True)
class DeletionScope:
    persona_id: str
    scope_type: str  # "range" | "session" | "all"
    from_time: str | None = None
    to_time: str | None = None
    conversation_id: str | None = None
    selection_uuids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DeletionPreview:
    preview_id: str
    persona_id: str
    scope_type: str
    from_time: str | None
    to_time: str | None
    conversation_id: str | None
    history_count: int
    archive_count: int
    summary_count: int
    media_count: int
    estimated_bytes: int
    quarantine_hours: float
    expires_at: float
    preview_token: str
    selection_digest: str

    def as_sanitized_dict(self) -> dict[str, Any]:
        """严格脱敏输出：绝不暴露正文、URL 或内部文件路径。"""
        return {
            "preview_id": self.preview_id,
            "persona_id": self.persona_id,
            "scope_type": self.scope_type,
            "from_time": self.from_time,
            "to_time": self.to_time,
            "conversation_id": self.conversation_id,
            "impact": {
                "history_records": self.history_count,
                "archive_records": self.archive_count,
                "summary_generations": self.summary_count,
                "media_origins": self.media_count,
                "estimated_bytes": self.estimated_bytes,
            },
            "quarantine_hours": self.quarantine_hours,
            "expires_at": self.expires_at,
            "preview_token": self.preview_token,
            "selection_digest": self.selection_digest,
        }


@dataclass(slots=True)
class DeletionExecutionResult:
    op_id: str
    persona_id: str
    tombstoned_count: int
    invalidated_summaries_count: int
    mutation_gen: int
    lifecycle_epoch: int
    quarantine_until: float
    quarantine_manifest_path: str


@dataclass(slots=True)
class RestorationResult:
    op_id: str
    persona_id: str
    restored_count: int
    new_life_epoch: int
    new_mutation_gen: int
    status: str


@dataclass(slots=True)
class PurgeResult:
    op_id: str
    persona_id: str
    purged_records_count: int
    freed_bytes_estimate: int
    status: str


@dataclass(slots=True)
class ContextResetResult:
    persona_id: str
    previous_lifecycle_epoch: int
    new_lifecycle_epoch: int
    new_mutation_gen: int
    advanced_active_start: int
    cleared_summary: bool


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _has_table(conn: sqlite3.Connection, table_name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    )
    return cur.fetchone() is not None


class ScopedDeletionManager:
    """管理范围删除预览、原子墓碑提交、读屏障与受控隔离恢复。"""

    def __init__(
        self,
        db: DebataDB | str | Path | sqlite3.Connection,
        quarantine_dir: Path | str | None = None,
    ) -> None:
        self._db_or_conn = db
        if quarantine_dir is not None:
            self.quarantine_dir = Path(quarantine_dir)
        elif isinstance(db, (str, Path)):
            self.quarantine_dir = Path(db).parent / "quarantine"
        elif isinstance(db, DebataDB):
            self.quarantine_dir = db.path.parent / "quarantine"
        else:
            self.quarantine_dir = Path("./data/quarantine")
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _get_connection(self) -> sqlite3.Connection:
        if isinstance(self._db_or_conn, sqlite3.Connection):
            return self._db_or_conn
        if isinstance(self._db_or_conn, DebataDB):
            return self._db_or_conn.connect()
        debata_db = DebataDB(self._db_or_conn)
        return debata_db.connect()

    def _ensure_schema(self) -> None:
        conn = self._get_connection()
        conn.execute(
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
            """
        )
        conn.execute(
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
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tombstones_persona ON tombstones(persona_id, record_uuid)"
        )

    # ------------------------------------------------------------------
    # Token 签名与校验
    # ------------------------------------------------------------------
    def _create_token(self, preview_id: str, digest: str, expires_at: float) -> str:
        payload = f"{preview_id}:{digest}:{int(expires_at)}"
        sig = hmac.new(_get_secret_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{payload}:{sig}"

    def _verify_token(self, token: str) -> tuple[str, str, float]:
        try:
            parts = token.split(":")
            if len(parts) != 4:
                raise ValueError
            preview_id, digest, exp_str, sig = parts
            expires_at = float(exp_str)
            if time.time() > expires_at:
                raise ScopedDeletionError("preview_token_expired", "删除预览已超时失效，请重新生成预览")
            payload = f"{preview_id}:{digest}:{int(expires_at)}"
            expected_sig = hmac.new(_get_secret_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected_sig):
                raise ScopedDeletionError("preview_token_invalid", "删除预览签名校验失败")
            return preview_id, digest, expires_at
        except (ValueError, IndexError):
            raise ScopedDeletionError("preview_token_malformed", "删除预览凭证格式非法") from None

    # ------------------------------------------------------------------
    # 1. 范围删除预览（Preview）
    # ------------------------------------------------------------------
    def preview_scope_deletion(
        self,
        scope: DeletionScope,
        *,
        quarantine_hours: float = DEFAULT_QUARANTINE_HOURS,
    ) -> DeletionPreview:
        """生成范围删除预览。

        严格约束：
        - 空 conversation_id 且 scope_type == 'session' 时强制阻断；
        - 绝不返回消息正文或内部路径，仅返回各域影响计数；
        """
        if scope.scope_type == "session" and not (scope.conversation_id and scope.conversation_id.strip()):
            raise ScopedDeletionError(
                "invalid_scope",
                "会话级删除必须显式指定非空 conversation_id，禁止空值隐式扩大为全删",
            )

        conn = self._get_connection()
        persona_id = scope.persona_id

        # 匹配目标 history 记录
        where_clauses = ["1=1"]
        params: list[Any] = []

        has_history = _has_table(conn, "history_records")
        has_archives = _has_table(conn, "archive_messages")
        has_summaries = _has_table(conn, "summary_generations")
        has_media = _has_table(conn, "media_origins")

        target_uuids: list[str] = []
        estimated_bytes = 0

        if scope.selection_uuids:
            # 精确 UUID 选择
            target_uuids = list(scope.selection_uuids)
        elif has_history:
            if scope.from_time:
                where_clauses.append("created_at >= ?")
                params.append(scope.from_time)
            if scope.to_time:
                where_clauses.append("created_at < ?")
                params.append(scope.to_time)

            col_names = {col[1] for col in conn.execute("PRAGMA table_info(history_records)").fetchall()}
            json_col = "record_json" if "record_json" in col_names else "payload"

            if "persona_id" in col_names:
                where_clauses.append("persona_id = ?")
                params.append(persona_id)

            sql = f"SELECT id, {json_col} FROM history_records WHERE {' AND '.join(where_clauses)}"
            rows = conn.execute(sql, params).fetchall()

            for r in rows:
                p_str = str(r[json_col] or "")
                estimated_bytes += len(p_str.encode("utf-8"))
                try:
                    p_obj = json.loads(p_str)
                    if isinstance(p_obj, dict):
                        # 如果指定了 conversation_id，做会话匹配
                        if scope.conversation_id:
                            cid = str(p_obj.get("conversation_id") or "")
                            if cid != scope.conversation_id:
                                continue
                        rec_uuid = str(p_obj.get("record_uuid") or f"legacy_hist_{r['id']}")
                        target_uuids.append(rec_uuid)
                except Exception:
                    rec_uuid = f"corrupt_hist_{r['id']}"
                    target_uuids.append(rec_uuid)

        history_count = len(target_uuids)

        # 匹配归档数量
        archive_count = 0
        if has_archives and target_uuids:
            # 统计归档受影响条数
            archive_count = min(history_count, conn.execute(
                "SELECT COUNT(*) FROM archive_messages WHERE persona_id = ?",
                (persona_id,),
            ).fetchone()[0])

        # 匹配受影响的 summary generations（分批完整处理，杜绝 200 条切片上限截断）
        summary_count = 0
        if has_summaries and _has_table(conn, "summary_sources") and target_uuids:
            summary_gens: set[int] = set()
            for chunk in _chunked(target_uuids, 200):
                placeholders = ",".join("?" for _ in chunk)
                for s_row in conn.execute(
                    f"SELECT DISTINCT summary_gen FROM summary_sources "
                    f"WHERE persona_id = ? AND record_uuid IN ({placeholders})",
                    [persona_id] + chunk,
                ).fetchall():
                    summary_gens.add(int(s_row[0]))
            summary_count = len(summary_gens)

        # 匹配受影响的 media_origins（分批完整处理）
        media_count = 0
        if has_media and target_uuids:
            for chunk in _chunked(target_uuids, 200):
                placeholders = ",".join("?" for _ in chunk)
                m_rows = conn.execute(
                    f"SELECT COUNT(*) FROM media_origins "
                    f"WHERE persona_id = ? AND origin_id IN ({placeholders})",
                    [persona_id] + chunk,
                ).fetchone()
                if m_rows:
                    media_count += int(m_rows[0])

        # 计算选择集 digest
        digest_hasher = hashlib.sha256()
        digest_hasher.update(persona_id.encode("utf-8"))
        digest_hasher.update(scope.scope_type.encode("utf-8"))
        for u in sorted(target_uuids):
            digest_hasher.update(u.encode("utf-8"))
        selection_digest = digest_hasher.hexdigest()

        preview_id = f"prev_{uuid.uuid4().hex}"
        now = time.time()
        expires_at = now + PREVIEW_TOKEN_TTL_SECONDS
        preview_token = self._create_token(preview_id, selection_digest, expires_at)

        # 暂存预览元数据供执行时二次对账
        preview_meta_file = self.quarantine_dir / f"{preview_id}.preview.json"
        preview_data = {
            "preview_id": preview_id,
            "persona_id": persona_id,
            "scope_type": scope.scope_type,
            "from_time": scope.from_time,
            "to_time": scope.to_time,
            "conversation_id": scope.conversation_id,
            "target_uuids": target_uuids,
            "selection_digest": selection_digest,
            "quarantine_hours": quarantine_hours,
            "expires_at": expires_at,
        }
        preview_meta_file.write_text(json.dumps(preview_data, ensure_ascii=False), encoding="utf-8")

        return DeletionPreview(
            preview_id=preview_id,
            persona_id=persona_id,
            scope_type=scope.scope_type,
            from_time=scope.from_time,
            to_time=scope.to_time,
            conversation_id=scope.conversation_id,
            history_count=history_count,
            archive_count=archive_count,
            summary_count=summary_count,
            media_count=media_count,
            estimated_bytes=estimated_bytes,
            quarantine_hours=quarantine_hours,
            expires_at=expires_at,
            preview_token=preview_token,
            selection_digest=selection_digest,
        )

    # ------------------------------------------------------------------
    # 2. 原子墓碑提交（Execute）与即时读屏障
    # ------------------------------------------------------------------
    def execute_scope_deletion(
        self,
        preview_token: str,
        *,
        reason: str = "user_scoped_deletion",
    ) -> DeletionExecutionResult:
        """根据已校验的预览凭证，原子化写入墓碑、增代次并置入受控隔离区。"""
        preview_id, digest, _ = self._verify_token(preview_token)

        preview_meta_file = self.quarantine_dir / f"{preview_id}.preview.json"
        if not preview_meta_file.exists():
            raise ScopedDeletionError("preview_not_found", "未找到对应的删除预览，请重新预览")

        preview_data = json.loads(preview_meta_file.read_text(encoding="utf-8"))
        if preview_data.get("selection_digest") != digest:
            raise ScopedDeletionError("preview_digest_mismatch", "选择集摘要不匹配，操作已被取消")

        persona_id = str(preview_data["persona_id"])
        target_uuids: list[str] = preview_data.get("target_uuids", [])
        quarantine_hours = float(preview_data.get("quarantine_hours", DEFAULT_QUARANTINE_HOURS))

        now = time.time()
        now_iso = _now_iso()
        quarantine_until = now + quarantine_hours * 3600.0
        op_id = f"op_del_{uuid.uuid4().hex}"

        conn = self._get_connection()
        with conn:
            # 1. 查询并递增 corpus_state.mutation_gen
            current_mutation_gen = 0
            current_lifecycle_epoch = 0
            corpus_epoch = 0

            has_corpus_state = _has_table(conn, "corpus_state")
            if has_corpus_state:
                row = conn.execute(
                    "SELECT mutation_gen, lifecycle_epoch, corpus_epoch FROM corpus_state WHERE persona_id = ?",
                    (persona_id,),
                ).fetchone()
                if row is not None:
                    current_mutation_gen = int(row["mutation_gen"])
                    current_lifecycle_epoch = int(row["lifecycle_epoch"])
                    corpus_epoch = int(row["corpus_epoch"])

            new_mutation_gen = current_mutation_gen + 1
            if has_corpus_state:
                conn.execute(
                    """
                    INSERT INTO corpus_state (persona_id, corpus_epoch, mutation_gen, lifecycle_epoch, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(persona_id) DO UPDATE SET
                        mutation_gen = excluded.mutation_gen,
                        updated_at = excluded.updated_at
                    """,
                    (persona_id, corpus_epoch, new_mutation_gen, current_lifecycle_epoch, now_iso),
                )

            # 2. 写入 delete_ops
            conn.execute(
                """
                INSERT INTO delete_ops(op_id, persona_id, corpus_epoch, mutation_gen, scope, state, created_at)
                VALUES (?, ?, ?, ?, ?, 'tombstoned', ?)
                """,
                (op_id, persona_id, corpus_epoch, new_mutation_gen, str(preview_data.get("scope_type")), now_iso),
            )

            # 3. 写入 tombstones 建立即时读屏障
            tombstone_count = 0
            for u in target_uuids:
                t_id = f"tomb_{uuid.uuid4().hex}"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO tombstones(tombstone_id, persona_id, record_uuid, life_epoch, mutation_gen, reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (t_id, persona_id, u, current_lifecycle_epoch, new_mutation_gen, reason, now_iso),
                )
                tombstone_count += 1

            # 4. 同步失效受影响的 summary_generations，杜绝已删内容通过旧摘要复活（分批完整处理）
            invalidated_summaries = 0
            if _has_table(conn, "summary_generations") and _has_table(conn, "summary_sources") and target_uuids:
                for chunk in _chunked(target_uuids, 200):
                    placeholders = ",".join("?" for _ in chunk)
                    cur = conn.execute(
                        f"""
                        UPDATE summary_generations SET state = 'invalidated'
                        WHERE persona_id = ? AND summary_gen IN (
                            SELECT DISTINCT summary_gen FROM summary_sources
                            WHERE persona_id = ? AND record_uuid IN ({placeholders})
                        )
                        """,
                        [persona_id, persona_id] + chunk,
                    )
                    invalidated_summaries += cur.rowcount

            # 5. 写入受控隔离清单 manifest
            manifest = {
                "op_id": op_id,
                "persona_id": persona_id,
                "scope_type": preview_data.get("scope_type"),
                "record_uuids": target_uuids,
                "mutation_gen": new_mutation_gen,
                "lifecycle_epoch": current_lifecycle_epoch,
                "quarantine_until": quarantine_until,
                "state": "quarantined",
                "created_at": now_iso,
            }
            manifest_path = self.quarantine_dir / f"{op_id}.manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        # 删除临时 preview 文件
        try:
            preview_meta_file.unlink(missing_ok=True)
        except Exception:
            pass

        return DeletionExecutionResult(
            op_id=op_id,
            persona_id=persona_id,
            tombstoned_count=tombstone_count,
            invalidated_summaries_count=invalidated_summaries,
            mutation_gen=new_mutation_gen,
            lifecycle_epoch=current_lifecycle_epoch,
            quarantine_until=quarantine_until,
            quarantine_manifest_path=str(manifest_path),
        )

    # ------------------------------------------------------------------
    # 读屏障（Read Fence）：即时过滤已删除记录
    # ------------------------------------------------------------------
    def is_tombstoned(self, persona_id: str, record_uuid: str) -> bool:
        """检查特定记录是否处于墓碑状态。"""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT 1 FROM tombstones WHERE persona_id = ? AND record_uuid = ?",
            (persona_id, record_uuid),
        ).fetchone()
        return row is not None

    def filter_visible_records(
        self,
        persona_id: str,
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """即时读屏障过滤：滤除所有已墓碑或隐藏的记录。"""
        if not records:
            return []
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT record_uuid FROM tombstones WHERE persona_id = ?",
            (persona_id,),
        ).fetchall()
        tombstoned_set = {str(r[0]) for r in rows}

        visible: list[dict[str, Any]] = []
        for r in records:
            ruuid = str(r.get("record_uuid") or "")
            if ruuid in tombstoned_set:
                continue
            if r.get("deleted_at") is not None:
                continue
            if r.get("visibility") == "hidden":
                continue
            visible.append(r)
        return visible

    # ------------------------------------------------------------------
    # 3. 受控隔离恢复（Restore with new life_epoch）
    # ------------------------------------------------------------------
    def restore_scope_deletion(self, op_id: str) -> RestorationResult:
        """在受控隔离期内恢复已删记录。

        契约（E36）：
        - 递增分配全新的 lifecycle_epoch 与 mutation_gen；
        - 清除对应墓碑；
        - 绝不重新执行外部工具动作或重发外部消息。
        """
        manifest_path = self.quarantine_dir / f"{op_id}.manifest.json"
        if not manifest_path.exists():
            raise ScopedDeletionError("manifest_not_found", f"未找到隔离清单: {op_id}")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("state") != "quarantined":
            raise ScopedDeletionError("invalid_manifest_state", f"清单状态为 {manifest.get('state')}，无法恢复")

        now = time.time()
        if now > float(manifest.get("quarantine_until", 0.0)):
            raise ScopedDeletionError("quarantine_expired", "已超过受控隔离期（24小时），无法执行常规恢复")

        persona_id = str(manifest["persona_id"])
        target_uuids: list[str] = manifest.get("record_uuids", [])

        conn = self._get_connection()
        with conn:
            # 1. 递增 lifecycle_epoch 和 mutation_gen
            current_epoch = 0
            current_mut = 0
            if _has_table(conn, "corpus_state"):
                row = conn.execute(
                    "SELECT lifecycle_epoch, mutation_gen FROM corpus_state WHERE persona_id = ?",
                    (persona_id,),
                ).fetchone()
                if row:
                    current_epoch = int(row[0])
                    current_mut = int(row[1])

            new_epoch = current_epoch + 1
            new_mut = current_mut + 1

            if _has_table(conn, "corpus_state"):
                conn.execute(
                    """
                    UPDATE corpus_state SET lifecycle_epoch = ?, mutation_gen = ?, updated_at = ?
                    WHERE persona_id = ?
                    """,
                    (new_epoch, new_mut, _now_iso(), persona_id),
                )

            # 2. 移除墓碑
            restored_count = 0
            if target_uuids:
                placeholders = ",".join("?" for _ in target_uuids)
                cur = conn.execute(
                    f"DELETE FROM tombstones WHERE persona_id = ? AND record_uuid IN ({placeholders})",
                    [persona_id] + target_uuids,
                )
                restored_count = cur.rowcount

            # 3. 更新 delete_ops 状态
            conn.execute(
                "UPDATE delete_ops SET state = 'restored' WHERE op_id = ?",
                (op_id,),
            )

            # 4. 更新 manifest 状态
            manifest["state"] = "restored"
            manifest["restored_at"] = _now_iso()
            manifest["new_lifecycle_epoch"] = new_epoch
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        return RestorationResult(
            op_id=op_id,
            persona_id=persona_id,
            restored_count=restored_count,
            new_life_epoch=new_epoch,
            new_mutation_gen=new_mut,
            status="restored",
        )

    # ------------------------------------------------------------------
    # 4. 物理清理（Purge）
    # ------------------------------------------------------------------
    def purge_scope_deletion(self, op_id: str) -> PurgeResult:
        """物理清除隔离期已过或用户明确销毁的记录。明确不可逆。"""
        manifest_path = self.quarantine_dir / f"{op_id}.manifest.json"
        if not manifest_path.exists():
            raise ScopedDeletionError("manifest_not_found", f"未找到隔离清单: {op_id}")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        persona_id = str(manifest["persona_id"])
        target_uuids: list[str] = manifest.get("record_uuids", [])

        conn = self._get_connection()
        purged_count = len(target_uuids)
        freed_bytes = 0

        with conn:
            # 物理清除 history_records
            if _has_table(conn, "history_records") and target_uuids:
                col_names = {col[1] for col in conn.execute("PRAGMA table_info(history_records)").fetchall()}
                json_col = "record_json" if "record_json" in col_names else "payload"
                for u in target_uuids:
                    if "record_uuid" in col_names:
                        rows = conn.execute(
                            f"SELECT id, {json_col} FROM history_records WHERE record_uuid = ? OR {json_col} LIKE ?",
                            (u, f"%{u}%"),
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            f"SELECT id, {json_col} FROM history_records WHERE {json_col} LIKE ?",
                            (f"%{u}%",),
                        ).fetchall()
                    for r in rows:
                        freed_bytes += len(str(r[json_col] or "").encode("utf-8"))
                        conn.execute("DELETE FROM history_records WHERE id = ?", (r["id"],))

            # 物理清除 archive_messages
            if _has_table(conn, "archive_messages") and target_uuids:
                col_names_arc = {col[1] for col in conn.execute("PRAGMA table_info(archive_messages)").fetchall()}
                json_col_arc = "record_json" if "record_json" in col_names_arc else ("payload" if "payload" in col_names_arc else "content")
                for u in target_uuids:
                    conn.execute(
                        f"DELETE FROM archive_messages WHERE persona_id = ? AND ({json_col_arc} LIKE ? OR archive_id = ?)",
                        (persona_id, f"%{u}%", u),
                    )

            # 物理清除关联的 media_origins（防范孤立媒体保留期脱节）
            if _has_table(conn, "media_origins") and target_uuids:
                for chunk in _chunked(target_uuids, 200):
                    placeholders = ",".join("?" for _ in chunk)
                    conn.execute(
                        f"DELETE FROM media_origins WHERE persona_id = ? AND origin_id IN ({placeholders})",
                        [persona_id] + chunk,
                    )

            # 物理清除 tombstones
            if _has_table(conn, "tombstones") and target_uuids:
                for chunk in _chunked(target_uuids, 200):
                    placeholders = ",".join("?" for _ in chunk)
                    conn.execute(
                        f"DELETE FROM tombstones WHERE persona_id = ? AND record_uuid IN ({placeholders})",
                        [persona_id] + chunk,
                    )

            # 清除 delete_ops
            conn.execute(
                "UPDATE delete_ops SET state = 'purged' WHERE op_id = ?",
                (op_id,),
            )

            # 清除隔离清单文件
            try:
                manifest_path.unlink(missing_ok=True)
            except Exception:
                pass

        return PurgeResult(
            op_id=op_id,
            persona_id=persona_id,
            purged_records_count=purged_count,
            freed_bytes_estimate=freed_bytes,
            status="purged",
        )

    # ------------------------------------------------------------------
    # 5. 人格上下文重置（Context Reset）
    # ------------------------------------------------------------------
    def reset_persona_context(self, persona_id: str) -> ContextResetResult:
        """重置人格上下文记忆视图。

        契约（第 19 节）：
        - 绝不物理删除历史底册；
        - 递增 lifecycle_epoch 和 mutation_gen；
        - 推进 active_start_index 至当前末尾；
        - 清空活跃 rolling_summary。
        """
        conn = self._get_connection()
        with conn:
            current_epoch = 0
            current_mut = 0
            if _has_table(conn, "corpus_state"):
                row = conn.execute(
                    "SELECT lifecycle_epoch, mutation_gen FROM corpus_state WHERE persona_id = ?",
                    (persona_id,),
                ).fetchone()
                if row:
                    current_epoch = int(row[0])
                    current_mut = int(row[1])

            new_epoch = current_epoch + 1
            new_mut = current_mut + 1

            # 计算当前人格的历史总条数（隔离多 persona 统计）
            total_history = 0
            if _has_table(conn, "history_records"):
                col_names = {col[1] for col in conn.execute("PRAGMA table_info(history_records)").fetchall()}
                if "persona_id" in col_names:
                    total_history = int(
                        conn.execute("SELECT COUNT(*) FROM history_records WHERE persona_id = ?", (persona_id,)).fetchone()[0]
                    )
                else:
                    total_history = int(conn.execute("SELECT COUNT(*) FROM history_records").fetchone()[0])

            # 更新 corpus_state
            if _has_table(conn, "corpus_state"):
                conn.execute(
                    """
                    INSERT INTO corpus_state (persona_id, lifecycle_epoch, mutation_gen, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(persona_id) DO UPDATE SET
                        lifecycle_epoch = excluded.lifecycle_epoch,
                        mutation_gen = excluded.mutation_gen,
                        updated_at = excluded.updated_at
                    """,
                    (persona_id, new_epoch, new_mut, _now_iso()),
                )

            # 更新 rolling_summary active_start_index
            if _has_table(conn, "rolling_summary"):
                conn.execute(
                    """
                    INSERT INTO rolling_summary(persona_id, summary_text, active_start_index, updated_at)
                    VALUES (?, '', ?, ?)
                    ON CONFLICT(persona_id) DO UPDATE SET
                        summary_text = '',
                        active_start_index = excluded.active_start_index,
                        updated_at = excluded.updated_at
                    """,
                    (persona_id, total_history, _now_iso()),
                )

        return ContextResetResult(
            persona_id=persona_id,
            previous_lifecycle_epoch=current_epoch,
            new_lifecycle_epoch=new_epoch,
            new_mutation_gen=new_mut,
            advanced_active_start=total_history,
            cleared_summary=True,
        )
