"""一键备份前端契约。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "webui" / "static" / "js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_settings_has_backup_tab_and_button():
    settings = _read(JS / "pages" / "settings.js")
    assert "appendBackupPanel" in settings
    assert "立即备份" in settings
    assert "一键备份用户数据" in settings
    assert "备份包含加密 API 密钥库和 RSA 私钥" in settings
    assert "refreshStatus" in settings
    assert "backup-progress" in settings


def test_backup_api_helpers_and_download_link():
    api = _read(JS / "api.js")
    assert "backupStatus" in api
    assert "backupStart" in api
    assert "backupJob" in api
    assert "backupDownloadUrl" in api
    settings = _read(JS / "pages" / "settings.js")
    assert "endpoints.backupDownloadUrl" in settings
    assert "README_RESTORE.txt" in settings


def test_backup_panel_shows_progress_warnings_and_history():
    settings = _read(JS / "pages" / "settings.js")
    assert "BACKUP_STATE_TEXT" in settings
    assert "BACKUP_STAGE_TEXT" in settings
    assert "total_bytes" in settings
    assert "processed_bytes" in settings
    assert "warnings" in settings
    assert "已有备份" in settings
    assert "含密钥材料" in settings
    assert "密钥材料可验证" in settings
    assert "未完整验证" in settings
    assert "部分完成" in settings
    assert "ZIP 本身未加密" in settings
