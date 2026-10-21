"""Overview usage API：主缓存比例必须 V2-only，缺失数据为 null。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from utils.usage_summary import UsageSummary, add_record_to_summary
from webui.api.overview import _usage_dict


def _summary(*records) -> UsageSummary:
    summary = UsageSummary()
    for record in records:
        add_record_to_summary(summary, record)
    return summary


def _rt(summary: UsageSummary):
    return SimpleNamespace(
        usage_stats=SimpleNamespace(summarize=lambda _range: summary)
    )


def test_overview_usage_mixed_history_uses_v2_ratio_only():
    summary = _summary(
        {"prompt_tokens": 100, "cached_tokens": 70, "total_tokens": 120},
        {
            "prompt_tokens": 185,
            "cached_tokens": 70,
            "cache_creation_tokens": 15,
            "total_tokens": 205,
            "usage_semantics_version": 2,
        },
        {
            "prompt_tokens": 50,
            "cached_tokens": 20,
            "total_tokens": 60,
            "usage_semantics_version": 3,
        },
    )

    data = _usage_dict(_rt(summary), "all")

    assert data["available"] is True
    assert data["prompt_tokens"] == 335
    assert data["current_prompt_tokens"] == 185
    assert data["current_cached_tokens"] == 70
    assert data["current_cache_hit_rate"] == 70 / 185
    assert data["cache_hit_rate"] == 70 / 185
    assert data["legacy_cache_hit_rate"] is None
    assert data["legacy_request_count"] == 1
    assert data["legacy_prompt_tokens"] == 100
    assert data["unknown_semantics_request_count"] == 1
    assert data["current_semantics_available"] is True


def test_overview_usage_all_legacy_has_null_rate_not_zero():
    summary = _summary(
        {"prompt_tokens": 100, "cached_tokens": 70, "total_tokens": 120}
    )
    data = _usage_dict(_rt(summary), "all")

    assert data["current_semantics_request_count"] == 0
    assert data["current_semantics_available"] is False
    assert data["cache_hit_rate"] is None
    assert data["current_cache_hit_rate"] is None
    assert data["uncached_prompt_tokens"] is None


def test_overview_usage_real_zero_v2_rate_stays_zero():
    summary = _summary(
        {
            "prompt_tokens": 100,
            "cached_tokens": 0,
            "total_tokens": 110,
            "usage_semantics_version": 2,
        }
    )
    data = _usage_dict(_rt(summary), "all")
    assert data["cache_hit_rate"] == 0.0
    assert data["current_cache_hit_rate"] == 0.0
    assert data["uncached_prompt_tokens"] == 100


def test_overview_js_uses_current_fields_and_preserves_null():
    source = (
        Path(__file__).resolve().parents[2]
        / "webui/static/js/pages/overview.js"
    ).read_text(encoding="utf-8")

    assert "Number(usage.cache_hit_rate || 0)" not in source
    assert "current_semantics_request_count" in source
    assert "current_cache_hit_rate" in source
    assert "current_prompt_tokens" in source
    assert "unknown_semantics_request_count" in source
    assert "legacy_prompt_tokens" in source
    assert "V2 缓存命中率" in source
