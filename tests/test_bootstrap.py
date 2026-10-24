"""B02-E0：epoch0 bootstrap 快照、drill、一次性窄 permit。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import zipfile

import pytest

from app_config.paths import AppPaths
from core.backup import BackupService
from core.bootstrap import (
    BootstrapError,
    bootstrap_status,
    consume_permit,
    create_bootstrap,
    read_permit,
    validate_epoch0_eligibility,
)
from core.maintenance import MaintenanceController


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
    memory = paths.MEMORY_DIR / "one"
    memory.mkdir(parents=True)
    with sqlite3.connect(memory / "chat.db") as conn:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO messages(body) VALUES ('bootstrap')")
        conn.commit()
    with sqlite3.connect(paths.MAINTENANCE_DB_FILE) as conn:
        conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, kind TEXT)")
        conn.commit()
    (paths.MAINTENANCE_DIR / "catalog.json").write_text(
        '{"catalog": "synthetic"}\n', encoding="utf-8"
    )
    (paths.PERSONAS_DIR / "one").mkdir(parents=True, exist_ok=True)
    (paths.PERSONAS_DIR / "one" / "role.md").write_text("# role\n", encoding="utf-8")
    return private


def _service(paths: AppPaths, private: str, tmp_path):
    return BackupService(
        paths.PROJECT_ROOT,
        paths,
        backup_dir=tmp_path / "backups",
        secrets_manager=Secrets(private),
    )


@pytest.mark.asyncio
async def test_bootstrap_creates_verified_permit_and_consumes_once(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    private = _seed(paths)
    service = _service(paths, private, tmp_path)
    controller = MaintenanceController(paths)
    target_build = "a" * 40
    target_manifest = "b" * 64
    result = await create_bootstrap(
        paths=paths,
        backup_service=service,
        controller=controller,
        target_build=target_build,
        target_manifest_hash=target_manifest,
        allowed_migration_ids=["M001"],
        expiry_seconds=3600,
    )

    assert result["ok"] is True
    assert result["bootstrap_restore_verified"] is True
    assert result["migration_safe"] is False
    permit = read_permit(paths)
    assert permit is not None
    assert permit["status"] == "unconsumed"
    assert permit["target_build"] == target_build
    assert permit["bootstrap_restore_verified"] is True
    assert permit["migration_safe"] is False
    assert "M001" in permit["allowed_migration_ids"]
    archive = tmp_path / "backups" / permit["snapshot_filename"]
    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["snapshot"]["kind"] == "bootstrap_epoch0"
    assert manifest["capabilities"]["migration_safe"] is False
    assert result["drill"]["ok"] is True

    status = bootstrap_status(paths)
    assert status["permit"]["permit_id"] == permit["permit_id"]
    consumed = consume_permit(
        paths,
        permit_id=permit["permit_id"],
        migration_id="M001",
        target_build=target_build,
        inventory_hash=permit["inventory_hash"],
    )
    assert consumed["status"] == "consumed"
    with pytest.raises(BootstrapError) as exc:
        consume_permit(
            paths,
            permit_id=permit["permit_id"],
            migration_id="M001",
            target_build=target_build,
            inventory_hash=permit["inventory_hash"],
        )
    assert exc.value.code == "permit_consumed"


@pytest.mark.asyncio
async def test_bootstrap_permit_rejects_wrong_scope_target_and_expiry(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    private = _seed(paths)
    service = _service(paths, private, tmp_path)
    result = await create_bootstrap(
        paths=paths,
        backup_service=service,
        controller=MaintenanceController(paths),
        target_build="c" * 40,
        target_manifest_hash="d" * 64,
        allowed_migration_ids=["M001"],
        expiry_seconds=3600,
    )
    permit = result["permit"]
    cases = [
        ({"migration_id": "M002"}, "migration_not_allowed"),
        ({"target_build": "e" * 40}, "target_build_mismatch"),
        ({"inventory_hash": "0" * 64}, "inventory_changed"),
        ({"now": permit["expires_at"] + 1}, "permit_expired"),
    ]
    for changes, expected in cases:
        args = {
            "permit_id": permit["permit_id"],
            "migration_id": "M001",
            "target_build": permit["target_build"],
            "inventory_hash": permit["inventory_hash"],
        }
        args.update(changes)
        with pytest.raises(BootstrapError) as exc:
            consume_permit(paths, **args)
        assert exc.value.code == expected


def test_epoch0_eligibility_rejects_lifecycle_features(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    _seed(paths)
    (paths.DATA_DIR / "compatibility_state.json").write_text(
        json.dumps({
            "state_format": 1,
            "data_contract_epoch": 0,
            "features": ["tombstone_read_barrier"],
        }),
        encoding="utf-8",
    )
    with pytest.raises(BootstrapError) as exc:
        validate_epoch0_eligibility(paths)
    assert exc.value.code == "source_has_lifecycle"


def test_bootstrap_requires_explicit_migration_ids(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    private = _seed(paths)
    service = _service(paths, private, tmp_path)
    with pytest.raises(BootstrapError) as exc:
        asyncio.run(
            create_bootstrap(
                paths=paths,
                backup_service=service,
                controller=MaintenanceController(paths),
                target_build="a" * 40,
                target_manifest_hash="b" * 64,
                allowed_migration_ids=[],
            )
        )
    assert exc.value.code == "migration_ids_invalid"
