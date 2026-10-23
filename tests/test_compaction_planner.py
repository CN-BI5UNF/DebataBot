"""C02 压缩状态机：预测、冷却、最少新增与失败退避。"""

from __future__ import annotations

from core.compaction import CompactionPlanner


def _decide(planner: CompactionPlanner, **overrides):
    kwargs = {
        "active_tokens": 900,
        "active_records": 6,
        "trigger_tokens": 1000,
        "now": 100.0,
        "cooldown_seconds": 60,
        "min_new_tokens": 200,
        "cold_growth_tokens": 200,
        "forecast_enabled": True,
        "force": False,
        "media_pressure": False,
    }
    kwargs.update(overrides)
    return planner.decide(**kwargs)


def test_forecast_triggers_below_hard_threshold_only_when_enabled():
    planner = CompactionPlanner()
    forecast = _decide(planner, active_tokens=900, cold_growth_tokens=200)
    assert forecast.should_run is True
    assert forecast.reason == "forecast"

    disabled = _decide(
        CompactionPlanner(),
        active_tokens=900,
        cold_growth_tokens=200,
        forecast_enabled=False,
    )
    assert disabled.should_run is False
    assert disabled.reason == "below_trigger"


def test_success_enters_cooldown_and_blocks_small_growth():
    planner = CompactionPlanner()
    planner.mark_success(
        now=100.0,
        active_tokens_before=5000,
        active_records_before=10,
        archived_count=8,
        target_after_tokens=1000,
    )
    decision = _decide(planner, active_tokens=1100, now=110.0, min_new_tokens=200)
    assert decision.should_run is False
    assert decision.reason == "cooldown"
    assert decision.new_tokens == 100


def test_new_material_allowed_after_cooldown_but_small_growth_still_skipped():
    planner = CompactionPlanner()
    planner.mark_success(
        now=100.0,
        active_tokens_before=5000,
        active_records_before=10,
        archived_count=8,
        target_after_tokens=1000,
    )
    decision = _decide(planner, active_tokens=1100, now=500.0, min_new_tokens=200)
    assert decision.should_run is False
    assert decision.reason == "no_new_compaction_material"

    enough = _decide(planner, active_tokens=1300, now=500.0, min_new_tokens=200)
    assert enough.should_run is True
    assert enough.new_tokens == 300


def test_failure_exponential_backoff_and_force_bypasses_it():
    planner = CompactionPlanner()
    planner.mark_failure(now=100.0, reason="summary_agent_error")
    blocked = _decide(planner, active_tokens=900, now=110.0)
    assert blocked.should_run is False
    assert blocked.reason == "backoff"

    forced = _decide(planner, active_tokens=900, now=110.0, force=True)
    assert forced.should_run is True
    assert forced.reason == "manual"

    media = _decide(planner, active_tokens=100, now=110.0, media_pressure=True)
    assert media.should_run is True
    assert media.reason == "media"


def test_backoff_expires_and_success_resets_failure_state():
    planner = CompactionPlanner()
    planner.mark_failure(now=100.0, reason="summary_agent_error")
    planner.mark_failure(now=100.0, reason="summary_agent_error")
    assert planner.failure_count == 2
    retry = _decide(planner, active_tokens=900, now=300.0)
    assert retry.should_run is True

    planner.mark_success(
        now=300.0,
        active_tokens_before=900,
        active_records_before=6,
        archived_count=4,
        target_after_tokens=400,
    )
    assert planner.failure_count == 0
    assert planner.backoff_until == 0.0
    assert planner.last_post_tokens == 400


def test_growth_ewma_uses_observed_delta_and_resets_on_window_shrink():
    planner = CompactionPlanner()
    planner.observe(1000)
    planner.observe(1400)
    assert planner.growth_ewma == 400
    planner.observe(1750)
    assert planner.growth_ewma == 385.0  # 400 * 0.7 + 350 * 0.3
    planner.observe(300)
    assert planner.growth_ewma is None
