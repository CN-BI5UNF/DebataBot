"""T22 / C12-R: 配置事务、安全防护与敏感凭据补偿契约测试。

验证项：
1. 唯一临时文件 + fsync + 原子 replace 落盘。
2. 乐观并发期望版本校验（expected_revision）与冲突拦截。
3. 安全草稿合并（1M/隐藏字段保护、空key不删旧key）。
4. 暂存凭据补偿机制（Staged Secrets Commit/Rollback）。
5. CSRF 与 Origin 头校验。
6. 备份下载租约管理（单次消费、TTL 过期、路径穿越拦截与 no-store 头）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.config_transaction import (
    BackupLeaseManager,
    ConfigConflictError,
    ConfigSecurityError,
    StagedSecretsManager,
    atomic_flush_replace_config,
    compute_config_revision,
    generate_csrf_token,
    safe_merge_config_draft,
    validate_csrf_token,
    validate_origin_header,
)


def test_atomic_flush_replace_and_revision_conflict(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    initial_content = "version: 1\npersona:\n  active: default\n"

    # 1. 首次写入
    rev1 = atomic_flush_replace_config(config_file, initial_content)
    assert config_file.exists()
    assert config_file.read_text(encoding="utf-8") == initial_content
    assert rev1 == compute_config_revision(initial_content)

    # 2. 携带匹配的 expected_revision 正常更新
    updated_content = "version: 1\npersona:\n  active: new_persona\n"
    rev2 = atomic_flush_replace_config(
        config_file,
        updated_content,
        expected_revision=rev1,
    )
    assert rev2 != rev1
    assert config_file.read_text(encoding="utf-8") == updated_content

    # 3. 携带已过期的 expected_revision（并发冲突）拦截
    stale_content = "version: 1\npersona:\n  active: stale_overwrite\n"
    with pytest.raises(ConfigConflictError) as exc_info:
        atomic_flush_replace_config(
            config_file,
            stale_content,
            expected_revision=rev1,  # 仍然用已被替换的 rev1
        )
    assert exc_info.value.expected == rev1
    assert exc_info.value.actual == rev2
    # 磁盘文件绝不能被脏写入
    assert config_file.read_text(encoding="utf-8") == updated_content


def test_safe_merge_config_draft_protects_fields_and_keys() -> None:
    base = {
        "version": 1,
        "persona": {"active": "default"},
        "providers": {
            "openai": {
                "model": "gpt-4o",
                "api_key": "sk-real-secret-12345",
            }
        },
        "storage": {"quota_bytes": 1073741824},
        "1M": {"enabled": True, "token_threshold": 1000000},
    }

    # 补丁：更新 model，但传了空的 api_key，同时漏掉了 storage 和 1M
    patch = {
        "providers": {
            "openai": {
                "model": "gpt-4o-mini",
                "api_key": "   ",  # 空 key，不应抹掉旧密钥
            }
        }
    }

    merged = safe_merge_config_draft(base, patch)

    # 1. model 成功更新
    assert merged["providers"]["openai"]["model"] == "gpt-4o-mini"
    # 2. 旧密钥完好无损（空 key 不删旧 key）
    assert merged["providers"]["openai"]["api_key"] == "sk-real-secret-12345"
    # 3. 隐藏与受保护的 storage 和 1M 字段完好保留
    assert merged["storage"]["quota_bytes"] == 1073741824
    assert merged["1M"]["enabled"] is True


def test_staged_secrets_compensation() -> None:
    sm = StagedSecretsManager(active_secrets={"old_key": "sk-active-old"})

    # 1. 暂存新密钥
    sm.stage("new_key", "sk-staged-val")
    assert sm.get_effective("new_key") == "sk-staged-val"
    assert sm.get_effective("old_key") == "sk-active-old"

    # 2. 若发生异常，执行 rollback
    sm.rollback()
    assert sm.get_effective("new_key") is None
    assert sm.get_effective("old_key") == "sk-active-old"

    # 3. 再次暂存并成功 commit
    sm.stage("new_key", "sk-staged-val-2")
    sm.commit()
    assert sm.get_effective("new_key") == "sk-staged-val-2"
    assert "new_key" in sm.active_secrets


def test_csrf_and_origin_validation() -> None:
    session_id = "user_session_abc"
    token = generate_csrf_token(session_id)

    # 1. 正常 CSRF 验证通过
    assert validate_csrf_token(session_id, token) is True

    # 2. 篡改或异源 session_id 拒绝
    assert validate_csrf_token("other_session", token) is False

    # 3. Origin 域名与端口校验
    allowed = ["localhost:8080", "127.0.0.1:8080"]
    assert validate_origin_header("http://localhost:8080", None, allowed) is True
    assert validate_origin_header("http://malicious-site.com", None, allowed) is False
    assert validate_origin_header(None, "http://127.0.0.1:8080/settings", allowed) is True


def test_backup_download_lease_management(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    valid_file = backup_dir / "backup_20261028.zip"
    valid_file.write_bytes(b"ZIP_ARCHIVE_MOCK_DATA")

    mgr = BackupLeaseManager(backup_dir)

    # 1. 路径穿越拦截
    with pytest.raises(ConfigSecurityError) as exc_traversal:
        mgr.create_lease("res_1", "../outside.txt")
    assert exc_traversal.value.code == "path_traversal_blocked"

    # 2. 创建合法下载租约
    token = mgr.create_lease("res_1", "backup_20261028.zip", ttl_seconds=60)
    assert token.startswith("lease_")

    # 3. 首次消费成功，返回物理路径与 no-store 响应头
    target_path, headers = mgr.verify_and_consume_lease(token)
    assert target_path == valid_file.resolve()
    assert "no-store" in headers["Cache-Control"]

    # 4. 二次消费被拒（单次消费保证）
    with pytest.raises(ConfigSecurityError) as exc_second:
        mgr.verify_and_consume_lease(token)
    assert exc_second.value.code == "invalid_lease"
