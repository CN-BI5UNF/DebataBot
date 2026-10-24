"""M00：维护状态、writer 屏障、启动拦截。"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from app_config.paths import AppPaths
from core.maintenance import (
    ControllerBarrier,
    MaintenanceController,
    MaintenanceError,
    startup_gate,
)
from webui.runtime_host import STATE_MAINTENANCE, RuntimeHost


def _paths(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    paths.ensure_data_dirs()
    return paths


def test_maintenance_writer_barrier_and_gate(tmp_path):
    paths = _paths(tmp_path)
    controller = MaintenanceController(paths)
    assert startup_gate(paths)["ok"] is True
    events: list[str] = []
    controller.register_writer(
        "history", lambda: events.append("stop-history"),
        lambda: events.append("resume-history"),
    )
    controller.register_writer(
        "rag", lambda: events.append("stop-rag"),
        lambda: events.append("resume-rag"),
    )
    token = controller.request("test", "snapshot", plan_hash="plan-1")
    assert startup_gate(paths)["ok"] is False
    controller.drain(token)
    marker = controller.quiesce(token, snapshot_hash="snap-1")
    controller.mark_phase(token, "snapshotting")
    assert marker.generation == 1
    assert startup_gate(paths)["ok"] is False
    state = controller.read_state()
    assert state.phase == "snapshotting"
    assert state.owner_token not in json.dumps(state.snapshot())
    if os.name != "nt":
        assert stat.S_IMODE(controller.state_file.stat().st_mode) == 0o600
    controller.resume(token)
    assert sorted(events) == ["resume-history", "resume-rag",
                             "stop-history", "stop-rag"]
    assert startup_gate(paths)["ok"] is True


def test_controller_barrier_releases_after_snapshot(tmp_path):
    paths = _paths(tmp_path)
    controller = MaintenanceController(paths)
    calls: list[str] = []
    controller.register_writer("one", lambda: calls.append("stop"))
    barrier = ControllerBarrier(
        controller, owner="test", purpose="strict", snapshot_hash="abc"
    )
    marker = barrier.freeze()
    assert marker.generation == 1
    assert controller.read_state().phase == "snapshotting"
    barrier.release(marker)
    assert calls == ["stop"]
    assert controller.read_state().phase == "running"
    assert startup_gate(paths)["ok"] is True


def test_controller_barrier_persists_contract_epoch_and_set(tmp_path):
    paths = _paths(tmp_path)
    controller = MaintenanceController(paths)
    barrier = ControllerBarrier(
        controller,
        owner="test",
        purpose="post-d01-snapshot",
        contract_epoch=1,
        migration_set_id="M001",
    )
    marker = barrier.freeze()
    assert marker.contract_epoch == 1
    assert marker.migration_set_id == "M001"
    state = controller.read_state()
    assert state.data_contract_epoch == 1
    assert state.migration_set_id == "M001"
    barrier.release(marker)
    assert controller.read_state().phase == "running"


def test_writer_failure_enters_recovery_and_blocks_start(tmp_path):
    paths = _paths(tmp_path)
    controller = MaintenanceController(paths)
    controller.register_writer("bad", lambda: (_ for _ in ()).throw(RuntimeError("nope")))
    with pytest.raises(MaintenanceError) as exc:
        token = controller.request("test", "snapshot")
        controller.drain(token)
    assert exc.value.code == "writer_drain_failed"
    state = controller.read_state()
    assert state.phase == "recovery_required"
    assert startup_gate(paths)["ok"] is False
    controller.release_lock()


def test_second_controller_cannot_take_lock(tmp_path):
    paths = _paths(tmp_path)
    first = MaintenanceController(paths)
    second = MaintenanceController(paths)
    first.acquire_lock()
    try:
        with pytest.raises(MaintenanceError) as exc:
            second.acquire_lock()
        assert exc.value.code == "maintenance_busy"
    finally:
        first.release_lock()


def test_runtime_host_compat_gate_blocks_newer_database(tmp_path):
    import sqlite3

    paths = _paths(tmp_path)
    manifest = Path(__file__).resolve().parents[1] / "compatibility.json"
    (tmp_path / "compatibility.json").write_text(
        manifest.read_text(encoding="utf-8"), encoding="utf-8"
    )
    host = RuntimeHost(project_root=tmp_path)
    host.build_paths()
    paths.CONFIG_FILE.write_text("version: 2\n", encoding="utf-8")
    memory = paths.MEMORY_DIR / "one"
    memory.mkdir(parents=True)
    with sqlite3.connect(memory / "chat.db") as conn:
        conn.execute("PRAGMA user_version=3")
    gate = host._runtime_compat_gate()
    assert gate["ok"] is False
    assert gate["code"] == "unsupported_data_contract"


@pytest.mark.asyncio
async def test_runtime_host_start_obeys_maintenance_gate(tmp_path):
    paths = _paths(tmp_path)
    host = RuntimeHost(project_root=tmp_path)
    host.build_paths()
    paths.CONFIG_FILE.write_text("version: 2\n", encoding="utf-8")
    controller = MaintenanceController(paths)
    token = controller.request("test", "snapshot")
    try:
        ok = await host.start()
        assert ok is False
        assert host.state == STATE_MAINTENANCE
        assert host.rt is None
    finally:
        controller.resume(token)
