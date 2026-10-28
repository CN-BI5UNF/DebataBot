"""用户数据备份：范围、SQLite 一致性、排除项、密钥导出、空间检查、下载安全。"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import zipfile
from pathlib import Path

import pytest

from app_config.paths import AppPaths
from core.backup import (
    BackupError,
    BackupService,
    resolve_backup_directory,
)


class FakeSecrets:
    def __init__(self, pem: str | None = "KEYRING-PEM") -> None:
        self.pem = pem

    def export_rsa_private_key(self) -> str | None:
        if self.pem is None:
            raise RuntimeError("keyring unavailable")
        return self.pem


def _seed(paths: AppPaths) -> None:
    paths.ensure_data_dirs()
    paths.PERSONAS_DIR.mkdir(parents=True, exist_ok=True)
    paths.USER_PROVIDER_PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    paths.CONFIG_FILE.write_text("version: 2\n", encoding="utf-8")
    paths.SECRETS_FILE.write_bytes(b"encrypted-secrets")
    paths.SECRETS_META_FILE.write_bytes(b"encrypted-meta")
    paths.RSA_PRIVATE_KEY_FILE.write_text("PRIVATE-PEM", encoding="utf-8")
    paths.RSA_PUBLIC_KEY_FILE.write_text("PUBLIC-PEM", encoding="utf-8")
    paths.TOKEN_CALIBRATION_FILE.write_text('{"factor": 1.0}\n', encoding="utf-8")
    (paths.DATA_DIR / "persona_brief.json").write_text('{"name": "x"}\n', encoding="utf-8")
    (paths.DATA_DIR / "knowledge.json").write_text("[]\n", encoding="utf-8")
    (paths.DATA_DIR / "knowledge_state.json").write_text("{}\n", encoding="utf-8")

    memory_dir = paths.MEMORY_DIR / "test"
    memory_dir.mkdir(parents=True)
    chat_db = memory_dir / "chat.db"
    with sqlite3.connect(chat_db) as conn:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO messages(body) VALUES ('hello-backup')")
        conn.commit()

    vector_dir = paths.VECTOR_DIR / "test"
    vector_dir.mkdir(parents=True)
    with sqlite3.connect(vector_dir / "rag.sqlite3") as conn:
        conn.execute("CREATE TABLE vectors (id INTEGER PRIMARY KEY, text TEXT)")
        conn.execute("INSERT INTO vectors(text) VALUES ('vector-data')")
        conn.commit()

    incoming = paths.WORKSPACE_DIR / "incoming"
    incoming.mkdir(parents=True)
    (incoming / "image.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    (paths.WORKSPACE_DIR / "notes.txt").write_text("workspace-note\n", encoding="utf-8")
    (paths.EMOJI_DIR / "emoji.txt").write_text("emoji\n", encoding="utf-8")
    (paths.USER_PROVIDER_PRESETS_DIR / "custom.yaml").write_text(
        "id: custom\n", encoding="utf-8"
    )
    persona_dir = paths.PERSONAS_DIR / "user"
    persona_dir.mkdir(parents=True)
    (persona_dir / "role.md").write_text("# role\n", encoding="utf-8")

    # 不应进入备份的目录/文件
    paths.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    (paths.LOGS_DIR / "karen.log").write_text("log\n", encoding="utf-8")
    (paths.CACHE_DIR / "cache.bin").write_bytes(b"cache")
    (paths.TMP_DIR / "tmp.bin").write_bytes(b"tmp")
    (paths.DATA_DIR / "config_backups").mkdir(exist_ok=True)
    (paths.DATA_DIR / "config_backups" / "old.yaml").write_text("old\n", encoding="utf-8")
    (paths.DATA_DIR / "webui_token.txt").write_text("secret-token\n", encoding="utf-8")
    (paths.DATA_DIR / "update_state.json").write_text("{}", encoding="utf-8")
    paths.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    (paths.MODELS_DIR / "model.bin").write_bytes(b"model")


def _real_keypair() -> tuple[str, str]:
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


def _service(tmp_path: Path, paths: AppPaths, *, include_media: bool = True, secrets=None):
    return BackupService(
        paths.PROJECT_ROOT,
        paths,
        backup_dir=tmp_path / "backups",
        include_media=include_media,
        secrets_manager=secrets,
    )


@pytest.mark.asyncio
async def test_backup_contents_manifest_and_permissions(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    _seed(paths)
    service = _service(tmp_path, paths, secrets=FakeSecrets())
    job = await service.create_backup()

    assert job["state"] == "partial", job
    archive = tmp_path / "backups" / job["filename"]
    assert archive.is_file()
    sidecar = archive.with_name(archive.name + ".json")
    if os.name != "nt":
        # Windows 的 chmod 只表达只读位，不能断言完整 POSIX mode。
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600
        assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        required = {
            "README_RESTORE.txt",
            "manifest.json",
            "data/config.yaml",
            "data/secrets.enc",
            "data/secrets.meta",
            "data/rsa_public.pem",
            "data/rsa_private.pem",
            "data/token_calibration.json",
            "data/persona_brief.json",
            "data/knowledge.json",
            "data/knowledge_state.json",
            "data/memory/test/chat.db",
            "data/vector/test/rag.sqlite3",
            "data/workspace/incoming/image.jpg",
            "data/workspace/notes.txt",
            "data/emoji/emoji.txt",
            "data/providers/presets/custom.yaml",
            "personas/user/role.md",
        }
        assert required <= names
        assert zf.read("data/rsa_private.pem") == b"KEYRING-PEM"
        assert not any(
            name.startswith(("data/logs/", "data/cache/", "data/tmp/",
                              "data/config_backups/", "data/models/"))
            for name in names
        )
        assert "data/webui_token.txt" not in names
        assert "data/update_state.json" not in names

        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["includes_secrets"] is True
        assert manifest["includes_keys"] is True
        assert manifest["rsa_private_key_source"] == "keyring"
        assert manifest["include_media"] is True
        assert manifest["backup_type"] == "user_export"
        assert manifest["format_version"] == 2
        assert manifest["validation_status"] == "partial"
        assert manifest["credential_material_present"] is True
        assert manifest["credential_restorable"] is False
        assert manifest["capabilities"]["migration_safe"] is False
        paths_in_manifest = {item["path"] for item in manifest["files"]}
        assert "data/memory/test/chat.db" in paths_in_manifest
        assert "data/logs/karen.log" not in paths_in_manifest
        assert "KEYRING-PEM" not in json.dumps(manifest, ensure_ascii=False)
        assert "encrypted-secrets" not in json.dumps(manifest, ensure_ascii=False)

        extracted = tmp_path / "extract"
        zf.extract("data/memory/test/chat.db", extracted)
    with sqlite3.connect(extracted / "data" / "memory" / "test" / "chat.db") as conn:
        row = conn.execute("SELECT body FROM messages").fetchone()
    assert row == ("hello-backup",)

    files = service.list_files()
    assert [item["filename"] for item in files] == [job["filename"]]
    assert files[0]["includes_keys"] is True
    assert service.resolve_download(job["filename"]).is_file()
    for bad in ("../x.zip", "x.zip", "Karen_Backup_x.txt", "Karen_Backup_x/../y.zip"):
        with pytest.raises(BackupError):
            service.resolve_download(bad)


@pytest.mark.asyncio
async def test_backup_can_exclude_inbound_media(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    _seed(paths)
    service = _service(tmp_path, paths, include_media=True, secrets=FakeSecrets())
    job = await service.create_backup(include_media=False)
    assert job["state"] == "partial", job
    with zipfile.ZipFile(tmp_path / "backups" / job["filename"]) as zf:
        names = set(zf.namelist())
        assert "data/workspace/incoming/image.jpg" not in names
        assert "data/workspace/notes.txt" in names
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["include_media"] is False


@pytest.mark.asyncio
async def test_backup_uses_private_key_file_when_keyring_fails(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    _seed(paths)
    service = _service(tmp_path, paths, secrets=FakeSecrets(None))
    job = await service.create_backup()
    assert job["state"] == "partial", job
    with zipfile.ZipFile(tmp_path / "backups" / job["filename"]) as zf:
        assert zf.read("data/rsa_private.pem") == b"PRIVATE-PEM"
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["rsa_private_key_source"] == "file"


@pytest.mark.asyncio
async def test_backup_insufficient_space_is_reported(tmp_path, monkeypatch):
    paths = AppPaths(project_root=tmp_path)
    _seed(paths)
    service = _service(tmp_path, paths, secrets=FakeSecrets())
    monkeypatch.setattr(
        "core.backup.shutil.disk_usage",
        lambda _path: type("Usage", (), {"free": 0})(),
    )
    job = await service.create_backup()
    assert job["state"] == "failed"
    assert job["error"]["code"] == "insufficient_space"
    assert not list((tmp_path / "backups").glob("Karen_Backup_*.zip"))


def test_backup_directory_is_file_is_rejected(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    _seed(paths)
    bad_dir = tmp_path / "backup-file"
    bad_dir.write_text("not a directory", encoding="utf-8")
    service = BackupService(
        paths.PROJECT_ROOT,
        paths,
        backup_dir=bad_dir,
        secrets_manager=FakeSecrets(),
    )

    async def run():
        with pytest.raises(BackupError) as exc:
            await service.start()
        assert exc.value.code == "invalid_directory"

    import asyncio

    asyncio.run(run())


def test_resolve_backup_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert resolve_backup_directory("") == tmp_path / "Karen_Backups"
    assert resolve_backup_directory("my-backups") == tmp_path / "my-backups"
    assert resolve_backup_directory(str(tmp_path / "abs")) == tmp_path / "abs"


def test_backup_manager_cache_and_http_error_mapping(tmp_path):
    from types import SimpleNamespace

    from webui.api import backup as backup_api

    paths = AppPaths(project_root=tmp_path)
    _seed(paths)
    config = SimpleNamespace(
        backup=SimpleNamespace(
            directory=str(tmp_path / "backups"),
            include_media=True,
        )
    )
    rt = SimpleNamespace(
        config=config,
        paths=paths,
        secrets=FakeSecrets(),
        project_root=tmp_path,
    )
    host = SimpleNamespace(rt=rt, project_root=tmp_path)
    manager = backup_api.build_manager(host)
    assert manager is backup_api.build_manager(host)
    assert manager.directory == (tmp_path / "backups")

    config.backup.directory = str(tmp_path / "backups2")
    changed = backup_api.build_manager(host)
    assert changed is not manager
    assert changed.directory == (tmp_path / "backups2")

    assert backup_api._http_error(BackupError("not_found", "x")).status_code == 404
    assert backup_api._http_error(BackupError("invalid_name", "x")).status_code == 400
    assert backup_api._http_error(BackupError("backup_running", "x")).status_code == 409
    assert backup_api._http_error(BackupError("insufficient_space", "x")).status_code == 409


@pytest.mark.asyncio
async def test_backup_routes_start_status_job_and_download(tmp_path):
    from types import SimpleNamespace

    from webui.api import backup as backup_api

    paths = AppPaths(project_root=tmp_path)
    _seed(paths)
    config = SimpleNamespace(
        backup=SimpleNamespace(
            directory=str(tmp_path / "backups"),
            include_media=True,
        )
    )
    rt = SimpleNamespace(
        config=config,
        paths=paths,
        secrets=FakeSecrets(),
        project_root=tmp_path,
    )
    host = SimpleNamespace(
        rt=rt,
        project_root=tmp_path,
        state="running",
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(host=host))
    )

    status = await backup_api.status(request)
    assert status["files"] == []
    started = await backup_api.start(request, None)
    manager = backup_api.build_manager(host)
    finished = await manager.wait(started["id"], timeout=30)
    assert finished["state"] in {"done", "partial"}, finished

    status = await backup_api.status(request)
    assert status["files"][0]["filename"] == finished["filename"]
    job = await backup_api.job(request, finished["id"])
    assert job["state"] in {"done", "partial"}
    response = await backup_api.download(request, finished["filename"])
    assert Path(response.path) == manager.resolve_download(finished["filename"])
    assert response.headers["content-disposition"].startswith("attachment;")


def test_backup_directory_parent_of_data_is_rejected(tmp_path):
    import asyncio

    paths = AppPaths(project_root=tmp_path)
    _seed(paths)
    # tmp_path 是 data/ 和 personas/ 的父目录
    service = BackupService(
        paths.PROJECT_ROOT,
        paths,
        backup_dir=tmp_path,
        secrets_manager=FakeSecrets(),
    )

    async def run():
        with pytest.raises(BackupError) as exc:
            await service.start()
        assert exc.value.code == "invalid_directory"

    asyncio.run(run())


@pytest.mark.asyncio
async def test_backup_directory_inside_data_is_excluded(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    _seed(paths)
    backup_dir = paths.DATA_DIR / "backups"
    service = BackupService(
        paths.PROJECT_ROOT,
        paths,
        backup_dir=backup_dir,
        secrets_manager=FakeSecrets(),
    )
    job = await service.create_backup()
    assert job["state"] == "partial", job
    archive = backup_dir / job["filename"]
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        assert "data/config.yaml" in names
        assert not any(name.startswith("data/backups/") for name in names)


@pytest.mark.asyncio
async def test_real_keypair_marks_credential_restorable(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    _seed(paths)
    private, public = _real_keypair()
    paths.RSA_PUBLIC_KEY_FILE.write_text(public, encoding="ascii")
    service = _service(tmp_path, paths, secrets=FakeSecrets(private))
    job = await service.create_backup()
    assert job["state"] == "done", job
    assert job["credential_restorable"] is True
    assert job["credential_material_present"] is True
    with zipfile.ZipFile(tmp_path / "backups" / job["filename"]) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["validation_status"] == "verified"
    assert manifest["credential_restorable"] is True
    assert manifest["credential_verification"] == "key_pair_matched"
    assert manifest["capabilities"]["credential_restorable"] is True


@pytest.mark.asyncio
async def test_list_rebuilds_from_manifest_when_sidecar_missing(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    _seed(paths)
    service = _service(tmp_path, paths, secrets=FakeSecrets())
    job = await service.create_backup()
    archive = tmp_path / "backups" / job["filename"]
    sidecar = archive.with_name(archive.name + ".json")
    sidecar.unlink()
    files = service.list_files()
    assert files[0]["filename"] == job["filename"]
    assert isinstance(files[0]["created_at"], float)
    assert files[0]["backup_type"] == "user_export"
    assert files[0]["validation_status"] == "partial"

    # 旧 sidecar 只写 ISO created_at 也必须能排序，不出现 str/float TypeError。
    sidecar.write_text(
        json.dumps({
            "filename": job["filename"],
            "size": archive.stat().st_size,
            "created_at": "2026-10-24T00:00:00+08:00",
        }),
        encoding="utf-8",
    )
    files = service.list_files()
    assert isinstance(files[0]["created_at"], float)


def test_verify_archive_rejects_member_hash_mismatch(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    _seed(paths)
    service = _service(tmp_path, paths, secrets=FakeSecrets())
    archive_path = tmp_path / "bad.zip"
    manifest = {
        "format": 2,
        "backup_type": "user_export",
        "files": [{
            "path": "x.bin",
            "size": 3,
            "sha256": "0" * 64,
            "kind": "file",
            "consistent": True,
        }],
    }
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("README_RESTORE.txt", "readme")
        zf.writestr("x.bin", b"abc")
        zf.writestr("manifest.json", json.dumps(manifest))
    with pytest.raises(BackupError) as exc:
        service._verify_archive(archive_path)
    assert exc.value.code == "backup_verify_failed"


@pytest.mark.asyncio
async def test_cancel_stops_before_publish_and_cleans_temp(tmp_path, monkeypatch):
    import asyncio
    import threading

    paths = AppPaths(project_root=tmp_path)
    _seed(paths)
    service = _service(tmp_path, paths, secrets=FakeSecrets())
    entered = threading.Event()
    release = threading.Event()
    original = service._add_file

    def slow_add_file(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_add_file", slow_add_file)
    job = await service.start()
    assert await asyncio.to_thread(entered.wait, 10)
    cancelling = await service.cancel(job["id"])
    assert cancelling["state"] == "cancelling"
    release.set()
    finished = await service.wait(job["id"], timeout=20)

    assert finished["state"] == "cancelled", finished
    assert finished["error"]["code"] == "backup_cancelled"
    assert not list((tmp_path / "backups").glob("Karen_Backup_*.zip"))
    assert not list((tmp_path / "backups").glob(".backup-work-*"))
    persisted = json.loads(
        (paths.DATA_DIR / "backup_jobs.json").read_text(encoding="utf-8")
    )
    persisted_job = next(item for item in persisted["jobs"] if item["id"] == job["id"])
    assert persisted_job["state"] == "cancelled"


def test_coordinator_marks_inflight_job_interrupted_on_reload(tmp_path):
    paths = AppPaths(project_root=tmp_path)
    _seed(paths)
    jobs_file = paths.DATA_DIR / "backup_jobs.json"
    jobs_file.write_text(json.dumps({
        "schema": 1,
        "jobs": [{
            "id": "stale-1",
            "state": "running",
            "stage": "writing",
            "started_at": 1,
            "warnings": [],
        }],
    }), encoding="utf-8")
    service = _service(tmp_path, paths, secrets=FakeSecrets())
    job = service.get_job("stale-1")
    assert job["state"] == "interrupted"
    assert job["retryable"] is True
    assert job["error"]["code"] == "backup_interrupted"

    # 另一个进程持锁时，新协调器不能把它的 running 覆盖成 interrupted。
    from core.backup import BackupCoordinator

    coord1 = BackupCoordinator(paths.PROJECT_ROOT, paths)
    coord1.acquire_instance_lock()
    try:
        jobs_file.write_text(json.dumps({
            "schema": 1,
            "jobs": [{
                "id": "live-other-process",
                "state": "running",
                "stage": "writing",
                "started_at": 2,
                "warnings": [],
            }],
        }), encoding="utf-8")
        coord2 = BackupCoordinator(paths.PROJECT_ROOT, paths)
        on_disk = json.loads(jobs_file.read_text(encoding="utf-8"))
        assert on_disk["jobs"][0]["state"] == "running"
        with coord2.state_lock:
            assert coord2.jobs["live-other-process"]["state"] == "running"
    finally:
        coord1.release_instance_lock()


@pytest.mark.asyncio
async def test_external_task_cancel_keeps_lock_until_worker_stops(tmp_path):
    import asyncio
    import threading

    paths = AppPaths(project_root=tmp_path)
    _seed(paths)
    service = _service(tmp_path, paths, secrets=FakeSecrets())
    entered = threading.Event()
    release = threading.Event()
    original = service._add_file

    def slow_add_file(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    service._add_file = slow_add_file
    job = await service.start()
    assert await asyncio.to_thread(entered.wait, 10)
    run_task = next(
        task for task in service._shared.tasks
        if task.get_name() == f"backup-{job['id']}"
    )
    run_task.cancel()
    await asyncio.sleep(0.05)

    # worker 未退出时，barrier/flock 和 active job 都不能被释放。
    assert service._shared.locked
    assert service._shared.active_job == job["id"]
    with pytest.raises(BackupError) as exc:
        await service.start()
    assert exc.value.code == "backup_running"

    release.set()
    deadline = asyncio.get_running_loop().time() + 10
    while (
        service._shared.active_job is not None
        or service._shared.locked
    ) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    assert service._shared.active_job is None
    assert service._shared.locked is False
    finished = service.get_job(job["id"])
    assert finished["state"] == "cancelled", finished
    assert not list((tmp_path / "backups").glob("Karen_Backup_*.zip"))


@pytest.mark.asyncio
async def test_managers_share_instance_mutex_across_config_changes(tmp_path):
    import asyncio
    import threading
    from types import SimpleNamespace

    from webui.api import backup as backup_api

    paths = AppPaths(project_root=tmp_path)
    _seed(paths)
    config = SimpleNamespace(
        backup=SimpleNamespace(
            directory=str(tmp_path / "backups"),
            include_media=True,
        )
    )
    rt = SimpleNamespace(
        config=config,
        paths=paths,
        secrets=FakeSecrets(),
        project_root=tmp_path,
    )
    host = SimpleNamespace(rt=rt, project_root=tmp_path)
    first = backup_api.build_manager(host)
    entered = threading.Event()
    release = threading.Event()
    original = first._add_file

    def slow_add_file(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    first._add_file = slow_add_file
    job = await first.start()
    assert await asyncio.to_thread(entered.wait, 10)

    config.backup.directory = str(tmp_path / "backups2")
    second = backup_api.build_manager(host)
    assert second is not first
    assert second._shared is first._shared
    with pytest.raises(BackupError) as exc:
        await second.start()
    assert exc.value.code == "backup_running"
    await second.cancel(job["id"])
    release.set()
    finished = await first.wait(job["id"], timeout=30)
    assert finished["state"] == "cancelled"
