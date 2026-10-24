"""T14：D01 后 ledger-aware 严格快照、归档复验和演练。"""

from __future__ import annotations

import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest

from app_config.paths import AppPaths
from core.backup import (
    BACKUP_TYPE_MIGRATION_SNAPSHOT,
    BackupService,
)
from core.d01 import D01Error, migrate_database
from core.restore import RestoreError, drill_backup, verify_backup
from core.snapshot_barrier import SnapshotBarrier, SnapshotMarker
from memory.debata_db import DebataDB


class Secrets:
    def __init__(self, pem: str) -> None:
        self.pem = pem

    def export_rsa_private_key(self) -> str:
        return self.pem


class Barrier(SnapshotBarrier):
    def __init__(
        self,
        *,
        contract_epoch: int = 1,
        migration_set_id: str = "M001",
        generation: int = 9,
        migration_safe: bool = False,
    ) -> None:
        self.contract_epoch = contract_epoch
        self.migration_set_id = migration_set_id
        self.generation = generation
        self.claimed_migration_safe = migration_safe

    def freeze(self) -> SnapshotMarker:
        return SnapshotMarker(
            epoch=1,
            generation=self.generation,
            high_water=100,
            contract_epoch=self.contract_epoch,
            migration_set_id=self.migration_set_id,
            features=frozenset(),
            migration_safe=self.claimed_migration_safe,
            details={"mode": "test"},
        )

    def release(self, marker: SnapshotMarker) -> None:
        assert marker.generation == self.generation


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


def _seed(
    paths: AppPaths,
    *,
    migrate: bool = True,
    state_epoch: int = 1,
    state_set: str = "M001",
) -> str:
    paths.ensure_data_dirs()
    private, public = _keypair()
    paths.CONFIG_FILE.write_text("version: 2\n", encoding="utf-8")
    paths.SECRETS_FILE.write_bytes(b"ciphertext")
    paths.SECRETS_META_FILE.write_bytes(b"meta")
    paths.RSA_PRIVATE_KEY_FILE.write_text(private, encoding="ascii")
    paths.RSA_PUBLIC_KEY_FILE.write_text(public, encoding="ascii")
    with sqlite3.connect(paths.MAINTENANCE_DB_FILE) as conn:
        conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY)")
    memory = paths.MEMORY_DIR / "demo"
    memory.mkdir(parents=True)
    chat_db = memory / "chat.db"
    db = DebataDB(chat_db)
    db.load()
    db.close()
    with sqlite3.connect(chat_db) as conn:
        conn.execute(
            """
            INSERT INTO history_records(
                persona_id, history_index, role, content_hash,
                content_length, record_json, timestamp
            ) VALUES ('p1', 0, 'user', 'h', 11, ?, '2026-10-24T00:00:00Z')
            """,
            (json.dumps({"role": "user", "content": "secret-body"}),),
        )
        conn.commit()
    if migrate:
        migrate_database(chat_db)
    (paths.DATA_DIR / "compatibility_state.json").write_text(
        json.dumps(
            {
                "state_format": 1,
                "phase": "ready",
                "generation": 3,
                "data_contract_epoch": state_epoch,
                "migration_set_id": state_set,
                "features": [
                    "stable_seq",
                    "tombstone_read_barrier",
                    "deletion_ledger",
                    "import_ledger",
                ],
            }
        ),
        encoding="utf-8",
    )
    (paths.PERSONAS_DIR / "demo").mkdir(parents=True, exist_ok=True)
    (paths.PERSONAS_DIR / "demo" / "role.md").write_text(
        "# role\n", encoding="utf-8"
    )
    return private


def _service(tmp_path: Path, paths: AppPaths, private: str) -> BackupService:
    return BackupService(
        paths.PROJECT_ROOT,
        paths,
        backup_dir=tmp_path / "backups",
        secrets_manager=Secrets(private),
    )


async def _run_snapshot(service: BackupService, barrier: Barrier) -> dict:
    job = await service.start(
        backup_type=BACKUP_TYPE_MIGRATION_SNAPSHOT,
        barrier=barrier,
    )
    return await service.wait(job["id"], timeout=60)


