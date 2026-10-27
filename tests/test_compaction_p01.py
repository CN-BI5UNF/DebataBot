"""P01 专项测试：真实测量、去重观察、样本上限、PressureSnapshot 与 CompactionJobStore。"""

from __future__ import annotations

from pathlib import Path

from core.compaction import (
    CompactionJobStore,
    CompactionPlanner,
)


def test_mark_success_uses_actual_after_tokens_and_coverage():
    planner = CompactionPlanner()
    planner.mark_success(
        now=100.0,
        active_tokens_before=5000,
        active_records_before=20,
        archived_count=8,
        target_after_tokens=2000,
        actual_after_tokens=1850,
        actual_coverage=8,
    )
    assert planner.last_post_tokens == 1850
    assert planner.last_actual_after_tokens == 1850
    assert planner.last_coverage == 8
    assert planner.last_active_records == 12

    # 没有 actual_after_tokens 时兼容回退到 target_after_tokens
    planner.mark_success(
        now=200.0,
        active_tokens_before=3000,
        active_records_before=12,
        archived_count=4,
        target_after_tokens=1200,
    )
    assert planner.last_post_tokens == 1200
    assert planner.last_actual_after_tokens is None


def test_observe_deduplication_and_zero_increment():
    planner = CompactionPlanner()
    planner.observe(1000, request_id="req-1", source_watermark=10, view_version=1)
    planner.observe(1500, request_id="req-2", source_watermark=15, view_version=1)
    assert planner.growth_ewma == 500.0
    assert len(planner._growth_samples) == 1

    # 重试发送使用相同 request_id + watermark，不重复记 0 增量
    planner.observe(1500, request_id="req-2", source_watermark=15, view_version=1)
    assert planner.growth_ewma == 500.0
    assert len(planner._growth_samples) == 1

    # 重试时如果 active_tokens 相同且无 request_id，growth=0 不入 samples
    planner.observe(1500)
    assert len(planner._growth_samples) == 1


def test_observe_baseline_reset_on_config_change():
    planner = CompactionPlanner()
    planner.observe(1000, baseline_tag="model-a:v1")
    planner.observe(1400, baseline_tag="model-a:v1")
    assert planner.growth_ewma == 400.0
    assert len(planner._growth_samples) == 1

    # 模型或策略版本切换，重置 baseline 与旧 EWMA
    planner.observe(2000, baseline_tag="model-b:v2")
    assert planner.growth_ewma is None
    assert len(planner._growth_samples) == 0
    assert planner.last_observed_tokens == 2000


def test_growth_samples_bounded_to_128():
    planner = CompactionPlanner()
    base = 100
    for _ in range(200):
        base += 10
        planner.observe(base)
    assert len(planner._growth_samples) <= 128


def test_pressure_snapshot_levels():
    planner = CompactionPlanner()
    # 正常
    snap_normal = planner.build_pressure_snapshot(
        active_tokens=400,
        input_budget=1000,
        soft_tokens=600,
        urgent_tokens=800,
        now=100.0,
    )
    assert snap_normal.pressure_level == "normal"
    assert snap_normal.needs_immediate_wait is False

    # 软线：后台触发，不强制等待
    snap_soft = planner.build_pressure_snapshot(
        active_tokens=650,
        input_budget=1000,
        soft_tokens=600,
        urgent_tokens=800,
        now=100.0,
    )
    assert snap_soft.pressure_level == "soft"
    assert snap_soft.needs_immediate_wait is False

    # 紧急线：前台必须等待压缩
    snap_urgent = planner.build_pressure_snapshot(
        active_tokens=850,
        input_budget=1000,
        soft_tokens=600,
        urgent_tokens=800,
        now=100.0,
    )
    assert snap_urgent.pressure_level == "urgent"
    assert snap_urgent.needs_immediate_wait is True

    # 超限
    snap_over = planner.build_pressure_snapshot(
        active_tokens=1050,
        input_budget=1000,
        soft_tokens=600,
        urgent_tokens=800,
        now=100.0,
    )
    assert snap_over.pressure_level == "over_budget"
    assert snap_over.needs_immediate_wait is True


def test_compaction_job_store_concurrency_and_lifecycle(tmp_path: Path):
    store_file = tmp_path / "compaction_jobs.json"
    store = CompactionJobStore(store_file, boot_id="boot-test-1")

    # 认领 Job 1
    job1, busy_id = store.claim_job(
        "job-101",
        persona_id="persona-alice",
        source_range_start=0,
        source_range_end=50,
        tokens_before=4000,
    )
    assert job1 is not None
    assert busy_id == ""
    assert job1.phase == "claimed"

    # 并发控制：已有活跃 job 时返回 busy 并给出现有 job_id
    job2, busy_id2 = store.claim_job(
        "job-102",
        persona_id="persona-alice",
    )
    assert job2 is None
    assert busy_id2 == "job-101"

    # 执行并提交 Job 1
    store.mark_running("job-101")
    assert store.get_job("job-101").phase == "running"

    store.commit_job("job-101", actual_after_tokens=1200, archived_count=30)
    committed = store.get_job("job-101")
    assert committed.phase == "committed"
    assert committed.already_committed is True
    assert committed.actual_after_tokens == 1200

    # 提交后活跃状态释放，Job 2 可以正常认领
    job2_retry, busy_id_after = store.claim_job("job-102", persona_id="persona-alice")
    assert job2_retry is not None
    assert busy_id_after == ""


def test_compaction_job_store_restart_interruption_recovery(tmp_path: Path):
    store_file = tmp_path / "compaction_jobs.json"
    store1 = CompactionJobStore(store_file, boot_id="boot-1")
    store1.claim_job("job-interrupted", persona_id="alice")
    store1.mark_running("job-interrupted")

    # 模拟进程崩溃重启，新 boot_id 打开同一持久文件
    store2 = CompactionJobStore(store_file, boot_id="boot-2")
    recovered = store2.get_job("job-interrupted")
    assert recovered is not None
    assert recovered.phase == "failed"
    assert recovered.failure_reason == "interrupted_by_restart"
    assert recovered.failure_class == "process_restart"

    # 重启后活跃状态已被清理，可认领新 job
    new_job, _ = store2.claim_job("job-new", persona_id="alice")
    assert new_job is not None
