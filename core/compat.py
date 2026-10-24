"""代码/数据兼容声明与离线预检。

目标：
    - 更新/回退前，用纯 JSON manifest 判断目标代码能否安全读写当前数据；
    - 不执行目标代码、不联网、不迁移、不创建空库；
    - 无 manifest 的 legacy tag 只展示，不允许自动 apply；
    - 任何未知格式/平面/范围/缺失字段默认 fail-closed。

这不是数据迁移实现。D01 迁移完成后，由可信保护层把
``compatibility_state.json`` 的 epoch/features/planes 推进；本模块只负责读和判断。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # PyYAML 是项目依赖；测试环境缺失时按不可读处理。
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

MANIFEST_FORMAT = 1
CURRENT_LAUNCHER_PROTOCOL = 1
MAX_MANIFEST_BYTES = 64 * 1024
MAX_VERSION = 1_000_000
REQUIRED_PLANES = (
    "root_config",
    "persona_db",
    "maintenance",
    "rag",
    "projection",
)
DEFAULT_PLANES = {
    "root_config": 2,
    "persona_db": 1,
    "maintenance": 1,
    "rag": 0,
    "projection": 0,
}
ALLOWED_PHASES = {
    "",
    "idle",
    "ready",
    "preparing",
    "running",
    "failed",
    "recovery_required",
    "complete",
    "committed",
}
STATE_FILENAME = "compatibility_state.json"
MANIFEST_FILENAME = "compatibility.json"


class CompatError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PlaneSupport:
    supported_read: int
    supported_write: int
    target_version: int


@dataclass(frozen=True)
class CompatManifest:
    raw_hash: str
    manifest_format: int
    minimum_launcher_protocol: int
    data_contract_epoch: int
    migration_set_id: str
    irreversible: bool
    planes: dict[str, PlaneSupport]
    required_features: frozenset[str]
    optional_features: frozenset[str]


@dataclass(frozen=True)
class DatabaseProbe:
    plane: str
    relative_path: str
    user_version: int | None
    schema_version: int | None
    error: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "plane": self.plane,
            "path": self.relative_path,
            "user_version": self.user_version,
            "schema_version": self.schema_version,
            "error": self.error,
        }


@dataclass(frozen=True)
class LocalDataState:
    planes: dict[str, int]
    data_contract_epoch: int = 0
    migration_set_id: str = ""
    features: frozenset[str] = frozenset()
    unreadable: tuple[str, ...] = ()
    databases: tuple[DatabaseProbe, ...] = ()
    phase: str = ""
    generation: int = 0
    state_format: int = 0
    first_run: bool = False
    config_missing: bool = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "planes": dict(self.planes),
            "data_contract_epoch": self.data_contract_epoch,
            "migration_set_id": self.migration_set_id,
            "features": sorted(self.features),
            "unreadable": list(self.unreadable),
            "databases": [item.snapshot() for item in self.databases],
            "phase": self.phase,
            "generation": self.generation,
            "state_format": self.state_format,
            "first_run": self.first_run,
            "config_missing": self.config_missing,
        }


def _require_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CompatError("manifest_invalid", f"{name} 必须是整数")
    if value < minimum or value > MAX_VERSION:
        raise CompatError("manifest_invalid", f"{name} 超出允许范围")
    return value


def _require_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise CompatError("manifest_invalid", f"{name} 必须是布尔值")
    return value


def _feature_set(value: Any, name: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or len(value) > 50:
        raise CompatError("manifest_invalid", f"{name} 必须是字符串数组")
    out: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 100:
            raise CompatError("manifest_invalid", f"{name} 含非法能力名")
        out.add(item)
    return frozenset(out)


def parse_manifest(raw: str | bytes) -> CompatManifest:
    """解析并校验 compatibility.json；不接受任何代码执行或路径引用。"""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CompatError("manifest_invalid", "manifest 不是 UTF-8") from exc
    else:
        text = str(raw)
    if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise CompatError("manifest_invalid", "manifest 过大")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CompatError("manifest_invalid", f"manifest 不是合法 JSON：{exc.msg}") from exc
    if not isinstance(data, dict):
        raise CompatError("manifest_invalid", "manifest 根必须是对象")

    manifest_format = _require_int(data.get("manifest_format"), "manifest_format", minimum=1)
    if manifest_format != MANIFEST_FORMAT:
        raise CompatError(
            "unsupported_manifest_format",
            f"不支持的 manifest_format={manifest_format}",
        )
    protocol = _require_int(
        data.get("minimum_launcher_protocol"),
        "minimum_launcher_protocol",
        minimum=0,
    )
    epoch = _require_int(data.get("data_contract_epoch"), "data_contract_epoch")
    migration_set_id = data.get("migration_set_id", "")
    if not isinstance(migration_set_id, str) or len(migration_set_id) > 200:
        raise CompatError("manifest_invalid", "migration_set_id 非法")
    irreversible = _require_bool(data.get("irreversible", False), "irreversible")

    planes_raw = data.get("planes")
    if not isinstance(planes_raw, dict):
        raise CompatError("manifest_invalid", "planes 必须是对象")
    unknown = sorted(set(planes_raw) - set(REQUIRED_PLANES))
    missing = sorted(set(REQUIRED_PLANES) - set(planes_raw))
    if unknown:
        raise CompatError("unknown_plane", f"manifest 含未知数据平面：{', '.join(unknown)}")
    if missing:
        raise CompatError("manifest_invalid", f"manifest 缺少数据平面：{', '.join(missing)}")

    planes: dict[str, PlaneSupport] = {}
    for name in REQUIRED_PLANES:
        item = planes_raw.get(name)
        if not isinstance(item, dict):
            raise CompatError("manifest_invalid", f"{name} 必须是对象")
        read_v = _require_int(item.get("supported_read"), f"{name}.supported_read")
        write_v = _require_int(item.get("supported_write"), f"{name}.supported_write")
        target_v = _require_int(item.get("target_version"), f"{name}.target_version")
        if write_v > read_v:
            raise CompatError(
                "manifest_invalid",
                f"{name} 的 supported_write 不能高于 supported_read",
            )
        if target_v > write_v:
            raise CompatError(
                "manifest_invalid",
                f"{name} 的 target_version 不能高于 supported_write",
            )
        planes[name] = PlaneSupport(read_v, write_v, target_v)

    required = _feature_set(data.get("required_features"), "required_features")
    optional = _feature_set(data.get("optional_features"), "optional_features")
    return CompatManifest(
        raw_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        manifest_format=manifest_format,
        minimum_launcher_protocol=protocol,
        data_contract_epoch=epoch,
        migration_set_id=migration_set_id,
        irreversible=irreversible,
        planes=planes,
        required_features=required,
        optional_features=optional,
    )


def _read_config_version(path: Path, unreadable: list[str]) -> int:
    if not path.is_file():
        return 0
    if yaml is None:
        unreadable.append("config.yaml：缺少 YAML 解析器")
        return 0
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        unreadable.append(f"config.yaml 不可读：{exc}")
        return 0
    if not isinstance(data, dict):
        unreadable.append("config.yaml 根不是对象")
        return 0
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        unreadable.append("config.yaml 的 version 非法")
        return 0
    return version


def _sqlite_probe(
    path: Path,
    plane: str,
    data_dir: Path,
    unreadable: list[str],
) -> DatabaseProbe:
    """只读探针；mode=ro + query_only，不创建缺失库，不写 WAL checkpoint。"""
    try:
        relative = path.relative_to(data_dir).as_posix()
    except ValueError:
        relative = path.name
    if not path.is_file() or path.is_symlink():
        unreadable.append(f"{relative}：不存在或不是普通文件")
        return DatabaseProbe(plane, relative, None, None, "missing")
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            conn.execute("PRAGMA query_only=ON")
            user_row = conn.execute("PRAGMA user_version").fetchone()
            schema_row = conn.execute("PRAGMA schema_version").fetchone()
        finally:
            conn.close()
        if user_row is None or schema_row is None:
            raise ValueError("PRAGMA 返回空结果")
        user_version = int(user_row[0])
        schema_version = int(schema_row[0])
        if user_version < 0 or schema_version < 0:
            raise ValueError("PRAGMA 版本为负")
        return DatabaseProbe(plane, relative, user_version, schema_version)
    except Exception as exc:  # noqa: BLE001
        unreadable.append(f"{relative} 不可读：{exc}")
        return DatabaseProbe(plane, relative, None, None, str(exc)[:200])


def _scan_databases(
    root: Path,
    plane: str,
    patterns: tuple[str, ...],
    data_dir: Path,
    unreadable: list[str],
) -> list[DatabaseProbe]:
    if not root.is_dir():
        return []
    probes: list[DatabaseProbe] = []
    seen: set[Path] = set()
    try:
        for pattern in patterns:
            for path in root.rglob(pattern):
                if path in seen:
                    continue
                seen.add(path)
                if path.is_symlink() or not path.is_file():
                    continue
                probes.append(_sqlite_probe(path, plane, data_dir, unreadable))
    except OSError as exc:
        unreadable.append(f"{root.name} 遍历失败：{exc}")
    return probes


_EPHEMERAL_TOP_DIRS = {"logs", "cache", "tmp", "models"}
_INITIALIZATION_FILES = {"root.json", "instance.json"}


def _has_persistent_entries(data_dir: Path) -> bool:
    """只按目录/文件名判断是否存在持久物，不读取文件内容。"""
    if not data_dir.exists():
        return False
    if not data_dir.is_dir() or data_dir.is_symlink():
        return True
    try:
        for current, dirs, files in os.walk(data_dir, followlinks=False):
            current_path = Path(current)
            try:
                relative = current_path.relative_to(data_dir)
            except ValueError:
                return True
            if len(relative.parts) == 1 and relative.parts[0] in _EPHEMERAL_TOP_DIRS:
                dirs[:] = []
                continue
            if any((current_path / name).is_symlink() for name in (*dirs, *files)):
                return True
            effective_files = [
                name for name in files
                if not (len(relative.parts) == 0 and name in _INITIALIZATION_FILES)
            ]
            if effective_files:
                return True
            # 跳过临时/日志目录下的所有内容。
            dirs[:] = [
                name for name in dirs
                if not (
                    len(relative.parts) == 0
                    and name in _EPHEMERAL_TOP_DIRS
                )
            ]
    except OSError:
        return True
    return False


def read_local_state(
    data_dir: Path,
    *,
    config_path: Path | None = None,
) -> LocalDataState:
    """只读本地版本元信息；不取聊天正文，不创建库/目录。"""
    data_dir = Path(data_dir)
    config_path = Path(config_path) if config_path is not None else data_dir / "config.yaml"
    unreadable: list[str] = []
    planes = {name: 0 for name in REQUIRED_PLANES}
    config_missing = not config_path.exists()
    if config_path.is_symlink():
        unreadable.append("config.yaml 是符号链接，拒绝读取")
        config_missing = True
    elif config_path.exists() and not config_path.is_file():
        unreadable.append("config.yaml 不是普通文件")
        config_missing = True
    if not config_missing:
        planes["root_config"] = _read_config_version(config_path, unreadable)
    databases: list[DatabaseProbe] = []

    memory_probes = _scan_databases(
        data_dir / "memory",
        "persona_db",
        ("*.db", "*.sqlite3"),
        data_dir,
        unreadable,
    )
    databases.extend(memory_probes)
    memory_versions = [
        probe.user_version for probe in memory_probes
        if probe.user_version is not None
    ]
    if memory_versions:
        planes["persona_db"] = max(memory_versions)

    vector_probes = _scan_databases(
        data_dir / "vector",
        "rag",
        ("*.sqlite3", "*.db"),
        data_dir,
        unreadable,
    )
    databases.extend(vector_probes)
    vector_versions = [
        probe.user_version for probe in vector_probes
        if probe.user_version is not None
    ]
    if vector_versions:
        planes["rag"] = max(vector_versions)

    maintenance_path = data_dir / "maintenance.sqlite3"
    if maintenance_path.exists():
        maintenance_probe = _sqlite_probe(
            maintenance_path, "maintenance", data_dir, unreadable
        )
        databases.append(maintenance_probe)
        if maintenance_probe.user_version is not None:
            planes["maintenance"] = maintenance_probe.user_version

    epoch = 0
    migration_set_id = ""
    features: frozenset[str] = frozenset()
    phase = ""
    generation = 0
    state_format = 0
    state_path = data_dir / STATE_FILENAME
    state_present = state_path.is_file()
    if state_path.is_symlink():
        unreadable.append(f"{STATE_FILENAME} 是符号链接，拒绝读取")
        state_present = False
    elif state_path.exists() and not state_path.is_file():
        unreadable.append(f"{STATE_FILENAME} 不是普通文件")
        state_present = False
    if state_present:
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            unreadable.append(f"{STATE_FILENAME} 不可读：{exc}")
            raw = None
        if raw is not None:
            if not isinstance(raw, dict):
                unreadable.append(f"{STATE_FILENAME} 根不是对象")
            else:
                try:
                    state_format = _require_int(
                        raw.get("state_format", 0), "state_format"
                    )
                    if state_format > 1:
                        raise ValueError(f"不支持的 state_format={state_format}")
                    epoch_value = raw.get("data_contract_epoch", 0)
                    if isinstance(epoch_value, bool) or not isinstance(epoch_value, int):
                        raise ValueError("data_contract_epoch 非法")
                    if epoch_value < 0 or epoch_value > MAX_VERSION:
                        raise ValueError("data_contract_epoch 越界")
                    epoch = epoch_value
                    set_id = raw.get("migration_set_id", "")
                    if not isinstance(set_id, str) or len(set_id) > 200:
                        raise ValueError("migration_set_id 非法")
                    migration_set_id = set_id
                    features = _feature_set(raw.get("features", []), "features")
                    generation = _require_int(
                        raw.get("generation", 0), "generation"
                    )
                    phase = raw.get("phase", "")
                    if not isinstance(phase, str) or phase not in ALLOWED_PHASES:
                        raise ValueError(f"未知迁移 phase={phase!r}")
                    overrides = raw.get("planes", {})
                    if not isinstance(overrides, dict):
                        raise ValueError("planes 非法")
                    for name, value in overrides.items():
                        if name not in DEFAULT_PLANES:
                            raise ValueError(f"未知平面 {name}")
                        if isinstance(value, bool) or not isinstance(value, int):
                            raise ValueError(f"{name} 版本非法")
                        if value < 0 or value > MAX_VERSION:
                            raise ValueError(f"{name} 版本越界")
                        if name == "projection":
                            # projection 没有独立探针，允许由受控状态声明；
                            # 其它平面必须以实测为准，JSON 不能升降版本。
                            planes[name] = value
                        elif planes[name] != value:
                            raise ValueError(
                                f"{name} 声明 v{value} 与实测 v{planes[name]} 不一致"
                            )
                except (ValueError, CompatError) as exc:
                    unreadable.append(f"{STATE_FILENAME} 非法：{exc}")

    persistent = _has_persistent_entries(data_dir)
    first_run = bool(
        config_missing
        and not state_present
        and not databases
        and not maintenance_path.exists()
        and not persistent
    )
    return LocalDataState(
        planes=planes,
        data_contract_epoch=epoch,
        migration_set_id=migration_set_id,
        features=features,
        unreadable=tuple(unreadable),
        databases=tuple(databases),
        phase=phase,
        generation=generation,
        state_format=state_format,
        first_run=first_run,
        config_missing=bool(config_missing),
    )


def _result(
    code: str,
    message: str,
    *,
    compatible: bool,
    manifest_hash: str,
    local: LocalDataState,
) -> dict[str, Any]:
    return {
        "compatible": bool(compatible),
        "code": code,
        "message": message,
        "manifest_hash": manifest_hash,
        "mode": "setup" if code == "setup_required" else "normal",
        "setup_allowed": bool(local.first_run),
        "data_snapshot": local.snapshot(),
    }


def legacy_result(local: LocalDataState) -> dict[str, Any]:
    return _result(
        "legacy_unknown",
        "目标版本没有 compatibility.json；旧 tag 只展示，不能自动应用",
        compatible=False,
        manifest_hash="",
        local=local,
    )


def invalid_result(exc: CompatError, local: LocalDataState) -> dict[str, Any]:
    return _result(
        exc.code,
        exc.message,
        compatible=False,
        manifest_hash="",
        local=local,
    )


def evaluate(
    manifest: CompatManifest,
    local: LocalDataState,
    *,
    current_launcher_protocol: int = CURRENT_LAUNCHER_PROTOCOL,
) -> dict[str, Any]:
    """判断目标 manifest 能否安全读写当前本地数据。"""
    def blocked(code: str, message: str) -> dict[str, Any]:
        return _result(
            code,
            message,
            compatible=False,
            manifest_hash=manifest.raw_hash,
            local=local,
        )

    if local.unreadable:
        return blocked(
            "data_unreadable",
            "无法读取本地数据版本：" + "；".join(local.unreadable[:3]),
        )
    if local.first_run:
        return _result(
            "setup_required",
            "未发现配置或持久数据；只允许首次配置向导，不允许普通 Runtime/迁移",
            compatible=False,
            manifest_hash=manifest.raw_hash,
            local=local,
        )
    if local.config_missing:
        return blocked(
            "recovery_required",
            "config.yaml 缺失但数据目录已有持久物；按恢复流程处理，不能重新初始化",
        )
    if local.phase in {"preparing", "running"}:
        return blocked(
            "migration_in_progress",
            f"本地数据迁移正在进行（phase={local.phase}）；禁止普通更新/启动",
        )
    if local.phase in {"failed", "recovery_required"}:
        return blocked(
            "recovery_required",
            f"本地数据需要受控恢复（phase={local.phase}）；禁止普通更新/启动",
        )
    if manifest.minimum_launcher_protocol > current_launcher_protocol:
        return blocked(
            "launcher_protocol_too_old",
            f"目标要求 launcher 协议 v{manifest.minimum_launcher_protocol}，"
            f"当前只支持 v{current_launcher_protocol}",
        )
    if local.data_contract_epoch < manifest.data_contract_epoch:
        return blocked(
            "migration_required",
            f"本地数据契约 v{local.data_contract_epoch} 低于目标 "
            f"v{manifest.data_contract_epoch}；自动更新不执行迁移",
        )
    if local.data_contract_epoch > manifest.data_contract_epoch:
        return blocked(
            "data_contract_too_new",
            f"本地数据契约 v{local.data_contract_epoch} 高于目标 "
            f"v{manifest.data_contract_epoch}；目标代码不能安全读取",
        )
    if manifest.migration_set_id:
        if not local.migration_set_id:
            return blocked(
                "migration_required",
                "本地没有迁移记录，目标版本需要先执行数据迁移",
            )
        if local.migration_set_id != manifest.migration_set_id:
            return blocked(
                "unsupported_data_contract",
                f"迁移集不一致：本地 {local.migration_set_id}，"
                f"目标 {manifest.migration_set_id}",
            )
    elif local.migration_set_id:
        return blocked(
            "downgrade_required",
            f"本地迁移集 {local.migration_set_id} 目标版本不认识",
        )

    missing = sorted(manifest.required_features - local.features)
    if missing:
        return blocked(
            "feature_missing",
            "目标版本需要本地尚不支持的数据能力：" + ", ".join(missing),
        )

    # 逐库检查，不能用同平面的 max 掩盖一个旧库或新增的异版本库。
    for probe in local.databases:
        support = manifest.planes.get(probe.plane)
        if support is None:
            continue
        if probe.user_version is None:
            return blocked(
                "data_unreadable",
                f"{probe.relative_path} 无法读取版本：{probe.error or 'unknown'}",
            )
        local_version = int(probe.user_version)
        if local_version > support.supported_read or local_version > support.supported_write:
            return blocked(
                "unsupported_data_contract",
                f"{probe.relative_path} 本地版本 v{local_version} 超出目标支持范围 "
                f"(read<=v{support.supported_read}, write<=v{support.supported_write})",
            )
        if support.target_version > local_version:
            return blocked(
                "migration_required",
                f"{probe.relative_path} 需要从 v{local_version} 迁移到 "
                f"v{support.target_version}；自动更新不执行迁移",
            )
        if support.target_version < local_version:
            return blocked(
                "downgrade_required",
                f"{probe.relative_path} 目标版本 v{support.target_version} 低于本地 "
                f"v{local_version}，不能安全回退",
            )

    for plane in REQUIRED_PLANES:
        support = manifest.planes[plane]
        local_version = int(local.planes.get(plane, 0))
        if local_version > support.supported_read or local_version > support.supported_write:
            return blocked(
                "unsupported_data_contract",
                f"{plane} 本地版本 v{local_version} 超出目标支持范围 "
                f"(read<=v{support.supported_read}, write<=v{support.supported_write})",
            )
        if support.target_version > local_version:
            return blocked(
                "migration_required",
                f"{plane} 需要从 v{local_version} 迁移到 v{support.target_version}；"
                "自动更新不执行迁移",
            )
        if support.target_version < local_version:
            return blocked(
                "downgrade_required",
                f"{plane} 目标版本 v{support.target_version} 低于本地 v{local_version}，"
                "不能安全回退",
            )

    return _result(
        "compatible",
        "目标版本与当前数据版本兼容",
        compatible=True,
        manifest_hash=manifest.raw_hash,
        local=local,
    )


def evaluate_manifest_text(raw: str | bytes, local: LocalDataState) -> dict[str, Any]:
    try:
        manifest = parse_manifest(raw)
    except CompatError as exc:
        return invalid_result(exc, local)
    return evaluate(manifest, local)


def public_compat(result: dict[str, Any]) -> dict[str, Any]:
    """列表页用的精简字段，不带完整 data_snapshot。"""
    return {
        "compatible": bool(result.get("compatible")),
        "code": result.get("code", ""),
        "message": result.get("message", ""),
        "manifest_hash": result.get("manifest_hash", ""),
    }


__all__ = [
    "CURRENT_LAUNCHER_PROTOCOL",
    "DEFAULT_PLANES",
    "MANIFEST_FILENAME",
    "MANIFEST_FORMAT",
    "CompatError",
    "CompatManifest",
    "LocalDataState",
    "PlaneSupport",
    "evaluate",
    "evaluate_manifest_text",
    "legacy_result",
    "parse_manifest",
    "public_compat",
    "read_local_state",
]
