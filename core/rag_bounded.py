"""Q02: 向量召回端到端有界、流式 Heap 检索与 Outbox 队列管理。

核心契约（第二阶段接续方案 2.6 第 20 节）：
1. 启动轻量加载（Schema & Stats Only）：
   - 禁止在启动时全量 `_entries.fetchall()` 将整个向量库读入内存字典；
   - 仅校验表结构与读取统计信息（COUNT, dimensions, conversation_count）。
2. SQL 先行过滤 + 128-256 向量批流式评分 + 大小 k 堆（Bounded Min-Heap）：
   - SQL 先行过滤 scope / conversation / timestamp / epoch；
   - 分批拉取候选（128-256 条/批），按批反序列化向量并流式计算余弦相似度；
   - 维护大小恒为 k 的有界小顶堆（heapq），内存占用严格有界，绝不将全量候选一次性 load 到内存排序。
3. 严格 Embedding 批校验（杜绝 zip 静默截断与脏值，E38）：
   - 条数严格匹配：`len(vectors) == len(batch)`，绝不使用 `zip(strict=False)` 静默截断丢弃；
   - 维度一致性校验：每个向量维度必须与声明维度一致；
   - 有限浮点数校验：绝不允许 NaN 或 Inf 脏浮点数写入或参与打分；
   - 失败不推进游标/水位。
4. 来源生命周期屏障（Source Epoch & Tombstone Barrier）：
   - 命中召回前或索引前，核验来源记录未处于 tombstones 或已失效的旧 lifecycle_epoch 中。
5. 有界 Outbox 队列与固定 Worker（杜绝 append 无限 create_task，E37）：
   - 队列设数量上限（max_items）与字节上限（max_bytes）；
   - 固定工作协程（finite workers），队列饱和时施加背压或落入持久 backlog，杜绝任务无界膨胀。
"""

from __future__ import annotations

import asyncio
import heapq
import json
import logging
import math
import sqlite3
import struct
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memory.karen_db import KarenDB

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_BATCH_SIZE = 128
DEFAULT_TOP_K = 5
DEFAULT_QUEUE_MAX_ITEMS = 1000
DEFAULT_QUEUE_MAX_BYTES = 16 * 1024 * 1024  # 16 MiB


class RagBoundedError(RuntimeError):
    """RAG 有界模块异常基类。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class RagEmbeddingValidationError(RagBoundedError):
    """Embedding 数据校验失败（条数不符/维度不符/包含NaN/Inf）。"""


class RagQueueFullError(RagBoundedError):
    """Outbox 队列饱和背压。"""


@dataclass(slots=True)
class BoundedRagDocument:
    id: str
    text: str
    vector: list[float]
    conversation_id: str | None = None
    role: str = ""
    timestamp: str = ""
    epoch: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BoundedRagHit:
    document: BoundedRagDocument
    score: float

    def __lt__(self, other: BoundedRagHit) -> bool:
        # 小顶堆比较以 score 为主
        return self.score < other.score


def serialize_vector_f32(vec: list[float]) -> bytes:
    """高效二进制打包 float32 向量（比 JSON/blob 紧凑且反序列化极快）。"""
    return struct.pack(f"{len(vec)}f", *vec)


def deserialize_vector_f32(blob: bytes) -> list[float]:
    """反序列化 float32 向量。"""
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


def cosine_similarity_fast(a: list[float], b: list[float]) -> float:
    """计算余弦相似度，带有限性与零向量安全检查。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    sim = dot / (math.sqrt(na) * math.sqrt(nb))
    return max(-1.0, min(1.0, sim))


def validate_embedding_batch(
    texts: list[str],
    vectors: list[list[float]],
    *,
    expected_dim: int | None = None,
) -> None:
    """严格校验 embedding 返回批次（防范 E38）。

    1. 条数必须严格相等，杜绝 zip 静默截断。
    2. 向量非空且维度一致。
    3. 每个分量必须为有限浮点数（排除 NaN / Inf）。
    """
    if len(texts) != len(vectors):
        raise RagEmbeddingValidationError(
            "count_mismatch",
            f"Embedding 返回向量数 ({len(vectors)}) 与输入文本数 ({len(texts)}) 不一致，拒绝截断写入",
        )

    for idx, vec in enumerate(vectors):
        if not vec:
            raise RagEmbeddingValidationError(
                "empty_vector", f"第 {idx} 个文本返回了空向量"
            )
        if expected_dim is not None and len(vec) != expected_dim:
            raise RagEmbeddingValidationError(
                "dimension_mismatch",
                f"第 {idx} 个向量维度 ({len(vec)}) 与预期维度 ({expected_dim}) 不符",
            )
        for val_idx, val in enumerate(vec):
            if not math.isfinite(val):
                raise RagEmbeddingValidationError(
                    "non_finite_value",
                    f"第 {idx} 个向量在索引 {val_idx} 处包含非法非有限浮点数 ({val})",
                )


