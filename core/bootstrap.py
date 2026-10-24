"""B02-E0：legacy epoch0 到 D01 的窄 bootstrap permit。

普通 ``migration_safe`` 在 D01 ledger 建立前保持 false。本模块只负责：

1. 证明源是 legacy epoch0（没有已启用 tombstone/import lifecycle）；
2. 在真实维护屏障下生成严格快照；
3. 离线 verify + 隔离 drill；
4. 写入一次性、带过期/目标构建/允许迁移 ID 绑定的 permit。

它不执行 D01 迁移、不删除/重写原数据、不制造空 ledger、不安装 launcher。
生产执行仍需用户单独授权；permit 只是给受控迁移工具的狭窄资格。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from core.backup import (
    BACKUP_TYPE_MIGRATION_SNAPSHOT,
    BackupError,
    BackupService,
)
from core.build_identity import compute_build_identity
from core.compat import read_local_state
from core.maintenance import ControllerBarrier, MaintenanceController
from core.restore import drill_backup, verify_backup

BOOTSTRAP_SCHEMA = 1
BOOTSTRAP_KIND = "bootstrap_epoch0"
PERMIT_FILENAME = "bootstrap_permit.json"
_PERMIT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MIGRATION_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_HEX_RE = re.compile(r"^[0-9a-f]{7,64}$")
_LIFECYCLE_FEATURES = {
    "stable_seq",
    "tombstone_read_barrier",
    "deletion_ledger",
    "import_ledger",
    "asset_epoch",
}
MAX_PERMIT_BYTES = 256 * 1024


class BootstrapError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> float:
    return time.time()


def _utc(ts: float | None = None) -> str:
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if ts is None else ts)
    )


def _inventory(paths: Any) -> dict[str, Any]:
    """只读元数据 inventory；不含聊天正文、密钥或文件内容。"""
    data_dir = Path(paths.DATA_DIR)
    state = read_local_state(data_dir, config_path=Path(paths.CONFIG_FILE))
    files: list[dict[str, Any]] = []
    candidates: list[Path] = [
        Path(paths.CONFIG_FILE),
        Path(paths.SECRETS_FILE),
        Path(paths.SECRETS_META_FILE),
        Path(paths.RSA_PUBLIC_KEY_FILE),
        Path(paths.RSA_PRIVATE_KEY_FILE),
        Path(paths.MAINTENANCE_DB_FILE),
        data_dir / "compatibility_state.json",
    ]
    for root in (
        Path(paths.MEMORY_DIR),
        Path(paths.VECTOR_DIR),
        Path(getattr(paths, "MAINTENANCE_DIR", data_dir / "maintenance")),
    ):
        try:
            if root.is_dir():
                candidates.extend(
                    path for path in root.rglob("*") if path.is_file()
                )
        except OSError:
            candidates.append(root)
    seen: set[str] = set()
    for path in candidates:
        try:
            rel = path.relative_to(data_dir).as_posix()
        except ValueError:
            rel = path.as_posix()
        if rel in seen:
            continue
        # 维护控制器自身状态/锁在 bootstrap 期间必然变化，不属于源数据
        # inventory；permit 等派生控制文件同样排除。
        if any(
            part in {
                "controller_state.json", "controller.lock",
                "bootstrap_permit.json", ".owner.json",
            }
            or part.endswith(".tmp") or part.endswith(".lock")
            for part in Path(rel).parts
        ):
            continue
        seen.add(rel)
        try:
            if path.is_symlink():
                files.append({"path": rel, "error": "symlink"})
            elif path.is_file():
                st = path.stat()
                files.append({"path": rel, "size": st.st_size, "mode": st.st_mode})
            else:
                files.append({"path": rel, "error": "missing_or_nonfile"})
        except OSError as exc:
            files.append({"path": rel, "error": str(exc)[:100]})
    files.sort(key=lambda item: item["path"])
    payload = {
        "data_dir": str(data_dir),
        "planes": state.planes,
        "databases": [item.snapshot() for item in state.databases],
        "features": sorted(state.features),
        "phase": state.phase,
        "generation": state.generation,
        "data_contract_epoch": state.data_contract_epoch,
        "migration_set_id": state.migration_set_id,
        "files": files,
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {"hash": digest, "payload": payload}


def validate_epoch0_eligibility(
    paths: Any, local_state: Any | None = None
) -> dict[str, Any]:
    local = local_state or read_local_state(
        Path(paths.DATA_DIR), config_path=Path(paths.CONFIG_FILE)
    )
    if local.unreadable:
        raise BootstrapError(
            "source_unreadable", "源数据不可读：" + "；".join(local.unreadable[:3])
        )
    if local.first_run or local.config_missing:
        raise BootstrapError(
            "source_not_epoch0", "源不是既有 epoch0 实例（缺配置或首次运行）"
        )
    if local.data_contract_epoch != 0:
        raise BootstrapError(
            "source_not_epoch0",
            f"源 data_contract_epoch={local.data_contract_epoch}，不是 legacy epoch0",
        )
    if local.phase in {"preparing", "running", "failed", "recovery_required"}:
        raise BootstrapError(
            "source_phase_blocked", f"源处于迁移/恢复 phase={local.phase}"
        )
    enabled = sorted(set(local.features) & _LIFECYCLE_FEATURES)
    if enabled:
        raise BootstrapError(
            "source_has_lifecycle",
            "源已启用 tombstone/import/stable 能力，不能按首次 epoch0 迁移："
            + ", ".join(enabled),
        )
    return {
        "eligible": True,
        "source_contract_epoch": 0,
        "source_features": sorted(local.features),
        "planes": dict(local.planes),
        "phase": local.phase,
        "generation": local.generation,
    }


def _permit_path(paths: Any) -> Path:
    return Path(getattr(paths, "MAINTENANCE_DIR", Path(paths.DATA_DIR) / "maintenance")) / PERMIT_FILENAME


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        if os.name != "nt":
            try:
                fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            except OSError:
                fd = -1
            if fd >= 0:
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _validate_permit_inputs(
    *,
    target_build: str,
    target_manifest_hash: str,
    allowed_migration_ids: list[str],
    expiry_seconds: int,
) -> None:
    if not isinstance(target_build, str) or not _HEX_RE.fullmatch(target_build):
        raise BootstrapError("target_invalid", "target_build 必须是 7..64 位十六进制")
    if (
        not isinstance(target_manifest_hash, str)
        or len(target_manifest_hash) != 64
        or not _HEX_RE.fullmatch(target_manifest_hash)
    ):
        raise BootstrapError(
            "target_invalid", "target_manifest_hash 必须是 64 位十六进制"
        )
    if (
        not allowed_migration_ids
        or len(allowed_migration_ids) > 8
        or not all(
            isinstance(item, str) and _MIGRATION_ID_RE.fullmatch(item)
            for item in allowed_migration_ids
        )
    ):
        raise BootstrapError(
            "migration_ids_invalid", "allowed_migration_ids 必须是非空、无通配的显式 ID"
        )
    if len(set(allowed_migration_ids)) != len(allowed_migration_ids):
        raise BootstrapError("migration_ids_invalid", "allowed_migration_ids 不能重复")
    if expiry_seconds < 60 or expiry_seconds > 7 * 24 * 3600:
        raise BootstrapError("expiry_invalid", "permit 有效期必须在 60 秒到 7 天之间")


def read_permit(paths: Any) -> dict[str, Any] | None:
    path = _permit_path(paths)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise BootstrapError("permit_invalid", "bootstrap permit 不是普通文件")
    if path.stat().st_size > MAX_PERMIT_BYTES:
        raise BootstrapError("permit_invalid", "bootstrap permit 过大")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError("permit_invalid", f"permit 不可读：{exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != BOOTSTRAP_SCHEMA:
        raise BootstrapError("permit_invalid", "permit schema 非法")
    return data


def consume_permit(
    paths: Any,
    *,
    permit_id: str,
    migration_id: str,
    target_build: str,
    inventory_hash: str,
    now: float | None = None,
) -> dict[str, Any]:
    """消费一次 D01 permit；绑定来源、目标构建、迁移 ID、inventory 和期限。"""
    permit = read_permit(paths)
    if permit is None:
        raise BootstrapError("permit_missing", "没有 bootstrap permit")
    if permit.get("status") != "unconsumed":
        raise BootstrapError("permit_consumed", "bootstrap permit 已被消费")
    if not isinstance(permit_id, str) or permit.get("permit_id") != permit_id:
        raise BootstrapError("permit_id_mismatch", "permit id 不匹配")
    if migration_id not in permit.get("allowed_migration_ids", []):
        raise BootstrapError("migration_not_allowed", "permit 不允许该迁移 ID")
    if permit.get("target_build") != target_build:
        raise BootstrapError("target_build_mismatch", "目标构建与 permit 不匹配")
    if permit.get("inventory_hash") != inventory_hash:
        raise BootstrapError("inventory_changed", "源 inventory 已变化，permit stale")
    current = _now() if now is None else float(now)
    expires_at = permit.get("expires_at")
    if not isinstance(expires_at, (int, float)) or current >= float(expires_at):
        raise BootstrapError("permit_expired", "bootstrap permit 已过期")
    updated = dict(permit)
    updated.update({
        "status": "consumed",
        "consumed_at": current,
        "consumed_migration_id": migration_id,
        "consumed_target_build": target_build,
    })
    _write_json_atomic(_permit_path(paths), updated)
    return updated


async def create_bootstrap(
    *,
    paths: Any,
    backup_service: BackupService,
    controller: MaintenanceController,
    target_build: str,
    target_manifest_hash: str,
    allowed_migration_ids: list[str],
    expiry_seconds: int = 24 * 3600,
    owner: str = "bootstrap",
) -> dict[str, Any]:
    """生成严格 bootstrap 包、verify+drill，并写一次性 permit。"""
    _validate_permit_inputs(
        target_build=target_build,
        target_manifest_hash=target_manifest_hash,
        allowed_migration_ids=allowed_migration_ids,
        expiry_seconds=expiry_seconds,
    )
    local = read_local_state(
        Path(paths.DATA_DIR), config_path=Path(paths.CONFIG_FILE)
    )
    eligibility = validate_epoch0_eligibility(paths, local)
    inventory_before = _inventory(paths)
    try:
        build = compute_build_identity(Path(paths.PROJECT_ROOT))
    except Exception:  # noqa: BLE001
        build = {}
    snapshot_meta = {
        "kind": BOOTSTRAP_KIND,
        "source_contract_epoch": 0,
        "source_features": eligibility["source_features"],
        "inventory_hash": inventory_before["hash"],
        "allowed_migration_ids": list(allowed_migration_ids),
        "permit_scope": "D01",
        "target_build": target_build,
        "target_manifest_hash": target_manifest_hash,
        "source_code_fingerprint": build.get("source_bundle_sha256", ""),
        "source_base_commit": build.get("base_commit", ""),
        "bootstrap_restore_verified": False,
        "migration_safe": False,
    }
    barrier = ControllerBarrier(
        controller,
        owner=owner,
        purpose="bootstrap_epoch0",
        plan_hash=inventory_before["hash"],
        snapshot_hash="",
    )
    try:
        job = await backup_service.start(
            backup_type=BACKUP_TYPE_MIGRATION_SNAPSHOT,
            barrier=barrier,
            include_media=True,
            snapshot_meta=snapshot_meta,
        )
        finished = await backup_service.wait(job["id"], timeout=900)
    except (BackupError, TimeoutError) as exc:
        raise BootstrapError("bootstrap_snapshot_failed", str(exc)) from exc
    if finished.get("state") != "done":
        raise BootstrapError(
            "bootstrap_snapshot_failed",
            f"严格 bootstrap 快照未成功：state={finished.get('state')}",
        )
    inventory_after = _inventory(paths)
    if inventory_after["hash"] != inventory_before["hash"]:
        raise BootstrapError(
            "source_changed", "bootstrap 期间源 inventory 变化；permit 未写入"
        )
    archive = Path(backup_service.directory) / str(finished["filename"])
    verification = verify_backup(archive, deep_sqlite=True)
    if not verification.get("ok"):
        raise BootstrapError(
            "bootstrap_verify_failed",
            "bootstrap 包 verify 失败：" + "; ".join(
                item["message"] for item in verification.get("errors", [])[:3]
            ),
        )
    try:
        drill = drill_backup(
            archive,
            protected_roots=[Path(paths.DATA_DIR), Path(paths.PERSONAS_DIR)],
        )
    except Exception as exc:  # noqa: BLE001
        raise BootstrapError("bootstrap_drill_failed", str(exc)) from exc
    if not drill.get("ok"):
        raise BootstrapError("bootstrap_drill_failed", "bootstrap 隔离 drill 未通过")
    drill_digest = hashlib.sha256(
        json.dumps(
            {
                "verification": verification,
                "extracted_files": drill.get("extracted_files"),
                "sqlite_checked": drill.get("sqlite_checked"),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    permit = {
        "schema": BOOTSTRAP_SCHEMA,
        "permit_id": secrets.token_hex(16),
        "status": "unconsumed",
        "created_at": _now(),
        "created_at_iso": _utc(),
        "expires_at": _now() + int(expiry_seconds),
        "one_time_generation": int(
            (finished.get("snapshot") or {}).get("generation", 0)
        ),
        "snapshot_filename": str(finished["filename"]),
        "snapshot_sha256": str(finished.get("sha256", "")),
        "snapshot_kind": BOOTSTRAP_KIND,
        "source_data_dir": str(Path(paths.DATA_DIR)),
        "source_contract_epoch": 0,
        "source_features": eligibility["source_features"],
        "source_inventory_payload": inventory_before["payload"],
        "inventory_hash": inventory_before["hash"],
        "source_code_fingerprint": build.get("source_bundle_sha256", ""),
        "source_base_commit": build.get("base_commit", ""),
        "target_build": target_build,
        "target_manifest_hash": target_manifest_hash,
        "allowed_migration_ids": list(allowed_migration_ids),
        "permit_scope": "D01",
        "drill_report_hash": drill_digest,
        "bootstrap_restore_verified": True,
        "migration_safe": False,
        "credentials_profile": "key_pair_checked_not_vault_decrypt",
        "note": "只授权显式 D01 migration ID；不授权删除/恢复/重写原数据；一般 migration_safe 仍 false",
    }
    _write_json_atomic(_permit_path(paths), permit)
    return {
        "ok": True,
        "permit": {k: v for k, v in permit.items() if k != "source_inventory_payload"},
        "snapshot": {
            "filename": finished["filename"],
            "sha256": finished.get("sha256", ""),
            "snapshot": finished.get("snapshot"),
        },
        "verification": verification,
        "drill": {
            "ok": drill.get("ok"),
            "extracted_files": drill.get("extracted_files"),
            "sqlite_checked": drill.get("sqlite_checked"),
            "cleaned": drill.get("cleaned"),
        },
        "bootstrap_restore_verified": True,
        "migration_safe": False,
    }


def bootstrap_status(paths: Any) -> dict[str, Any]:
    try:
        permit = read_permit(paths)
    except BootstrapError as exc:
        return {"ok": False, "code": exc.code, "message": exc.message}
    if permit is None:
        return {"ok": True, "permit": None}
    public = {k: v for k, v in permit.items() if k != "source_inventory_payload"}
    now = _now()
    public["expired"] = now >= float(permit.get("expires_at") or 0)
    return {"ok": True, "permit": public}


__all__ = [
    "BOOTSTRAP_KIND",
    "BOOTSTRAP_SCHEMA",
    "BootstrapError",
    "bootstrap_status",
    "consume_permit",
    "create_bootstrap",
    "read_permit",
    "validate_epoch0_eligibility",
]
