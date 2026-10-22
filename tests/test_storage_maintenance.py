"""存储盘点、预览、执行与路径安全。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app_config.schema import RootConfig
from core.storage_maintenance import StorageMaintenanceError, StorageMaintenanceService
from memory.debata_stores import DebataHistoryStore
from memory.history import HistoryManager
from memory.maintenance_store import safe_unlink


def _config() -> RootConfig:
    return RootConfig.model_validate(
        {
            "persona": {"name": "test_bot"},
            "adapters": {"default": {"type": "napcat"}},
            "providers": {"p": {"preset": "deepseek", "api_key_id": "k"}},
            "agents": {
                "chat": {"provider": "p", "model": "deepseek-v4-flash"}
            },
        }
    )


def _runtime(tmp_paths):
    history = HistoryManager(
        tmp_paths.memory_dir_for("test_bot") / "history.jsonl",
        store=DebataHistoryStore(
            tmp_paths.memory_dir_for("test_bot") / "test_bot.db",
            "test_bot",
        ),
    )
    return SimpleNamespace(
        paths=tmp_paths,
        config=_config(),
        history=history,
        pipeline=None,
    )


def _old_file(path: Path, content: bytes = b"old") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    old = 1_700_000_000
    os.utime(path, (old, old))


def _new_file(path: Path, content: bytes = b"new") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


async def _wait_job(service, job_id: str, timeout: float = 5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        job = await service.get_job(job_id)
        if job and job.get("state") in {
            "completed", "partial_success", "failed", "cancelled", "interrupted"
        }:
            return job
        await asyncio.sleep(0.02)
    raise AssertionError("清理任务超时")


@pytest.mark.asyncio
async def test_usage_reports_protected_and_reclaimable_without_paths(tmp_paths):
    runtime = _runtime(tmp_paths)
    await runtime.history.add_user_message("hello", conversation_id="private:1")
    (tmp_paths.WORKSPACE_DIR / "note.txt").write_text("user-owned", encoding="utf-8")
    service = StorageMaintenanceService(runtime)

    report = await service.usage()
    categories = {item["key"]: item for item in report["categories"]}

    assert report["complete"] is True
    assert report["protected_bytes"] > 0
    assert categories["workspace_user"]["protected"] is True
    assert categories["logs"]["reclaimable"] is False
    assert categories["temp_files"]["reclaimable"] is True
    assert "note.txt" not in str(report)


@pytest.mark.asyncio
async def test_preview_and_execute_only_expired_temp_files(tmp_paths):
    runtime = _runtime(tmp_paths)
    old = tmp_paths.TMP_DIR / "nested" / "old.bin"
    new = tmp_paths.TMP_DIR / "new.bin"
    _old_file(old, b"old-data")
    _new_file(new, b"new-data")
    service = StorageMaintenanceService(runtime)

    preview = await service.preview(["temp_files"])
    assert preview["expected_items"] == 1
    assert preview["expected_bytes"] == len(b"old-data")
    assert "old.bin" not in str(preview)
    assert "relative_path" not in str(preview)

    job, created = await service.execute(
        preview_id=preview["preview_id"],
        confirmation_token=preview["confirmation_token"],
        idempotency_key="key-1",
        expected_generation=preview["generation"],
    )
    assert created is True
    finished = await _wait_job(service, job["job_id"])

    assert finished["state"] == "completed"
    assert finished["result"]["bytes_freed"] == len(b"old-data")
    assert not old.exists()
    assert new.exists()

    same, created_again = await service.execute(
        preview_id=preview["preview_id"],
        confirmation_token=preview["confirmation_token"],
        idempotency_key="key-1",
        expected_generation=preview["generation"],
    )
    assert created_again is False
    assert same["job_id"] == job["job_id"]

    with pytest.raises(StorageMaintenanceError) as exc:
        await service.execute(
            preview_id=preview["preview_id"],
            confirmation_token=preview["confirmation_token"],
            idempotency_key="key-2",
            expected_generation=preview["generation"],
        )
    assert exc.value.code == "preview_already_used"


@pytest.mark.asyncio
async def test_inbound_media_ttl_preview_and_execute(tmp_paths):
    runtime = _runtime(tmp_paths)
    incoming = tmp_paths.WORKSPACE_DIR / "incoming"
    old_image = incoming / "images" / "old.jpg"
    new_image = incoming / "new.png"
    old_video = incoming / "old.mp4"
    old_audio = incoming / "keep.ogg"
    _old_file(old_image, b"jpg-old")
    _new_file(new_image, b"png-new")
    _old_file(old_video, b"mp4-old")
    _old_file(old_audio, b"ogg-keep")

    service = StorageMaintenanceService(runtime)
    preview = await service.preview(["image_assets", "inbound_videos"])

    assert preview["expected_items"] == 2
    assert preview["expected_bytes"] == len(b"jpg-old") + len(b"mp4-old")
    assert preview["category_counts"]["image_assets"]["items"] == 1
    assert preview["category_counts"]["inbound_videos"]["items"] == 1
    assert "old.jpg" not in str(preview)

    job, _ = await service.execute(
        preview_id=preview["preview_id"],
        confirmation_token=preview["confirmation_token"],
        idempotency_key="media-ttl",
        expected_generation=preview["generation"],
    )
    finished = await _wait_job(service, job["job_id"])

    assert finished["state"] == "completed"
    assert not old_image.exists()
    assert not old_video.exists()
    assert new_image.exists()
    assert old_audio.exists()

    usage = await service.usage()
    categories = {item["key"]: item for item in usage["categories"]}
    assert categories["image_assets"]["reclaimable"] is True
    assert categories["inbound_videos"]["reclaimable"] is True
    assert categories["workspace_incoming_other"]["protected"] is True
    assert usage["maintenance"]["inbound_media_hours"] == 48


@pytest.mark.asyncio
async def test_preview_rejects_bad_token_and_generation(tmp_paths):
    runtime = _runtime(tmp_paths)
    service = StorageMaintenanceService(runtime)
    preview = await service.preview(["chat_page_cache"])

    with pytest.raises(StorageMaintenanceError) as exc:
        await service.execute(
            preview_id=preview["preview_id"],
            confirmation_token="wrong-token",
            idempotency_key="bad-token",
            expected_generation=preview["generation"],
        )
    assert exc.value.code == "confirmation_token_invalid"

    with pytest.raises(StorageMaintenanceError) as exc:
        await service.execute(
            preview_id=preview["preview_id"],
            confirmation_token=preview["confirmation_token"],
            idempotency_key="bad-generation",
            expected_generation="0" * 64,
        )
    assert exc.value.code == "generation_conflict"


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_preview_is_conflict(tmp_paths):
    runtime = _runtime(tmp_paths)
    service = StorageMaintenanceService(runtime)
    first = await service.preview(["chat_page_cache"])
    second = await service.preview(["chat_page_cache"])

    job, created = await service.execute(
        preview_id=first["preview_id"],
        confirmation_token=first["confirmation_token"],
        idempotency_key="shared-key",
        expected_generation=first["generation"],
    )
    assert created is True
    await _wait_job(service, job["job_id"])

    with pytest.raises(StorageMaintenanceError) as exc:
        await service.execute(
            preview_id=second["preview_id"],
            confirmation_token=second["confirmation_token"],
            idempotency_key="shared-key",
            expected_generation=second["generation"],
        )
    assert exc.value.code == "preview_conflict"


@pytest.mark.asyncio
async def test_durable_and_unknown_categories_are_rejected(tmp_paths):
    service = StorageMaintenanceService(_runtime(tmp_paths))
    with pytest.raises(StorageMaintenanceError) as exc:
        await service.preview(["chat_history"])
    assert exc.value.code == "category_not_implemented"

    with pytest.raises(StorageMaintenanceError) as exc:
        await service.preview(["no_such_category"])
    assert exc.value.code == "unknown_category"


@pytest.mark.asyncio
async def test_changed_file_is_skipped_not_deleted(tmp_paths):
    runtime = _runtime(tmp_paths)
    target = tmp_paths.TMP_DIR / "changing.bin"
    _old_file(target, b"before")
    service = StorageMaintenanceService(runtime)
    preview = await service.preview(["temp_files"])

    target.write_bytes(b"changed-after-preview")
    job, _ = await service.execute(
        preview_id=preview["preview_id"],
        confirmation_token=preview["confirmation_token"],
        idempotency_key="changed",
        expected_generation=preview["generation"],
    )
    finished = await _wait_job(service, job["job_id"])

    assert finished["state"] == "partial_success"
    assert finished["result"]["skipped"] >= 1
    assert target.exists()


def test_safe_unlink_blocks_symlink_and_outside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "real.txt"
    inside.write_text("keep", encoding="utf-8")
    link = root / "link.txt"
    link.symlink_to(inside)

    deleted, status = safe_unlink(link, root)
    assert deleted is False
    assert status == "not_regular_file"
    assert inside.exists()

    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    deleted, status = safe_unlink(outside, root)
    assert deleted is False
    assert status == "outside_root"
    assert outside.exists()


@pytest.mark.asyncio
async def test_memory_cache_clear_reports_entries(tmp_paths, monkeypatch):
    runtime = _runtime(tmp_paths)
    service = StorageMaintenanceService(runtime)

    import webui.chatlog.records as chat_records

    chat_records._chat_page_cache[(1, 2, 3)] = (1, 0.0, [{"content": "x"}])
    preview = await service.preview(["chat_page_cache"])
    job, _ = await service.execute(
        preview_id=preview["preview_id"],
        confirmation_token=preview["confirmation_token"],
        idempotency_key="memory-clear",
        expected_generation=preview["generation"],
    )
    finished = await _wait_job(service, job["job_id"])

    assert finished["state"] == "completed"
    assert finished["result"]["categories"]["chat_page_cache"]["entries_removed"] == 1
    assert chat_records.chat_page_cache_info()["entries"] == 0