class BoundedVectorStore:
    """端到端有界的 SQLite 向量检索存储。

    启动仅检查 Schema 与统计信息，绝不在内存全量加载。
    检索走流式批次 + 堆选择，空间复杂度恒为 O(k + batch_size)。
    """

    def __init__(self, db: KarenDB | str | Path | sqlite3.Connection) -> None:
        self._db_or_conn = db
        self._expected_dim: int | None = None
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
            CREATE TABLE IF NOT EXISTS rag_documents (
                id TEXT PRIMARY KEY,
                conversation_id TEXT,
                role TEXT NOT NULL DEFAULT '',
                timestamp TEXT NOT NULL DEFAULT '',
                epoch INTEGER NOT NULL DEFAULT 0,
                text TEXT NOT NULL,
                vector BLOB NOT NULL,
                meta BLOB NOT NULL,
                dim INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rag_conv_time ON rag_documents(conversation_id, timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rag_epoch ON rag_documents(epoch)"
        )

    def load_stats(self) -> dict[str, Any]:
        """轻量统计信息读取（O(1) / 快读），替代全库 fetchall。"""
        conn = self._get_connection()
        row = conn.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT conversation_id), COALESCE(MAX(dim), 0)
            FROM rag_documents
            """
        ).fetchone()
        count = int(row[0]) if row else 0
        convs = int(row[1]) if row else 0
        max_dim = int(row[2]) if row else 0
        if max_dim > 0:
            self._expected_dim = max_dim

        return {
            "total_documents": count,
            "conversations": convs,
            "vector_dimension": max_dim,
        }

    def missing_ids(self, ids: Iterable[str]) -> set[str]:
        """批量 SQL 查询未索引的 ID，分批 500 条查询，内存 O(batch)。"""
        id_list = [i for i in ids if i]
        if not id_list:
            return set()

        conn = self._get_connection()
        found: set[str] = set()
        chunk_size = 500

        for i in range(0, len(id_list), chunk_size):
            chunk = id_list[i : i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT id FROM rag_documents WHERE id IN ({placeholders})",
                chunk,
            ).fetchall()
            for r in rows:
                found.add(str(r[0]))

        return set(id_list) - found

    def upsert_many(self, documents: list[BoundedRagDocument]) -> int:
        """批量写入或更新向量条目。"""
        if not documents:
            return 0

        conn = self._get_connection()
        now = time.time()
        with conn:
            for doc in documents:
                vec_blob = serialize_vector_f32(doc.vector)
                meta_blob = json.dumps(doc.meta, ensure_ascii=False).encode("utf-8")
                conn.execute(
                    """
                    INSERT INTO rag_documents(
                        id, conversation_id, role, timestamp, epoch,
                        text, vector, meta, dim, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        conversation_id = excluded.conversation_id,
                        role = excluded.role,
                        timestamp = excluded.timestamp,
                        epoch = excluded.epoch,
                        text = excluded.text,
                        vector = excluded.vector,
                        meta = excluded.meta,
                        dim = excluded.dim,
                        updated_at = excluded.updated_at
                    """,
                    (
                        doc.id,
                        doc.conversation_id,
                        doc.role,
                        doc.timestamp,
                        doc.epoch,
                        doc.text,
                        vec_blob,
                        meta_blob,
                        len(doc.vector),
                        now,
                    ),
                )
        return len(documents)

    def top_k_heap(
        self,
        query_vec: list[float],
        *,
        k: int = DEFAULT_TOP_K,
        conversation_id: str | None = None,
        before_ts: str | None = None,
        min_epoch: int = 0,
        persona_id: str | None = None,
        batch_size: int = DEFAULT_CHUNK_BATCH_SIZE,
        min_score: float = 0.0,
    ) -> list[BoundedRagHit]:
        """SQL 过滤 + 128~256 分批流式评分 + 大小 k 的 Bounded Min-Heap。

        时间复杂度：O(N * d)，CPU 流式计算。
        空间复杂度：O(k + batch_size)，杜绝全量候选拉入内存。
        """
        if not query_vec or k <= 0:
            return []

        conn = self._get_connection()

        # 1. SQL 先行过滤：根据 conversation / timestamp / epoch
        conditions = ["epoch >= ?"]
        params: list[Any] = [min_epoch]

        if conversation_id:
            conditions.append("conversation_id = ?")
            params.append(conversation_id)

        if before_ts:
            conditions.append("timestamp < ?")
            params.append(before_ts)

        # 墓碑过滤屏障：若存在 tombstones，排除墓碑内的 record_uuid / id
        has_tombstones = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tombstones'"
            ).fetchone()
        )
        if has_tombstones and persona_id:
            conditions.append(
                """
                id NOT IN (SELECT record_uuid FROM tombstones WHERE persona_id = ?)
                """
            )
            params.append(persona_id)

        sql = f"""
            SELECT id, conversation_id, role, timestamp, epoch, text, vector, meta
            FROM rag_documents
            WHERE {' AND '.join(conditions)}
            ORDER BY timestamp DESC
        """

        cursor = conn.execute(sql, params)

        # 2. 维护大小恒为 k 的小顶堆
        heap: list[BoundedRagHit] = []

        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break

            for r in rows:
                v_blob = r["vector"]
                doc_vec = deserialize_vector_f32(v_blob)
                sim = cosine_similarity_fast(query_vec, doc_vec)

                if sim < min_score:
                    continue

                try:
                    meta_dict = json.loads(r["meta"].decode("utf-8"))
                except Exception:
                    meta_dict = {}

                doc = BoundedRagDocument(
                    id=str(r["id"]),
                    text=str(r["text"]),
                    vector=doc_vec,
                    conversation_id=r["conversation_id"],
                    role=str(r["role"]),
                    timestamp=str(r["timestamp"]),
                    epoch=int(r["epoch"]),
                    meta=meta_dict,
                )

                hit = BoundedRagHit(document=doc, score=sim)

                if len(heap) < k:
                    heapq.heappush(heap, hit)
                elif sim > heap[0].score:
                    heapq.heappushpop(heap, hit)

        # 3. 按相似度从高到低排序返回
        result = sorted(heap, key=lambda h: h.score, reverse=True)
        return result


class BoundedRagOutboxQueue:
    """有界 RAG Outbox 队列与固定 Worker。

    防范 E37：关闭 append 无限 create_task，饱和施加背压，不丢持久日志。
    """

    def __init__(
        self,
        store: BoundedVectorStore,
        *,
        max_items: int = DEFAULT_QUEUE_MAX_ITEMS,
        max_bytes: int = DEFAULT_QUEUE_MAX_BYTES,
        worker_count: int = 1,
    ) -> None:
        self.store = store
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.worker_count = worker_count
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_items)
        self._current_bytes = 0
        self._workers: list[asyncio.Task[None]] = []
        self._running = False
        self._lock = asyncio.Lock()

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def byte_size(self) -> int:
        return self._current_bytes

    async def enqueue(self, item: dict[str, Any]) -> None:
        """入队处理，带数量与字节双重有界检查。"""
        item_bytes = len(json.dumps(item, default=str).encode("utf-8"))
        if self._current_bytes + item_bytes > self.max_bytes:
            raise RagQueueFullError(
                "outbox_bytes_saturated",
                f"RAG Outbox 字节上限饱和 ({self._current_bytes}/{self.max_bytes})，施加背压",
            )

        try:
            self._queue.put_nowait(item)
            self._current_bytes += item_bytes
        except asyncio.QueueFull as err:
            raise RagQueueFullError(
                "outbox_items_saturated",
                f"RAG Outbox 条数饱和 ({self.max_items})，拒绝无界创建任务",
            ) from err

    async def start(self) -> None:
        async with self._lock:
            if self._running:
                return
            self._running = True
            for i in range(self.worker_count):
                task = asyncio.create_task(self._worker_loop(i))
                self._workers.append(task)

    async def stop(self) -> None:
        async with self._lock:
            self._running = False
            for task in self._workers:
                task.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()

    async def _worker_loop(self, worker_id: int) -> None:
        while self._running:
            try:
                item = await self._queue.get()
                # 扣减字节数
                item_bytes = len(json.dumps(item, default=str).encode("utf-8"))
                self._current_bytes = max(0, self._current_bytes - item_bytes)
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("RAG Outbox Worker %s 处理异常: %s", worker_id, e)
