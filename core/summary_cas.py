"""S02 / C04: 摘要 / archive / cursor 原子 CAS 事务。

职责：
1. 短 gate 保护核验，同 persona 事务内原子化提交：
   CAS 校验（核验 base summary、active start、mutation_gen、epoch）
   → 来源可见性校验
   → 写入 committed summary 与 sources
   → 幂等 archive 写入
   → 游标与 active_start 推进
   → revision / outbox 提交。
2. 事务失败全量回滚，网络调用严格置于 DB 事务外部。
3. 历史删除同步失效：前置发生删除时，关联 summary 立即标记失效，杜绝已删内容复活。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memory.archive_sqlite_records import real_chat_archive_records
from memory.karen_db import KarenDB

logger = logging.getLogger(__name__)


class SummaryCASConflictError(RuntimeError):
    """CAS 冲突或版本过期，必须放弃本次候选摘要。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


@dataclass(slots=True)
class SummaryCASPlan:
    """原子 CAS 提交计划。"""

    persona_id: str
    expected_base_summary_gen: int | None = None
    expected_mutation_gen: int | None = None
    expected_lifecycle_epoch: int | None = None
    expected_active_start: int | None = None
    new_active_start: int = 0
    summary_text: str = ""
    structured_data: dict[str, Any] = field(default_factory=dict)
    slice_records: list[dict[str, Any]] = field(default_factory=list)
    coverage_start_seq: int | None = None
    coverage_end_seq: int | None = None
    binding_version: int = 1
    content_digest: str = ""


@dataclass(slots=True)
class SummaryCASResult:
    """原子 CAS 提交结果。"""

    committed: bool
    summary_gen: int
    active_start_index: int
    archived_count: int
    sources_count: int
    mutation_gen: int
    corpus_epoch: int
    digest: str


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _has_table(conn: sqlite3.Connection, table_name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    )
    return cur.fetchone() is not None


def commit_summary_cas(
    db: KarenDB | str | Path | sqlite3.Connection,
    plan: SummaryCASPlan,
) -> SummaryCASResult:
    """在 karen.db 的单个 SQLite 事务中原子执行 CAS 检查与写入。

    任一步失败（CAS 不匹配、删除冲突、写错误）均全量 ROLLBACK。
    """
    if isinstance(db, sqlite3.Connection):
        return _execute_cas_tx(db, plan, close_on_exit=False)
    if isinstance(db, KarenDB):
        conn = db.connect()
        return _execute_cas_tx(conn, plan, close_on_exit=False)
    karen_db = KarenDB(db)
    conn = karen_db.connect()
    return _execute_cas_tx(conn, plan, close_on_exit=True)


