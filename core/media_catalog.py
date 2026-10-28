"""A01 / A02: 媒体资产目录、48小时保留期与来源权益（Origin Equity）。

核心契约（第二阶段接续方案 2.6 第 18 节）：
1. 区分 4 种媒体生命周期：
   - referenced retention expiry：入站媒体默认 48 小时保留期。
   - orphan GC：未被任何来源引用且无活跃租约的孤立文件 GC。
   - derived cache：可重建派生缓存（如缩略图/编码副本）。
   - explicit media delete：用户显式目标删除。
2. 来源权益（Origin Equity，E34）：
   - 同一 hash 新来源（origin）注册独立权益；
   - 共享底层文件字节，物理保留期取所有有效权益的最大保留期（effective_retention = max(retention_until)）；
   - 历史读取/查看绝不续期；
   - 已过期的旧 origin 绝不因新字节流入而复活（新流入只给新 origin 赋权）。
3. Ingest Reservation 机制：
   - reservation -> 写入文件 -> catalog 登记；
   - 处于 lease 有效期内的 reservation 受到强保护，防止重启或并发 GC 误删。
4. 保护策略：音频、用户自拥文件默认受保护；被 pin 的媒体永久受保护。
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memory.karen_db import KarenDB

logger = logging.getLogger(__name__)

# 默认保留期 48 小时
DEFAULT_MEDIA_RETENTION_HOURS = 48.0
# 默认入站租约 15 分钟
DEFAULT_INGEST_LEASE_SECONDS = 900.0


@dataclass(slots=True)
class MediaRecord:
    media_id: str
    sha256: str
    kind: str
    path: str
    size_bytes: int
    transform_version: str = "pillow-first-frame-max1536-v2"
    generation: int = 1
    created_at: str = ""


@dataclass(slots=True)
class OriginEquity:
    origin_id: str
    persona_id: str
    media_id: str
    admitted_at: float
    retention_until: float
    pinned: bool = False
    life_epoch: int = 0
    created_at: str = ""

    @property
    def is_expired(self) -> bool:
        return not self.pinned and time.time() > self.retention_until


@dataclass(slots=True)
class IngestReservation:
    reservation_id: str
    sha256: str
    path: str
    lease_until: float
    created_at: float

    @property
    def is_active(self) -> bool:
        return time.time() <= self.lease_until


@dataclass(slots=True)
class RetentionEvaluation:
    media_id: str
    sha256: str
    path: str
    size_bytes: int
    is_retained: bool
    is_pinned: bool
    is_leased: bool
    max_retention_until: float
    active_origins_count: int
    expired_origins_count: int
    decision: str  # "retain" | "expire" | "orphan"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class MediaCatalogManager:
    """管理媒体资产目录、来源权益与 Ingest 预留。"""

    def __init__(self, db: KarenDB | str | Path | sqlite3.Connection) -> None:
        self._db_or_conn = db
        self._ensure_schema()

    def _get_connection(self) -> sqlite3.Connection:
        if isinstance(self._db_or_conn, sqlite3.Connection):
            return self._db_or_conn
        if isinstance(self._db_or_conn, KarenDB):
            return self._db_or_conn.connect()
        karen_db = KarenDB(self._db_or_conn)
        return karen_db.connect()

    def _ensure_schema(self) -> None:
        conn = self._get_connection()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media_catalog (
                media_id TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                transform_version TEXT NOT NULL DEFAULT 'pillow-first-frame-max1536-v2',
                generation INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE(sha256, transform_version)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media_origins (
                origin_id TEXT NOT NULL,
                persona_id TEXT NOT NULL,
                media_id TEXT NOT NULL,
                admitted_at REAL NOT NULL,
                retention_until REAL NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0,
                life_epoch INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                PRIMARY KEY(persona_id, origin_id, media_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media_reservations (
                reservation_id TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                path TEXT NOT NULL,
                lease_until REAL NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_catalog_sha ON media_catalog(sha256)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_origins_media ON media_origins(media_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_origins_retention ON media_origins(retention_until)"
        )

    # ------------------------------------------------------------------
    # Ingest Reservation 租约机制
    # ------------------------------------------------------------------
    def create_reservation(
        self,
        sha256: str,
        path: str,
        *,
        lease_seconds: float = DEFAULT_INGEST_LEASE_SECONDS,
    ) -> IngestReservation:
        """创建入站写入租约，保护正在落盘或尚未关联消息的文件。"""
        now = time.time()
        res_id = f"res_{uuid.uuid4().hex}"
        lease_until = now + max(10.0, float(lease_seconds))
        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO media_reservations(reservation_id, sha256, path, lease_until, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (res_id, sha256, path, lease_until, now),
            )
        return IngestReservation(
            reservation_id=res_id,
            sha256=sha256,
            path=path,
            lease_until=lease_until,
            created_at=now,
        )

    def release_reservation(self, reservation_id: str) -> None:
        """完成入站登记或异常退出时释放租约。"""
        conn = self._get_connection()
        with conn:
            conn.execute(
                "DELETE FROM media_reservations WHERE reservation_id = ?",
                (reservation_id,),
            )

    def clean_expired_reservations(self, now: float | None = None) -> int:
        """清理已自然过期的悬挂租约。"""
        curr = now if now is not None else time.time()
        conn = self._get_connection()
        with conn:
            cur = conn.execute(
                "DELETE FROM media_reservations WHERE lease_until < ?",
                (curr,),
            )
            return cur.rowcount

    # ------------------------------------------------------------------
    # 来源权益（Origin Equity）管理
    # ------------------------------------------------------------------
    def admit_media(
        self,
        *,
        persona_id: str,
        origin_id: str,
        sha256: str,
        path: str,
        kind: str = "image",
        size_bytes: int = 0,
        retention_hours: float = DEFAULT_MEDIA_RETENTION_HOURS,
        pinned: bool = False,
        life_epoch: int = 0,
        transform_version: str = "pillow-first-frame-max1536-v2",
        reservation_id: str | None = None,
        admitted_at: float | None = None,
    ) -> MediaRecord:
        """登记媒体并赋予来源权益。

        同 hash 权益规则（E34）：
        - 如果底层文件已存在，保留原 media_id 和物理元数据；
        - 新增该 (persona_id, origin_id) 的独立权益记录，retention_until = admitted_at + retention_hours；
        - 若旧 origin 已过期，其自身状态依然保持过期，不会被“复活”；
        - 若有关联 reservation_id，在同事务内原子释放。
        """
        curr = admitted_at if admitted_at is not None else time.time()
        now_iso = _now_iso()
        retention_until = curr + max(0.0, float(retention_hours) * 3600.0)
        conn = self._get_connection()

        with conn:
            # 1. 查找或创建 catalog 记录
            row = conn.execute(
                "SELECT media_id, sha256, kind, path, size_bytes, transform_version, generation, created_at "
                "FROM media_catalog WHERE sha256 = ? AND transform_version = ?",
                (sha256, transform_version),
            ).fetchone()

            if row is not None:
                media_id = str(row["media_id"])
                media_rec = MediaRecord(
                    media_id=media_id,
                    sha256=str(row["sha256"]),
                    kind=str(row["kind"]),
                    path=str(row["path"]),
                    size_bytes=int(row["size_bytes"]),
                    transform_version=str(row["transform_version"]),
                    generation=int(row["generation"]),
                    created_at=str(row["created_at"]),
                )
            else:
                media_id = f"med_{uuid.uuid4().hex}"
                conn.execute(
                    """
                    INSERT INTO media_catalog(
                        media_id, sha256, kind, path, size_bytes,
                        transform_version, generation, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (media_id, sha256, kind, path, size_bytes, transform_version, now_iso),
                )
                media_rec = MediaRecord(
                    media_id=media_id,
                    sha256=sha256,
                    kind=kind,
                    path=path,
                    size_bytes=size_bytes,
                    transform_version=transform_version,
                    generation=1,
                    created_at=now_iso,
                )

            # 2. 插入或更新来源权益
            # 若同一 origin 重新登记，赋予新权益；其他已有 origin 权益不受侵扰
            conn.execute(
                """
                INSERT INTO media_origins(
                    origin_id, persona_id, media_id, admitted_at,
                    retention_until, pinned, life_epoch, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(persona_id, origin_id, media_id) DO UPDATE SET
                    admitted_at = excluded.admitted_at,
                    retention_until = excluded.retention_until,
                    pinned = excluded.pinned,
                    life_epoch = excluded.life_epoch
                """,
                (
                    origin_id,
                    persona_id,
                    media_id,
                    curr,
                    retention_until,
                    1 if pinned else 0,
                    life_epoch,
                    now_iso,
                ),
            )

            # 3. 若有预留租约，释放
            if reservation_id:
                conn.execute(
                    "DELETE FROM media_reservations WHERE reservation_id = ?",
                    (reservation_id,),
                )

        return media_rec

    def get_effective_retention(
        self,
        media_id: str,
        now: float | None = None,
    ) -> tuple[bool, float, bool]:
        """计算媒体的物理有效保留状态（E34）。

        返回：(is_retained, max_retention_until, is_pinned)
        只要存在任一来源 pinned 或 retention_until > now，物理文件即受保护。
        """
        curr = now if now is not None else time.time()
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT retention_until, pinned FROM media_origins WHERE media_id = ?",
            (media_id,),
        ).fetchall()

        if not rows:
            return False, 0.0, False

        is_pinned = any(bool(r["pinned"]) for r in rows)
        max_retention = max(float(r["retention_until"]) for r in rows)
        is_retained = is_pinned or (max_retention > curr)
        return is_retained, max_retention, is_pinned

    def touch_read(self, media_id: str) -> None:
        """历史阅读/查看。按契约规范：历史查看绝不延长保留期（No-op）。"""
        # 显式不更新 retention_until，保证契约诚实

    def set_pin(
        self,
        persona_id: str,
        origin_id: str,
        media_id: str,
        pinned: bool = True,
    ) -> bool:
        """显式锁定或解锁某个来源的保留权益。"""
        conn = self._get_connection()
        with conn:
            cur = conn.execute(
                """
                UPDATE media_origins SET pinned = ?
                WHERE persona_id = ? AND origin_id = ? AND media_id = ?
                """,
                (1 if pinned else 0, persona_id, origin_id, media_id),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 评定与 GC 决策（Evaluation）
    # ------------------------------------------------------------------
    def evaluate_all(
        self,
        now: float | None = None,
    ) -> list[RetentionEvaluation]:
        """评定所有 catalog 中的媒体条目。"""
        curr = now if now is not None else time.time()
        conn = self._get_connection()

        # 读取所有活跃租约中的 hash 与 path
        res_rows = conn.execute(
            "SELECT sha256, path FROM media_reservations WHERE lease_until >= ?",
            (curr,),
        ).fetchall()
        leased_hashes = {str(r["sha256"]) for r in res_rows}
        leased_paths = {str(r["path"]) for r in res_rows}

        # 读取所有 catalog 记录
        cat_rows = conn.execute(
            "SELECT media_id, sha256, kind, path, size_bytes FROM media_catalog"
        ).fetchall()

        # 预先批量读取所有 origins，避免 N+1 查询
        all_origins = conn.execute(
            "SELECT media_id, retention_until, pinned FROM media_origins"
        ).fetchall()
        origins_by_media: dict[str, list[Any]] = {}
        for o in all_origins:
            origins_by_media.setdefault(str(o["media_id"]), []).append(o)

        evaluations: list[RetentionEvaluation] = []
        for cat in cat_rows:
            mid = str(cat["media_id"])
            sha = str(cat["sha256"])
            path = str(cat["path"])
            size = int(cat["size_bytes"])

            is_leased = sha in leased_hashes or path in leased_paths
            origins = origins_by_media.get(mid, [])

            if not origins:
                # 没有任何来源引用的孤立项
                decision = "retain" if is_leased else "orphan"
                evaluations.append(
                    RetentionEvaluation(
                        media_id=mid,
                        sha256=sha,
                        path=path,
                        size_bytes=size,
                        is_retained=is_leased,
                        is_pinned=False,
                        is_leased=is_leased,
                        max_retention_until=0.0,
                        active_origins_count=0,
                        expired_origins_count=0,
                        decision=decision,
                    )
                )
                continue

            pinned = any(bool(r["pinned"]) for r in origins)
            max_ret = max(float(r["retention_until"]) for r in origins)
            active_count = sum(1 for r in origins if bool(r["pinned"]) or float(r["retention_until"]) > curr)
            expired_count = len(origins) - active_count

            is_retained = pinned or (max_ret > curr) or is_leased
            decision = "retain" if is_retained else "expire"

            evaluations.append(
                RetentionEvaluation(
                    media_id=mid,
                    sha256=sha,
                    path=path,
                    size_bytes=size,
                    is_retained=is_retained,
                    is_pinned=pinned,
                    is_leased=is_leased,
                    max_retention_until=max_ret,
                    active_origins_count=active_count,
                    expired_origins_count=expired_count,
                    decision=decision,
                )
            )

        return evaluations

    def delete_media_entry(self, media_id: str) -> bool:
        """从 catalog 及 origins 中移除元数据。"""
        conn = self._get_connection()
        with conn:
            conn.execute("DELETE FROM media_origins WHERE media_id = ?", (media_id,))
            cur = conn.execute("DELETE FROM media_catalog WHERE media_id = ?", (media_id,))
            return cur.rowcount > 0
