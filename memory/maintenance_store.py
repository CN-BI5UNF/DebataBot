"""实例级维护元数据存储。

只保存清理计划/任务、候选条目的路径与大小等运维元数据，不保存聊天正文、
密钥或 Provider 原始响应。路径以数据根为基准的相对路径保存，便于恢复时
重新做边界校验。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import orjson

MAINTENANCE_SCHEMA_VERSION = 1


class MaintenanceStoreError(RuntimeError):
    """维护存储的可恢复错误。"""


class MaintenanceConflict(MaintenanceStoreError):
    """计划状态不允许执行（重复执行/已过期/正在运行）。"""


class MaintenanceStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._ready = False
        self._init_lock = threading.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._ensure_schema_sync)

    async def create_plan(
        self,
        *,
        preview_id: str,
        created_at: float,
        expires_at: float,
        categories: list[str],
        items: list[dict[str, Any]],
        expected_bytes: int,
        expected_items: int,
        generation: str,
        token_hash: str,
    ) -> None:
        def _run() -> None:
            with closing(self._connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        """
                        INSERT INTO cleanup_plans (
                            preview_id, created_at, expires_at, categories_json,
                            items_json, expected_bytes, expected_items, generation,
                            token_hash, state
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready')
                        """,
                        (
                            preview_id,
                            float(created_at),
                            float(expires_at),
                            _dumps(categories),
                            _dumps(items),
                            int(expected_bytes),
                            int(expected_items),
                            generation,
                            token_hash,
                        ),
                    )
                except Exception:
                    conn.rollback()
                    raise
                else:
                    conn.commit()

        await asyncio.to_thread(_run)

    async def get_plan(self, preview_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_plan_sync, preview_id)

    def _get_plan_sync(self, preview_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM cleanup_plans WHERE preview_id = ?",
                (preview_id,),
            ).fetchone()
        return _row_dict(row) if row is not None else None

    async def create_job(
        self,
        *,
        job_id: str,
        preview_id: str,
        idempotency_key: str,
        categories: list[str],
    ) -> tuple[dict[str, Any], bool]:
        return await asyncio.to_thread(
            self._create_job_sync,
            job_id,
            preview_id,
            idempotency_key,
            categories,
        )

    def _create_job_sync(
        self,
        job_id: str,
        preview_id: str,
        idempotency_key: str,
        categories: list[str],
    ) -> tuple[dict[str, Any], bool]:
        now = time.time()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT * FROM cleanup_jobs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["preview_id"]) != preview_id
                        or list(_loads(existing["categories_json"], [])) != list(categories)
                    ):
                        raise MaintenanceConflict("idempotency_key_reused")
                    conn.commit()
                    return _row_dict(existing), False
                plan = conn.execute(
                    "SELECT * FROM cleanup_plans WHERE preview_id = ?",
                    (preview_id,),
                ).fetchone()
                if plan is None:
                    raise MaintenanceConflict("preview_not_found")
                if str(plan["state"]) != "ready":
                    raise MaintenanceConflict("preview_already_used")
                conn.execute(
                    """
                    INSERT INTO cleanup_jobs (
                        job_id, preview_id, idempotency_key, state,
                        categories_json, created_at
                    ) VALUES (?, ?, ?, 'queued', ?, ?)
                    """,
                    (job_id, preview_id, idempotency_key, _dumps(categories), now),
                )
                conn.execute(
                    """
                    UPDATE cleanup_plans
                    SET state = 'executing', job_id = ?
                    WHERE preview_id = ? AND state = 'ready'
                    """,
                    (job_id, preview_id),
                )
                job = conn.execute(
                    "SELECT * FROM cleanup_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
        return _row_dict(job), True

    async def update_job(
        self,
        job_id: str,
        *,
        state: str,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        started: bool = False,
        finished: bool = False,
    ) -> None:
        await asyncio.to_thread(
            self._update_job_sync,
            job_id,
            state,
            result,
            error_code,
            error_message,
            started,
            finished,
        )

    def _update_job_sync(
        self,
        job_id: str,
        state: str,
        result: dict[str, Any] | None,
        error_code: str | None,
        error_message: str | None,
        started: bool,
        finished: bool,
    ) -> None:
        now = time.time()
        fields = ["state = ?"]
        values: list[Any] = [state]
        if result is not None:
            fields.append("result_json = ?")
            values.append(_dumps(result))
        if error_code is not None:
            fields.append("error_code = ?")
            values.append(str(error_code))
        if error_message is not None:
            fields.append("error_message = ?")
            values.append(str(error_message)[:500])
        if started:
            fields.append("started_at = ?")
            values.append(now)
        if finished:
            fields.append("finished_at = ?")
            values.append(now)
        values.append(job_id)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    f"UPDATE cleanup_jobs SET {', '.join(fields)} WHERE job_id = ?",
                    values,
                )
                conn.execute(
                    """
                    UPDATE cleanup_plans
                    SET state = CASE WHEN ? IN ('completed','partial_success','failed','cancelled','interrupted')
                                     THEN 'finished' ELSE state END
                    WHERE job_id = ?
                    """,
                    (state, job_id),
                )
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_job_sync, job_id)

    def _get_job_sync(self, job_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM cleanup_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return _row_dict(row) if row is not None else None

    async def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_jobs_sync, max(1, min(100, int(limit))))

    def _list_jobs_sync(self, limit: int) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM cleanup_jobs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row_dict(row) for row in rows]

    async def mark_interrupted(self) -> int:
        return await asyncio.to_thread(self._mark_interrupted_sync)

    def _mark_interrupted_sync(self) -> int:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    """
                    UPDATE cleanup_jobs
                    SET state = 'interrupted', finished_at = ?,
                        error_code = 'process_restarted',
                        error_message = '维护任务在进程重启时中断'
                    WHERE state IN ('queued', 'running', 'cancelling')
                    """,
                    (time.time(),),
                )
                affected = int(cursor.rowcount or 0)
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
        return affected

    async def request_cancel(self, job_id: str) -> bool:
        return await asyncio.to_thread(self._request_cancel_sync, job_id)

    def _request_cancel_sync(self, job_id: str) -> bool:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    """
                    UPDATE cleanup_jobs
                    SET state = 'cancelling'
                    WHERE job_id = ? AND state IN ('queued', 'running')
                    """,
                    (job_id,),
                )
                changed = int(cursor.rowcount or 0) > 0
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
        return changed

    def _connect(self) -> sqlite3.Connection:
        if not self._ready:
            self._ensure_schema_sync()
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema_sync(self) -> None:
        if self._ready:
            return
        with self._init_lock:
            if self._ready:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=30)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cleanup_plans (
                        preview_id TEXT PRIMARY KEY,
                        created_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        categories_json TEXT NOT NULL,
                        items_json TEXT NOT NULL,
                        expected_bytes INTEGER NOT NULL,
                        expected_items INTEGER NOT NULL,
                        generation TEXT NOT NULL,
                        token_hash TEXT NOT NULL,
                        state TEXT NOT NULL,
                        job_id TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cleanup_jobs (
                        job_id TEXT PRIMARY KEY,
                        preview_id TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        state TEXT NOT NULL,
                        categories_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        started_at REAL,
                        finished_at REAL,
                        result_json TEXT,
                        error_code TEXT,
                        error_message TEXT
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cleanup_jobs_created "
                    "ON cleanup_jobs(created_at DESC)"
                )
                conn.execute(
                    f"PRAGMA user_version = {MAINTENANCE_SCHEMA_VERSION}"
                )
                conn.commit()
            self._ready = True


def _dumps(value: Any) -> str:
    return orjson.dumps(value).decode("utf-8")


def _loads(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return orjson.loads(value)
    except (orjson.JSONDecodeError, TypeError):
        return default


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["categories"] = _loads(data.pop("categories_json", None), [])
    data["items"] = _loads(data.pop("items_json", None), [])
    result_json = data.pop("result_json", None)
    data["result"] = _loads(result_json, None) if result_json else None
    data["token_hash"] = data.pop("token_hash", "")
    return data


def token_fingerprint(token: str, *, preview_id: str) -> str:
    """计划 token 的不可逆指纹，避免把明文 token 写进维护库。"""
    digest = hashlib.sha256()
    digest.update(preview_id.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(token.encode("utf-8"))
    return digest.hexdigest()


def safe_unlink(path: Path, root: Path) -> tuple[bool, str]:
    """仅删除 root 内、非符号链接的普通文件；返回 (deleted, status)。"""
    try:
        root_resolved = root.resolve()
        target = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return False, "missing"
    try:
        target.relative_to(root_resolved)
    except ValueError:
        return False, "outside_root"
    try:
        os.lstat(path)
    except OSError:
        return False, "missing"
    if not os.path.isfile(path) or os.path.islink(path):
        return False, "not_regular_file"
    try:
        path.unlink()
    except FileNotFoundError:
        return False, "missing"
    except OSError as exc:
        return False, f"permission_denied:{type(exc).__name__}"
    return True, "deleted"
