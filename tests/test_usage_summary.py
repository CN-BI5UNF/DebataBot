"""用量聚合与估算误差测试。"""

from __future__ import annotations

from utils.usage_summary import UsageSummary, add_record_to_summary


def test_summary_clamps_cached_tokens_and_exposes_uncached():
    summary = UsageSummary()
    add_record_to_summary(
        summary,
        {
            "prompt_tokens": 100,
            "cached_tokens": 130,
            "cache_creation_tokens": 200,
            "completion_tokens": 10,
            "total_tokens": 110,
            "usage_semantics_version": 2,
        },
    )
    assert summary.prompt_tokens == 100
    assert summary.cached_tokens == 100
    assert summary.cache_creation_tokens == 100
    assert summary.uncached_prompt_tokens == 0
    assert summary.cache_hit_rate == 1.0
    assert summary.raw_cache_inconsistent_count == 1


def test_summary_collects_estimate_error_only_for_samples():
    summary = UsageSummary()
    add_record_to_summary(
        summary,
        {
            "prompt_tokens": 1000,
            "estimated_prompt_tokens": 900,
            "completion_tokens": 50,
        },
    )
    add_record_to_summary(
        summary,
        {
            "prompt_tokens": 500,
            "completion_tokens": 20,
        },
    )
    assert summary.estimate_samples == 1
    assert summary.estimated_prompt_tokens == 900
    assert summary.estimate_actual_prompt_tokens == 1000
    assert summary.estimate_abs_error_tokens == 100
    assert abs(summary.estimate_error_rate - 0.1) < 1e-9


def test_empty_summary_has_no_current_cache_rate():
    summary = UsageSummary()
    assert summary.uncached_prompt_tokens is None
    assert summary.cache_hit_rate is None
    assert summary.current_cache_hit_rate is None
    assert summary.has_current_semantics_data is False
    assert summary.estimate_error_rate == 0.0


def test_summary_distinguishes_legacy_and_current_usage_semantics():
    from utils.usage_summary import UsageSummary, add_record_to_summary

    summary = UsageSummary()
    add_record_to_summary(
        summary,
        {"prompt_tokens": 100, "cached_tokens": 70, "total_tokens": 120},
    )
    add_record_to_summary(
        summary,
        {
            "prompt_tokens": 185,
            "cached_tokens": 70,
            "cache_creation_tokens": 15,
            "cache_miss_tokens": 40,
            "total_tokens": 205,
            "usage_semantics_version": 2,
        },
    )

    assert summary.request_count == 2
    assert summary.legacy_request_count == 1
    assert summary.current_semantics_request_count == 1
    assert summary.cache_miss_tokens == 40
    assert summary.cache_creation_tokens == 15
    assert summary.current_prompt_tokens == 185
    assert summary.current_cached_tokens == 70
    assert summary.current_cache_hit_rate == 70 / 185
    assert summary.cache_hit_rate == 70 / 185
    assert summary.legacy_cache_hit_rate is None


def test_future_and_legacy_do_not_enter_v2_cache_rate():
    summary = UsageSummary()
    add_record_to_summary(
        summary,
        {
            "prompt_tokens": 100,
            "cached_tokens": 80,
            "total_tokens": 110,
        },
    )
    add_record_to_summary(
        summary,
        {
            "prompt_tokens": 200,
            "cached_tokens": 100,
            "total_tokens": 210,
            "usage_semantics_version": 3,
        },
    )

    assert summary.legacy_request_count == 1
    assert summary.unknown_semantics_request_count == 1
    assert summary.current_semantics_request_count == 0
    assert summary.cache_hit_rate is None
    assert summary.unknown_semantics_prompt_tokens == 200
    assert summary.legacy_prompt_tokens == 100


def test_real_zero_v2_hit_rate_is_distinct_from_no_data():
    summary = UsageSummary()
    add_record_to_summary(
        summary,
        {
            "prompt_tokens": 100,
            "cached_tokens": 0,
            "total_tokens": 110,
            "usage_semantics_version": 2,
        },
    )
    assert summary.cache_hit_rate == 0.0