def _rewrite_manifest(archive: Path, mutate) -> None:
    temp = archive.with_name(archive.name + ".rewrite")
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(temp, "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "manifest.json":
                manifest = json.loads(data)
                mutate(manifest)
                data = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
            dst.writestr(info, data)
    os.replace(temp, archive)


@pytest.mark.asyncio
async def test_strict_snapshot_is_ledger_aware_and_verified(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    private = _seed(paths)
    service = _service(tmp_path, paths, private)
    # 屏障自报 migration_safe=false；结论必须来自真实 ledger+契约证据。
    finished = await _run_snapshot(service, Barrier(migration_safe=False))
    assert finished["state"] == "done", finished
    capabilities = finished["capabilities"]
    assert capabilities["migration_safe"] is True
    assert capabilities["ledger_aware"] is True
    assert capabilities["ledger_complete"] is True
    assert capabilities["cross_plane_consistent"] is True
    snapshot = finished["snapshot"]
    assert snapshot["contract_match"] is True
    assert snapshot["deletion_ledger_present"] is True
    assert snapshot["migration_blocked_reason"] == ""

    archive = tmp_path / "backups" / finished["filename"]
    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["ledger"]["complete"] is True
        assert manifest["ledger"]["personas"]["p1"]["history_records"] == 1
        assert "secret-body" not in json.dumps(manifest, ensure_ascii=False)

    report = verify_backup(archive)
    assert report["ok"] is True, report["errors"]
    assert report["migration_ready"] is True
    assert report["ledger"]["complete"] is True
    assert report["capabilities"]["ledger_complete"] is True

    drill = drill_backup(archive)
    assert drill["ok"] is True
    assert drill["ledger_complete"] is True
    assert drill["migration_ready"] is True


@pytest.mark.asyncio
async def test_fake_ledger_file_cannot_make_snapshot_migration_safe(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    private = _seed(paths, migrate=False)
    (paths.MAINTENANCE_DIR / "deletion_ledger.jsonl").write_text(
        '{"id": 1}\n', encoding="utf-8"
    )
    service = _service(tmp_path, paths, private)
    finished = await _run_snapshot(service, Barrier(migration_safe=True))
    assert finished["state"] == "done", finished
    assert finished["capabilities"]["migration_safe"] is False
    assert finished["capabilities"]["ledger_aware"] is False
    assert finished["snapshot"]["ledger_artifact_present"] is True
    assert finished["snapshot"]["deletion_ledger_present"] is False
    assert (
        finished["snapshot"]["migration_blocked_reason"]
        == "semantic_deletion_ledger_required"
    )
    report = verify_backup(tmp_path / "backups" / finished["filename"])
    assert report["ok"] is True
    assert report["migration_ready"] is False


@pytest.mark.asyncio
async def test_contract_epoch_mismatch_keeps_migration_safe_false(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    private = _seed(paths, state_epoch=0)
    service = _service(tmp_path, paths, private)
    finished = await _run_snapshot(service, Barrier(migration_safe=True))
    assert finished["state"] == "done", finished
    assert finished["capabilities"]["migration_safe"] is False
    assert finished["capabilities"]["ledger_aware"] is True
    assert finished["capabilities"]["ledger_complete"] is True
    assert finished["snapshot"]["contract_match"] is False
    assert (
        finished["snapshot"]["migration_blocked_reason"]
        == "contract_epoch_or_migration_set_mismatch"
    )
    report = verify_backup(tmp_path / "backups" / finished["filename"])
    assert report["ok"] is True
    assert report["migration_ready"] is False


@pytest.mark.asyncio
async def test_tampered_manifest_ledger_is_rejected(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    private = _seed(paths)
    service = _service(tmp_path, paths, private)
    finished = await _run_snapshot(service, Barrier())
    archive = tmp_path / "backups" / finished["filename"]
    _rewrite_manifest(
        archive,
        lambda manifest: manifest["ledger"].update({"digest": "0" * 64}),
    )
    report = verify_backup(archive)
    assert report["ok"] is False
    assert any(
        item["code"] == "ledger_evidence_mismatch"
        for item in report["errors"]
    )
    with pytest.raises(RestoreError) as exc:
        drill_backup(archive)
    assert exc.value.code == "backup_verify_failed"


def test_post_d01_snapshot_replay_drill_is_bounded(tmp_path):
    """恢复演练只允许 exact/tombstone replay；新消息必须阻断（T14）。"""
    from core.d01 import (
        append_record,
        apply_tombstone_replay,
        plan_restore_replay,
        tombstone_record,
    )

    path = tmp_path / "debata.db"
    db = DebataDB(path)
    db.load()
    db.close()
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO history_records(
                persona_id, history_index, role, content_hash,
                content_length, record_json, timestamp
            ) VALUES ('p1', 0, 'user', 'h', 3, '{}', 't')
            """
        )
        conn.commit()
    migrate_database(path)
    snapshot = tmp_path / "snapshot.db"
    with sqlite3.connect(path) as src, sqlite3.connect(snapshot) as dst:
        src.backup(dst)
    with sqlite3.connect(path) as conn:
        uuid = conn.execute(
            "SELECT record_uuid FROM history_records LIMIT 1"
        ).fetchone()[0]
    tombstone_record(path, "p1", uuid)
    assert plan_restore_replay(snapshot, path)["decision"] == "replay_tombstones"
    assert apply_tombstone_replay(snapshot, path)["verified"] is True
    append_record(path, "p1", {"role": "user", "content": "new"})
    assert plan_restore_replay(snapshot, path)["decision"] == "blocked"
    with pytest.raises(D01Error) as exc:
        apply_tombstone_replay(snapshot, path)
    assert exc.value.code == "replay_blocked"
