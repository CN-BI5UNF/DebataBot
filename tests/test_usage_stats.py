from __future__ import annotations

import time

import pytest

from core.usage_stats import UsageStatsStore
from providers.base import Usage


@pytest.mark.asyncio
async def test_usage_stats_records_and_summarizes_ranges(tmp_path, monkeypatch):
    now = time.mktime((2026, 6, 4, 12, 0, 0, 3, 155, -1))
    monkeypatch.setattr(time, "time", lambda: now)

    store = UsageStatsStore(tmp_path / "usage.jsonl")
    await store.load()
    await store.record(
        Usage(
            prompt_tokens=100,
            completion_tokens=20,
            reasoning_tokens=5,
            cached_tokens=80,
            cache_creation_tokens=10,
            total_tokens=120,
        ),
        provider="deepseek_main",
        model="deepseek-chat",
        agent="主模型",
        operation="agent_loop",
    )

    summary = store.summarize("today")

    assert summary.request_count == 1
    assert summary.prompt_tokens == 100
    assert summary.completion_tokens == 20
    assert summary.reasoning_tokens == 5
    assert summary.cached_tokens == 80
    assert summary.cache_creation_tokens == 10
    assert summary.total_tokens == 120
    assert summary.cache_hit_rate == 0.8

    reloaded = UsageStatsStore(tmp_path / "usage.jsonl")
    await reloaded.load()
    assert reloaded.summarize("all").request_count == 1


@pytest.mark.asyncio
async def test_usage_stats_records_extra_kv_diagnostics(tmp_path):
    store = UsageStatsStore(tmp_path / "usage.jsonl")
    await store.load()

    await store.record(
        Usage(prompt_tokens=10, completion_tokens=1, total_tokens=11),
        provider="deepseek_main",
        model="deepseek-chat",
        agent="主模型",
        operation="agent_loop",
        extra={"kv_message_count": 3, "kv_prefix_8k_hash": "abc123"},
    )

    text = (tmp_path / "usage.jsonl").read_text(encoding="utf-8")
    assert '"kv_message_count":3' in text
    assert '"kv_prefix_8k_hash":"abc123"' in text


@pytest.mark.asyncio
async def test_usage_stats_operation_breakdown_includes_auxiliary_tokens(tmp_path):
    store = UsageStatsStore(tmp_path / "usage.jsonl")
    await store.load()
    await store.record(
        Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
        operation="chat",
    )
    await store.record(
        Usage(prompt_tokens=50, completion_tokens=10, total_tokens=60),
        operation="web_search_official",
    )
    await store.record(
        Usage(prompt_tokens=30, total_tokens=30),
        operation="embedding",
    )

    rows = store.operation_breakdown("all")
    by_operation = {row["operation"]: row for row in rows}
    assert by_operation["web_search_official"]["current_prompt_tokens"] == 50
    assert by_operation["web_search_official"]["current_total_tokens"] == 60
    assert by_operation["embedding"]["current_total_tokens"] == 30
    assert by_operation["chat"]["current_total_tokens"] == 120


@pytest.mark.asyncio
async def test_usage_stats_store_separates_legacy_v2_and_future(tmp_path):
    store = UsageStatsStore(tmp_path / "usage.jsonl")
    await store.load()
    await store.record(
        Usage(prompt_tokens=100, cached_tokens=70, total_tokens=120),
        extra={"usage_semantics_version": 1},
    )
    await store.record(
        Usage(prompt_tokens=185, cached_tokens=70, total_tokens=205)
    )
    await store.record(
        Usage(prompt_tokens=50, cached_tokens=20, total_tokens=60),
        extra={"usage_semantics_version": 3},
    )

    summary = store.summarize("all")
    assert summary.current_semantics_request_count == 1
    assert summary.legacy_request_count == 1
    assert summary.unknown_semantics_request_count == 1
    assert summary.current_prompt_tokens == 185
    assert summary.cache_hit_rate == 70 / 185
