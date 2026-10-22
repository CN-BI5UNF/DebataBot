"""存储盘点与安全缓存清理。

安全边界：
    - 只清理明确可重建的内存缓存，以及 ``DATA_DIR/tmp``、``DATA_DIR/cache``
      下超过保留期的普通文件。
    - 原始聊天、唯一图片、workspace 用户文件、长期记忆、备份和日志不在
      本模块的删除范围内。
    - 所有路径在执行时重新 resolve 并校验仍位于受管根目录内；符号链接、
      目录替换和身份变化均跳过并计数。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory.maintenance_store import (
    MaintenanceStore,
    MaintenanceStoreError,
    safe_unlink,
    token_fingerprint,
)

logger = logging.getLogger(__name__)

PREVIEW_TTL_SECONDS = 300

IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif", ".avif"}
)
VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".m4v", ".ts", ".3gp"}
)
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


@dataclass(frozen=True, slots=True)
class CategorySpec:
    key: str
    label: str
    kind: str
    description: str
    implemented: bool = True
    protected: bool = False


CATEGORY_SPECS: dict[str, CategorySpec] = {
    "chat_page_cache": CategorySpec(
        "chat_page_cache", "聊天页合并缓存", "memory",
        "WebUI 为 conversations/display/timeline 合并的进程内记录，清空后下次请求重建。",
    ),
    "history_page_cache": CategorySpec(
        "history_page_cache", "历史分页热缓存", "memory",
        "DebataHistoryStore 的有界分页缓存，清空不影响 SQLite 原始历史。",
    ),
    "image_encoding_cache": CategorySpec(
        "image_encoding_cache", "图片编码内存缓存", "memory",
        "已规范化图片的 base64/data URL 进程内 LRU；磁盘唯一图片不受影响。",
    ),
    "token_cache": CategorySpec(
        "token_cache", "Token 估算缓存", "memory",
        "只包含模型名、长度、哈希和整数 token 数，不保存聊天原文。",
    ),
    "tool_display_cache": CategorySpec(
        "tool_display_cache", "工具展示缓存", "memory",
        "WebUI 工具调用/结果的人类可读格式化缓存，可随时重建。",
    ),
    "temp_files": CategorySpec(
        "temp_files", "过期临时文件", "disk_temp",
        "DATA_DIR/tmp 下超过保留期的普通文件；不扫描 workspace 或用户目录。",
    ),
    "derived_cache": CategorySpec(
        "derived_cache", "可重建派生缓存", "disk_cache",
        "DATA_DIR/cache 下超过保留期的普通文件；当前没有已登记的派生缩略图。",
    ),
    "chat_history": CategorySpec(
        "chat_history", "原始聊天记录", "durable",
        "需要按来源引用、墓碑和投影失效的完整删除链路；本轮未启用。",
        implemented=False, protected=True,
    ),
    "image_assets": CategorySpec(
        "image_assets", "入站图片", "disk_media",
        "workspace/incoming 下的图片文件；用户确认超过保留期可清理。"
        "删除后历史文字保留，但原图字节不可用，模型需要时会要求重传。",
    ),
    "inbound_videos": CategorySpec(
        "inbound_videos", "入站视频", "disk_media",
        "workspace/incoming 下的视频文件；用户确认超过保留期可清理。"
        "删除后历史文字保留，但原视频字节不可用。",
    ),
    "rag_index": CategorySpec(
        "rag_index", "RAG 向量索引", "durable",
        "清理后需要重新 embedding，可能产生费用；本轮未启用。",
        implemented=False, protected=True,
    ),
    "logs": CategorySpec(
        "logs", "运行日志", "durable",
        "日志仍用于故障诊断，默认不纳入安全清理；本轮未启用。",
        implemented=False, protected=True,
    ),
    "workspace_user": CategorySpec(
        "workspace_user", "Workspace 用户文件", "durable",
        "用户上传/创建或 AI 写入的工作文件，默认永久保留。",
        implemented=False, protected=True,
    ),
    "workspace_incoming_other": CategorySpec(
        "workspace_incoming_other", "其他入站附件", "durable",
        "workspace/incoming 下除图片/视频外的音频和文件副本，默认永久保留。",
        implemented=False, protected=True,
    ),
    "workspace_agent_tasks": CategorySpec(
        "workspace_agent_tasks", "Agent 任务产物", "durable",
        "子 Agent 的任务输出，默认保留；删除需要独立策略。",
        implemented=False, protected=True,
    ),
    "workspace_runtime": CategorySpec(
        "workspace_runtime", "运行投影数据", "durable",
        "chat timeline 等运行时投影，可重建但本轮不自动清理。",
        implemented=False, protected=True,
    ),
    "config_backups": CategorySpec(
        "config_backups", "配置备份", "durable",
        "配置保存时的回滚副本，不能当作缓存自动删除。",
        implemented=False, protected=True,
    ),
    "maintenance_trash": CategorySpec(
        "maintenance_trash", "维护隔离区", "durable",
        "本轮不启用隔离区；该目录通常为空。",
        implemented=False, protected=True,
    ),
}


class StorageMaintenanceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class StorageMaintenanceService:
    def __init__(self, runtime: Any) -> None:
        self.rt = runtime
        self.paths = runtime.paths
        self.store = MaintenanceStore(self.paths.MAINTENANCE_DB_FILE)
        self._started = False
        self._start_lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel_requested: set[str] = set()

    async def ensure_started(self) -> None:
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            await self.store.initialize()
            interrupted = await self.store.mark_interrupted()
            if interrupted:
                logger.warning("发现 %s 个上次进程遗留的维护任务，已标记 interrupted", interrupted)
            self._started = True

    async def shutdown(self) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    # ------------------------------------------------------------------
    # 盘点
    # ------------------------------------------------------------------
    async def usage(self) -> dict[str, Any]:
        await self.ensure_started()
        cfg = self._maintenance_cfg()
        retention = self._retention_cfg()
        memory = await self._memory_cache_usage()
        history_stats: dict[str, Any] = {}
        try:
            history_stats = await self.rt.history.stats()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 history 分页统计失败: %s", type(exc).__name__)
        disk = await asyncio.to_thread(self._scan_disk_sync)
        categories: list[dict[str, Any]] = []
        for key, spec in CATEGORY_SPECS.items():
            if spec.kind in {"memory", "disk_temp", "disk_cache", "disk_media"}:
                data = memory.get(key) or disk.get(key) or {}
                categories.append(
                    {
                        "key": key,
                        "label": spec.label,
                        "kind": spec.kind,
                        "description": spec.description,
                        "implemented": spec.implemented,
                        "protected": spec.protected,
                        "bytes": int(data.get("bytes") or 0),
                        "items": int(data.get("items") or 0),
                        "files": int(data.get("files") or 0),
                        "reclaimable": bool(spec.implemented),
                    }
                )
            elif spec.kind == "durable":
                data = disk.get(key) or {}
                categories.append(
                    {
                        "key": key,
                        "label": spec.label,
                        "kind": spec.kind,
                        "description": spec.description,
                        "implemented": spec.implemented,
                        "protected": True,
                        "bytes": int(data.get("bytes") or 0),
                        "items": int(data.get("items") or 0),
                        "files": int(data.get("files") or 0),
                        "reclaimable": False,
                    }
                )
        protected_bytes = sum(
            int(category["bytes"]) for category in categories if category["protected"]
        )
        reclaimable_bytes = sum(
            int(category["bytes"]) for category in categories if category["reclaimable"]
        )
        disk_usage = _disk_usage(self.paths.DATA_DIR)
        low_disk = _low_disk_state(disk_usage, cfg)
        return {
            "generated_at": _iso_now(),
            "data_dir": str(self.paths.DATA_DIR),
            "disk": disk_usage,
            "low_disk": low_disk,
            "categories": categories,
            "protected_bytes": protected_bytes,
            "reclaimable_bytes": reclaimable_bytes,
            "memory_caches": memory,
            "history": history_stats,
            "maintenance": {
                "mode": str(getattr(cfg, "mode", "inventory_only")),
                "temp_hours": int(getattr(cfg, "temp_hours", 24)),
                "derived_cache_days": int(getattr(cfg, "derived_cache_days", 7)),
                "inbound_media_hours": int(
                    getattr(retention, "inbound_media_hours", 48)
                ),
                "preview_ttl_seconds": PREVIEW_TTL_SECONDS,
                "automatic_cleanup": False,
            },
            "complete": True,
        }

    async def _memory_cache_usage(self) -> dict[str, dict[str, int]]:
        from features.vision.image_assets import ImageAssetStore
        from utils.token_budget import raw_text_cache_approx_bytes, raw_text_cache_info
        from webui.chatlog.records import chat_page_cache_info
        from webui.chatlog.tool_display import tool_display_cache_info

        chat_info = chat_page_cache_info()
        token_info = raw_text_cache_info()
        tool_info = tool_display_cache_info()
        image_store = getattr(getattr(self.rt, "pipeline", None), "_image_asset_store_instance", None)
        if isinstance(image_store, ImageAssetStore):
            image_info = image_store.cache_info()
        else:
            image_info = {"entries": 0, "current_bytes": 0, "max_bytes": 0}
        try:
            history_pages = await self.rt.history.stats()
        except Exception:  # noqa: BLE001
            history_pages = {}
        return {
            "chat_page_cache": {
                "bytes": int(chat_info.get("approx_bytes") or 0),
                "items": int(chat_info.get("entries") or 0),
                "records": int(chat_info.get("records") or 0),
            },
            "history_page_cache": {
                "bytes": int(history_pages.get("page_cache_bytes") or 0),
                "items": int(history_pages.get("page_cache_entries") or 0),
            },
            "image_encoding_cache": {
                "bytes": int(image_info.get("current_bytes") or 0),
                "items": int(image_info.get("entries") or 0),
                "max_bytes": int(image_info.get("max_bytes") or 0),
            },
            "token_cache": {
                "bytes": int(raw_text_cache_approx_bytes()),
                "items": int(token_info.get("currsize") or 0),
                "max_items": int(token_info.get("maxsize") or 0),
            },
            "tool_display_cache": {
                "bytes": 0,
                "items": int(tool_info.get("entries") or 0),
            },
        }

    def _scan_disk_sync(self) -> dict[str, dict[str, int]]:
        memory_dir = self.paths.MEMORY_DIR
        vector_dir = self.paths.VECTOR_DIR
        workspace = self.paths.WORKSPACE_DIR
        incoming = workspace / "incoming"
        agent_tasks = workspace / "agent_tasks"
        runtime_dir = workspace / "runtime"
        logs = self.paths.LOGS_DIR
        backups = self.paths.DATA_DIR / "config_backups"
        cache = self.paths.CACHE_DIR
        tmp = self.paths.TMP_DIR
        trash = self.paths.MAINTENANCE_TRASH_DIR

        db_size = _tree_size(memory_dir)
        rag_size = _tree_size(vector_dir)
        incoming_all = _tree_size(incoming)
        incoming_images = _tree_size_matching(incoming, IMAGE_EXTENSIONS)
        incoming_videos = _tree_size_matching(incoming, VIDEO_EXTENSIONS)
        incoming_other = _subtract_sizes(
            incoming_all,
            incoming_images,
            incoming_videos,
        )
        tasks_size = _tree_size(agent_tasks)
        runtime_size = _tree_size(runtime_dir)
        workspace_other = _tree_size(
            workspace,
            exclude_dirs={incoming, agent_tasks, runtime_dir},
        )
        logs_size = _tree_size(logs)
        backups_size = _tree_size(backups)
        cache_size = _tree_size(cache)
        tmp_size = _tree_size(tmp)
        trash_size = _tree_size(trash)
        return {
            "temp_files": tmp_size,
            "derived_cache": cache_size,
            "chat_history": db_size,
            "image_assets": incoming_images,
            "inbound_videos": incoming_videos,
            "rag_index": rag_size,
            "logs": logs_size,
            "workspace_user": workspace_other,
            "workspace_incoming_other": incoming_other,
            "workspace_agent_tasks": tasks_size,
            "workspace_runtime": runtime_size,
            "config_backups": backups_size,
            "maintenance_trash": trash_size,
        }

    # ------------------------------------------------------------------
    # 预览
    # ------------------------------------------------------------------
    async def preview(self, categories: list[str]) -> dict[str, Any]:
        await self.ensure_started()
        selected = self._validate_categories(categories)
        cfg = self._maintenance_cfg()
        retention = self._retention_cfg()
        items: list[dict[str, Any]] = []
        category_counts: dict[str, dict[str, int]] = {}
        now = time.time()
        for key in selected:
            spec = CATEGORY_SPECS[key]
            if spec.kind == "memory":
                info = (await self._memory_cache_usage()).get(key, {})
                category_counts[key] = {
                    "items": int(info.get("items") or 0),
                    "files": 0,
                    "bytes": int(info.get("bytes") or 0),
                }
            elif spec.kind == "disk_temp":
                root = self.paths.TMP_DIR
                found = await asyncio.to_thread(
                    _collect_disk_items,
                    root,
                    self.paths.DATA_DIR,
                    max(0, int(getattr(cfg, "temp_hours", 24)) * 3600),
                    key,
                    now,
                )
                items.extend(found)
                category_counts[key] = _category_totals(found)
            elif spec.kind == "disk_cache":
                root = self.paths.CACHE_DIR
                found = await asyncio.to_thread(
                    _collect_disk_items,
                    root,
                    self.paths.DATA_DIR,
                    max(0, int(getattr(cfg, "derived_cache_days", 7)) * 86400),
                    key,
                    now,
                )
                items.extend(found)
                category_counts[key] = _category_totals(found)
            elif spec.kind == "disk_media":
                extensions = (
                    IMAGE_EXTENSIONS if key == "image_assets" else VIDEO_EXTENSIONS
                )
                found = await asyncio.to_thread(
                    _collect_disk_items,
                    self.paths.WORKSPACE_DIR / "incoming",
                    self.paths.DATA_DIR,
                    max(
                        1,
                        int(getattr(retention, "inbound_media_hours", 48)),
                    )
                    * 3600,
                    key,
                    now,
                    extensions,
                )
                items.extend(found)
                category_counts[key] = _category_totals(found)
        expected_bytes = sum(int(v.get("bytes") or 0) for v in category_counts.values())
        expected_items = sum(int(v.get("items") or 0) for v in category_counts.values())
        preview_id = secrets.token_urlsafe(18)
        token = secrets.token_urlsafe(32)
        generation = _generation_for(selected, items)
        expires_at = now + PREVIEW_TTL_SECONDS
        await self.store.create_plan(
            preview_id=preview_id,
            created_at=now,
            expires_at=expires_at,
            categories=selected,
            items=items,
            expected_bytes=expected_bytes,
            expected_items=expected_items,
            generation=generation,
            token_hash=token_fingerprint(token, preview_id=preview_id),
        )
        return {
            "preview_id": preview_id,
            "confirmation_token": token,
            "expires_at": _iso_from_epoch(expires_at),
            "generation": generation,
            "categories": selected,
            "category_counts": category_counts,
            "expected_bytes": expected_bytes,
            "expected_items": expected_items,
            "warnings": [
                "只清理进程内缓存与 DATA_DIR/tmp、DATA_DIR/cache 的过期普通文件",
                "原始聊天、唯一图片、workspace 用户文件、长期记忆和备份不会被删除",
                "内存缓存为估算字节；磁盘文件删除前会再次校验路径、大小与 mtime",
            ],
        }

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    async def execute(
        self,
        *,
        preview_id: str,
        confirmation_token: str,
        idempotency_key: str,
        expected_generation: str,
    ) -> tuple[dict[str, Any], bool]:
        await self.ensure_started()
        plan = await self.store.get_plan(preview_id)
        if plan is None:
            raise StorageMaintenanceError("preview_not_found", "清理预览不存在")
        if (
            str(plan.get("state")) == "ready"
            and float(plan.get("expires_at") or 0) < time.time()
        ):
            raise StorageMaintenanceError("preview_expired", "清理预览已过期，请重新预览")
        expected_hash = token_fingerprint(confirmation_token, preview_id=preview_id)
        if not secrets.compare_digest(str(plan.get("token_hash") or ""), expected_hash):
            raise StorageMaintenanceError("confirmation_token_invalid", "确认令牌不匹配")
        if str(plan.get("generation") or "") != str(expected_generation):
            raise StorageMaintenanceError("generation_conflict", "盘点代次不一致，请重新预览")
        job_id = secrets.token_urlsafe(18)
        try:
            job, created = await self.store.create_job(
                job_id=job_id,
                preview_id=preview_id,
                idempotency_key=idempotency_key,
                categories=list(plan.get("categories") or []),
            )
        except MaintenanceStoreError as exc:
            code = str(exc)
            if code not in {"preview_already_used", "preview_not_found"}:
                code = "preview_conflict"
            raise StorageMaintenanceError(code, str(exc)) from exc
        if created:
            task = asyncio.create_task(
                self._run_job(job_id, plan),
                name=f"storage-cleanup-{job_id}",
            )
            self._tasks[job_id] = task

            def _done(_task: asyncio.Task, jid: str = job_id) -> None:
                self._tasks.pop(jid, None)
                self._cancel_requested.discard(jid)

            task.add_done_callback(_done)
        return job, created

    async def _run_job(self, job_id: str, plan: dict[str, Any]) -> None:
        await self.store.update_job(job_id, state="running", started=True)
        categories = list(plan.get("categories") or [])
        items = list(plan.get("items") or [])
        result: dict[str, Any] = {
            "categories": {},
            "bytes_freed": 0,
            "entries_freed": 0,
            "skipped": 0,
            "errors": [],
            "complete": False,
        }
        try:
            for key in categories:
                if job_id in self._cancel_requested:
                    await self.store.update_job(
                        job_id,
                        state="cancelled",
                        result=result,
                        finished=True,
                    )
                    return
                spec = CATEGORY_SPECS.get(key)
                if spec is None or not spec.implemented:
                    result["errors"].append({"category": key, "code": "not_implemented"})
                    continue
                if spec.kind == "memory":
                    outcome = await self._clear_memory_category(key)
                    result["categories"][key] = outcome
                    result["bytes_freed"] += int(outcome.get("bytes_removed") or 0)
                    result["entries_freed"] += int(outcome.get("entries_removed") or 0)
                else:
                    category_items = [item for item in items if item.get("category") == key]
                    outcome = await self._delete_disk_items(category_items, job_id)
                    result["categories"][key] = outcome
                    result["bytes_freed"] += int(outcome.get("bytes_freed") or 0)
                    result["skipped"] += int(outcome.get("skipped") or 0)
            result["complete"] = True
            state = (
                "partial_success"
                if result["skipped"] or result["errors"]
                else "completed"
            )
        except asyncio.CancelledError:
            result["errors"].append({"category": "job", "code": "cancelled"})
            await self.store.update_job(
                job_id,
                state="cancelled",
                result=result,
                finished=True,
            )
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("缓存清理任务失败 job=%s", job_id)
            await self.store.update_job(
                job_id,
                state="failed",
                result=result,
                error_code="internal_error",
                error_message=type(exc).__name__,
                finished=True,
            )
            return
        await self.store.update_job(
            job_id,
            state=state,
            result=result,
            finished=True,
        )

    async def _clear_memory_category(self, key: str) -> dict[str, int]:
        if key == "chat_page_cache":
            from webui.chatlog.records import chat_page_cache_info, clear_chat_page_cache

            before = chat_page_cache_info()
            clear_chat_page_cache()
            after = chat_page_cache_info()
            return {
                "entries_removed": max(0, int(before["entries"]) - int(after["entries"])),
                "bytes_removed": max(0, int(before["approx_bytes"]) - int(after["approx_bytes"])),
            }
        if key == "history_page_cache":
            removed = await self.rt.history.clear_page_cache()
            return {
                "entries_removed": int(removed.get("entries") or 0),
                "bytes_removed": int(removed.get("bytes") or 0),
            }
        if key == "image_encoding_cache":
            store = getattr(getattr(self.rt, "pipeline", None), "_image_asset_store_instance", None)
            if store is None:
                return {"entries_removed": 0, "bytes_removed": 0}
            removed = await store.clear_cache()
            return {
                "entries_removed": int(removed.get("entries") or 0),
                "bytes_removed": int(removed.get("bytes") or 0),
            }
        if key == "token_cache":
            from utils.token_budget import (
                clear_raw_text_cache,
                raw_text_cache_approx_bytes,
                raw_text_cache_info,
            )

            before = raw_text_cache_info()
            before_bytes = raw_text_cache_approx_bytes()
            clear_raw_text_cache()
            return {
                "entries_removed": int(before.get("currsize") or 0),
                "bytes_removed": int(before_bytes),
            }
        if key == "tool_display_cache":
            from webui.chatlog.tool_display import (
                _clear_tool_display_caches,
                tool_display_cache_info,
            )

            before = tool_display_cache_info()
            _clear_tool_display_caches()
            return {
                "entries_removed": int(before.get("entries") or 0),
                "bytes_removed": 0,
            }
        return {"entries_removed": 0, "bytes_removed": 0}

    async def _delete_disk_items(
        self,
        items: list[dict[str, Any]],
        job_id: str,
    ) -> dict[str, int]:
        data_dir = self.paths.DATA_DIR.resolve()
        outcome = {
            "items": len(items),
            "deleted": 0,
            "bytes_freed": 0,
            "skipped": 0,
            "skip_reasons": {},
        }
        for item in items:
            if job_id in self._cancel_requested:
                outcome["skipped"] += 1
                outcome["skip_reasons"]["cancelled"] = (
                    outcome["skip_reasons"].get("cancelled", 0) + 1
                )
                break
            rel = str(item.get("relative_path") or "")
            rel_path = Path(rel)
            if rel_path.is_absolute() or ".." in rel_path.parts or not rel:
                outcome["skipped"] += 1
                _bump(outcome["skip_reasons"], "outside_root")
                continue
            target = data_dir / rel_path
            # 不先 resolve：符号链接必须保留原路径交给 safe_unlink 拒绝。
            try:
                target.parent.resolve().relative_to(data_dir)
            except (OSError, RuntimeError, ValueError):
                outcome["skipped"] += 1
                _bump(outcome["skip_reasons"], "outside_root")
                continue
            if target.is_symlink():
                outcome["skipped"] += 1
                _bump(outcome["skip_reasons"], "not_regular_file")
                continue
            try:
                stat = target.stat(follow_symlinks=False)
            except OSError:
                outcome["skipped"] += 1
                _bump(outcome["skip_reasons"], "missing")
                continue
            if (
                int(stat.st_size) != int(item.get("size") or -1)
                or int(stat.st_mtime_ns) != int(item.get("mtime_ns") or -1)
            ):
                outcome["skipped"] += 1
                _bump(outcome["skip_reasons"], "changed_since_preview")
                continue
            root = self._disk_root_for_category(str(item.get("category") or ""))
            if root is None:
                outcome["skipped"] += 1
                _bump(outcome["skip_reasons"], "unknown_root")
                continue
            deleted, status = await asyncio.to_thread(safe_unlink, target, root)
            if deleted:
                outcome["deleted"] += 1
                outcome["bytes_freed"] += int(item.get("size") or 0)
            else:
                outcome["skipped"] += 1
                _bump(outcome["skip_reasons"], status)
        return outcome

    async def cancel(self, job_id: str) -> dict[str, Any]:
        await self.ensure_started()
        job = await self.store.get_job(job_id)
        if job is None:
            raise StorageMaintenanceError("job_not_found", "清理任务不存在")
        if str(job.get("state")) in {
            "completed", "partial_success", "failed", "cancelled", "interrupted"
        }:
            raise StorageMaintenanceError("job_not_cancellable", "任务已结束")
        self._cancel_requested.add(job_id)
        await self.store.request_cancel(job_id)
        return job

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        await self.ensure_started()
        return await self.store.get_job(job_id)

    async def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        await self.ensure_started()
        return await self.store.list_jobs(limit)

    async def restore(self, job_id: str) -> dict[str, Any]:
        raise StorageMaintenanceError(
            "restore_not_supported",
            "本轮安全清理不进入隔离区，删除项不可恢复；请先预览再执行",
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _maintenance_cfg(self) -> Any:
        storage = getattr(self.rt.config, "storage", None)
        return getattr(storage, "maintenance", None)

    def _retention_cfg(self) -> Any:
        storage = getattr(self.rt.config, "storage", None)
        return getattr(storage, "retention", None)

    def _disk_root_for_category(self, category: str) -> Path | None:
        if category == "temp_files":
            return self.paths.TMP_DIR
        if category == "derived_cache":
            return self.paths.CACHE_DIR
        if category in {"image_assets", "inbound_videos"}:
            return self.paths.WORKSPACE_DIR / "incoming"
        return None

    def _validate_categories(self, categories: list[str]) -> list[str]:
        cleaned = []
        for raw in categories:
            key = str(raw or "").strip()
            if not key:
                continue
            spec = CATEGORY_SPECS.get(key)
            if spec is None:
                raise StorageMaintenanceError(
                    "unknown_category", f"未知清理类别：{key}"
                )
            if not spec.implemented:
                raise StorageMaintenanceError(
                    "category_not_implemented",
                    f"{spec.label} 本轮未启用：{spec.description}",
                )
            if key not in cleaned:
                cleaned.append(key)
        if not cleaned:
            raise StorageMaintenanceError("empty_categories", "至少选择一个清理类别")
        return cleaned


def _tree_size(
    root: Path,
    *,
    exclude_dirs: set[Path] | None = None,
) -> dict[str, int]:
    """递归统计普通文件大小，不跟随符号链接，不返回文件名。"""

    result = {"bytes": 0, "files": 0, "items": 0}
    try:
        root = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return result
    excluded = {path.resolve() for path in (exclude_dirs or set()) if path.exists()}
    stack = [root]
    while stack:
        current = stack.pop()
        if current in excluded:
            continue
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            stat = entry.stat(follow_symlinks=False)
                            result["bytes"] += int(stat.st_size)
                            result["files"] += 1
                    except OSError:
                        continue
        except OSError:
            continue
    result["items"] = result["files"]
    return result


def _tree_size_matching(
    root: Path,
    extensions: frozenset[str],
) -> dict[str, int]:
    """只统计指定扩展名的普通文件，不跟随符号链接。"""

    result = {"bytes": 0, "files": 0, "items": 0}
    try:
        root = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return result
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            if Path(entry.name).suffix.lower() not in extensions:
                                continue
                            stat = entry.stat(follow_symlinks=False)
                            result["bytes"] += int(stat.st_size)
                            result["files"] += 1
                    except OSError:
                        continue
        except OSError:
            continue
    result["items"] = result["files"]
    return result


def _subtract_sizes(
    base: dict[str, int],
    *parts: dict[str, int],
) -> dict[str, int]:
    bytes_left = int(base.get("bytes") or 0)
    files_left = int(base.get("files") or 0)
    for part in parts:
        bytes_left -= int(part.get("bytes") or 0)
        files_left -= int(part.get("files") or 0)
    bytes_left = max(0, bytes_left)
    files_left = max(0, files_left)
    return {"bytes": bytes_left, "files": files_left, "items": files_left}


def _collect_disk_items(
    root: Path,
    data_dir: Path,
    min_age_seconds: int,
    category: str,
    now: float,
    extensions: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    try:
        root_resolved = root.resolve(strict=True)
        data_resolved = data_dir.resolve(strict=True)
    except (OSError, RuntimeError):
        return []
    try:
        root_resolved.relative_to(data_resolved)
    except ValueError:
        return []
    items: list[dict[str, Any]] = []
    stack = [root_resolved]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if extensions is not None:
                            suffix = Path(entry.name).suffix.lower()
                            if suffix not in extensions:
                                continue
                        stat = entry.stat(follow_symlinks=False)
                        if now - float(stat.st_mtime) < min_age_seconds:
                            continue
                        path = Path(entry.path)
                        items.append(
                            {
                                "category": category,
                                "relative_path": path.relative_to(data_resolved).as_posix(),
                                "size": int(stat.st_size),
                                "mtime_ns": int(stat.st_mtime_ns),
                            }
                        )
                    except OSError:
                        continue
        except OSError:
            continue
    return items


def _category_totals(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "items": len(items),
        "files": len(items),
        "bytes": sum(int(item.get("size") or 0) for item in items),
    }


def _generation_for(categories: list[str], items: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    digest.update("\x00".join(categories).encode("utf-8"))
    for item in sorted(items, key=lambda value: str(value.get("relative_path") or "")):
        digest.update(str(item.get("relative_path") or "").encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(item.get("size") or "").encode("ascii"))
        digest.update(b"\x00")
        digest.update(str(item.get("mtime_ns") or "").encode("ascii"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _disk_usage(path: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"total": 0, "used": 0, "free": 0, "percent": 0.0}
    percent = round((usage.used / usage.total) * 100, 2) if usage.total else 0.0
    return {
        "total": int(usage.total),
        "used": int(usage.used),
        "free": int(usage.free),
        "percent": percent,
    }


def _low_disk_state(disk: dict[str, Any], cfg: Any) -> dict[str, Any]:
    free = int(disk.get("free") or 0)
    total = int(disk.get("total") or 0)
    min_bytes = int(getattr(cfg, "min_free_bytes", 0) or 0)
    min_percent = int(getattr(cfg, "min_free_percent", 0) or 0)
    percent_free = (free / total * 100) if total else 0.0
    warning = free < max(min_bytes, 0) or percent_free < max(min_percent, 0)
    return {
        "warning": bool(warning),
        "free_bytes": free,
        "free_percent": round(percent_free, 2),
        "min_free_bytes": min_bytes,
        "min_free_percent": min_percent,
    }


def _bump(counter: dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
