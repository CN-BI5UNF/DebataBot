"""存储 API 的响应脱敏与错误映射。"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from core.storage_maintenance import StorageMaintenanceError
from webui.api.storage import _http_error, _public_job


def test_public_job_hides_internal_items_and_token_hash():
    job = {
        "job_id": "j1",
        "preview_id": "p1",
        "state": "running",
        "categories": ["temp_files"],
        "created_at": 1700000000,
        "started_at": None,
        "finished_at": None,
        "result": {"bytes_freed": 10},
        "error_code": None,
        "error_message": None,
        "items": [{"relative_path": "tmp/secret-name.bin"}],
        "token_hash": "deadbeef",
    }

    public = _public_job(job)

    assert "items" not in public
    assert "token_hash" not in public
    assert "secret-name" not in str(public)
    assert public["result"]["bytes_freed"] == 10


def test_error_mapping_uses_machine_codes():
    assert _http_error(StorageMaintenanceError("preview_expired", "expired")).status_code == 410
    assert _http_error(StorageMaintenanceError("generation_conflict", "conflict")).status_code == 409
    assert _http_error(StorageMaintenanceError("unknown_category", "unknown")).status_code == 422
    assert _http_error(StorageMaintenanceError("preview_not_found", "missing")).status_code == 404

    error = _http_error(StorageMaintenanceError("confirmation_token_invalid", "bad"))
    assert isinstance(error, HTTPException)
    assert error.detail["code"] == "confirmation_token_invalid"


def test_storage_cleanup_button_is_labelled_clear():
    root = Path(__file__).resolve().parents[2]
    js = (root / "webui/static/js/pages/storage.js").read_text(encoding="utf-8")
    assert "}, '清除');" in js
    assert "预览影响" not in js
