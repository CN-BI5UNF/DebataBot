"""T21 / Q02: RAG 端到端有界、流式 Heap 检索与 Outbox 队列测试。

验证项：
1. 启动轻量 Schema/Stats 加载（非全量 fetchall）。
2. Embedding 严格批校验（条数/维度/NaN/Inf 校验）。
3. SQL 先行过滤 + 128~256 分批流式评分 + 大小 k 堆（Bounded Min-Heap）。
4. 来源墓碑（Tombstones）即时读屏障过滤。
5. Outbox 队列数量与字节饱和背压（E37）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.d01 import migrate_database
from core.rag_bounded import (
    BoundedRagDocument,
    BoundedRagOutboxQueue,
    BoundedVectorStore,
    RagEmbeddingValidationError,
    RagQueueFullError,
    validate_embedding_batch,
)
from memory.karen_db import KarenDB


@pytest.fixture
def rag_conn(tmp_path: Path) -> sqlite3.Connection:
    db_file = tmp_path / "karen_rag_test.db"
    db = KarenDB(db_file)
    db.load()
    migrate_database(db_file)
    return db.connect()


def test_embedding_batch_strict_validation() -> None:
    texts = ["apple", "banana", "cherry"]

    # 1. 条数不匹配：输入 3 条，返回 2 条向量
    vectors_short = [[0.1, 0.2], [0.3, 0.4]]
    with pytest.raises(RagEmbeddingValidationError) as exc1:
        validate_embedding_batch(texts, vectors_short, expected_dim=2)
    assert exc1.value.code == "count_mismatch"

    # 2. 维度不匹配：预期 3 维，实际 2 维
    vectors_wrong_dim = [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
    with pytest.raises(RagEmbeddingValidationError) as exc2:
        validate_embedding_batch(texts, vectors_wrong_dim, expected_dim=3)
    assert exc2.value.code == "dimension_mismatch"

    # 3. 包含 NaN 脏浮点数
    vectors_nan = [[0.1, 0.2], [float("nan"), 0.4], [0.5, 0.6]]
    with pytest.raises(RagEmbeddingValidationError) as exc3:
        validate_embedding_batch(texts, vectors_nan, expected_dim=2)
    assert exc3.value.code == "non_finite_value"

    # 4. 包含 Inf
    vectors_inf = [[0.1, 0.2], [float("inf"), 0.4], [0.5, 0.6]]
    with pytest.raises(RagEmbeddingValidationError) as exc4:
        validate_embedding_batch(texts, vectors_inf, expected_dim=2)
    assert exc4.value.code == "non_finite_value"

    # 5. 正常批次校验通过
    vectors_valid = [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
    validate_embedding_batch(texts, vectors_valid, expected_dim=2)


def test_vector_store_load_stats_and_missing_ids(rag_conn: sqlite3.Connection) -> None:
    store = BoundedVectorStore(rag_conn)
    stats0 = store.load_stats()
    assert stats0["total_documents"] == 0

    docs = [
        BoundedRagDocument(
            id=f"doc_{i}",
            text=f"text {i}",
            vector=[float(i), 1.0],
            conversation_id="conv_1",
            timestamp=f"2026-10-28T10:{i:02d}:00Z",
        )
        for i in range(10)
    ]
    store.upsert_many(docs)

    stats1 = store.load_stats()
    assert stats1["total_documents"] == 10
    assert stats1["conversations"] == 1
    assert stats1["vector_dimension"] == 2

    # 查询 missing_ids
    check_ids = [f"doc_{i}" for i in range(8, 15)]  # 8,9 存在；10..14 不存在
    missing = store.missing_ids(check_ids)
    assert missing == {f"doc_{i}" for i in range(10, 15)}


def test_bounded_top_k_heap_and_tombstone_barrier(rag_conn: sqlite3.Connection) -> None:
    store = BoundedVectorStore(rag_conn)

    # 插入多条具有不同相关度的文档
    # query_vec = [1.0, 0.0]
    docs = [
        BoundedRagDocument(id="doc_low", text="irrelevant", vector=[0.0, 1.0], conversation_id="conv_1", timestamp="2026-10-28T10:00:00Z"),
        BoundedRagDocument(id="doc_mid", text="partially relevant", vector=[0.5, 0.5], conversation_id="conv_1", timestamp="2026-10-28T10:01:00Z"),
        BoundedRagDocument(id="doc_high", text="highly relevant", vector=[0.9, 0.1], conversation_id="conv_1", timestamp="2026-10-28T10:02:00Z"),
        BoundedRagDocument(id="doc_exact", text="exact match", vector=[1.0, 0.0], conversation_id="conv_1", timestamp="2026-10-28T10:03:00Z"),
        BoundedRagDocument(id="doc_other_conv", text="exact but other conv", vector=[1.0, 0.0], conversation_id="conv_2", timestamp="2026-10-28T10:04:00Z"),
    ]
    store.upsert_many(docs)

    query = [1.0, 0.0]

    # 1. 在 conv_1 中检索 top-2
    hits = store.top_k_heap(query, k=2, conversation_id="conv_1")
    assert len(hits) == 2
    assert hits[0].document.id == "doc_exact"
    assert hits[1].document.id == "doc_high"
    assert hits[0].score >= hits[1].score

    # 2. 墓碑屏障：将 doc_exact 加入 tombstones 表
    rag_conn.execute(
        """
        INSERT INTO tombstones(tombstone_id, persona_id, record_uuid, life_epoch, mutation_gen, reason, created_at)
        VALUES ('t_doc_exact', 'persona_a', 'doc_exact', 1, 1, 'deleted', '2026-10-28T10:05:00Z')
        """
    )
    rag_conn.commit()

    # 带 persona_id 检索，已墓碑化的 doc_exact 必须被屏障剔除
    hits_after_tomb = store.top_k_heap(query, k=2, conversation_id="conv_1", persona_id="persona_a")
    assert len(hits_after_tomb) == 2
    assert hits_after_tomb[0].document.id == "doc_high"
    assert hits_after_tomb[1].document.id == "doc_mid"


@pytest.mark.asyncio
async def test_rag_outbox_queue_backpressure(rag_conn: sqlite3.Connection) -> None:
    store = BoundedVectorStore(rag_conn)
    # 设置极小队列容量：max_items=2, max_bytes=500
    queue = BoundedRagOutboxQueue(store, max_items=2, max_bytes=500, worker_count=1)

    # 1. 成功入队 2 条
    await queue.enqueue({"id": "1", "content": "short"})
    await queue.enqueue({"id": "2", "content": "short"})
    assert queue.queue_size == 2

    # 2. 第 3 条触发条数饱和背压
    with pytest.raises(RagQueueFullError) as exc_items:
        await queue.enqueue({"id": "3", "content": "short"})
    assert exc_items.value.code == "outbox_items_saturated"

    # 3. 超大字节 payload 触发字节饱和背压
    queue_byte_limited = BoundedRagOutboxQueue(store, max_items=10, max_bytes=100)
    with pytest.raises(RagQueueFullError) as exc_bytes:
        await queue_byte_limited.enqueue({"id": "big", "data": "x" * 200})
    assert exc_bytes.value.code == "outbox_bytes_saturated"
