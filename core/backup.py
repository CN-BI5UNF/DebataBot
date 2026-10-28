"""用户数据一键备份。

备份范围（默认）：
    - config.yaml
    - secrets.enc / secrets.meta / RSA 公钥，以及 RSA 私钥（本地文件或 keyring 导出）
    - memory/（聊天历史数据库）
    - vector/（RAG 向量数据库）
    - workspace/（用户文件、入站媒体；可关闭入站媒体）
    - emoji/、providers/presets/
    - 仓库根 personas/（人格定义）
    - token_calibration.json、persona_brief.json（存在时）

不备份：logs/、cache/、tmp/、models/、config_backups/、维护任务/隔离区、
WebUI token、更新状态。日志和缓存可重建；备份目录本身也会被排除，避免递归。

安全边界：
    - 备份包含加密密钥库和 RSA 私钥，二者结合可解密 API 密钥；压缩包和
      sidecar 都写 0600，目录尽量 0700。
    - 不把任何明文密钥写进 manifest 或日志；manifest 只记录文件名/大小/hash。
    - 下载接口走 WebUI 鉴权，并拒绝路径穿越。
    - SQLite 使用 sqlite3 backup API 生成一致性快照；失败时才退回文件复制并
      在 manifest 中标记 consistent=false。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets as _secrets
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:  # POSIX 跨进程租约；Windows 退化为进程内互斥。
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

TERMINAL_JOB_STATES = {"done", "partial", "failed", "cancelled", "interrupted"}
JOB_SCHEMA_VERSION = 1

BACKUP_FORMAT = 2
BACKUP_TYPE_USER_EXPORT = "user_export"
BACKUP_TYPE_MIGRATION_SNAPSHOT = "migration_snapshot"
BACKUP_PREFIX = "Karen_Backup_"
BACKUP_SUFFIX = ".zip"
BACKUP_NAME_RE = re.compile(r"^Karen_Backup_[A-Za-z0-9_.-]+\.zip$")
SIDECAR_SUFFIX = ".json"
CHUNK_SIZE = 1024 * 1024
SPACE_MARGIN = 64 * 1024 * 1024
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
STORE_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".avif",
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv",
    ".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus",
    ".zip", ".7z", ".rar", ".gz", ".xz", ".bz2",
}
EXCLUDED_NAMES = {
    "__pycache__",
    ".git",
    ".run",
    ".DS_Store",
    "webui_token.txt",
    "update_state.json",
}


class BackupError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class BackupCancelled(BackupError):
    """合作式取消；不是失败，未发布的临时包会被清理。"""

    def __init__(self) -> None:
        super().__init__("backup_cancelled", "备份已取消")


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii"
        ).strip()
    except OSError:
        return ""


def _job_is_terminal(state: str) -> bool:
    return state in TERMINAL_JOB_STATES


_LOCAL_LOCK_GUARD = threading.Lock()
_LOCAL_LOCKS: dict[str, threading.Lock] = {}


def _local_instance_lock(data_dir: Path) -> threading.Lock:
    """无 flock 平台（Windows）的进程内实例锁。"""
    key = str(data_dir)
    with _LOCAL_LOCK_GUARD:
        lock = _LOCAL_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCAL_LOCKS[key] = lock
        return lock


def _fsync_directory(path: Path) -> None:
    """atomic replace 后同步目录项；Windows/不支持时静默跳过。"""
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@dataclass
class _Source:
    path: Path
    arcname: str
    size: int
    kind: str = "file"  # file | sqlite
    data: bytes | None = None  # 仅用于 keyring 导出的 RSA 私钥
    consistent: bool = True


class BackupCoordinator:
    """实例级备份互斥、job 注册表与跨进程租约。

    一个 BackupService 可以随配置/backup.directory 变化而重建，但同一数据
    目录只能有一个协调器。这样切换 include_media 或 runtime 重启时，不会
    出现两个备份同时读写同一批数据库。
    """

    def __init__(self, project_root: Path, paths: Any) -> None:
        self.project_root = Path(project_root).resolve()
        self.data_dir = Path(paths.DATA_DIR).resolve()
        self.jobs_file = self.data_dir / "backup_jobs.json"
        self.lock_file = self.data_dir / ".backup.lock"
        self.boot_id = _boot_id()
        self.pid = os.getpid()
        self.state_lock = threading.Lock()
        self.persist_lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.job_order: list[str] = []
        self.tasks: set[asyncio.Task] = set()
        self.cancel_events: dict[str, threading.Event] = {}
        self.active_job: str | None = None
        self._lock_fd: int | None = None
        self._local_lock = _local_instance_lock(self.data_dir)
        self._local_lock_held = False
        self._start_lock: asyncio.Lock | None = None
        self._start_lock_loop: Any = None
        self._load_jobs(mark_interrupted=False)
        # 只有在没有其他进程持锁时才把未完成 job 标为 interrupted；
        # 否则会覆盖另一个正在运行的进程的权威 job 文件。
        try:
            self.acquire_instance_lock()
        except BackupError:
            pass
        else:
            try:
                self._load_jobs(mark_interrupted=True)
                self.persist()
            finally:
                self.release_instance_lock()

    # -- 生命周期 -------------------------------------------------

    def start_lock(self) -> asyncio.Lock:
        """每个事件循环一个锁；测试可能在多个 asyncio.run 中复用协调器。"""
        loop = asyncio.get_running_loop()
        if self._start_lock is None or self._start_lock_loop is not loop:
            self._start_lock = asyncio.Lock()
            self._start_lock_loop = loop
        return self._start_lock

    def _load_jobs(self, *, mark_interrupted: bool) -> bool:
        """从磁盘重建 job 注册表；调用方负责在需要时持有实例锁。"""
        try:
            raw = json.loads(self.jobs_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        raw_jobs = raw.get("jobs") if isinstance(raw, dict) else None
        if not isinstance(raw_jobs, list):
            return False
        loaded: list[dict] = []
        changed = False
        for item in raw_jobs[-50:]:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            job = dict(item)
            state = str(job.get("state") or "")
            if mark_interrupted and state not in TERMINAL_JOB_STATES:
                # 未完成 job 在进程/协调器重建时不可能还有人写入临时包；
                # 明确标 interrupted，绝不沿用假 running/done。
                job.update({
                    "state": "interrupted",
                    "stage": "interrupted",
                    "finished_at": time.time(),
                    "error": {
                        "code": "backup_interrupted",
                        "message": "服务重启或备份协调器重建，任务已中断；可重新发起备份",
                    },
                    "retryable": True,
                })
                changed = True
            job["cancel_requested"] = bool(job.get("cancel_requested", False))
            loaded.append(job)
        with self.state_lock:
            self.jobs = {job["id"]: job for job in loaded}
            self.job_order = [job["id"] for job in loaded]
        if changed:
            logger.warning("发现未完成备份任务，已标记 interrupted")
        return changed

    def persist(self) -> bool:
        """只持久化元数据/阶段/统计；不含聊天正文、密钥或异常栈。"""
        with self.state_lock:
            jobs = [
                self._public_job(self.jobs[job_id])
                for job_id in self.job_order[-50:]
                if job_id in self.jobs
            ]
        payload = {
            "schema": JOB_SCHEMA_VERSION,
            "pid": self.pid,
            "boot_id": self.boot_id,
            "jobs": jobs,
        }
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.jobs_file.with_name(self.jobs_file.name + ".tmp")
            with self.persist_lock:
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
                os.replace(tmp, self.jobs_file)
                _fsync_directory(self.jobs_file.parent)
                try:
                    os.chmod(self.jobs_file, 0o600)
                except OSError:
                    pass
            return True
        except OSError:
            logger.warning("持久化备份 job 元数据失败", exc_info=True)
            return False

    def acquire_instance_lock(self) -> None:
        if fcntl is None:
            if self._local_lock_held:
                return
            if not self._local_lock.acquire(blocking=False):
                raise BackupError(
                    "backup_running",
                    "另一个备份协调器正在使用该实例",
                )
            self._local_lock_held = True
            return
        if self._lock_fd is not None:
            return
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                self.lock_file,
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
        except OSError as exc:
            raise BackupError(
                "backup_lock_failed", f"无法创建备份实例锁：{exc}"
            ) from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise BackupError(
                "backup_running",
                "另一个 Karen 进程正在备份；同一实例只允许一个备份任务",
            ) from exc
        self._lock_fd = fd

    def release_instance_lock(self) -> None:
        if self._local_lock_held:
            self._local_lock_held = False
            self._local_lock.release()
        fd = self._lock_fd
        self._lock_fd = None
        if fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    @property
    def locked(self) -> bool:
        return self._lock_fd is not None or self._local_lock_held

    def _public_job(self, job: dict) -> dict:
        out = dict(job)
        out["warnings"] = list(job.get("warnings") or [])
        error = job.get("error")
        out["error"] = dict(error) if isinstance(error, dict) else error
        out["retryable"] = bool(job.get("retryable", False))
        return out


def resolve_backup_directory(raw: str | None) -> Path:
    """把配置值解析为绝对目录；空值使用用户主目录下的 Karen_Backups。"""
    text = str(raw or "").strip()
    if not text:
        return (Path.home() / "Karen_Backups").resolve()
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.home() / path
    return path.resolve()


def _coerce_timestamp(value: Any, fallback: Any = 0.0) -> float:
    """sidecar 旧版本可能写 ISO 字符串或 epoch 浮点；统一成可排序的 epoch。"""
    if isinstance(value, bool):
        value = None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    try:
        return float(fallback)
    except (TypeError, ValueError):
        return 0.0


def _format_bytes(value: int) -> str:
    value = max(0, int(value))
    if value >= 1024 ** 3:
        return f"{value / 1024 ** 3:.2f} GiB"
    if value >= 1024 ** 2:
        return f"{value / 1024 ** 2:.1f} MiB"
    if value >= 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value} B"


def _zip_date(timestamp: float) -> tuple[int, int, int, int, int, int]:
    try:
        tm = time.localtime(timestamp)
        if tm.tm_year < 1980:
            return (1980, 1, 1, 0, 0, 0)
        return (tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec)
    except (OSError, OverflowError, ValueError):
        return (1980, 1, 1, 0, 0, 0)


def _is_sqlite_file(path: Path) -> bool:
    if path.suffix.lower() not in SQLITE_SUFFIXES:
        return False
    try:
        with path.open("rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _safe_arcname(parts: tuple[str, ...]) -> str:
    clean: list[str] = []
    for part in parts:
        part = str(part).replace("\\", "/")
        if part in ("", ".", "..") or "/" in part:
            continue
        clean.append(part)
    return "/".join(clean)


class BackupService:
    """异步备份 job 管理器。一次只允许一个备份。"""

    def __init__(
        self,
        project_root: Path,
        paths: Any,
        *,
        backup_dir: Path,
        include_media: bool = True,
        secrets_manager: Any | None = None,
        coordinator: BackupCoordinator | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.paths = paths
        self.directory = Path(backup_dir).resolve()
        self.include_media = bool(include_media)
        self.secrets_manager = secrets_manager
        # 协调器按 DATA_DIR 共享：即使 backup.directory/include_media 改变，
        # 或 Runtime 重启，也不会出现第二个并发备份。
        self._shared = coordinator or BackupCoordinator(self.project_root, paths)

    # -- 共享 job 状态 -------------------------------------------

    def _check_cancel(self, job_id: str | None = None) -> None:
        if job_id is None:
            return
        event = self._shared.cancel_events.get(job_id)
        if event is not None and event.is_set():
            raise BackupCancelled()

    def _persist(self) -> bool:
        return self._shared.persist()

    # ============================================================
    # 状态 / 文件
    # ============================================================

    def _validate_directory(self) -> None:
        """拒绝把备份目录设成数据/人格目录的父目录。

        备份目录在数据目录**内部**是可以的（会从扫描中排除），但如果它是
        DATA_DIR/PERSONAS_DIR 的父目录，扫描会把整个源目录误判为“备份目录本身”
        而全部排除，最后得到一个空备份。这里直接 fail-closed。
        """
        for attr in ("DATA_DIR", "PERSONAS_DIR", "WORKSPACE_DIR"):
            root = getattr(self.paths, attr, None)
            if root is None:
                continue
            try:
                resolved_root = Path(root).resolve()
            except OSError:
                continue
            if resolved_root == self.directory or self._is_relative_to(
                resolved_root, self.directory
            ):
                raise BackupError(
                    "invalid_directory",
                    "备份目录不能是数据目录、人格目录或工作区的父目录；"
                    "请选择独立目录（例如 ~/Karen_Backups）",
                )

    def _ensure_directory(self) -> None:
        existed = self.directory.exists()
        if self.directory.exists() and not self.directory.is_dir():
            raise BackupError("invalid_directory", f"备份路径不是目录：{self.directory}")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackupError(
                "invalid_directory", f"无法创建备份目录 {self.directory}: {exc}"
            ) from exc
        if not existed:
            try:
                os.chmod(self.directory, 0o700)
            except OSError:
                logger.warning("设置备份目录权限失败 %s", self.directory)
        elif os.name != "nt":
            # 已存在的目录不擅自 chmod 用户目录；权限不安全时 fail-closed。
            try:
                st = self.directory.stat()
            except OSError as exc:
                raise BackupError(
                    "invalid_directory", f"无法检查备份目录权限：{exc}"
                ) from exc
            if st.st_uid != os.geteuid():
                raise BackupError(
                    "unsafe_directory",
                    "备份目录不属于当前用户；请换一个当前用户拥有的目录",
                )
            if stat.S_IMODE(st.st_mode) & 0o077:
                raise BackupError(
                    "unsafe_directory",
                    "备份包含密钥材料，目录不能对组或其他用户开放；"
                    f"请先 chmod 700 {self.directory}",
                )

    def _cleanup_stale_workdirs(self) -> None:
        """只在本实例持有备份锁时调用；按 owner/job/boot 判断，不盲删。

        旧版本没有 owner 标记的目录一律保留（只记录日志），避免把另一个
        进程正在写的临时包删掉。
        """
        try:
            if not self.directory.is_dir():
                return
            for entry in self.directory.iterdir():
                if not entry.name.startswith(".backup-work-"):
                    continue
                try:
                    if entry.is_symlink() or not entry.is_dir():
                        continue
                    marker = entry / ".owner.json"
                    if not marker.is_file() or marker.is_symlink():
                        logger.warning("备份临时目录缺少 owner 标记，保留不删：%s", entry)
                        continue
                    data = json.loads(marker.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        continue
                    job_id = str(data.get("job_id") or "")
                    same_process = (
                        int(data.get("pid") or -1) == self._shared.pid
                        and str(data.get("boot_id") or "") == self._shared.boot_id
                    )
                    if same_process and job_id == self._shared.active_job:
                        continue
                    shutil.rmtree(entry, ignore_errors=True)
                except (OSError, ValueError, json.JSONDecodeError):
                    logger.debug("清理旧备份临时目录失败 %s", entry, exc_info=True)
        except OSError:
            logger.debug("扫描备份临时目录失败", exc_info=True)

    def _write_workdir_owner(self, workdir: Path, job_id: str) -> None:
        marker = workdir / ".owner.json"
        try:
            with open(marker, "w", encoding="utf-8") as handle:
                json.dump({
                    "job_id": job_id,
                    "pid": self._shared.pid,
                    "boot_id": self._shared.boot_id,
                    "created_at": time.time(),
                }, handle)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(marker, 0o600)
        except OSError:
            logger.warning("写入备份临时目录 owner 标记失败 %s", workdir)

    def _sidecar_path(self, zip_path: Path) -> Path:
        return zip_path.with_name(zip_path.name + SIDECAR_SUFFIX)

    def list_files(self) -> list[dict]:
        try:
            if not self.directory.is_dir():
                return []
            items = []
            for path in self.directory.glob(BACKUP_PREFIX + "*" + BACKUP_SUFFIX):
                if not path.is_file() or not BACKUP_NAME_RE.match(path.name):
                    continue
                if path.is_symlink():
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                meta: dict[str, Any] = {}
                sidecar = self._sidecar_path(path)
                if sidecar.is_file() and not sidecar.is_symlink():
                    try:
                        meta = json.loads(sidecar.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        logger.warning("备份 sidecar 损坏，尝试从包内 manifest 重建：%s", path.name)
                if not isinstance(meta, dict) or not meta:
                    # sidecar 只是索引缓存；包内 manifest 才是权威。
                    try:
                        with zipfile.ZipFile(path) as archive:
                            raw = archive.read("manifest.json")
                        manifest = json.loads(raw)
                        if isinstance(manifest, dict):
                            meta = manifest
                            meta["size"] = st.st_size
                    except (OSError, KeyError, json.JSONDecodeError, zipfile.BadZipFile):
                        meta = {}
                created_ts = _coerce_timestamp(
                    meta.get("created_at_ts", meta.get("created_at")),
                    st.st_mtime,
                )
                warnings = meta.get("warnings", [])
                if not isinstance(warnings, list):
                    warnings = []
                items.append({
                    "filename": path.name,
                    "size": int(meta.get("size") or st.st_size),
                    "created_at": created_ts,
                    "sha256": meta.get("sha256", ""),
                    "file_count": meta.get("file_count"),
                    "includes_keys": bool(meta.get("includes_keys", False)),
                    "include_media": meta.get("include_media"),
                    "warnings": warnings,
                    "backup_type": meta.get("backup_type", "legacy_user_export"),
                    "format_version": meta.get(
                        "format_version", meta.get("format", 1)
                    ),
                    "validation_status": meta.get("validation_status", "unknown"),
                    "partial": bool(meta.get("partial", False)),
                    "credential_restorable": bool(
                        meta.get("credential_restorable", False)
                    ),
                    "credential_material_present": bool(
                        meta.get("credential_material_present", False)
                    ),
                })
            items.sort(key=lambda item: item.get("created_at") or 0, reverse=True)
            return items
        except OSError:
            logger.warning("读取备份列表失败", exc_info=True)
            return []

    def resolve_download(self, name: str) -> Path:
        name = str(name or "")
        if (
            not name
            or Path(name).name != name
            or not BACKUP_NAME_RE.match(name)
            or "/" in name
            or "\\" in name
            or ".." in name
        ):
            raise BackupError("invalid_name", "备份文件名不合法")
        path = (self.directory / name).resolve()
        try:
            path.relative_to(self.directory.resolve())
        except ValueError:
            raise BackupError("invalid_name", "备份文件不在备份目录内") from None
        if not path.is_file() or path.is_symlink():
            raise BackupError("not_found", "备份文件不存在")
        return path

    def get_job(self, job_id: str) -> dict:
        with self._shared.state_lock:
            job = self._shared.jobs.get(job_id)
            if job is None:
                raise BackupError("not_found", "备份任务不存在")
            return self._public_job(job)

    def status(self) -> dict:
        with self._shared.state_lock:
            jobs = [
                self._public_job(self._shared.jobs[job_id])
                for job_id in reversed(self._shared.job_order[-10:])
                if job_id in self._shared.jobs
            ]
            active_job = self._shared.active_job
            running = active_job is not None
        return {
            "directory": str(self.directory),
            "include_media": self.include_media,
            "running": running,
            "active_job_id": active_job,
            "jobs": jobs,
            "files": self.list_files(),
            "coordinator": {
                "pid": self._shared.pid,
                "boot_id": self._shared.boot_id,
                "jobs_file": str(self._shared.jobs_file),
                "instance_lock": self._shared.locked,
            },
        }

    # ============================================================
    # Job 生命周期
    # ============================================================

    async def start(
        self,
        *,
        include_media: bool | None = None,
        backup_type: str = BACKUP_TYPE_USER_EXPORT,
        barrier: Any | None = None,
        snapshot_meta: dict[str, Any] | None = None,
    ) -> dict:
        from core.snapshot_barrier import SnapshotBarrier

        if backup_type not in {BACKUP_TYPE_USER_EXPORT, BACKUP_TYPE_MIGRATION_SNAPSHOT}:
            raise BackupError("invalid_backup_type", f"未知备份类型：{backup_type}")
        if backup_type == BACKUP_TYPE_MIGRATION_SNAPSHOT:
            if not isinstance(barrier, SnapshotBarrier):
                raise BackupError(
                    "barrier_required",
                    "严格迁移快照必须由可信维护控制器提供 SnapshotBarrier；"
                    "用户导出不能冒充迁移快照",
                )
        elif snapshot_meta is not None:
            raise BackupError(
                "snapshot_meta_invalid", "普通用户导出不能携带迁移快照元数据"
            )
        if snapshot_meta is not None:
            if not isinstance(snapshot_meta, dict):
                raise BackupError("snapshot_meta_invalid", "snapshot_meta 必须是对象")
            try:
                encoded = json.dumps(snapshot_meta, ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                raise BackupError(
                    "snapshot_meta_invalid", f"snapshot_meta 不可序列化：{exc}"
                ) from exc
            if len(encoded.encode("utf-8")) > 64 * 1024:
                raise BackupError("snapshot_meta_invalid", "snapshot_meta 过大")
        async with self._shared.start_lock():
            if self._shared.active_job is not None:
                raise BackupError("backup_running", "已有备份任务在进行中")
            self._validate_directory()
            self._ensure_directory()
            self._shared.acquire_instance_lock()
            # 持锁后磁盘 job 文件才是权威；刷新并清理上次崩溃留下的非终态 job。
            self._shared._load_jobs(mark_interrupted=True)
            # 只有持有实例锁时才清理；否则可能删掉另一进程正在写的临时包。
            self._cleanup_stale_workdirs()
            job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + _secrets.token_hex(3)
            strict = backup_type == BACKUP_TYPE_MIGRATION_SNAPSHOT
            job = {
                "id": job_id,
                "state": "queued",
                "stage": "queued",
                "started_at": time.time(),
                "finished_at": None,
                "total_bytes": 0,
                "processed_bytes": 0,
                "current": "",
                "filename": "",
                "size": 0,
                "sha256": "",
                "file_count": 0,
                "warnings": [],
                "error": None,
                "include_media": self.include_media if include_media is None else bool(include_media),
                "directory": str(self.directory),
                "backup_type": backup_type,
                "strict": strict,
                "snapshot_meta": dict(snapshot_meta or {}),
                "pid": self._shared.pid,
                "boot_id": self._shared.boot_id,
                "cancel_requested": False,
                "retryable": True,
            }
            try:
                with self._shared.state_lock:
                    self._shared.jobs[job_id] = job
                    self._shared.job_order.append(job_id)
                    self._shared.active_job = job_id
                    self._shared.cancel_events[job_id] = threading.Event()
                    while len(self._shared.job_order) > 50:
                        old = self._shared.job_order.pop(0)
                        self._shared.jobs.pop(old, None)
                if not self._persist():
                    with self._shared.state_lock:
                        self._shared.active_job = None
                        self._shared.cancel_events.pop(job_id, None)
                    self._shared.release_instance_lock()
                    raise BackupError(
                        "job_persist_failed",
                        "无法持久化备份任务；为免虚假 durable，已拒绝启动",
                    )
                task = asyncio.create_task(
                    self._run(job_id, barrier=barrier),
                    name=f"backup-{job_id}",
                )
                self._shared.tasks.add(task)
                task.add_done_callback(self._shared.tasks.discard)
            except Exception:
                with self._shared.state_lock:
                    if self._shared.active_job == job_id:
                        self._shared.active_job = None
                    self._shared.cancel_events.pop(job_id, None)
                self._shared.release_instance_lock()
                raise
            return self._public_job(job)

    async def cancel(self, job_id: str) -> dict:
        """请求合作式取消；线程在扫描/写入/SQLite 进度/校验边界响应。"""
        with self._shared.state_lock:
            job = self._shared.jobs.get(job_id)
            if job is None:
                raise BackupError("not_found", "备份任务不存在")
            if _job_is_terminal(str(job.get("state") or "")):
                raise BackupError("not_cancellable", "备份任务已经结束")
            event = self._shared.cancel_events.get(job_id)
            if event is None:
                raise BackupError("not_cancellable", "备份任务尚未开始")
            event.set()
            job["cancel_requested"] = True
            job["state"] = "cancelling"
            job["stage"] = "cancelling"
        self._persist()
        return self.get_job(job_id)

    def _release_marker(self, barrier: Any | None, marker: Any | None) -> None:
        if marker is None or barrier is None:
            return
        try:
            barrier.release(marker)
        except Exception:  # noqa: BLE001
            logger.exception("释放维护屏障失败")

    async def _finalize_job(
        self,
        job_id: str,
        *,
        result: dict[str, Any] | None,
        error: BaseException | None,
        barrier: Any | None,
        marker: Any | None,
        cancelled_externally: bool = False,
    ) -> None:
        """worker 已结算后：持锁写终态，再释放 barrier/flock。"""
        public_result: dict[str, Any] = {}
        if result is not None:
            public_result = dict(result)
            published = bool(public_result.pop("published", False))
            if cancelled_externally and not published:
                error = BackupCancelled()
            elif cancelled_externally and published:
                public_result["cancel_too_late"] = True
                public_result.setdefault("warnings", [])
                public_result["warnings"] = [
                    *list(public_result["warnings"]),
                    "取消请求到达时包已发布；未删除已完成的备份",
                ]
            elif cancelled_externally:
                error = BackupCancelled()
        elif cancelled_externally:
            error = BackupCancelled()

        if result is not None and not (
            cancelled_externally and not public_result.get("cancel_too_late")
        ):
            state = "partial" if result.get("partial") else "done"
            self._update(
                job_id,
                state=state,
                stage="done",
                finished_at=time.time(),
                retryable=False,
                **public_result,
            )
        elif isinstance(error, BackupCancelled):
            logger.info("备份已取消 %s", job_id)
            self._update(
                job_id,
                state="cancelled",
                stage="cancelled",
                finished_at=time.time(),
                error={"code": "backup_cancelled", "message": "备份已取消"},
                retryable=True,
            )
        elif isinstance(error, BackupError):
            logger.warning("备份失败 %s: %s", error.code, error.message)
            self._update(
                job_id,
                state="failed",
                stage="failed",
                finished_at=time.time(),
                error={"code": error.code, "message": error.message},
                retryable=error.code not in {
                    "invalid_directory", "unsafe_directory", "barrier_required"
                },
            )
        else:
            if error is not None:
                logger.error("备份任务异常", exc_info=error)
            else:
                logger.error("备份任务结束但没有结果 job=%s", job_id)
            self._update(
                job_id,
                state="failed",
                stage="failed",
                finished_at=time.time(),
                error={
                    "code": "backup_failed",
                    "message": str(error)[:300] if error is not None else "没有结果",
                },
                retryable=True,
            )

        # 先持久化终态（_update 内 persist），再释放维护屏障和实例锁。
        with self._shared.state_lock:
            strict = bool(self._shared.jobs.get(job_id, {}).get("strict", False))
        if not self._persist():
            logger.critical("备份终态持久化失败 job=%s strict=%s", job_id, strict)
            if strict:
                # 保留 barrier/flock 和 active job，阻止业务恢复/第二个备份；
                # 现场留给管理员对账，不能只因内存里 done 就放行。
                self._update(
                    job_id,
                    state="failed",
                    stage="failed",
                    retryable=True,
                    error={
                        "code": "job_persist_failed",
                        "message": "严格快照终态无法持久化；实例保持锁定，需人工恢复",
                    },
                )
                return
        self._release_marker(barrier, marker)
        with self._shared.state_lock:
            if self._shared.active_job == job_id:
                self._shared.active_job = None
            self._shared.cancel_events.pop(job_id, None)
        # 终态已在上面的持久化中写入；不要 unlock 后再写 job 文件，
        # 否则旧 owner 可能覆盖新 owner 的 registry。
        self._shared.release_instance_lock()

    async def _run(self, job_id: str, *, barrier: Any | None = None) -> None:
        cancel_event = self._shared.cancel_events.get(job_id)
        marker: Any | None = None
        result: dict[str, Any] | None = None
        error: BaseException | None = None
        worker: asyncio.Task | None = None
        handed_off = False
        try:
            with self._shared.state_lock:
                job = dict(self._shared.jobs.get(job_id, {}))
            include_media = bool(job.get("include_media", self.include_media))
            backup_type = str(job.get("backup_type") or BACKUP_TYPE_USER_EXPORT)
            strict = backup_type == BACKUP_TYPE_MIGRATION_SNAPSHOT
            snapshot_meta = job.get("snapshot_meta")
            if not isinstance(snapshot_meta, dict):
                snapshot_meta = {}
            if cancel_event is not None and cancel_event.is_set():
                raise BackupCancelled()
            if strict:
                if barrier is None:
                    raise BackupError("barrier_required", "严格快照缺少维护屏障")
                self._update(
                    job_id,
                    state="running",
                    stage="freezing",
                    retryable=False,
                )
                # 屏障接口是同步的；不让它变成不可取消的 to_thread 线程。
                marker = barrier.freeze()
                self._check_cancel(job_id)
            self._update(
                job_id,
                state="running",
                stage="scanning",
                retryable=False,
            )
            worker = asyncio.create_task(
                asyncio.to_thread(
                    self._create_sync,
                    job_id,
                    include_media,
                    cancel_event,
                    backup_type=backup_type,
                    strict=strict,
                    barrier_meta=(
                        marker.manifest_fields() if marker is not None else None
                    ),
                    snapshot_meta=snapshot_meta,
                ),
                name=f"backup-worker-{job_id}",
            )
            self._shared.tasks.add(worker)
            worker.add_done_callback(self._shared.tasks.discard)
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            # 外层 Task 被取消时，to_thread 的线程仍在跑；不能在这一层释放
            # barrier/flock。交给独立 finalizer 等 worker 真正结算。
            if cancel_event is not None:
                cancel_event.set()
            self._update(
                job_id,
                state="cancelling",
                stage="cancelling",
                cancel_requested=True,
            )
            handed_off = True
            finalizer = asyncio.create_task(
                self._finalize_after_external_cancel(
                    job_id, worker, barrier, marker
                ),
                name=f"backup-finalize-{job_id}",
            )
            self._shared.tasks.add(finalizer)
            finalizer.add_done_callback(self._shared.tasks.discard)
            raise
        except BackupCancelled as exc:
            error = exc
        except BackupError as exc:
            error = exc
        except Exception as exc:  # noqa: BLE001
            error = exc
        finally:
            if not handed_off:
                await self._finalize_job(
                    job_id, result=result, error=error,
                    barrier=barrier, marker=marker,
                )

    async def _finalize_after_external_cancel(
        self,
        job_id: str,
        worker: asyncio.Task | None,
        barrier: Any | None,
        marker: Any | None,
    ) -> None:
        result: dict[str, Any] | None = None
        error: BaseException | None = None
        if worker is not None:
            try:
                result = await worker
            except BaseException as exc:  # noqa: BLE001
                error = exc
        await self._finalize_job(
            job_id,
            result=result,
            error=error,
            barrier=barrier,
            marker=marker,
            cancelled_externally=True,
        )

    async def create_backup(self, *, include_media: bool | None = None) -> dict:
        """测试/脚本便利入口：启动并等待完成。"""
        job = await self.start(include_media=include_media)
        return await self.wait(job["id"])

    async def wait(self, job_id: str, *, timeout: float = 900.0) -> dict:
        deadline = time.monotonic() + max(1.0, float(timeout))
        while True:
            job = self.get_job(job_id)
            if job["state"] in TERMINAL_JOB_STATES:
                return job
            if time.monotonic() >= deadline:
                raise TimeoutError("等待备份完成超时")
            await asyncio.sleep(0.05)

    # ============================================================
    # 同步实现（在线程中运行）
    # ============================================================

    def _credential_capability(
        self, rsa_source: str, rsa_private: bytes | None
    ) -> tuple[bool, bool, str]:
        """返回 (material_present, restorable_material_verified, verification)。"""
        secrets_file = Path(self.paths.SECRETS_FILE)
        meta_file = Path(self.paths.SECRETS_META_FILE)
        public_file = Path(self.paths.RSA_PUBLIC_KEY_FILE)
        material = (
            secrets_file.is_file()
            and meta_file.is_file()
            and public_file.is_file()
            and rsa_source != "missing"
        )
        if not material:
            return False, False, "material_incomplete"
        private_bytes = rsa_private
        if private_bytes is None and rsa_source == "file":
            try:
                private_bytes = Path(self.paths.RSA_PRIVATE_KEY_FILE).read_bytes()
            except OSError:
                return True, False, "private_key_unreadable"
        if not private_bytes:
            return True, False, "private_key_missing"
        try:
            from cryptography.hazmat.primitives import serialization

            private = serialization.load_pem_private_key(private_bytes, password=None)
            public = serialization.load_pem_public_key(public_file.read_bytes())
            matched = (
                private.public_key().public_numbers()
                == public.public_numbers()
            )
            return True, bool(matched), (
                "key_pair_matched" if matched else "key_pair_mismatch"
            )
        except Exception:  # noqa: BLE001
            return True, False, "key_pair_unverified"

    def _verify_archive(self, path: Path, job_id: str | None = None) -> dict:
        """关闭 ZIP 后重开验证 CRC/成员集合/逐成员 size+hash/SQLite 头。"""
        self._check_cancel(job_id)
        try:
            with zipfile.ZipFile(path) as archive:
                bad = archive.testzip()
                if bad:
                    raise BackupError(
                        "backup_verify_failed", f"ZIP CRC 校验失败：{bad}"
                    )
                names = archive.namelist()
                if len(names) != len(set(names)):
                    raise BackupError("backup_verify_failed", "ZIP 含重复成员")
                try:
                    manifest = json.loads(archive.read("manifest.json"))
                except (KeyError, json.JSONDecodeError) as exc:
                    raise BackupError(
                        "backup_verify_failed", f"manifest 不可读：{exc}"
                    ) from exc
                entries = manifest.get("files")
                if not isinstance(entries, list):
                    raise BackupError("backup_verify_failed", "manifest.files 非法")
                expected = {"README_RESTORE.txt", "manifest.json"} | {
                    entry.get("path") for entry in entries
                    if isinstance(entry, dict) and entry.get("path")
                }
                if set(names) != expected:
                    raise BackupError(
                        "backup_verify_failed", "ZIP 成员集合与 manifest 不一致"
                    )
                for entry in entries:
                    self._check_cancel(job_id)
                    if not isinstance(entry, dict):
                        raise BackupError("backup_verify_failed", "manifest 条目非法")
                    name = entry.get("path")
                    info = archive.getinfo(name)
                    if int(info.file_size) != int(entry.get("size", -1)):
                        raise BackupError(
                            "backup_verify_failed", f"成员大小不一致：{name}"
                        )
                    digest = hashlib.sha256()
                    with archive.open(name) as handle:
                        while True:
                            self._check_cancel(job_id)
                            chunk = handle.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            digest.update(chunk)
                    if digest.hexdigest() != entry.get("sha256"):
                        raise BackupError(
                            "backup_verify_failed", f"成员 hash 不一致：{name}"
                        )
                    if entry.get("kind") == "sqlite":
                        with archive.open(name) as handle:
                            header = handle.read(16)
                        if header != b"SQLite format 3\x00":
                            raise BackupError(
                                "backup_verify_failed",
                                f"SQLite 成员头非法：{name}",
                            )
                return manifest
        except BackupError:
            raise
        except (OSError, zipfile.BadZipFile, KeyError) as exc:
            raise BackupError(
                "backup_verify_failed", f"ZIP 校验失败：{exc}"
            ) from exc

    def _create_sync(
        self,
        job_id: str,
        include_media: bool,
        cancel_event: threading.Event | None = None,
        *,
        backup_type: str = BACKUP_TYPE_USER_EXPORT,
        strict: bool = False,
        barrier_meta: dict[str, Any] | None = None,
        snapshot_meta: dict[str, Any] | None = None,
    ) -> dict:
        warnings: list[str] = []
        self._check_cancel(job_id)
        self._update(job_id, state="running", stage="scanning")
        sources, rsa_private, rsa_source = self._collect_sources(
            warnings, include_media, job_id=job_id, strict=strict
        )
        self._check_cancel(job_id)
        if rsa_private is not None:
            sources.append(_Source(
                path=Path("<keyring>"),
                arcname="data/rsa_private.pem",
                size=len(rsa_private),
                kind="key",
                data=rsa_private,
            ))
        if not sources:
            raise BackupError("no_data", "没有找到可备份的用户数据")
        if strict and not include_media:
            raise BackupError(
                "strict_media_required",
                "严格迁移快照必须包含 workspace/incoming 媒体，不能沿用用户导出的排除开关",
            )
        self._validate_known_databases(sources, strict, warnings)
        if strict:
            self._validate_strict_sources(sources, rsa_source)

        total_bytes = sum(max(0, item.size) for item in sources)
        largest_sqlite = max(
            (item.size for item in sources if item.kind == "sqlite"), default=0
        )
        needed = total_bytes + largest_sqlite + SPACE_MARGIN
        try:
            free = shutil.disk_usage(self.directory).free
        except OSError as exc:
            raise BackupError("space_check_failed", f"无法检查备份磁盘空间：{exc}") from exc
        if free < needed:
            raise BackupError(
                "insufficient_space",
                f"备份空间不足：需要约 {_format_bytes(needed)}，"
                f"备份目录可用 {_format_bytes(free)}",
            )

        self._update(
            job_id,
            stage="writing",
            total_bytes=total_bytes,
            processed_bytes=0,
            warnings=list(warnings),
        )
        self._ensure_directory()
        filename = self._next_filename()
        workdir = Path(tempfile.mkdtemp(prefix=".backup-work-", dir=self.directory))
        try:
            os.chmod(workdir, 0o700)
        except OSError:
            pass
        self._write_workdir_owner(workdir, job_id)
        tmp_zip = workdir / (filename + ".tmp")
        final_zip = self.directory / filename
        entries: list[dict] = []
        ledger_probes: list[dict] = []
        try:
            with zipfile.ZipFile(
                tmp_zip,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=3,
                allowZip64=True,
            ) as archive:
                archive.writestr("README_RESTORE.txt", self._restore_readme())
                for index, source in enumerate(sources, start=1):
                    self._check_cancel(job_id)
                    arcname = _safe_arcname(tuple(source.arcname.split("/")))
                    if not arcname:
                        continue
                    self._update(job_id, current=source.arcname)
                    if source.data is not None:
                        entry = self._add_bytes(
                            archive, arcname, source.data, source.kind, job_id
                        )
                    elif source.kind == "sqlite":
                        entry = self._add_sqlite(
                            archive, source, workdir, index, job_id, warnings,
                            strict=strict,
                            ledger_probes=ledger_probes if strict else None,
                        )
                    else:
                        entry = self._add_file(
                            archive, source, job_id, warnings, strict=strict
                        )
                    if entry is not None:
                        entries.append(entry)
                        self._update(job_id, warnings=list(warnings))

                secrets_exists = bool(
                    getattr(self.paths, "SECRETS_FILE", None)
                    and Path(self.paths.SECRETS_FILE).is_file()
                )
                material_present, credential_restorable, credential_verification = (
                    self._credential_capability(rsa_source, rsa_private)
                )
                db_entries = [
                    entry for entry in entries if entry.get("kind") == "sqlite"
                ]
                inconsistent = any(
                    not bool(entry.get("consistent", True)) for entry in db_entries
                )
                skipped = any(
                    ("跳过" in warning) or ("失败" in warning)
                    for warning in warnings
                )
                missing: list[str] = []
                if not Path(self.paths.CONFIG_FILE).is_file():
                    missing.append("config.yaml")
                if not secrets_exists:
                    missing.append("secrets.enc")
                if not Path(self.paths.SECRETS_META_FILE).is_file():
                    missing.append("secrets.meta")
                if rsa_source == "missing":
                    missing.append("rsa_private_key")
                partial = bool(
                    inconsistent
                    or skipped
                    or missing
                    or (material_present and not credential_restorable)
                )
                chat_restorable = any(
                    entry.get("path", "").startswith("data/memory/")
                    and bool(entry.get("consistent", True))
                    for entry in db_entries
                )
                media_complete = bool(include_media) and not any(
                    "workspace/incoming" in warning for warning in warnings
                )
                all_sqlite_consistent = all(
                    bool(entry.get("consistent", True)) for entry in db_entries
                )
                if strict and partial:
                    raise BackupError(
                        "strict_snapshot_partial",
                        "严格迁移快照存在缺项/不一致项，拒绝发布为迁移材料",
                    )
                if strict and not credential_restorable:
                    raise BackupError(
                        "strict_credentials_failed",
                        "严格迁移快照的密钥材料无法验证，拒绝发布",
                    )
                if strict and not all_sqlite_consistent:
                    raise BackupError(
                        "strict_snapshot_partial",
                        "严格迁移快照存在未一致性快照的数据库",
                    )
                validation_status = "partial" if partial else "verified"
                barrier_fields = dict(barrier_meta or {})
                ledger_artifact_present = any(
                    "ledger" in entry.get("path", "").lower()
                    or "tombstone" in entry.get("path", "").lower()
                    or "deletion" in entry.get("path", "").lower()
                    for entry in entries
                )
                ledger_summary: dict[str, Any] | None = None
                ledger_aware = False
                ledger_complete = False
                contract_match = False
                contract_epoch_source = 0
                migration_set_source = ""
                barrier_epoch = 0
                barrier_set = ""
                barrier_generation = 0
                deletion_ledger_present = False
                migration_safe = False
                if strict:
                    from core.d01 import (
                        D01_CONTRACT_EPOCH,
                        D01_MIGRATION_ID,
                        aggregate_ledger_evidence,
                        ledger_evidence_digest,
                    )

                    ledger_summary = aggregate_ledger_evidence(ledger_probes)
                    memory_paths = {
                        str(entry.get("path") or "")
                        for entry in entries
                        if str(entry.get("path") or "").startswith("data/memory/")
                        and entry.get("kind") == "sqlite"
                    }
                    probed_paths = {
                        str(item.get("label") or "")
                        for item in ledger_probes
                    }
                    missing_probes = sorted(memory_paths - probed_paths)
                    if missing_probes:
                        ledger_summary["complete"] = False
                        ledger_summary.setdefault("issues", []).append(
                            "ledger_probe_missing:"
                            + ",".join(missing_probes[:5])
                        )
                        ledger_summary["digest"] = ledger_evidence_digest(
                            ledger_summary
                        )
                    ledger_aware = bool(ledger_summary.get("aware"))
                    ledger_complete = bool(ledger_summary.get("complete"))
                    # 语义账本存在不等于迁移安全；契约 epoch/set 还必须由
                    # 维护屏障与受控 compatibility_state 共同证明。
                    try:
                        from core.compat import read_local_state

                        local_state = read_local_state(
                            Path(self.paths.DATA_DIR),
                            config_path=Path(self.paths.CONFIG_FILE),
                        )
                        contract_epoch_source = int(
                            local_state.data_contract_epoch
                        )
                        migration_set_source = str(
                            local_state.migration_set_id
                        )
                        local_contract_ready = bool(
                            not local_state.unreadable
                            and local_state.phase
                            not in {
                                "preparing", "running", "failed",
                                "recovery_required",
                            }
                            and contract_epoch_source >= D01_CONTRACT_EPOCH
                            and migration_set_source == D01_MIGRATION_ID
                        )
                    except Exception:  # noqa: BLE001 - fail closed
                        local_contract_ready = False
                    try:
                        barrier_epoch = int(
                            barrier_fields.get("contract_epoch", 0) or 0
                        )
                    except (TypeError, ValueError):
                        barrier_epoch = -1
                    barrier_set = str(
                        barrier_fields.get("migration_set_id", "") or ""
                    )
                    try:
                        barrier_generation = int(
                            barrier_fields.get("generation", 0) or 0
                        )
                    except (TypeError, ValueError):
                        barrier_generation = 0
                    contract_match = bool(
                        ledger_complete
                        and D01_MIGRATION_ID
                        in (ledger_summary.get("migration_ids") or [])
                        and local_contract_ready
                        and barrier_epoch == contract_epoch_source
                        and barrier_set == migration_set_source
                    )
                    deletion_ledger_present = bool(
                        ledger_aware and ledger_complete
                    )
                    migration_safe = bool(
                        barrier_generation > 0
                        and all_sqlite_consistent
                        and ledger_complete
                        and contract_match
                    )
                snapshot_fields = None
                if strict:
                    if migration_safe:
                        blocked_reason = ""
                    elif not all_sqlite_consistent:
                        blocked_reason = "sqlite_inconsistent"
                    elif not ledger_aware:
                        blocked_reason = "semantic_deletion_ledger_required"
                    elif not ledger_complete:
                        blocked_reason = "ledger_evidence_incomplete"
                    elif not contract_match:
                        blocked_reason = (
                            "contract_epoch_or_migration_set_mismatch"
                        )
                    else:
                        blocked_reason = "barrier_attestation_required"
                    snapshot_fields = {
                        "kind": "strict_current_epoch",
                        "epoch": barrier_fields.get("epoch", 0),
                        "generation": barrier_fields.get("generation", 0),
                        "high_water": barrier_fields.get("high_water", 0),
                        "contract_epoch": barrier_fields.get("contract_epoch", 0),
                        "migration_set_id": barrier_fields.get("migration_set_id", ""),
                        "features": list(barrier_fields.get("features") or []),
                        "deletion_ledger_present": bool(deletion_ledger_present),
                        "ledger_artifact_present": bool(ledger_artifact_present),
                        "ledger_aware": ledger_aware,
                        "ledger_complete": ledger_complete,
                        "ledger_digest": str(
                            (ledger_summary or {}).get("digest") or ""
                        ),
                        "ledger_schema_version": int(
                            (ledger_summary or {}).get("schema_version") or 0
                        ),
                        "ledger_migration_ids": list(
                            (ledger_summary or {}).get("migration_ids") or []
                        ),
                        "contract_epoch_source": contract_epoch_source,
                        "migration_set_id_source": migration_set_source,
                        "contract_epoch_barrier": barrier_epoch,
                        "migration_set_id_barrier": barrier_set,
                        "contract_match": contract_match,
                        "barrier_reported_migration_safe": bool(
                            barrier_fields.get("migration_safe")
                        ),
                        "migration_blocked_reason": blocked_reason,
                    }
                    if snapshot_meta:
                        # 水位/epoch 以屏障为准；bootstrap 额外元数据不能
                        # 覆盖 migration_safe/ledger 结论。
                        protected = {
                            "epoch", "generation", "high_water",
                            "contract_epoch", "migration_set_id",
                            "deletion_ledger_present", "migration_blocked_reason",
                            "ledger_artifact_present", "ledger_aware",
                            "ledger_complete", "ledger_digest",
                            "ledger_schema_version", "ledger_migration_ids",
                            "contract_epoch_source", "migration_set_id_source",
                            "contract_epoch_barrier", "migration_set_id_barrier",
                            "contract_match", "barrier_reported_migration_safe",
                        }
                        for key, value in snapshot_meta.items():
                            if isinstance(key, str) and key not in protected:
                                snapshot_fields[key] = value
                manifest = {
                    "format": BACKUP_FORMAT,
                    "format_version": BACKUP_FORMAT,
                    "backup_type": backup_type,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "created_at_ts": time.time(),
                    "app_version": self._app_version(),
                    "git_commit": self._git_commit(),
                    "instance": getattr(self.paths, "instance_name", "default"),
                    "includes_secrets": secrets_exists,
                    "includes_keys": bool(secrets_exists or rsa_source != "missing"),
                    "credential_material_present": material_present,
                    "credential_restorable": credential_restorable,
                    "credential_verification": credential_verification,
                    "rsa_private_key_source": rsa_source,
                    "include_media": include_media,
                    "files": entries,
                    "missing": missing,
                    "partial": partial,
                    "validation_status": validation_status,
                    "strict": bool(strict),
                    "snapshot": snapshot_fields,
                    "capabilities": {
                        "chat_restorable": bool(chat_restorable),
                        "media_complete": bool(media_complete),
                        "credential_restorable": bool(credential_restorable),
                        "migration_safe": migration_safe,
                        "cross_plane_consistent": bool(strict and all_sqlite_consistent),
                        "ledger_aware": bool(strict and ledger_aware),
                        "ledger_complete": bool(strict and ledger_complete),
                    },
                    "consistency_scope": (
                        "maintenance_barrier_and_sqlite_backup_api"
                        if strict else "sqlite_backup_api_per_database"
                    ),
                    "excluded": [
                        "logs/", "cache/", "tmp/", "models/", "config_backups/",
                        "webui_token.txt", "update_state.json", "备份目录本身",
                    ] + ([] if strict else ["maintenance/ 与 maintenance.sqlite3"]),
                    "warnings": list(warnings),
                    "restore_hint": (
                        "严格快照必须配合受信 launcher/维护控制器和恢复演练使用；"
                        "ZIP 未加密，包含敏感凭据。"
                        if strict else
                        "停止服务后按 README_RESTORE.txt 恢复；ZIP 未加密，包含敏感凭据。"
                    ),
                }
                if strict:
                    manifest["ledger"] = ledger_summary
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )

            self._update(job_id, stage="verifying", current="")
            verified_manifest = self._verify_archive(tmp_zip, job_id)
            if verified_manifest.get("backup_type") != backup_type:
                raise BackupError("backup_verify_failed", "manifest backup_type 不一致")
            if strict:
                from core.restore import verify_backup

                deep = verify_backup(tmp_zip, deep_sqlite=True)
                if not deep.get("ok") or not deep.get("credential_restorable"):
                    details = "; ".join(
                        item["message"] for item in deep.get("errors", [])[:3]
                    ) or "凭据材料无法验证"
                    raise BackupError(
                        "strict_snapshot_verify_failed",
                        f"严格快照发布前复验失败：{details}",
                    )
            self._update(job_id, stage="hashing", current="")
            zip_sha = self._sha256_file(tmp_zip, job_id=job_id)
            size = tmp_zip.stat().st_size
            try:
                os.chmod(tmp_zip, 0o600)
            except OSError:
                pass
            self._check_cancel(job_id)
            try:
                with open(tmp_zip, "rb") as handle:
                    os.fsync(handle.fileno())
            except OSError:
                pass
            os.replace(tmp_zip, final_zip)
            _fsync_directory(self.directory)
            try:
                os.chmod(final_zip, 0o600)
            except OSError:
                pass
            sidecar = {
                "format": BACKUP_FORMAT,
                "format_version": BACKUP_FORMAT,
                "backup_type": backup_type,
                "strict": bool(strict),
                "snapshot": snapshot_fields,
                "filename": filename,
                "size": size,
                "sha256": zip_sha,
                "created_at": manifest["created_at"],
                "created_at_ts": manifest["created_at_ts"],
                "file_count": len(entries),
                "includes_keys": manifest["includes_keys"],
                "credential_material_present": material_present,
                "credential_restorable": credential_restorable,
                "credential_verification": credential_verification,
                "include_media": include_media,
                "rsa_private_key_source": rsa_source,
                "partial": partial,
                "validation_status": validation_status,
                "capabilities": manifest["capabilities"],
                "missing": missing,
                "warnings": list(warnings),
            }
            try:
                self._write_sidecar(final_zip, sidecar)
            except OSError as exc:
                warnings.append(f"备份元数据写入失败（压缩包仍可用）：{exc}")
                self._update(job_id, warnings=list(warnings))
            return {
                "filename": filename,
                "size": size,
                "sha256": zip_sha,
                "file_count": len(entries),
                "warnings": list(warnings),
                "partial": partial,
                "validation_status": validation_status,
                "backup_type": backup_type,
                "strict": bool(strict),
                "snapshot": snapshot_fields,
                "credential_restorable": credential_restorable,
                "credential_material_present": material_present,
                "capabilities": manifest["capabilities"],
                "missing": missing,
                "published": True,
            }
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _collect_sources(
        self,
        warnings: list[str],
        include_media: bool,
        *,
        job_id: str | None = None,
        strict: bool = False,
    ) -> tuple[list[_Source], bytes | None, str]:
        sources: list[_Source] = []
        paths = self.paths

        def add_file(path: Path, arcname: str) -> None:
            self._check_cancel(job_id)
            try:
                if not path.is_file() or path.is_symlink():
                    return
                st = path.stat()
                kind = "sqlite" if _is_sqlite_file(path) else "file"
                sources.append(_Source(path=path, arcname=arcname, size=st.st_size, kind=kind))
            except OSError as exc:
                warnings.append(f"跳过无法读取的文件 {path.name}: {exc}")

        def add_tree(root: Path, prefix: str) -> None:
            self._check_cancel(job_id)
            try:
                if not root.is_dir():
                    return
                for current, dirs, files in os.walk(root, followlinks=False):
                    self._check_cancel(job_id)
                    current_path = Path(current)
                    dirs[:] = [
                        name for name in dirs
                        if not self._excluded_name(name)
                        and not self._is_excluded(
                            current_path / name, include_media, strict=strict
                        )
                    ]
                    for name in files:
                        self._check_cancel(job_id)
                        path = current_path / name
                        if self._excluded_name(name) or self._is_excluded(
                            path, include_media, strict=strict
                        ):
                            continue
                        try:
                            if path.is_symlink() or not path.is_file():
                                continue
                            rel = path.relative_to(root)
                            arcname = _safe_arcname((*prefix.split("/"), *rel.parts))
                            if not arcname:
                                continue
                            st = path.stat()
                            kind = "sqlite" if _is_sqlite_file(path) else "file"
                            sources.append(_Source(
                                path=path, arcname=arcname, size=st.st_size, kind=kind
                            ))
                        except OSError as exc:
                            warnings.append(f"跳过无法读取的路径 {path.name}: {exc}")
            except OSError as exc:
                warnings.append(f"跳过无法遍历的目录 {root.name}: {exc}")

        for path, arcname in (
            (paths.CONFIG_FILE, "data/config.yaml"),
            (paths.SECRETS_FILE, "data/secrets.enc"),
            (paths.SECRETS_META_FILE, "data/secrets.meta"),
            (paths.RSA_PUBLIC_KEY_FILE, "data/rsa_public.pem"),
            (paths.TOKEN_CALIBRATION_FILE, "data/token_calibration.json"),
            (paths.DATA_DIR / "persona_brief.json", "data/persona_brief.json"),
            (paths.DATA_DIR / "slang.json", "data/slang.json"),
            (paths.DATA_DIR / "knowledge.json", "data/knowledge.json"),
            (paths.DATA_DIR / "knowledge_state.json", "data/knowledge_state.json"),
        ):
            add_file(Path(path), arcname)
        if not Path(paths.CONFIG_FILE).is_file():
            warnings.append("未找到 config.yaml；备份中缺少主配置。")
        if not Path(paths.SECRETS_FILE).is_file():
            warnings.append("未找到 secrets.enc；API 密钥可能无法恢复。")

        add_tree(Path(paths.MEMORY_DIR), "data/memory")
        add_tree(Path(paths.VECTOR_DIR), "data/vector")
        add_tree(Path(paths.EMOJI_DIR), "data/emoji")
        add_tree(Path(paths.WORKSPACE_DIR), "data/workspace")
        add_tree(Path(paths.USER_PROVIDER_PRESETS_DIR), "data/providers/presets")
        add_tree(Path(paths.PERSONAS_DIR), "personas")
        if strict:
            # user_export 不包含维护/墓碑；strict 快照必须包含维护库和整个
            # maintenance/（其中可能有 deletion ledger、jobs、隔离元数据）。
            add_file(Path(paths.MAINTENANCE_DB_FILE), "data/maintenance.sqlite3")
            add_tree(Path(paths.MAINTENANCE_DIR), "data/maintenance")
            add_file(
                Path(paths.DATA_DIR) / "compatibility_state.json",
                "data/compatibility_state.json",
            )

        # RSA 私钥：优先 keyring 导出；keyring 不可用时退回文件。
        rsa_private: bytes | None = None
        rsa_source = "missing"
        if self.secrets_manager is not None:
            try:
                exported = self.secrets_manager.export_rsa_private_key()
                if exported:
                    rsa_private = exported.encode("ascii", "strict")
                    rsa_source = "keyring"
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"无法从系统 keyring 导出 RSA 私钥：{exc}")
        if rsa_private is None:
            private_file = Path(paths.RSA_PRIVATE_KEY_FILE)
            if private_file.is_file() and not private_file.is_symlink():
                add_file(private_file, "data/rsa_private.pem")
                rsa_source = "file"
            else:
                warnings.append(
                    "未找到 RSA 私钥；如果密钥保存在系统 keyring 且导出失败，"
                    "恢复后 API 密钥可能无法解密。"
                )

        if include_media is False:
            warnings.append("已按配置排除 workspace/incoming 入站媒体。")

        # 去重（同一路径可能同时命中列表和目录；manifest 只保留第一次）。
        unique: dict[str, _Source] = {}
        for item in sources:
            unique.setdefault(item.arcname, item)
        return list(unique.values()), rsa_private, rsa_source

    def _validate_known_databases(
        self, sources: list[_Source], strict: bool, warnings: list[str]
    ) -> None:
        """已知数据库读头失败必须是错误，不能当普通文件悄悄复制。"""
        for source in sources:
            name = source.arcname
            known = (
                name.startswith("data/memory/")
                or name.startswith("data/vector/")
                or name == "data/maintenance.sqlite3"
            )
            if not known or source.kind == "sqlite":
                continue
            if Path(name).suffix.lower() not in SQLITE_SUFFIXES:
                continue
            if strict:
                raise BackupError(
                    "snapshot_invalid_database",
                    f"已知数据库读取 SQLite 头失败：{name}",
                )
            if source.kind != "key":
                # 让 user_export 至少明确标 partial，而不是静默当普通文件。
                source.consistent = False
                warnings.append(
                    f"已知数据库读取 SQLite 头失败（按普通文件备份）：{name}"
                )

    def _validate_strict_sources(
        self, sources: list[_Source], rsa_source: str
    ) -> None:
        names = {item.arcname for item in sources}
        required = {
            "data/config.yaml",
            "data/secrets.enc",
            "data/secrets.meta",
            "data/rsa_public.pem",
            "data/rsa_private.pem",
            "data/maintenance.sqlite3",
        }
        missing = sorted(required - names)
        if rsa_source == "missing" and "data/rsa_private.pem" not in names:
            missing.append("rsa_private_key")
        if missing:
            raise BackupError(
                "snapshot_missing_required",
                "严格迁移快照缺少必需项：" + ", ".join(sorted(set(missing))),
            )

    def _excluded_name(self, name: str) -> bool:
        return (
            name in EXCLUDED_NAMES
            or name.endswith(".pyc")
            or name.endswith(".pyo")
            or name.endswith("-wal")
            or name.endswith("-shm")
            or name.endswith(".tmp")
        )

    def _is_excluded(
        self,
        path: Path,
        include_media: bool | None = None,
        *,
        strict: bool = False,
    ) -> bool:
        media_enabled = self.include_media if include_media is None else bool(include_media)
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            return True
        if resolved == self.directory or self._is_relative_to(resolved, self.directory):
            return True
        excluded_roots = [
            getattr(self.paths, "LOGS_DIR", None),
            getattr(self.paths, "CACHE_DIR", None),
            getattr(self.paths, "TMP_DIR", None),
            getattr(self.paths, "MODELS_DIR", None),
            Path(self.paths.DATA_DIR) / "config_backups",
        ]
        if not strict:
            excluded_roots.append(Path(self.paths.DATA_DIR) / "maintenance")
        if not media_enabled:
            excluded_roots.append(Path(self.paths.WORKSPACE_DIR) / "incoming")
        for root in excluded_roots:
            if root is None:
                continue
            try:
                root_resolved = Path(root).resolve(strict=False)
            except OSError:
                continue
            if resolved == root_resolved or self._is_relative_to(resolved, root_resolved):
                return True
        return False

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _next_filename(self) -> str:
        base = BACKUP_PREFIX + time.strftime("%Y%m%d_%H%M%S", time.localtime())
        candidate = base + BACKUP_SUFFIX
        counter = 2
        while (self.directory / candidate).exists():
            candidate = f"{base}_{counter}{BACKUP_SUFFIX}"
            counter += 1
        return candidate

    def _add_bytes(
        self, archive: zipfile.ZipFile, arcname: str, data: bytes, kind: str, job_id: str
    ) -> dict:
        info = zipfile.ZipInfo(arcname, date_time=time.localtime()[:6])
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = (0o600 & 0xFFFF) << 16
        digest = hashlib.sha256(data)
        archive.writestr(info, data)
        self._add_progress(job_id, len(data))
        return {
            "path": arcname,
            "size": len(data),
            "sha256": digest.hexdigest(),
            "kind": kind,
            "consistent": True,
        }

    def _add_file(
        self,
        archive: zipfile.ZipFile,
        source: _Source,
        job_id: str,
        warnings: list[str],
        *,
        strict: bool = False,
    ) -> dict | None:
        try:
            self._check_cancel(job_id)
            st = source.path.stat()
            info = zipfile.ZipInfo(source.arcname, date_time=_zip_date(st.st_mtime))
            info.compress_type = (
                zipfile.ZIP_STORED
                if source.path.suffix.lower() in STORE_SUFFIXES
                else zipfile.ZIP_DEFLATED
            )
            info.external_attr = (stat.S_IMODE(st.st_mode) & 0xFFFF) << 16
            digest = hashlib.sha256()
            written = 0
            with source.path.open("rb") as src, archive.open(info, "w") as dest:
                while True:
                    self._check_cancel(job_id)
                    chunk = src.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    dest.write(chunk)
                    written += len(chunk)
                    self._add_progress(job_id, len(chunk))
            consistent = True
            try:
                after = source.path.stat()
                if (
                    after.st_size != written
                    or after.st_mtime_ns != st.st_mtime_ns
                ):
                    if strict:
                        raise BackupError(
                            "snapshot_changed",
                            f"严格快照期间文件发生变化：{source.arcname}",
                        )
                    warnings.append(
                        f"文件在备份期间发生变化：{source.arcname}；"
                        "该成员标记为不一致"
                    )
                    consistent = False
            except OSError as exc:
                if strict:
                    raise BackupError(
                        "snapshot_changed",
                        f"严格快照无法复核文件身份：{source.arcname}: {exc}",
                    ) from exc
                warnings.append(f"备份后无法复核文件身份：{source.arcname}")
                consistent = False
            return {
                "path": source.arcname,
                "size": written,
                "sha256": digest.hexdigest(),
                "kind": "sqlite" if source.kind == "sqlite" else "file",
                "consistent": consistent,
            }
        except OSError as exc:
            warnings.append(f"备份文件失败，已跳过 {source.arcname}: {exc}")
            return None

    def _add_sqlite(
        self,
        archive: zipfile.ZipFile,
        source: _Source,
        workdir: Path,
        index: int,
        job_id: str,
        warnings: list[str],
        *,
        strict: bool = False,
        ledger_probes: list[dict] | None = None,
    ) -> dict | None:
        snapshot = workdir / f"sqlite-{index}.db"
        self._check_cancel(job_id)
        if not source.path.is_file():
            if strict:
                raise BackupError(
                    "snapshot_missing_source",
                    f"严格快照的数据库不存在：{source.arcname}",
                )
            warnings.append(f"备份数据库失败，源不存在：{source.arcname}")
            return None
        try:
            # 优先只读 URI，避免源文件消失时创建空库；WAL 中已提交的数据由
            # backup() 读取。只读模式在个别缺少 -shm 的环境可能失败，user_export
            # 才允许退回普通只读连接，strict 不允许掩盖。
            uri = source.path.resolve().as_uri() + "?mode=ro"
            try:
                src_conn = sqlite3.connect(uri, uri=True, timeout=30)
            except sqlite3.Error:
                if strict:
                    raise
                src_conn = sqlite3.connect(str(source.path), timeout=30)
            dst_conn = sqlite3.connect(str(snapshot), timeout=30)

            def progress(_status: Any, _remaining: int, _total: int) -> None:
                self._check_cancel(job_id)

            try:
                src_conn.execute("PRAGMA query_only=ON")
                src_conn.backup(dst_conn, pages=256, progress=progress)
            finally:
                dst_conn.close()
                src_conn.close()
            consistent = True
            read_path = snapshot
        except BackupCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise BackupError(
                    "strict_snapshot_failed",
                    f"严格快照 SQLite backup 失败 {source.arcname}: {exc}",
                ) from exc
            warnings.append(
                f"SQLite 一致性快照失败，退回文件复制 {source.arcname}: {exc}"
            )
            self._check_cancel(job_id)
            try:
                shutil.copy2(source.path, snapshot)
            except OSError as copy_exc:
                warnings.append(f"备份数据库失败，已跳过 {source.arcname}: {copy_exc}")
                return None
            consistent = False
            read_path = snapshot
        self._check_cancel(job_id)
        try:
            check_conn = sqlite3.connect(str(snapshot), timeout=30)
            try:
                row = check_conn.execute("PRAGMA quick_check").fetchone()
            finally:
                check_conn.close()
            if not row or row[0] != "ok":
                if strict:
                    raise BackupError(
                        "strict_snapshot_failed",
                        f"严格快照 SQLite quick_check 未通过：{source.arcname}",
                    )
                warnings.append(
                    f"SQLite 快照 quick_check 未通过：{source.arcname}；"
                    "该成员标记为不一致"
                )
                consistent = False
        except BackupCancelled:
            raise
        except BackupError:
            raise
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise BackupError(
                    "strict_snapshot_failed",
                    f"严格快照 SQLite quick_check 失败 {source.arcname}: {exc}",
                ) from exc
            warnings.append(
                f"SQLite 快照 quick_check 失败：{source.arcname}: {exc}"
            )
            consistent = False
        if (
            strict
            and ledger_probes is not None
            and source.arcname.startswith("data/memory/")
        ):
            from core.d01 import ledger_evidence

            # 只读元数据证据；在快照临时文件上探测，避免二次打开源库。
            ledger_probes.append(
                ledger_evidence(snapshot, label=source.arcname)
            )
        self._check_cancel(job_id)
        try:
            st = read_path.stat()
            info = zipfile.ZipInfo(source.arcname, date_time=_zip_date(st.st_mtime))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = (0o600 & 0xFFFF) << 16
            digest = hashlib.sha256()
            written = 0
            with read_path.open("rb") as src, archive.open(info, "w") as dest:
                while True:
                    self._check_cancel(job_id)
                    chunk = src.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    dest.write(chunk)
                    written += len(chunk)
                    self._add_progress(job_id, len(chunk))
            return {
                "path": source.arcname,
                "size": written,
                "sha256": digest.hexdigest(),
                "kind": "sqlite",
                "consistent": consistent,
            }
        except OSError as exc:
            warnings.append(f"写入数据库备份失败，已跳过 {source.arcname}: {exc}")
            return None
        finally:
            try:
                snapshot.unlink(missing_ok=True)
            except OSError:
                pass

    def _add_progress(self, job_id: str, amount: int) -> None:
        if amount <= 0:
            return
        with self._shared.state_lock:
            job = self._shared.jobs.get(job_id)
            if job is not None:
                job["processed_bytes"] = int(job.get("processed_bytes") or 0) + amount

    def _update(self, job_id: str, **fields: Any) -> None:
        persist_fields = {
            "state", "stage", "finished_at", "error", "warnings",
            "filename", "size", "sha256", "file_count", "validation_status",
            "partial", "credential_restorable", "credential_material_present",
            "capabilities", "missing", "retryable",
        }
        with self._shared.state_lock:
            job = self._shared.jobs.get(job_id)
            if job is not None:
                job.update(fields)
        if persist_fields.intersection(fields):
            self._persist()

    def _public_job(self, job: dict) -> dict:
        return self._shared._public_job(job)

    def _write_sidecar(self, zip_path: Path, data: dict) -> None:
        sidecar = self._sidecar_path(zip_path)
        tmp = sidecar.with_name(sidecar.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, sidecar)
            _fsync_directory(sidecar.parent)
            try:
                os.chmod(sidecar, 0o600)
            except OSError:
                pass
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _sha256_file(self, path: Path, *, job_id: str | None = None) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                self._check_cancel(job_id)
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _app_version(self) -> str:
        try:
            from app_config.versioning import read_pyproject_version

            return read_pyproject_version(self.project_root)
        except Exception:  # noqa: BLE001
            return ""

    def _git_commit(self) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.project_root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _restore_readme(self) -> str:
        return (
            "KarenBot 用户数据备份\n"
            "==========================\n\n"
            "此压缩包包含聊天数据库、RAG 向量库、工作区文件、配置、加密密钥库和\n"
            "RSA 私钥。拿到它就等于拿到 API 密钥的解密能力，请像密码一样保管，\n"
            "不要上传到公共网盘或通过不安全渠道发送。ZIP 本身没有加密；0600/0700\n"
            "只是本机文件权限，不是密码保护。\n\n"
            "恢复步骤（建议先备份当前数据）：\n"
            "1. 停止 karenbot 服务。\n"
            "2. 解压本压缩包。\n"
            "3. 把 data/ 下的内容复制回当前实例数据目录（默认\n"
            "   ~/.local/share/KarenBot/instances/default/）。\n"
            "4. 把 personas/ 复制回项目根目录的 personas/。\n"
            "5. 确认 data/rsa_private.pem 权限为 0600。若原环境使用系统 keyring，\n"
            "   把 PEM 留在数据目录即可作为兜底；确认能解密模型密钥后再删除。\n"
            "6. 重启服务并检查人格、聊天、模型密钥是否正常。\n\n"
            "注意：备份是某一时刻的一致快照；备份之后的新消息不会包含在内。\n"
            "如果 manifest.json 中某个文件的 consistent=false，表示该数据库\n"
            "无法用 SQLite backup API 快照，已退回文件复制，恢复时请重点检查。\n"
        )


__all__ = [
    "BACKUP_FORMAT",
    "BACKUP_NAME_RE",
    "BACKUP_PREFIX",
    "BACKUP_SUFFIX",
    "BackupError",
    "BackupService",
    "resolve_backup_directory",
]
