"""History 分页读、SQL 计数与有界页缓存。"""

from __future__ import annotations

import sqlite3

import pytest

from memory.karen_stores import KarenHistoryStore


def _records(count: int) -> list[dict]:
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"record-{index:04d}",
            "conversation_id": "private:1",
        }
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_paged_reads_do_not_require_full_cache(tmp_path):
    store = KarenHistoryStore(tmp_path / "karen.db", "paging")
    await store.append_many(_records(300))

    assert store._cache is None
    assert await store.length() == 300

    suffix = await store.fetch_suffix(250)
    assert suffix.start_index == 250
    assert suffix.total == 300
    assert [record["content"] for record in suffix.records] == [
        f"record-{index:04d}" for index in range(250, 300)
    ]
    assert store._cache is None

    recent = await store.fetch_recent(10)
    assert [record["content"] for record in recent.records] == [
        f"record-{index:04d}" for index in range(290, 300)
    ]
    assert await store.get_slice(0, 3) == _records(3)
    assert await store.get_slice(-2) == _records(300)[-2:]

    stats = await store.stats()
    assert stats["record_count"] == 300
    assert stats["max_history_index"] == 299
    assert stats["full_cache_loaded"] is False
    assert stats["page_cache_entries"] >= 1


@pytest.mark.asyncio
async def test_page_cache_respects_byte_and_page_limits(tmp_path):
    store = KarenHistoryStore(
        tmp_path / "karen.db",
        "bounded",
        page_cache_max_bytes=64 * 1024,
        page_size=16,
        max_pages=2,
    )
    await store.append_many(_records(100))

    for start in range(0, 100, 10):
        await store.fetch_suffix(start)

    info = await store.page_cache_info()
    assert info["page_cache_entries"] <= 2
    assert info["page_cache_bytes"] <= 64 * 1024
    removed = await store.clear_page_cache()
    assert removed["entries"] >= 1
    assert (await store.page_cache_info())["page_cache_entries"] == 0


@pytest.mark.asyncio
async def test_streaming_iteration_and_invalid_rows(tmp_path):
    path = tmp_path / "karen.db"
    store = KarenHistoryStore(path, "stream")
    await store.append_many(_records(5))
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO history_records(
                persona_id, history_index, role, content_hash, content_length, record_json
            ) VALUES ('stream', 99, 'user', 'bad', 3, '{bad')
            """
        )
        conn.execute(
            """
            INSERT INTO history_records(
                persona_id, history_index, role, content_hash, content_length, record_json
            ) VALUES ('stream', 100, 'user', 'list', 2, '[]')
            """
        )

    assert await store.length() == 5
    stats = await store.stats()
    assert stats["raw_row_count"] == 7
    assert stats["invalid_row_count"] == 2
    streamed = [record["content"] async for record in store.iter_records(batch_size=2)]
    assert streamed == [f"record-{index:04d}" for index in range(5)]


@pytest.mark.asyncio
async def test_mutation_invalidates_page_cache_and_keeps_length_exact(tmp_path):
    store = KarenHistoryStore(tmp_path / "karen.db", "mutation")
    await store.append_many(_records(20))
    await store.fetch_suffix(10)
    before = await store.stats()
    assert before["page_cache_entries"] >= 1

    await store.append({"role": "user", "content": "tail"})
    after = await store.stats()
    assert await store.length() == 21
    assert after["page_cache_entries"] == 0
    assert after["revision"] == before["revision"] + 1
    recent = await store.fetch_recent(1)
    assert recent.records[0]["content"] == "tail"
