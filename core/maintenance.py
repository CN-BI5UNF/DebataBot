"""维护控制器：持久状态、跨进程锁、writer 屏障与启动拦截。

这是 M00 的最小可验收实现：状态文件位于数据目录的 ``maintenance/``，用
POSIX flock 做实例独占；writer 注册表先覆盖可注入的维护参与者。生产部署仍
需要把实际 history/GC/summary/RAG/配置等 writer 注册进来，并停 Runtime 后
取锁；未注册的旁路 writer 不能被本模块神奇拦截。

状态文件只存元数据/水位/owner，不存聊天正文、密钥或凭据。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core.snapshot_barrier import SnapshotBarrier, SnapshotMarker

try:  # POSIX 跨进程锁；Windows 退化为进程内互斥并明确限制。
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

STATE_SCHEMA = 1
STATE_FILENAME = "controller_state.json"
LOCK_FILENAME = "controller.lock"
PHASES = {
    "idle",
    "requesting",
    "draining",
    "quiescent",
    "snapshotting",
    "migrating",
    "validating",
    "ready_to_resume",
    "running",
    "recovery_required",
}
BLOCKING_PHASES = PHASES - {"idle", "running"}
MAX_STATE_BYTES = 256 * 1024


class MaintenanceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii"
        ).strip()
    except OSError:
        return ""


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _fsync_dir(path: Path) -> None:
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


@dataclass(frozen=True)
class MaintenanceState:
    schema: int
    phase: str
    generation: int
    owner: str
    owner_token: str
    pid: int
    boot_id: str
    purpose: str
    plan_hash: str
    snapshot_hash: str
    data_contract_epoch: int
    migration_set_id: str
    updated_at: str
    error: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "phase": self.phase,
            "generation": self.generation,
            "owner": self.owner,
            "pid": self.pid,
            "boot_id": self.boot_id,
            "purpose": self.purpose,
            "plan_hash": self.plan_hash,
            "snapshot_hash": self.snapshot_hash,
            "data_contract_epoch": self.data_contract_epoch,
            "migration_set_id": self.migration_set_id,
            "updated_at": self.updated_at,
            "error": self.error,
        }


def _idle_state() -> MaintenanceState:
    return MaintenanceState(
        schema=STATE_SCHEMA,
        phase="idle",
        generation=0,
        owner="",
        owner_token="",
        pid=0,
        boot_id="",
        purpose="",
        plan_hash="",
        snapshot_hash="",
        data_contract_epoch=0,
        migration_set_id="",
        updated_at="",
        error="",
    )


class MaintenanceController:
    """实例维护状态与 writer 屏障。"""

    def __init__(self, paths: Any) -> None:
        self.paths = paths
        self.data_dir = Path(paths.DATA_DIR).resolve()
        self.directory = Path(getattr(paths, "MAINTENANCE_DIR", self.data_dir / "maintenance"))
        self.state_file = self.directory / STATE_FILENAME
        self.lock_file = self.directory / LOCK_FILENAME
        self._lock_fd: int | None = None
        self._writers: dict[str, tuple[Callable[[], Any], Callable[[], Any] | None]] = {}
        self._settled: set[str] = set()
        self._mutex = threading.RLock()

    # -- state ---------------------------------------------------

    def read_state(self) -> MaintenanceState:
        try:
            info = self.state_file.lstat()
        except FileNotFoundError:
            return _idle_state()
        except OSError as exc:
            raise MaintenanceError("state_unreadable", f"无法读取维护状态：{exc}") from exc
        if not os.path.isfile(self.state_file) or os.path.islink(self.state_file):
            raise MaintenanceError("state_unreadable", "维护状态不是普通文件")
        if info.st_size > MAX_STATE_BYTES:
            raise MaintenanceError("state_invalid", "维护状态过大")
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MaintenanceError("state_invalid", f"维护状态不可读：{exc}") from exc
        if not isinstance(raw, dict):
            raise MaintenanceError("state_invalid", "维护状态根不是对象")
        schema = raw.get("schema")
        if schema != STATE_SCHEMA:
            raise MaintenanceError("state_invalid", f"不支持的 state schema={schema}")
        phase = raw.get("phase")
        if phase not in PHASES:
            raise MaintenanceError("state_invalid", f"未知维护 phase={phase!r}")
        generation = raw.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise MaintenanceError("state_invalid", "generation 非法")
        def string(name: str, limit: int = 200) -> str:
            value = raw.get(name, "")
            if not isinstance(value, str) or len(value) > limit:
                raise MaintenanceError("state_invalid", f"{name} 非法")
            return value
        def integer(name: str) -> int:
            value = raw.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise MaintenanceError("state_invalid", f"{name} 非法")
            return value
        return MaintenanceState(
            schema=STATE_SCHEMA,
            phase=phase,
            generation=generation,
            owner=string("owner"),
            owner_token=string("owner_token"),
            pid=integer("pid"),
            boot_id=string("boot_id", 100),
            purpose=string("purpose"),
            plan_hash=string("plan_hash", 100),
            snapshot_hash=string("snapshot_hash", 100),
            data_contract_epoch=integer("data_contract_epoch"),
            migration_set_id=string("migration_set_id"),
            updated_at=string("updated_at", 100),
            error=string("error", 500),
        )

    def _write_state(self, state: MaintenanceState) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        payload = state.snapshot()
        # owner_token 只用于同一维护请求的 fencing，不出现在公开 status。
        payload["owner_token"] = state.owner_token
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.state_file)
            _fsync_dir(self.directory)
            try:
                os.chmod(self.state_file, 0o600)
            except OSError:
                pass
        except OSError as exc:
            raise MaintenanceError("state_write_failed", f"维护状态写入失败：{exc}") from exc
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # -- lock / lifecycle ---------------------------------------

    def acquire_lock(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if fcntl is None:
            if self._lock_fd is not None:
                raise MaintenanceError("maintenance_busy", "维护锁已由当前实例持有")
            self._lock_fd = -1
            return
        if self._lock_fd is not None:
            raise MaintenanceError("maintenance_busy", "维护锁已由当前实例持有")
        try:
            fd = os.open(self.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise MaintenanceError("lock_failed", f"无法创建维护锁：{exc}") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise MaintenanceError(
                "maintenance_busy", "另一个进程持有维护锁"
            ) from exc
        self._lock_fd = fd

    def release_lock(self) -> None:
        fd = self._lock_fd
        self._lock_fd = None
        if fd is None:
            return
        if fd >= 0 and fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass

    @property
    def locked(self) -> bool:
        return self._lock_fd is not None

    def register_writer(
        self,
        name: str,
        stop: Callable[[], Any],
        resume: Callable[[], Any] | None = None,
    ) -> None:
        if not name or len(name) > 80:
            raise MaintenanceError("writer_invalid", "writer 名称非法")
        self._writers[name] = (stop, resume)

    def request(self, owner: str, purpose: str, *, plan_hash: str = "") -> str:
        """取得维护锁并进入 requesting；调用方随后 stop Runtime、drain。"""
        if not owner or len(owner) > 120 or not purpose or len(purpose) > 200:
            raise MaintenanceError("request_invalid", "维护 owner/purpose 非法")
        with self._mutex:
            self.acquire_lock()
            try:
                state = self.read_state()
                if state.phase not in {"idle", "running"}:
                    raise MaintenanceError(
                        "maintenance_busy", f"当前维护 phase={state.phase} 不允许新请求"
                    )
                token = secrets.token_hex(16)
                new_state = MaintenanceState(
                    schema=STATE_SCHEMA,
                    phase="requesting",
                    generation=state.generation + 1,
                    owner=owner,
                    owner_token=token,
                    pid=os.getpid(),
                    boot_id=_boot_id(),
                    purpose=purpose,
                    plan_hash=plan_hash,
                    snapshot_hash="",
                    data_contract_epoch=state.data_contract_epoch,
                    migration_set_id=state.migration_set_id,
                    updated_at=_utc_now(),
                )
                self._write_state(new_state)
                self._settled.clear()
                return token
            except Exception:
                self.release_lock()
                raise

    def drain(self, token: str) -> list[str]:
        """停止已注册 writer；失败进入 recovery_required 并保持锁。"""
        with self._mutex:
            state = self._require_token(token)
            if state.phase not in {"requesting", "draining"}:
                raise MaintenanceError("invalid_phase", f"drain phase={state.phase}")
            self._settled.clear()
            for name, (stop, _resume) in self._writers.items():
                try:
                    stop()
                except Exception as exc:  # noqa: BLE001
                    self.fail(token, f"writer {name} 取消失败：{exc}")
                    raise MaintenanceError(
                        "writer_drain_failed", f"writer {name} 取消失败：{exc}"
                    ) from exc
                self._settled.add(name)
            self._transition(token, "draining")
            return sorted(self._settled)

    def quiesce(
        self,
        token: str,
        *,
        snapshot_hash: str = "",
        contract_epoch: int | None = None,
        migration_set_id: str | None = None,
    ) -> SnapshotMarker:
        with self._mutex:
            state = self._require_token(token)
            if state.phase not in {"draining", "requesting"}:
                raise MaintenanceError("invalid_phase", f"quiesce phase={state.phase}")
            unsettled = set(self._writers) - self._settled
            if unsettled:
                raise MaintenanceError(
                    "writer_unsettled", "writer 未结算：" + ", ".join(sorted(unsettled))
                )
            epoch = state.data_contract_epoch if contract_epoch is None else contract_epoch
            set_id = state.migration_set_id if migration_set_id is None else migration_set_id
            new_state = replace(
                state,
                phase="quiescent",
                snapshot_hash=snapshot_hash,
                data_contract_epoch=epoch,
                migration_set_id=set_id,
                updated_at=_utc_now(),
            )
            self._write_state(new_state)
            return SnapshotMarker(
                epoch=epoch,
                generation=new_state.generation,
                high_water=new_state.generation,
                contract_epoch=epoch,
                migration_set_id=set_id,
                features=frozenset(),
                migration_safe=False,
                details={"owner": new_state.owner, "purpose": new_state.purpose},
            )

    def mark_phase(self, token: str, phase: str) -> None:
        if phase not in PHASES:
            raise MaintenanceError("invalid_phase", f"未知 phase={phase}")
        self._transition(token, phase)

    def resume(self, token: str) -> None:
        """恢复 writer，清除维护锁；必须由当前 owner 调用。"""
        with self._mutex:
            self._require_token(token)
            for name, (_stop, resume) in self._writers.items():
                if name not in self._settled:
                    continue
                if resume is not None:
                    try:
                        resume()
                    except Exception as exc:  # noqa: BLE001
                        self.fail(token, f"writer {name} 恢复失败")
                        raise MaintenanceError(
                            "writer_resume_failed", f"writer {name} 恢复失败"
                        ) from exc
            self._transition(token, "running")
            self._settled.clear()
            self.release_lock()

    def fail(self, token: str, reason: str) -> None:
        try:
            state = self._require_token(token)
        except MaintenanceError:
            return
        new_state = replace(
            state,
            phase="recovery_required",
            error=str(reason)[:500],
            updated_at=_utc_now(),
        )
        try:
            self._write_state(new_state)
        except MaintenanceError:
            logger.exception("写入 recovery_required 状态失败")
        # 不自动释放锁；由管理员恢复流程处理。

    def _require_token(self, token: str) -> MaintenanceState:
        state = self.read_state()
        if not token or state.owner_token != token:
            raise MaintenanceError("stale_owner", "维护 owner token 不匹配")
        return state

    def _transition(self, token: str, phase: str) -> None:
        state = self._require_token(token)
        new_state = replace(state, phase=phase, updated_at=_utc_now())
        self._write_state(new_state)


class ControllerBarrier(SnapshotBarrier):
    """把维护控制器适配成严格快照的 barrier。"""

    def __init__(
        self,
        controller: MaintenanceController,
        *,
        owner: str,
        purpose: str,
        plan_hash: str = "",
        snapshot_hash: str = "",
        contract_epoch: int | None = None,
        migration_set_id: str | None = None,
    ) -> None:
        self.controller = controller
        self.owner = owner
        self.purpose = purpose
        self.plan_hash = plan_hash
        self.snapshot_hash = snapshot_hash
        self.contract_epoch = contract_epoch
        self.migration_set_id = migration_set_id
        self.token: str | None = None

    def freeze(self) -> SnapshotMarker:
        if self.token is not None:
            raise MaintenanceError("barrier_busy", "barrier 已冻结")
        self.token = self.controller.request(
            self.owner, self.purpose, plan_hash=self.plan_hash
        )
        try:
            self.controller.drain(self.token)
            marker = self.controller.quiesce(
                self.token,
                snapshot_hash=self.snapshot_hash,
                contract_epoch=self.contract_epoch,
                migration_set_id=self.migration_set_id,
            )
            self.controller.mark_phase(self.token, "snapshotting")
            return marker
        except Exception:
            self.controller.fail(self.token, "barrier freeze 失败")
            raise

    def release(self, marker: SnapshotMarker) -> None:
        token, self.token = self.token, None
        if token is None:
            return
        self.controller.resume(token)


def startup_gate(paths: Any) -> dict[str, Any]:
    """WebUI/Runtime 启动前查询维护状态；缺失状态视为 idle。"""

    try:
        controller = MaintenanceController(paths)
        state = controller.read_state()
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "code": "maintenance_state_invalid",
            "message": f"无法读取维护状态：{exc}",
            "phase": "",
        }
    if state.phase in BLOCKING_PHASES:
        return {
            "ok": False,
            "code": "maintenance_blocked",
            "message": f"实例处于维护 phase={state.phase}；禁止普通 Runtime 启动",
            "phase": state.phase,
            "generation": state.generation,
        }
    return {
        "ok": True,
        "code": "ok",
        "phase": state.phase,
        "generation": state.generation,
    }


__all__ = [
    "BLOCKING_PHASES",
    "ControllerBarrier",
    "MaintenanceController",
    "MaintenanceError",
    "MaintenanceState",
    "PHASES",
    "startup_gate",
]
