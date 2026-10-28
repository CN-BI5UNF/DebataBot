"""用户备份的离线验证与隔离恢复演练。

这里不连接 Runtime、不启动模型/适配器、不写入生产 DATA_DIR。用途：

- ``verify_backup``：只读打开 ZIP，校验成员集合/路径/CRC/hash/大小，
  在临时目录逐个验证 SQLite，并检查 RSA 公私钥是否同代匹配。
- ``drill_backup``：把包解压到一个全新的隔离目录，复验 SQLite/密钥后
  报告；默认使用系统临时目录并在结束后清理。提供 ``--destination`` 时
  拒绝覆盖非空目录，方便人工检查。

限制：解析包内 manifest、拒绝绝对路径/``..``/反斜杠/符号链接/异常压缩比，
不执行包内任何脚本。ZIP 本身未加密；本模块不读取或打印聊天正文与明文密钥。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

CHUNK_SIZE = 1024 * 1024
MANIFEST_NAME = "manifest.json"
README_NAME = "README_RESTORE.txt"
SUPPORTED_TYPES = {"user_export", "migration_snapshot"}
FORMAT_MIN = 2
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 200_000
MAX_MEMBER_BYTES = 32 * 1024 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1000
SPACE_MARGIN = 64 * 1024 * 1024


class RestoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ArchiveLimits:
    max_members: int = MAX_MEMBERS
    max_member_bytes: int = MAX_MEMBER_BYTES
    max_total_bytes: int = MAX_TOTAL_BYTES
    max_compression_ratio: int = MAX_COMPRESSION_RATIO


def _error(errors: list[dict], code: str, message: str) -> None:
    errors.append({"code": code, "message": message})


def _warn(warnings: list[str], message: str) -> None:
    if message not in warnings:
        warnings.append(message)


def _safe_member_name(name: str) -> str | None:
    """返回规范化的 zip 成员名；拒绝绝对/穿越/平台相关路径。"""
    if not isinstance(name, str) or not name:
        return None
    if name.startswith("/") or "\\" in name or "\x00" in name:
        return None
    # Windows 盘符/UNC 形式即使运行在 POSIX 也拒绝。
    if len(name) >= 2 and name[1] == ":":
        return None
    path = PurePosixPath(name)
    if any(part in ("", ".", "..") for part in path.parts):
        return None
    normalized = path.as_posix()
    if normalized in {MANIFEST_NAME, README_NAME}:
        return normalized
    if normalized.startswith("data/") or normalized.startswith("personas/"):
        return normalized
    return None


def _sha256_stream(handle: Any) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = handle.read(CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_quick_check(path: Path) -> tuple[bool, str]:
    """只读 quick_check；不创建缺失库，不读取业务正文。"""
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        try:
            conn.execute("PRAGMA query_only=ON")
            row = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
        return bool(row and row[0] == "ok"), "" if row else "quick_check 返回空"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]


def _key_pair_check(
    archive: zipfile.ZipFile,
    entries: dict[str, dict],
) -> tuple[bool, str]:
    """只比较公/私钥是否匹配；不解密 secrets.enc，不输出 PEM。"""
    names = set(entries)
    if "data/rsa_private.pem" not in names or "data/rsa_public.pem" not in names:
        return False, "key_material_missing"
    try:
        from cryptography.hazmat.primitives import serialization

        private_bytes = archive.read("data/rsa_private.pem")
        public_bytes = archive.read("data/rsa_public.pem")
        if len(private_bytes) > 1024 * 1024 or len(public_bytes) > 1024 * 1024:
            return False, "key_material_too_large"
        private = serialization.load_pem_private_key(private_bytes, password=None)
        public = serialization.load_pem_public_key(public_bytes)
        matched = (
            private.public_key().public_numbers() == public.public_numbers()
        )
        return bool(matched), "key_pair_matched" if matched else "key_pair_mismatch"
    except Exception:  # noqa: BLE001
        return False, "key_pair_unverified"


def verify_backup(
    path: Path,
    *,
    deep_sqlite: bool = True,
    limits: ArchiveLimits | None = None,
) -> dict[str, Any]:
    """只读验证备份包；失败项放进 errors，不抛异常（除文件不可打开外）。"""
    archive_path = Path(path)
    limits = limits or ArchiveLimits()
    errors: list[dict] = []
    warnings: list[str] = []
    result: dict[str, Any] = {
        "filename": archive_path.name,
        "ok": False,
        "integrity_ok": False,
        "backup_type": "",
        "format_version": None,
        "validation_status": "unknown",
        "partial": False,
        "size": 0,
        "sha256": "",
        "member_count": 0,
        "declared_file_count": 0,
        "uncompressed_bytes": 0,
        "sqlite_checked": 0,
        "chat_restorable": False,
        "credential_restorable": False,
        "credential_verification": "not_checked",
        "capabilities": {},
        "ledger": None,
        "migration_ready": False,
        "errors": errors,
        "warnings": warnings,
        "limits": {
            "max_members": limits.max_members,
            "max_member_bytes": limits.max_member_bytes,
            "max_total_bytes": limits.max_total_bytes,
            "max_compression_ratio": limits.max_compression_ratio,
        },
    }
    try:
        st = archive_path.stat()
        if not archive_path.is_file() or archive_path.is_symlink():
            raise RestoreError("not_found", "备份文件不存在或不是普通文件")
        result["size"] = st.st_size
        result["sha256"] = _sha256_file(archive_path)
    except (OSError, RestoreError) as exc:
        _error(errors, "not_found", str(exc))
        return result

    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        _error(errors, "bad_zip", f"无法打开 ZIP：{exc}")
        return result

    with archive:
        infos = archive.infolist()
        if len(infos) > limits.max_members:
            _error(errors, "member_limit", "ZIP 成员数量超过限制")
            return result
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            _error(errors, "duplicate_member", "ZIP 含重复成员")
        result["member_count"] = len(infos)
        total_uncompressed = 0
        for info in infos:
            if info.file_size < 0 or info.file_size > limits.max_member_bytes:
                _error(errors, "member_too_large", f"成员过大：{info.filename}")
            if info.compress_size > 0:
                ratio = info.file_size / info.compress_size
                if info.file_size > 1024 * 1024 and ratio > limits.max_compression_ratio:
                    _error(
                        errors,
                        "compression_ratio",
                        f"成员压缩比异常：{info.filename}",
                    )
            total_uncompressed += max(0, info.file_size)
            if stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK:
                _error(errors, "symlink_member", f"ZIP 含符号链接：{info.filename}")
        result["uncompressed_bytes"] = total_uncompressed
        if total_uncompressed > limits.max_total_bytes:
            _error(errors, "archive_too_large", "解压后总大小超过限制")
        if errors:
            return result
        try:
            bad = archive.testzip()
        except (OSError, zipfile.BadZipFile) as exc:
            _error(errors, "crc_failed", f"ZIP CRC 校验失败：{exc}")
            return result
        if bad:
            _error(errors, "crc_failed", f"ZIP CRC 校验失败：{bad}")
            return result

        try:
            manifest_info = archive.getinfo(MANIFEST_NAME)
            if manifest_info.file_size > MAX_MANIFEST_BYTES:
                raise ValueError("manifest 过大")
            manifest = json.loads(archive.read(MANIFEST_NAME))
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            _error(errors, "manifest_invalid", f"manifest 不可读：{exc}")
            return result
        if not isinstance(manifest, dict):
            _error(errors, "manifest_invalid", "manifest 根不是对象")
            return result

        backup_type = str(manifest.get("backup_type") or "")
        result["backup_type"] = backup_type
        result["format_version"] = manifest.get(
            "format_version", manifest.get("format")
        )
        result["validation_status"] = str(
            manifest.get("validation_status") or "unknown"
        )
        result["partial"] = bool(manifest.get("partial", False))
        warnings.extend(
            str(item)[:500]
            for item in (manifest.get("warnings") or [])
            if isinstance(item, str)
        )
        if backup_type not in SUPPORTED_TYPES:
            _error(errors, "unsupported_type", f"不支持的备份类型：{backup_type}")
        if not isinstance(result["format_version"], int) or result["format_version"] < FORMAT_MIN:
            _error(errors, "legacy_manifest", "旧格式备份不提供逐成员验证")
        files = manifest.get("files")
        if not isinstance(files, list):
            _error(errors, "manifest_invalid", "manifest.files 非法")
            return result
        result["declared_file_count"] = len(files)
        entries: dict[str, dict] = {}
        for item in files:
            if not isinstance(item, dict):
                _error(errors, "manifest_invalid", "manifest 条目非法")
                continue
            raw_name = item.get("path")
            name = _safe_member_name(raw_name)
            if name is None:
                _error(errors, "unsafe_member", f"成员路径不安全：{raw_name!r}")
                continue
            if name in entries:
                _error(errors, "duplicate_manifest", f"manifest 重复成员：{name}")
                continue
            entries[name] = item
        expected = {MANIFEST_NAME, README_NAME} | set(entries)
        actual = set(names)
        if expected != actual:
            missing = sorted(expected - actual)[:10]
            extra = sorted(actual - expected)[:10]
            _error(
                errors,
                "member_mismatch",
                f"成员集合不一致：缺少 {missing}，多出 {extra}",
            )
        if errors:
            return result

        sqlite_checked = 0
        ledger_probes: list[dict] = []
        with tempfile.TemporaryDirectory(prefix="karen-backup-verify-") as tmp:
            tmp_root = Path(tmp)
            for index, (name, entry) in enumerate(entries.items()):
                expected_size = entry.get("size")
                expected_hash = entry.get("sha256")
                if not isinstance(expected_size, int) or expected_size < 0:
                    _error(errors, "manifest_invalid", f"成员大小非法：{name}")
                    continue
                if not isinstance(expected_hash, str) or len(expected_hash) != 64:
                    _error(errors, "manifest_invalid", f"成员 hash 非法：{name}")
                    continue
                try:
                    info = archive.getinfo(name)
                except KeyError:
                    _error(errors, "member_missing", f"成员缺失：{name}")
                    continue
                if info.file_size != expected_size:
                    _error(errors, "size_mismatch", f"成员大小不一致：{name}")
                kind = entry.get("kind", "file")
                if kind == "sqlite":
                    temp_path = tmp_root / f"{index}.db"
                    digest = hashlib.sha256()
                    written = 0
                    try:
                        with archive.open(name) as src, temp_path.open("wb") as dst:
                            while True:
                                chunk = src.read(CHUNK_SIZE)
                                if not chunk:
                                    break
                                dst.write(chunk)
                                digest.update(chunk)
                                written += len(chunk)
                        if written != expected_size:
                            _error(errors, "size_mismatch", f"成员大小不一致：{name}")
                        if digest.hexdigest() != expected_hash:
                            _error(errors, "hash_mismatch", f"成员 hash 不一致：{name}")
                        with temp_path.open("rb") as handle:
                            if handle.read(16) != b"SQLite format 3\x00":
                                _error(errors, "sqlite_header", f"SQLite 头非法：{name}")
                            else:
                                ok, detail = _sqlite_quick_check(temp_path)
                                sqlite_checked += 1
                                if not ok:
                                    _error(
                                        errors,
                                        "sqlite_check_failed",
                                        f"SQLite quick_check 失败：{name} {detail}",
                                    )
                                elif name.startswith("data/memory/"):
                                    from core.d01 import ledger_evidence

                                    ledger_probes.append(
                                        ledger_evidence(temp_path, label=name)
                                    )
                    except OSError as exc:
                        _error(errors, "member_read_failed", f"读取成员失败：{name}: {exc}")
                    finally:
                        try:
                            temp_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                else:
                    try:
                        with archive.open(name) as handle:
                            digest = hashlib.sha256()
                            written = 0
                            while True:
                                chunk = handle.read(CHUNK_SIZE)
                                if not chunk:
                                    break
                                digest.update(chunk)
                                written += len(chunk)
                        if written != expected_size:
                            _error(errors, "size_mismatch", f"成员大小不一致：{name}")
                        if digest.hexdigest() != expected_hash:
                            _error(errors, "hash_mismatch", f"成员 hash 不一致：{name}")
                    except OSError as exc:
                        _error(errors, "member_read_failed", f"读取成员失败：{name}: {exc}")

        ledger_manifest = manifest.get("ledger")
        ledger_aggregate = None
        if ledger_manifest is not None:
            if not isinstance(ledger_manifest, dict):
                _error(
                    errors,
                    "ledger_manifest_invalid",
                    "manifest.ledger 不是对象",
                )
            else:
                from core.d01 import aggregate_ledger_evidence

                ledger_aggregate = aggregate_ledger_evidence(ledger_probes)
                if str(ledger_manifest.get("digest") or "") != str(
                    ledger_aggregate.get("digest") or ""
                ):
                    _error(
                        errors,
                        "ledger_evidence_mismatch",
                        "归档数据库重算的 ledger 证据与 manifest 不一致",
                    )
                for field in ("aware", "complete"):
                    if bool(ledger_manifest.get(field)) != bool(
                        ledger_aggregate.get(field)
                    ):
                        _error(
                            errors,
                            "ledger_manifest_mismatch",
                            f"manifest.ledger.{field} 与重算证据不一致",
                        )
                if int(ledger_manifest.get("schema_version") or 0) != int(
                    ledger_aggregate.get("schema_version") or 0
                ):
                    _error(
                        errors,
                        "ledger_manifest_mismatch",
                        "manifest.ledger.schema_version 与重算证据不一致",
                    )
                declared_caps = manifest.get("capabilities") or {}
                if bool(declared_caps.get("ledger_aware")) != bool(
                    ledger_aggregate.get("aware")
                ):
                    _error(
                        errors,
                        "ledger_capability_mismatch",
                        "capabilities.ledger_aware 与归档证据不一致",
                    )
                if bool(declared_caps.get("ledger_complete")) != bool(
                    ledger_aggregate.get("complete")
                ):
                    _error(
                        errors,
                        "ledger_capability_mismatch",
                        "capabilities.ledger_complete 与归档证据不一致",
                    )
                if bool(declared_caps.get("migration_safe")):
                    snapshot = manifest.get("snapshot") or {}
                    if (
                        not ledger_aggregate.get("complete")
                        or snapshot.get("contract_match") is not True
                        or not bool(
                            declared_caps.get("cross_plane_consistent")
                        )
                    ):
                        _error(
                            errors,
                            "migration_safe_evidence_mismatch",
                            "migration_safe=true 缺少完整 ledger/契约/跨平面证据",
                        )
                result["ledger"] = ledger_aggregate

        result["sqlite_checked"] = sqlite_checked
        result["chat_restorable"] = any(
            name.startswith("data/memory/")
            and entry.get("kind") == "sqlite"
            and bool(entry.get("consistent", True))
            for name, entry in entries.items()
        ) and not any(
            item["code"].startswith("sqlite_") or item["code"] == "hash_mismatch"
            for item in errors
        )
        key_ok, key_verification = _key_pair_check(archive, entries)
        result["credential_verification"] = key_verification
        result["credential_restorable"] = bool(
            key_ok and manifest.get("credential_restorable", False)
        )
        if key_verification == "key_material_missing":
            _warn(warnings, "归档中没有同时包含 RSA 公私钥，API 密钥可能无法恢复")
        elif not key_ok:
            _warn(warnings, "归档中的 RSA 公私钥无法匹配，凭据可恢复性未通过")
        result["capabilities"] = dict(manifest.get("capabilities") or {})
        result["capabilities"]["credential_restorable"] = result["credential_restorable"]
        if ledger_aggregate is not None:
            # 能力以归档数据库重算结果为准，不采用 manifest 自报值。
            result["capabilities"]["ledger_aware"] = bool(
                ledger_aggregate.get("aware")
            )
            result["capabilities"]["ledger_complete"] = bool(
                ledger_aggregate.get("complete")
            )
        result["integrity_ok"] = not errors
        result["ok"] = not errors
        result["restore_ready"] = bool(
            not errors
            and backup_type == "user_export"
            and result["chat_restorable"]
        )
        result["migration_ready"] = bool(
            not errors
            and backup_type == "migration_snapshot"
            and result["capabilities"].get("migration_safe")
            and result["capabilities"].get("ledger_complete")
            and result["capabilities"].get("cross_plane_consistent")
        )
        if result["partial"]:
            _warn(warnings, "该包在生成时标记为 partial，不代表完整备份")
        if not result["chat_restorable"]:
            _warn(warnings, "没有可验证的聊天数据库；聊天记录可能无法恢复")
        return result


def _ensure_destination(destination: Path, protected_roots: Iterable[Path]) -> None:
    raw_destination = Path(destination)
    if raw_destination.is_symlink():
        raise RestoreError("unsafe_destination", "演练目标不能是符号链接")
    destination = raw_destination.resolve()
    for root in protected_roots:
        try:
            root = Path(root).resolve()
        except OSError:
            continue
        if destination == root or destination in root.parents or root in destination.parents:
            raise RestoreError(
                "unsafe_destination",
                f"演练目录不能与生产数据目录重叠：{root}",
            )
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise RestoreError("unsafe_destination", "演练目标不能是符号链接或非目录")
        if any(destination.iterdir()):
            raise RestoreError(
                "destination_not_empty",
                "演练目标目录非空；为防覆盖，请使用全新目录",
            )
    else:
        destination.mkdir(parents=True, exist_ok=False)
    if os.name != "nt":
        os.chmod(destination, 0o700)


def drill_backup(
    path: Path,
    *,
    destination: Path | None = None,
    protected_roots: Iterable[Path] = (),
    keep: bool = False,
    deep_sqlite: bool = True,
) -> dict[str, Any]:
    """验证并解压到隔离目录，再复验；默认使用并清理临时目录。"""
    verification = verify_backup(path, deep_sqlite=deep_sqlite)
    if not verification.get("ok"):
        raise RestoreError("backup_verify_failed", "备份验证未通过，不能进行恢复演练")

    created_temp = destination is None
    target = Path(destination) if destination is not None else Path(
        tempfile.mkdtemp(prefix="karen-restore-drill-")
    )
    try:
        if not created_temp:
            _ensure_destination(target, protected_roots)
        else:
            os.chmod(target, 0o700)
        free = shutil.disk_usage(target).free
        needed = int(verification.get("uncompressed_bytes") or 0) + SPACE_MARGIN
        if free < needed:
            raise RestoreError(
                "insufficient_space",
                f"演练空间不足：需要约 {needed} 字节，可用 {free} 字节",
            )
        extracted: list[str] = []
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read(MANIFEST_NAME))
            expected_entries = {
                item["path"]: item
                for item in manifest.get("files", [])
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            }
            for info in archive.infolist():
                name = _safe_member_name(info.filename)
                if name is None:
                    raise RestoreError("unsafe_member", f"成员路径不安全：{info.filename!r}")
                out_path = target / PurePosixPath(name)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                if os.name != "nt":
                    os.chmod(out_path.parent, 0o700)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                fd = os.open(out_path, flags, 0o600)
                expected = expected_entries.get(name)
                digest = hashlib.sha256()
                written = 0
                try:
                    with archive.open(info) as src, os.fdopen(fd, "wb") as dst:
                        fd = -1  # 已交给 fdopen
                        while True:
                            chunk = src.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            dst.write(chunk)
                            digest.update(chunk)
                            written += len(chunk)
                finally:
                    if fd >= 0:
                        os.close(fd)
                if os.name != "nt":
                    os.chmod(out_path, 0o600)
                if expected is not None:
                    if written != int(expected.get("size", -1)):
                        raise RestoreError(
                            "extract_mismatch", f"解压成员大小不一致：{name}"
                        )
                    if digest.hexdigest() != expected.get("sha256"):
                        raise RestoreError(
                            "extract_mismatch", f"解压成员 hash 不一致：{name}"
                        )
                extracted.append(name)

        sqlite_checked = 0
        failures: list[str] = []
        for name in extracted:
            if not name.startswith("data/") or not name.endswith((".db", ".sqlite", ".sqlite3")):
                continue
            candidate = target / PurePosixPath(name)
            if candidate.is_file() and not candidate.is_symlink():
                ok, detail = _sqlite_quick_check(candidate)
                sqlite_checked += 1
                if not ok:
                    failures.append(f"{name}: {detail}")
        if failures:
            raise RestoreError(
                "drill_sqlite_failed",
                "演练解压后的 SQLite 复验失败：" + "; ".join(failures[:3]),
            )

        # 解压后的数据库再次计算 ledger 证据，绑定归档字节身份，防止
        # verify 与 drill 之间被替换。只在 data/memory 的 SQLite 成员上执行。
        ledger_probes: list[dict] = []
        for name in extracted:
            entry = expected_entries.get(name)
            if (
                not isinstance(entry, dict)
                or entry.get("kind") != "sqlite"
                or not name.startswith("data/memory/")
            ):
                continue
            candidate = target / PurePosixPath(name)
            if candidate.is_file() and not candidate.is_symlink():
                from core.d01 import ledger_evidence

                ledger_probes.append(
                    ledger_evidence(candidate, label=name)
                )
        ledger_manifest = manifest.get("ledger")
        ledger_aggregate = None
        if ledger_manifest is not None:
            if not isinstance(ledger_manifest, dict):
                raise RestoreError(
                    "drill_ledger_mismatch", "manifest.ledger 不是对象"
                )
            from core.d01 import aggregate_ledger_evidence

            ledger_aggregate = aggregate_ledger_evidence(ledger_probes)
            if str(ledger_manifest.get("digest") or "") != str(
                ledger_aggregate.get("digest") or ""
            ):
                raise RestoreError(
                    "drill_ledger_mismatch",
                    "演练解压后的 ledger 证据与 manifest 不一致",
                )
            capabilities = manifest.get("capabilities") or {}
            if bool(capabilities.get("migration_safe")):
                snapshot = manifest.get("snapshot") or {}
                if (
                    not ledger_aggregate.get("complete")
                    or snapshot.get("contract_match") is not True
                    or not bool(capabilities.get("cross_plane_consistent"))
                ):
                    raise RestoreError(
                        "drill_ledger_mismatch",
                        "migration_safe=true 缺少完整 ledger/契约/跨平面证据",
                    )
        report = {
            "ok": True,
            "destination": str(target),
            "kept": bool(keep or not created_temp),
            "cleaned": False,
            "extracted_files": len(extracted),
            "sqlite_checked": sqlite_checked,
            "verification": verification,
            "ledger": ledger_aggregate,
            "ledger_aware": bool(
                ledger_aggregate and ledger_aggregate.get("aware")
            ),
            "ledger_complete": bool(
                ledger_aggregate and ledger_aggregate.get("complete")
            ),
            "migration_ready": bool(
                ledger_aggregate
                and ledger_aggregate.get("complete")
                and (manifest.get("capabilities") or {}).get("migration_safe")
            ),
            "warnings": list(verification.get("warnings") or []),
        }
        if created_temp and not keep:
            shutil.rmtree(target, ignore_errors=True)
            report["cleaned"] = True
        return report
    finally:
        if created_temp and not keep:
            shutil.rmtree(target, ignore_errors=True)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Karen 备份离线验证/隔离恢复演练（不接触生产数据）"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("verify", "drill"):
        item = sub.add_parser(command)
        item.add_argument("backup", help="备份 zip 路径")
        if command == "drill":
            item.add_argument("--destination", default="", help="全新演练目录；默认临时目录")
            item.add_argument("--keep", action="store_true", help="保留临时演练目录")
            item.add_argument(
                "--protected-root", action="append", default=[],
                help="禁止与之重叠的目录（可重复）",
            )
        item.add_argument("--no-deep-sqlite", action="store_true")
        item.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            report = verify_backup(
                Path(args.backup), deep_sqlite=not args.no_deep_sqlite
            )
        else:
            report = drill_backup(
                Path(args.backup),
                destination=Path(args.destination) if args.destination else None,
                protected_roots=[Path(item) for item in args.protected_root],
                keep=args.keep,
                deep_sqlite=not args.no_deep_sqlite,
            )
    except RestoreError as exc:
        payload = {"ok": False, "code": exc.code, "message": exc.message}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else payload)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"{'OK' if report.get('ok') else 'FAILED'} "
            f"{report.get('filename') or report.get('destination')}"
        )
        for item in report.get("errors", []):
            print(f"  error: {item['code']}: {item['message']}")
        for item in report.get("warnings", []):
            print(f"  warning: {item}")
        if args.command == "drill":
            print(f"  drill: {report.get('destination')} "
                  f"(cleaned={report.get('cleaned')})")
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    sys.exit(_main())
