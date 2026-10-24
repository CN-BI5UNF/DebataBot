"""B01：离线验证/隔离恢复演练，不接触生产数据。"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import zipfile

import pytest

from app_config.paths import AppPaths
from core.backup import BackupService
from core.restore import RestoreError, drill_backup, verify_backup


class Secrets:
    def __init__(self, pem: str) -> None:
        self.pem = pem

    def export_rsa_private_key(self) -> str:
        return self.pem


def _keypair() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return private, public


def _seed(paths: AppPaths) -> str:
    paths.ensure_data_dirs()
    paths.PERSONAS_DIR.mkdir(parents=True, exist_ok=True)
    paths.USER_PROVIDER_PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    private, public = _keypair()
    paths.CONFIG_FILE.write_text("version: 2\n", encoding="utf-8")
    paths.SECRETS_FILE.write_bytes(b"not-a-real-secret")
    paths.SECRETS_META_FILE.write_bytes(b"not-a-real-meta")
    paths.RSA_PRIVATE_KEY_FILE.write_text(private, encoding="ascii")
    paths.RSA_PUBLIC_KEY_FILE.write_text(public, encoding="ascii")
    memory = paths.MEMORY_DIR / "demo"
    memory.mkdir(parents=True)
    with sqlite3.connect(memory / "chat.db") as conn:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO messages(body) VALUES ('offline-drill')")
        conn.commit()
    (paths.PERSONAS_DIR / "demo").mkdir()
    (paths.PERSONAS_DIR / "demo" / "role.md").write_text("# role\n", encoding="utf-8")
    return private


@pytest.fixture()
def backup_path(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    private = _seed(paths)
    service = BackupService(
        paths.PROJECT_ROOT,
        paths,
        backup_dir=tmp_path / "backups",
        secrets_manager=Secrets(private),
    )
    import asyncio

    job = asyncio.run(service.create_backup())
    assert job["state"] == "done", job
    return tmp_path / "backups" / job["filename"]


def test_verify_backup_checks_members_sqlite_and_keys(backup_path):
    report = verify_backup(backup_path)
    assert report["ok"] is True
    assert report["integrity_ok"] is True
    assert report["backup_type"] == "user_export"
    assert report["format_version"] == 2
    assert report["chat_restorable"] is True
    assert report["credential_restorable"] is True
    assert report["credential_verification"] == "key_pair_matched"
    assert report["sqlite_checked"] >= 1
    assert report["restore_ready"] is True
    assert report["errors"] == []
    assert "not-a-real-secret" not in json.dumps(report, ensure_ascii=False)


def test_verify_backup_rejects_hash_mismatch(backup_path, tmp_path):
    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(backup_path) as src, zipfile.ZipFile(broken, "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename.endswith("config.yaml"):
                data = b"version: 999\n"
            dst.writestr(info, data)
    report = verify_backup(broken)
    assert report["ok"] is False
    assert any(item["code"] == "hash_mismatch" for item in report["errors"])


def test_verify_backup_rejects_unsafe_member_and_symlink(tmp_path):
    unsafe = tmp_path / "unsafe.zip"
    manifest = {
        "format_version": 2,
        "backup_type": "user_export",
        "files": [{
            "path": "../escape.txt",
            "size": 1,
            "sha256": "0" * 64,
            "kind": "file",
            "consistent": True,
        }],
    }
    with zipfile.ZipFile(unsafe, "w") as zf:
        zf.writestr("README_RESTORE.txt", "readme")
        zf.writestr("../escape.txt", b"x")
        zf.writestr("manifest.json", json.dumps(manifest))
    report = verify_backup(unsafe)
    assert report["ok"] is False
    assert any(item["code"] == "unsafe_member" for item in report["errors"])

    symlink = tmp_path / "symlink.zip"
    manifest["files"] = [{
        "path": "data/link",
        "size": 0,
        "sha256": "0" * 64,
        "kind": "file",
        "consistent": True,
    }]
    with zipfile.ZipFile(symlink, "w") as zf:
        zf.writestr("README_RESTORE.txt", "readme")
        info = zipfile.ZipInfo("data/link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "")
        zf.writestr("manifest.json", json.dumps(manifest))
    report = verify_backup(symlink)
    assert report["ok"] is False
    assert any(item["code"] == "symlink_member" for item in report["errors"])


def test_drill_extracts_to_empty_isolated_directory(backup_path, tmp_path):
    destination = tmp_path / "drill"
    report = drill_backup(backup_path, destination=destination)
    assert report["ok"] is True
    assert report["cleaned"] is False
    assert destination.is_dir()
    assert (destination / "data" / "config.yaml").is_file()
    assert (destination / "personas" / "demo" / "role.md").is_file()
    assert not any(path.is_symlink() for path in destination.rglob("*"))
    if os.name != "nt":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o700
        assert stat.S_IMODE((destination / "data" / "config.yaml").stat().st_mode) == 0o600


def test_drill_refuses_nonempty_destination(backup_path, tmp_path):
    destination = tmp_path / "occupied"
    destination.mkdir()
    (destination / "keep.txt").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(RestoreError) as exc:
        drill_backup(backup_path, destination=destination)
    assert exc.value.code == "destination_not_empty"
    assert (destination / "keep.txt").read_text() == "do not overwrite"


def test_drill_rejects_protected_overlap(backup_path, tmp_path):
    protected = tmp_path / "data"
    protected.mkdir(exist_ok=True)
    with pytest.raises(RestoreError) as exc:
        drill_backup(
            backup_path,
            destination=protected / "restore",
            protected_roots=[protected],
        )
    assert exc.value.code == "unsafe_destination"
    assert not (protected / "restore").exists()
