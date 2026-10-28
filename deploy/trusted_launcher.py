#!/usr/bin/env python3
"""Karen 受信 launcher（独立部署，未安装到 systemd）。

这个脚本刻意只依赖 Python 标准库，不 import 项目里的 ``core.compat``：
一旦源码被 ``git switch`` 换回旧版本，launcher 自身仍必须是可信的、固定的。
启动顺序：

    launcher 校验目标源码 manifest + 本地数据 → exec 目标 Python/Runtime

它不能拦截拥有同 UID/root 权限的手动绕过；也不负责安装 systemd。示例部署见
``deploy/README.md``。校验失败退出码 78（EX_CONFIG），绝不启动目标 Runtime。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

_EPHEMERAL_TOP_DIRS = {"logs", "cache", "tmp", "models"}
_INITIALIZATION_FILES = {"root.json", "instance.json"}
_ALLOWED_PHASES = {
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

LAUNCHER_PROTOCOL = 1
SUPPORTED_MANIFEST_FORMAT = 1
PLANE_NAMES = (
    "root_config", "persona_db", "maintenance", "rag", "projection",
)
DEFAULT_PLANES = {
    "root_config": 0,
    "persona_db": 0,
    "maintenance": 0,
    "rag": 0,
    "projection": 0,
}
EXIT_INCOMPATIBLE = 78
_CONFIG_VERSION_RE = re.compile(r"^\s*version\s*:\s*(-?\d+)\s*$", re.MULTILINE)


def _result(codes: list[str], message: str, **extra: Any) -> dict[str, Any]:
    code = extra.pop("code", codes[0] if codes else "compatible")
    compatible = bool(extra.pop("compatible", not codes))
    mode = extra.pop("mode", "normal")
    setup_allowed = bool(extra.pop("setup_allowed", False))
    return {
        "ok": compatible,
        "compatible": compatible,
        "code": code,
        "codes": codes,
        "mode": mode,
        "setup_allowed": setup_allowed,
        "message": message,
        "launcher_protocol": LAUNCHER_PROTOCOL,
        **extra,
    }


def _read_json(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("JSON 文件过大")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("根不是对象")
    return data


def _validate_manifest(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(raw, dict):
        return None, "manifest 根不是对象"
    if raw.get("manifest_format") != SUPPORTED_MANIFEST_FORMAT:
        return None, "manifest_format 不受支持"
    protocol = raw.get("minimum_launcher_protocol")
    if not isinstance(protocol, int) or isinstance(protocol, bool) or protocol < 1:
        return None, "minimum_launcher_protocol 非法"
    if protocol > LAUNCHER_PROTOCOL:
        return None, f"目标要求 launcher protocol {protocol}，当前仅 {LAUNCHER_PROTOCOL}"
    epoch = raw.get("data_contract_epoch")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        return None, "data_contract_epoch 非法"
    planes = raw.get("planes")
    if not isinstance(planes, dict):
        return None, "planes 非法"
    for name in PLANE_NAMES:
        item = planes.get(name)
        if not isinstance(item, dict):
            return None, f"plane {name} 缺失"
        for field in ("supported_read", "supported_write", "target_version"):
            value = item.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                return None, f"plane {name}.{field} 非法"
        if item["supported_write"] > item["supported_read"]:
            return None, f"plane {name} 的 supported_write 不能高于 supported_read"
        if item["target_version"] > item["supported_write"]:
            return None, f"plane {name} 的 target_version 不能高于 supported_write"
    required = raw.get("required_features", [])
    if not isinstance(required, list) or not all(isinstance(x, str) for x in required):
        return None, "required_features 非法"
    return raw, None


def _sqlite_version(path: Path) -> tuple[int | None, str | None]:
    if not path.is_file() or path.is_symlink():
        return None, "missing"
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            conn.execute("PRAGMA query_only=ON")
            row = conn.execute("PRAGMA user_version").fetchone()
        finally:
            conn.close()
        if row is None:
            return None, "empty"
        return int(row[0]), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)[:160]


def _scan_plane(
    root: Path,
    plane: str,
    patterns: tuple[str, ...],
    data_dir: Path,
) -> tuple[list[int], list[str], list[dict[str, Any]]]:
    versions: list[int] = []
    errors: list[str] = []
    probes: list[dict[str, Any]] = []
    if not root.is_dir():
        return versions, errors, probes
    seen: set[Path] = set()
    for pattern in patterns:
        for path in root.rglob(pattern):
            if path in seen:
                continue
            seen.add(path)
            if path.is_symlink() or not path.is_file():
                continue
            version, error = _sqlite_version(path)
            try:
                rel = path.relative_to(data_dir).as_posix()
            except ValueError:
                rel = path.name
            probes.append({
                "plane": plane,
                "path": rel,
                "user_version": version,
                "error": error,
            })
            if version is None:
                errors.append(f"{rel}: {error}")
            else:
                versions.append(version)
    return versions, errors, probes


_CONTROLLER_BLOCKING_PHASES = {
    "requesting", "draining", "quiescent", "snapshotting", "migrating",
    "validating", "ready_to_resume", "recovery_required",
}


def _read_maintenance_state(data_dir: Path) -> tuple[str, str]:
    """返回 (phase, error)；缺失状态按 idle。"""
    path = data_dir / "maintenance" / "controller_state.json"
    if not path.exists():
        return "idle", ""
    if path.is_symlink() or not path.is_file():
        return "", "维护状态不是普通文件"
    try:
        if path.stat().st_size > 256 * 1024:
            return "", "维护状态过大"
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("schema") != 1:
            return "", "维护状态 schema 非法"
        phase = state.get("phase")
        if phase not in {
            "idle", "requesting", "draining", "quiescent", "snapshotting",
            "migrating", "validating", "ready_to_resume", "running",
            "recovery_required",
        }:
            return "", f"未知维护 phase={phase!r}"
        generation = state.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            return "", "维护 generation 非法"
        return str(phase), ""
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return "", f"维护状态不可读：{exc}"


def _has_persistent_entries(data_dir: Path) -> bool:
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
    data_dir: Path, *, config_path: Path | None = None
) -> dict[str, Any]:
    data_dir = Path(data_dir)
    planes = {name: 0 for name in PLANE_NAMES}
    errors: list[str] = []
    probes: list[dict[str, Any]] = []
    config_path = Path(config_path) if config_path is not None else data_dir / "config.yaml"
    config_missing = not config_path.exists()
    if config_path.is_symlink():
        errors.append("config.yaml 是符号链接，拒绝读取")
        config_missing = True
    elif config_path.exists() and not config_path.is_file():
        errors.append("config.yaml 不是普通文件")
        config_missing = True
    if not config_missing:
        try:
            text = config_path.read_text(encoding="utf-8")
            matches = _CONFIG_VERSION_RE.findall(text)
            if not matches:
                errors.append("config.yaml 缺少 version")
            else:
                version = int(matches[-1])
                if version < 0 or version > 1_000_000:
                    errors.append("config.yaml version 越界")
                else:
                    planes["root_config"] = version
        except (OSError, ValueError) as exc:
            errors.append(f"config.yaml 不可读：{exc}")

    memory_versions, memory_errors, memory_probes = _scan_plane(
        data_dir / "memory", "persona_db", ("*.db", "*.sqlite3"), data_dir
    )
    errors.extend(memory_errors)
    probes.extend(memory_probes)
    if memory_versions:
        planes["persona_db"] = max(memory_versions)

    vector_versions, vector_errors, vector_probes = _scan_plane(
        data_dir / "vector", "rag", ("*.sqlite3", "*.db"), data_dir
    )
    errors.extend(vector_errors)
    probes.extend(vector_probes)
    if vector_versions:
        planes["rag"] = max(vector_versions)

    maintenance = data_dir / "maintenance.sqlite3"
    if maintenance.exists():
        version, error = _sqlite_version(maintenance)
        probes.append({
            "plane": "maintenance",
            "path": "maintenance.sqlite3",
            "user_version": version,
            "error": error,
        })
        if version is None:
            errors.append(f"maintenance.sqlite3: {error}")
        else:
            planes["maintenance"] = version

    features: set[str] = set()
    phase = ""
    generation = 0
    contract_epoch = 0
    migration_set_id = ""
    state_present = False
    state_format = 0
    state_path = data_dir / "compatibility_state.json"
    if state_path.is_symlink():
        errors.append("compatibility_state.json 是符号链接，拒绝读取")
    elif state_path.exists() and not state_path.is_file():
        errors.append("compatibility_state.json 不是普通文件")
    elif state_path.is_file():
        state_present = True
        try:
            state = _read_json(state_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"compatibility_state.json 不可读：{exc}")
            state = {}
        try:
            raw_state_format = state.get("state_format", 0)
            if (
                isinstance(raw_state_format, bool)
                or not isinstance(raw_state_format, int)
                or raw_state_format < 0
                or raw_state_format > 1
            ):
                raise ValueError("state_format 非法")
            state_format = raw_state_format
            raw_epoch = state.get("data_contract_epoch", 0)
            if isinstance(raw_epoch, bool) or not isinstance(raw_epoch, int):
                raise ValueError("data_contract_epoch 非法")
            if raw_epoch < 0 or raw_epoch > 1_000_000:
                raise ValueError("data_contract_epoch 越界")
            contract_epoch = raw_epoch
            raw_generation = state.get("generation", 0)
            if isinstance(raw_generation, bool) or not isinstance(raw_generation, int):
                raise ValueError("generation 非法")
            if raw_generation < 0 or raw_generation > 1_000_000:
                raise ValueError("generation 越界")
            generation = raw_generation
            phase = str(state.get("phase") or "")
            if phase not in _ALLOWED_PHASES:
                raise ValueError(f"未知迁移 phase={phase!r}")
            raw_set = state.get("migration_set_id", "")
            if not isinstance(raw_set, str) or len(raw_set) > 200:
                raise ValueError("migration_set_id 非法")
            migration_set_id = raw_set
            raw_features = state.get("features") or []
            if (
                not isinstance(raw_features, list)
                or len(raw_features) > 50
                or not all(
                    isinstance(item, str) and item and len(item) <= 100
                    for item in raw_features
                )
            ):
                raise ValueError("features 非法")
            features = set(raw_features)
            state_planes = state.get("planes")
            if state_planes is not None and not isinstance(state_planes, dict):
                raise ValueError("planes 非法")
            if isinstance(state_planes, dict):
                for name, value in state_planes.items():
                    if name not in PLANE_NAMES:
                        raise ValueError(f"未知平面 {name}")
                    if isinstance(value, bool) or not isinstance(value, int):
                        raise ValueError(f"{name} 版本非法")
                    if value < 0 or value > 1_000_000:
                        raise ValueError(f"{name} 版本越界")
                    if name == "projection":
                        planes[name] = value
                    elif planes[name] != value:
                        raise ValueError(
                            f"{name} 声明 v{value} 与实测 v{planes[name]} 不一致"
                        )
        except (ValueError, TypeError) as exc:
            errors.append(f"compatibility_state.json 非法：{exc}")

    persistent = _has_persistent_entries(data_dir)
    first_run = bool(
        config_missing
        and not state_present
        and not probes
        and not maintenance.exists()
        and not persistent
    )
    return {
        "data_dir": str(data_dir),
        "config_present": not config_missing,
        "config_missing": bool(config_missing),
        "first_run": first_run,
        "planes": planes,
        "probes": probes,
        "errors": errors,
        "features": sorted(features),
        "phase": phase,
        "generation": generation,
        "contract_epoch": contract_epoch,
        "migration_set_id": migration_set_id,
        "state_present": state_present,
        "state_format": state_format,
    }


def evaluate(
    manifest: dict[str, Any],
    local: dict[str, Any],
    *,
    allow_first_run: bool = True,
) -> dict[str, Any]:
    """与 core.compat.evaluate 同一上界语义，返回首个阻断 code。"""
    errors = list(local.get("errors") or [])
    if errors:
        return _result(
            ["data_unreadable"],
            "无法读取本地数据版本：" + "；".join(errors[:3]),
            code="data_unreadable",
            compatible=False,
            mode="normal",
            setup_allowed=False,
            local=local,
        )
    if local.get("first_run"):
        setup_allowed = bool(allow_first_run)
        return _result(
            ["setup_required"],
            "未发现配置或持久数据；只允许首次配置向导",
            code="setup_required",
            compatible=False,
            mode="setup",
            setup_allowed=setup_allowed,
            local=local,
        )
    if local.get("config_missing"):
        return _result(
            ["recovery_required"],
            "config.yaml 缺失但数据目录已有持久物；按恢复流程处理",
            code="recovery_required",
            compatible=False,
            mode="normal",
            setup_allowed=False,
            local=local,
        )
    phase = local.get("phase") or ""
    if phase in {"preparing", "running"}:
        return _result(
            ["migration_in_progress"],
            "本地迁移正在进行，禁止启动普通 Runtime",
            code="migration_in_progress",
            compatible=False,
            mode="maintenance",
            setup_allowed=False,
            local=local,
        )
    if phase in {"failed", "recovery_required"}:
        return _result(
            ["recovery_required"],
            "本地迁移失败/需要恢复，禁止启动普通 Runtime",
            code="recovery_required",
            compatible=False,
            mode="recovery",
            setup_allowed=False,
            local=local,
        )

    if manifest["minimum_launcher_protocol"] > LAUNCHER_PROTOCOL:
        return _result(
            ["launcher_protocol_too_old"],
            f"目标要求 launcher 协议 v{manifest['minimum_launcher_protocol']}，"
            f"当前 v{LAUNCHER_PROTOCOL}",
            code="launcher_protocol_too_old",
            compatible=False,
            mode="normal",
            setup_allowed=False,
            local=local,
        )
    target_epoch = int(manifest.get("data_contract_epoch") or 0)
    local_epoch = int(local.get("contract_epoch") or 0)
    if local_epoch < target_epoch:
        code = "migration_required"
        message = f"本地数据契约 v{local_epoch} 低于目标 v{target_epoch}"
    elif local_epoch > target_epoch:
        code = "data_contract_too_new"
        message = f"本地数据契约 v{local_epoch} 高于目标 v{target_epoch}"
    else:
        code = ""
        message = ""
    if code:
        return _result(
            [code], message, code=code, compatible=False,
            mode="normal", setup_allowed=False, local=local,
        )

    target_set = str(manifest.get("migration_set_id") or "")
    local_set = str(local.get("migration_set_id") or "")
    if target_set:
        if not local_set:
            code, message = (
                "migration_required",
                "本地没有迁移记录，目标版本需要先执行数据迁移",
            )
        elif local_set != target_set:
            code, message = (
                "unsupported_data_contract",
                f"迁移集不一致：本地 {local_set}，目标 {target_set}",
            )
        else:
            code, message = "", ""
    elif local_set:
        code, message = (
            "downgrade_required",
            f"本地迁移集 {local_set} 目标版本不认识",
        )
    else:
        code, message = "", ""
    if code:
        return _result(
            [code], message, code=code, compatible=False,
            mode="normal", setup_allowed=False, local=local,
        )

    required = set(manifest.get("required_features") or [])
    missing = sorted(required - set(local.get("features") or []))
    if missing:
        return _result(
            ["feature_missing"],
            "目标版本需要本地尚不支持的数据能力：" + ", ".join(missing),
            code="feature_missing",
            compatible=False,
            mode="normal",
            setup_allowed=False,
            local=local,
        )

    # 逐库检查，不能用 max 掩盖一个旧库或新增异版本库。
    target_planes = manifest.get("planes") or {}
    for probe in local.get("probes") or []:
        name = probe.get("plane")
        target = target_planes.get(name)
        if target is None:
            continue
        if probe.get("user_version") is None:
            return _result(
                ["data_unreadable"],
                f"{probe.get('path')} 无法读取版本：{probe.get('error') or 'unknown'}",
                code="data_unreadable",
                compatible=False,
                mode="normal",
                setup_allowed=False,
                local=local,
            )
        value = int(probe["user_version"])
        if value > target["supported_read"] or value > target["supported_write"]:
            code, message = (
                "unsupported_data_contract",
                f"{probe.get('path')} 本地版本 v{value} 超出目标支持范围 "
                f"(read<=v{target['supported_read']}, write<=v{target['supported_write']})",
            )
        elif target["target_version"] > value:
            code, message = (
                "migration_required",
                f"{probe.get('path')} 需要从 v{value} 迁移到 "
                f"v{target['target_version']}",
            )
        elif target["target_version"] < value:
            code, message = (
                "downgrade_required",
                f"{probe.get('path')} 目标版本 v{target['target_version']} "
                f"低于本地 v{value}",
            )
        else:
            code, message = "", ""
        if code:
            return _result(
                [code], message, code=code, compatible=False,
                mode="normal", setup_allowed=False, local=local,
            )

    local_planes = local.get("planes") or {}
    for name in PLANE_NAMES:
        target = target_planes[name]
        value = int(local_planes.get(name, 0))
        if value > target["supported_read"] or value > target["supported_write"]:
            code, message = (
                "unsupported_data_contract",
                f"{name} 本地版本 v{value} 超出目标支持范围 "
                f"(read<=v{target['supported_read']}, write<=v{target['supported_write']})",
            )
        elif target["target_version"] > value:
            code, message = (
                "migration_required",
                f"{name} 需要从 v{value} 迁移到 v{target['target_version']}",
            )
        elif target["target_version"] < value:
            code, message = (
                "downgrade_required",
                f"{name} 目标版本 v{target['target_version']} 低于本地 v{value}",
            )
        else:
            code, message = "", ""
        if code:
            return _result(
                [code], message, code=code, compatible=False,
                mode="normal", setup_allowed=False, local=local,
            )

    return _result(
        [],
        "目标版本与当前数据版本兼容",
        code="compatible",
        compatible=True,
        mode="normal",
        setup_allowed=False,
        manifest={
            "data_contract_epoch": target_epoch,
            "migration_set_id": target_set,
            "required_features": sorted(required),
        },
        local=local,
    )


def check(
    project_root: Path,
    data_dir: Path,
    *,
    manifest_path: Path | None = None,
    config_path: Path | None = None,
    allow_first_run: bool = True,
) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
    data_dir = Path(data_dir).resolve()
    path = manifest_path or (project_root / "compatibility.json")
    try:
        path = path.resolve()
        path.relative_to(project_root)
    except (OSError, ValueError):
        return _result(
            ["manifest_path_escape"], "manifest 不在 project root 内"
        )
    if not path.is_file() or path.is_symlink():
        return _result(
            ["manifest_missing"], f"目标源码缺少 compatibility.json：{path}"
        )
    try:
        raw = _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _result(["manifest_invalid"], f"兼容 manifest 不可读：{exc}")
    manifest, error = _validate_manifest(raw)
    if error:
        return _result(["manifest_invalid"], error)
    assert manifest is not None
    maintenance_phase, maintenance_error = _read_maintenance_state(data_dir)
    if maintenance_error:
        return _result(
            ["maintenance_state_invalid"], maintenance_error,
            code="maintenance_state_invalid", compatible=False,
            mode="maintenance", setup_allowed=False,
        )
    if maintenance_phase in _CONTROLLER_BLOCKING_PHASES:
        return _result(
            ["maintenance_blocked"],
            f"实例处于维护 phase={maintenance_phase}；禁止普通 Runtime 启动",
            code="maintenance_blocked", compatible=False,
            mode="maintenance", setup_allowed=False,
            maintenance_phase=maintenance_phase,
        )
    return evaluate(
        manifest,
        read_local_state(data_dir, config_path=config_path),
        allow_first_run=allow_first_run,
    )


def _command_config_path(command: list[str], data_dir: Path) -> Path:
    default = (data_dir / "config.yaml").resolve()
    for index, item in enumerate(command):
        if item == "--config" and index + 1 < len(command):
            return Path(command[index + 1]).expanduser().resolve()
        if item.startswith("--config="):
            return Path(item.split("=", 1)[1]).expanduser().resolve()
    return default


def _validate_target_command(
    project_root: Path,
    data_dir: Path,
    command: list[str],
    *,
    config_path: Path | None,
) -> str | None:
    """受管启动只接受 project root 内的 Python + main.py。"""
    if len(command) < 2:
        return "受管启动必须提供 Python 和 main.py"
    root = project_root.resolve()
    try:
        executable = Path(command[0]).expanduser()
        if not executable.is_absolute():
            return "受管启动要求 Python 使用绝对路径"
        executable = executable.resolve()
        executable.relative_to(root)
    except (OSError, ValueError):
        return "受管启动的 Python 必须在 project root 内"
    try:
        entry = Path(command[1]).expanduser()
        if not entry.is_absolute():
            entry = root / entry
        entry = entry.resolve()
        entry.relative_to(root)
    except (OSError, ValueError):
        return "受管启动的入口必须在 project root 内"
    if entry.name != "main.py":
        return "受管启动只允许项目根 main.py 入口"
    expected = (
        Path(config_path).expanduser().resolve()
        if config_path is not None
        else (data_dir / "config.yaml").resolve()
    )
    actual = _command_config_path(command, data_dir)
    if actual != expected:
        return (
            "启动参数 --config 与 launcher 检查的配置不一致："
            f"命令 {actual}，检查 {expected}"
        )
    return None


def _parse_command(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" in argv:
        index = argv.index("--")
        return argv[:index], argv[index + 1:]
    return argv, []


def main(argv: list[str] | None = None) -> int:
    args_list, command = _parse_command(list(argv if argv is not None else sys.argv[1:]))
    parser = argparse.ArgumentParser(
        prog="karen-trusted-launcher",
        description="在启动 Runtime 前独立检查 compatibility.json 与本地数据",
    )
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--config", default="", help="受管实例的 config.yaml")
    parser.add_argument("--manifest", default="")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--no-first-run", action="store_true")
    parser.add_argument(
        "--allow-setup",
        action="store_true",
        help="允许首次运行向导；普通 Runtime 仍不得在 setup_required 下启动",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(args_list)

    project_root = Path(args.project_root).expanduser().resolve()
    data_dir = Path(args.data_dir).expanduser().resolve()
    config_path = Path(args.config).expanduser().resolve() if args.config else None
    result = check(
        project_root,
        data_dir,
        manifest_path=Path(args.manifest) if args.manifest else None,
        config_path=config_path,
        allow_first_run=not args.no_first_run,
    )
    setup_only = bool(
        result.get("code") == "setup_required" and result.get("setup_allowed")
    )
    if args.json or args.check_only or not (result["ok"] or setup_only):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    if not (result["ok"] or setup_only):
        return EXIT_INCOMPATIBLE
    if args.check_only:
        return 0
    if setup_only and not args.allow_setup:
        print(
            "当前只允许首次配置向导；请使用 --allow-setup 并确保进入的是向导模式",
            file=sys.stderr,
        )
        return EXIT_INCOMPATIBLE
    if not command:
        print("兼容检查通过，但没有提供要启动的命令（使用 -- <command...>）", file=sys.stderr)
        return 2
    binding_error = _validate_target_command(
        project_root, data_dir, command, config_path=config_path
    )
    if binding_error:
        print(binding_error, file=sys.stderr)
        return EXIT_INCOMPATIBLE
    env = os.environ.copy()
    # 受管进程的数据根由 launcher 固定；不继承可改写目标的旧环境。
    env["KAREN_DATA_ROOT"] = str(data_dir)
    env["KAREN_LAUNCHER_PROTOCOL"] = str(LAUNCHER_PROTOCOL)
    try:
        os.chdir(project_root)
        os.execvpe(command[0], command, env)
    except OSError as exc:
        print(f"无法启动目标命令：{exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