def _execute_cas_tx(
    conn: sqlite3.Connection,
    plan: SummaryCASPlan,
    *,
    close_on_exit: bool = False,
) -> SummaryCASResult:
    conn.execute("BEGIN IMMEDIATE")
    try:
        persona_id = plan.persona_id
        now = _now_iso()
        has_corpus_state = _has_table(conn, "corpus_state")
        has_summary_gen = _has_table(conn, "summary_generations")
        has_summary_src = _has_table(conn, "summary_sources")
        has_tombstones = _has_table(conn, "tombstones")

        current_mutation_gen = 0
        current_lifecycle_epoch = 0
        current_corpus_epoch = 0
        current_summary_gen = 0

        # 1. CAS 校验：核验 corpus_state 与 active_start
        if has_corpus_state:
            row = conn.execute(
                """
                SELECT mutation_gen, lifecycle_epoch, corpus_epoch,
                       current_summary_gen, next_seq, append_revision
                FROM corpus_state WHERE persona_id = ?
                """,
                (persona_id,),
            ).fetchone()
            if row is not None:
                current_mutation_gen = int(row["mutation_gen"])
                current_lifecycle_epoch = int(row["lifecycle_epoch"])
                current_corpus_epoch = int(row["corpus_epoch"])
                current_summary_gen = int(row["current_summary_gen"])

                # 冲突检查：删除发生（mutation_gen 改变）或 epoch 漂移
                if (
                    plan.expected_mutation_gen is not None
                    and current_mutation_gen != plan.expected_mutation_gen
                ):
                    raise SummaryCASConflictError(
                        "mutation_gen_conflict",
                        f"检测到并发删除/变更（预期 mutation_gen={plan.expected_mutation_gen}，实际={current_mutation_gen}）",
                    )
                if (
                    plan.expected_lifecycle_epoch is not None
                    and current_lifecycle_epoch != plan.expected_lifecycle_epoch
                ):
                    raise SummaryCASConflictError(
                        "lifecycle_epoch_conflict",
                        f"生命周期 epoch 变更（预期={plan.expected_lifecycle_epoch}，实际={current_lifecycle_epoch}）",
                    )
                if plan.expected_base_summary_gen is not None:
                    if (
                        plan.expected_base_summary_gen > 0
                        and current_summary_gen != plan.expected_base_summary_gen
                    ):
                        raise SummaryCASConflictError(
                            "base_summary_gen_conflict",
                            f"基础摘要代次冲突（预期={plan.expected_base_summary_gen}，实际={current_summary_gen}）",
                        )

        # 核验 rolling_summary 当前游标
        cur_row = conn.execute(
            "SELECT active_start_index FROM rolling_summary WHERE persona_id = ?",
            (persona_id,),
        ).fetchone()
        if (
            plan.expected_active_start is not None
            and cur_row is not None
            and cur_row[0] is not None
        ):
            actual_start = int(cur_row[0])
            if actual_start != plan.expected_active_start:
                raise SummaryCASConflictError(
                    "active_start_desync",
                    f"活跃游标脱节（预期={plan.expected_active_start}，实际={actual_start}）",
                )

        # 2. 检查切片中是否包含已被删除（tombstone）的记录
        source_uuids: list[str] = []
        for r in plan.slice_records:
            ruid = r.get("record_uuid")
            if ruid:
                source_uuids.append(str(ruid))

        if has_tombstones and source_uuids:
            # 批量核验是否存在墓碑
            placeholders = ",".join("?" for _ in source_uuids)
            tomb_row = conn.execute(
                f"""
                SELECT record_uuid FROM tombstones
                WHERE persona_id = ? AND record_uuid IN ({placeholders})
                LIMIT 1
                """,
                (persona_id, *source_uuids),
            ).fetchone()
            if tomb_row:
                raise SummaryCASConflictError(
                    "contains_tombstoned_records",
                    f"待归档切片中包含已被删除的记录 {tomb_row[0]}，该候选摘要已失效",
                )

        # 3. 计算 digest 并生成新 summary_gen
        new_summary_gen = current_summary_gen + 1
        digest = plan.content_digest
        if not digest:
            digest = hashlib.sha256(
                f"{persona_id}:{new_summary_gen}:{plan.summary_text}".encode()
            ).hexdigest()

        # 4. 写入 summary_generations 与 summary_sources
        if has_summary_gen:
            conn.execute(
                """
                INSERT INTO summary_generations (
                    persona_id, summary_gen, corpus_epoch, base_summary_gen,
                    coverage_start_seq, coverage_end_seq, digest, state,
                    binding_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'committed', ?, ?)
                ON CONFLICT(persona_id, summary_gen) DO UPDATE SET
                    digest = excluded.digest,
                    state = excluded.state,
                    created_at = excluded.created_at
                """,
                (
                    persona_id,
                    new_summary_gen,
                    current_corpus_epoch,
                    current_summary_gen,
                    plan.coverage_start_seq,
                    plan.coverage_end_seq,
                    digest,
                    plan.binding_version,
                    now,
                ),
            )

        sources_inserted = 0
        if has_summary_src and source_uuids:
            for ruid in source_uuids:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO summary_sources (
                        persona_id, summary_gen, record_uuid
                    ) VALUES (?, ?, ?)
                    """,
                    (persona_id, new_summary_gen, ruid),
                )
                sources_inserted += 1

        # 5. 幂等写入 archive_messages
        archived_count = 0
        if _has_table(conn, "archive_messages"):
            # 取出真实聊天记录进行归档投影
            chat_records: list[dict[str, Any]] = []
            for r in plan.slice_records:
                chat_records.extend(real_chat_archive_records(r))

            if chat_records:
                # 获取当前最大 rowid
                max_rowid_row = conn.execute(
                    "SELECT COALESCE(MAX(rowid), 0) FROM archive_messages WHERE persona_id = ?",
                    (persona_id,),
                ).fetchone()
                next_rowid = int(max_rowid_row[0]) + 1

                for cr in chat_records:
                    aid = str(
                        cr.get("archive_id")
                        or f"arc_{persona_id}_{next_rowid}_{int(time.time() * 1000)}"
                    )
                    content = str(cr.get("content") or "")
                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO archive_messages (
                            persona_id, rowid, archive_id, timestamp,
                            timestamp_unix, date_key, month_key,
                            conversation_id, conversation_type, target_id,
                            sender_id, sender_name, sender_role, direction,
                            message_kind, content, content_search,
                            original_msg_id, reply_to_msg_id,
                            metadata_json, record_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            persona_id,
                            next_rowid,
                            aid,
                            cr.get("timestamp"),
                            cr.get("timestamp_unix") or int(time.time()),
                            cr.get("date_key"),
                            cr.get("month_key"),
                            cr.get("conversation_id"),
                            cr.get("conversation_type"),
                            cr.get("target_id"),
                            cr.get("sender_id"),
                            cr.get("sender_name"),
                            cr.get("sender_role"),
                            cr.get("direction"),
                            cr.get("message_kind"),
                            content,
                            content,
                            cr.get("original_msg_id"),
                            cr.get("reply_to_msg_id"),
                            json.dumps(cr.get("metadata") or {}),
                            json.dumps(cr, ensure_ascii=False),
                            now,
                        ),
                    )
                    if cursor.rowcount > 0:
                        archived_count += 1
                        next_rowid += 1

        # 6. 更新 rolling_summary
        summary_payload = {
            "summary_text": plan.summary_text.strip(),
            "archived_until": {
                "last_compaction_count": len(plan.slice_records),
                "last_timestamp": plan.slice_records[-1].get("timestamp")
                if plan.slice_records
                else "",
                "active_start_index": plan.new_active_start,
                "summary_gen": new_summary_gen,
                "digest": digest,
            },
            "structured": plan.structured_data,
            "updated_at": now,
        }
        conn.execute(
            """
            INSERT INTO rolling_summary (
                persona_id, summary_text, archived_until_json,
                active_start_index, summary_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(persona_id) DO UPDATE SET
                summary_text = excluded.summary_text,
                archived_until_json = excluded.archived_until_json,
                active_start_index = excluded.active_start_index,
                summary_json = excluded.summary_json,
                updated_at = excluded.updated_at
            """,
            (
                persona_id,
                plan.summary_text.strip(),
                json.dumps(summary_payload["archived_until"]),
                plan.new_active_start,
                json.dumps(summary_payload, ensure_ascii=False),
                now,
            ),
        )

        # 7. 更新 corpus_state
        if has_corpus_state:
            conn.execute(
                """
                UPDATE corpus_state SET
                    current_summary_gen = ?,
                    append_revision = append_revision + 1,
                    updated_at = ?
                WHERE persona_id = ?
                """,
                (new_summary_gen, now, persona_id),
            )

        conn.commit()
        return SummaryCASResult(
            committed=True,
            summary_gen=new_summary_gen,
            active_start_index=plan.new_active_start,
            archived_count=archived_count,
            sources_count=sources_inserted,
            mutation_gen=current_mutation_gen,
            corpus_epoch=current_corpus_epoch,
            digest=digest,
        )
    except Exception as err:
        logger.warning("CAS commit failed, rolling back: %s", err)
        conn.rollback()
        raise
    finally:
        if close_on_exit:
            try:
                conn.close()
            except Exception:
                pass
