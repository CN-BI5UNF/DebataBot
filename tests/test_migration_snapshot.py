"""B02：严格迁移快照必须由可信屏障保护，不能冒充普通用户导出。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import zipfile

import pytest

from app_config.paths import AppPaths
from core.backup import (
    BACKUP_TYPE_MIGRATION_SNAPSHOT,
    BackupError,
    BackupService,
)
from core.restore import verify_backup
from core.snapshot_barrier import SnapshotBarrier, SnapshotMarker


class Barrier(SnapshotBarrier):
    def __init__(self) -> None:
        self.frozen = False
        self.released = False

    def freeze(self) -> SnapshotMarker:
        self.frozen = True
        return SnapshotMarker(
            epoch=0,
            generation=7,
            high_water=42,
            contract_epoch=0,
            migration_set_id="",
            features=frozenset(),
            migration_safe=False,
            details={"mode": "test"},
        )

    def release(self, marker: SnapshotMarker) -> None:
        assert marker.generation == 7
        self.released = True


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
    private, public = _keypair()
    paths.CONFIG_FILE.write_text("version: 2\n", encoding="utf-8")
    paths.SECRETS_FILE.write_bytes(b"ciphertext")
    paths.SECRETS_META_FILE.write_bytes(b"meta")
    paths.RSA_PRIVATE_KEY_FILE.write_text(private, encoding="ascii")
    paths.RSA_PUBLIC_KEY_FILE.write_text(public, encoding="ascii")
    memory = paths.MEMORY_DIR / "demo"
    memory.mkdir(parents=True)
    with sqlite3.connect(memory / "chat.db") as conn:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO messages(body) VALUES ('strict')")
        conn.commit()
    with sqlite3.connect(paths.MAINTENANCE_DB_FILE) as conn:
        conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, kind TEXT)")
        conn.commit()
    (paths.MAINTENANCE_DIR / "deletion_ledger.jsonl").write_text(
        '{"id": 1}\n', encoding="utf-8"
    )
    (paths.DATA_DIR / "compatibility_state.json").write_text(
        json.dumps({"state_format": 1, "phase": "steady", "generation": 7}),
        encoding="utf-8",
    )
    (paths.PERSONAS_DIR / "demo").mkdir(parents=True, exist_ok=True)
    (paths.PERSONAS_DIR / "demo" / "role.md").write_text("# role\n", encoding="utf-8")
    return private


@pytest.mark.asyncio
async def test_strict_snapshot_contains_maintenance_and_requires_barrier(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    private = _seed(paths)
    service = BackupService(
        paths.PROJECT_ROOT,
        paths,
        backup_dir=tmp_path / "backups",
        secrets_manager=Secrets(private),
    )
    with pytest.raises(BackupError) as exc:
        await service.start(backup_type=BACKUP_TYPE_MIGRATION_SNAPSHOT)
    assert exc.value.code == "barrier_required"

    barrier = Barrier()
    job = await service.start(
        backup_type=BACKUP_TYPE_MIGRATION_SNAPSHOT,
        barrier=barrier,
    )
    finished = await service.wait(job["id"], timeout=30)
    assert finished["state"] == "done", finished
    assert finished["backup_type"] == BACKUP_TYPE_MIGRATION_SNAPSHOT
    assert finished["strict"] is True
    assert barrier.frozen is True
    assert barrier.released is True
    assert finished["capabilities"]["migration_safe"] is False
    assert finished["capabilities"]["cross_plane_consistent"] is True
    assert finished["snapshot"]["generation"] == 7
    # 路径名含 deletion/ledger 只是工件提示，不能证明 D01 语义账本。
    assert finished["snapshot"]["deletion_ledger_present"] is False
    assert finished["snapshot"]["ledger_artifact_present"] is True
    assert finished["snapshot"]["ledger_aware"] is False
    assert finished["capabilities"]["ledger_aware"] is False
    assert (
        finished["snapshot"]["migration_blocked_reason"]
        == "semantic_deletion_ledger_required"
    )

    archive = tmp_path / "backups" / finished["filename"]
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
    assert "data/maintenance.sqlite3" in names
    assert "data/maintenance/deletion_ledger.jsonl" in names
    assert "data/compatibility_state.json" in names
    assert manifest["backup_type"] == BACKUP_TYPE_MIGRATION_SNAPSHOT
    assert manifest["strict"] is True
    report = verify_backup(archive)
    assert report["ok"] is True
    assert report["backup_type"] == BACKUP_TYPE_MIGRATION_SNAPSHOT


@pytest.mark.asyncio
async def test_strict_snapshot_fails_if_maintenance_db_missing(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    private = _seed(paths)
    paths.MAINTENANCE_DB_FILE.unlink()
    service = BackupService(
        paths.PROJECT_ROOT,
        paths,
        backup_dir=tmp_path / "backups",
        secrets_manager=Secrets(private),
    )
    barrier = Barrier()
    job = await service.start(
        backup_type=BACKUP_TYPE_MIGRATION_SNAPSHOT,
        barrier=barrier,
    )
    finished = await service.wait(job["id"], timeout=30)
    assert finished["state"] == "failed"
    assert finished["error"]["code"] == "snapshot_missing_required"
    assert barrier.released is True
    assert not list((tmp_path / "backups").glob("Debata_Backup_*.zip"))


@pytest.mark.asyncio
async def test_strict_snapshot_rejects_media_exclusion_and_corrupt_db(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    private = _seed(paths)
    service = BackupService(
        paths.PROJECT_ROOT,
        paths,
        backup_dir=tmp_path / "backups",
        secrets_manager=Secrets(private),
    )
    barrier = Barrier()
    job = await service.start(
        backup_type=BACKUP_TYPE_MIGRATION_SNAPSHOT,
        barrier=barrier,
        include_media=False,
    )
    finished = await service.wait(job["id"], timeout=30)
    assert finished["state"] == "failed"
    assert finished["error"]["code"] == "strict_media_required"
    assert barrier.released is True

    (paths.MEMORY_DIR / "bad.db").write_bytes(b"not-a-sqlite-file")
    barrier = Barrier()
    job = await service.start(
        backup_type=BACKUP_TYPE_MIGRATION_SNAPSHOT,
        barrier=barrier,
    )
    finished = await service.wait(job["id"], timeout=30)
    assert finished["state"] == "failed"
    assert finished["error"]["code"] == "snapshot_invalid_database"
    assert barrier.released is True


def test_user_export_still_excludes_maintenance(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    private = _seed(paths)

    async def run():
        service = BackupService(
            paths.PROJECT_ROOT,
            paths,
            backup_dir=tmp_path / "backups",
            secrets_manager=Secrets(private),
        )
        return await service.create_backup()

    job = asyncio.run(run())
    assert job["state"] == "done", job
    with zipfile.ZipFile(tmp_path / "backups" / job["filename"]) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
    assert "data/maintenance.sqlite3" not in names
    assert "data/maintenance/deletion_ledger.jsonl" not in names
    assert manifest["backup_type"] == "user_export"
    assert manifest["capabilities"]["migration_safe"] is False
