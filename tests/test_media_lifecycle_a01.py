"""T19: 媒体 48 小时保留期、多来源权益（Origin Equity）与入站租约测试。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from core.media_catalog import (
    DEFAULT_MEDIA_RETENTION_HOURS,
    MediaCatalogManager,
)
from memory.debata_db import DebataDB


@pytest.fixture
def test_db(tmp_path: Path) -> DebataDB:
    db_file = tmp_path / "debata_test.db"
    db = DebataDB(db_file)
    db.load()
    return db


def test_ingest_reservation_lease_and_protection(test_db: DebataDB) -> None:
    """测试入站预留租约的创建、保护与释放。"""
    mgr = MediaCatalogManager(test_db)
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    path = "incoming/images/v2/test_img.jpg"

    # 1. 创建租约
    res = mgr.create_reservation(sha, path, lease_seconds=100.0)
    assert res.reservation_id.startswith("res_")
    assert res.is_active

    # 2. 评定孤立项时，拥有活跃租约的项受到保护，不被当成 orphan 误删
    _ = mgr.evaluate_all()
    # 此时尚未 admit 到 catalog，但租约有效
    assert mgr.clean_expired_reservations(now=time.time()) == 0

    # 3. 模拟租约过期
    expired_count = mgr.clean_expired_reservations(now=time.time() + 200.0)
    assert expired_count == 1


def test_48h_retention_expiry_single_origin(test_db: DebataDB) -> None:
    """测试单来源入站媒体在 48 小时到达后正常过期。"""
    mgr = MediaCatalogManager(test_db)
    sha = "aaa111222333444555666777888999000aaa111222333444555666777888999000"
    path = "incoming/images/v2/single.jpg"

    now = time.time()
    # 登记媒体，保留期 48 小时
    rec = mgr.admit_media(
        persona_id="p1",
        origin_id="msg_1",
        sha256=sha,
        path=path,
        kind="image",
        size_bytes=1024,
        retention_hours=DEFAULT_MEDIA_RETENTION_HOURS,
    )
    assert rec.sha256 == sha

    # 47 小时后：仍有效保留
    is_retained, max_ret, is_pinned = mgr.get_effective_retention(rec.media_id, now=now + 47 * 3600)
    assert is_retained
    assert not is_pinned

    # 49 小时后：已过期
    is_retained, max_ret, is_pinned = mgr.get_effective_retention(rec.media_id, now=now + 49 * 3600)
    assert not is_retained

    evals = mgr.evaluate_all(now=now + 49 * 3600)
    assert len(evals) == 1
    assert evals[0].decision == "expire"


def test_origin_equity_multi_origin_max_retention(test_db: DebataDB) -> None:
    """测试多来源权益（E34）：共享同一底层 hash，物理文件取最长有效保留期。"""
    mgr = MediaCatalogManager(test_db)
    sha = "shared_hash_1234567890abcdef1234567890abcdef1234567890abcdef1234"
    path = "incoming/images/v2/shared.jpg"

    now = time.time()
    # Origin 1 (Msg 1) 在 T=0 时摄入
    rec1 = mgr.admit_media(
        persona_id="p1",
        origin_id="msg_1",
        sha256=sha,
        path=path,
        kind="image",
        size_bytes=2048,
        retention_hours=48.0,
        admitted_at=now,
    )

    # 模拟在 T=40 小时时，另一个会话/消息 Msg 2 也收到了相同的图片（同一 hash）
    # Origin 2 登记，具有新的 48h 权益（有效期至 T = 40 + 48 = 88 小时）
    rec2 = mgr.admit_media(
        persona_id="p2",
        origin_id="msg_2",
        sha256=sha,
        path=path,
        kind="image",
        size_bytes=2048,
        retention_hours=48.0,
        admitted_at=now + 40 * 3600,
    )
    assert rec1.media_id == rec2.media_id  # 共享同一底层 catalog 记录

    # 在 T = 50 小时时检查：
    # 此时 Origin 1 (T=0~48h) 已经过期，但 Origin 2 (T=40~88h) 依然有效！
    # 物理底层文件必须 RETAINED，绝不能因为 Origin 1 到期而误删共享文件！
    is_retained, max_ret, is_pinned = mgr.get_effective_retention(rec1.media_id, now=now + 50 * 3600)
    assert is_retained
    assert max_ret > now + 50 * 3600

    evals = mgr.evaluate_all(now=now + 50 * 3600)
    assert len(evals) == 1
    assert evals[0].decision == "retain"
    assert evals[0].active_origins_count == 1
    assert evals[0].expired_origins_count == 1

    # 在 T = 90 小时时检查：所有来源均已过期
    is_retained_all, _, _ = mgr.get_effective_retention(rec1.media_id, now=now + 90 * 3600)
    assert not is_retained_all


def test_origin_equity_expired_origin_never_revived(test_db: DebataDB) -> None:
    """测试已过期的旧来源绝不因新流入而复活（E34）。"""
    mgr = MediaCatalogManager(test_db)
    sha = "hash_no_resurrect_111111111111111111111111111111111111111111111"
    path = "incoming/images/v2/no_revive.jpg"

    now = time.time()
    # Msg 1 在 T=0 时摄入，48h 保留
    _ = mgr.admit_media(
        persona_id="p1",
        origin_id="msg_old",
        sha256=sha,
        path=path,
        retention_hours=48.0,
    )

    # 到了 T=60 小时，Msg 1 已经彻底过期
    # 此时第三方又发了相同图片 Msg 2，赋予新权益
    mgr.admit_media(
        persona_id="p1",
        origin_id="msg_new",
        sha256=sha,
        path=path,
        retention_hours=48.0,
    )

    # 核验底册：msg_old 的 retention_until 绝没有被悄悄后移（不复活）
    conn = test_db.connect()
    row_old = conn.execute(
        "SELECT retention_until FROM media_origins WHERE origin_id = 'msg_old'"
    ).fetchone()
    assert float(row_old[0]) <= now + 48.0 * 3600 + 1.0


def test_historical_reading_does_not_extend_retention(test_db: DebataDB) -> None:
    """测试历史查看/阅读绝不延长保留期（E34 契约）。"""
    mgr = MediaCatalogManager(test_db)
    sha = "hash_touch_read_222222222222222222222222222222222222222222222"
    path = "incoming/images/v2/touch.jpg"

    rec = mgr.admit_media(
        persona_id="p1",
        origin_id="msg_touch",
        sha256=sha,
        path=path,
        retention_hours=48.0,
    )

    conn = test_db.connect()
    before_ret = float(conn.execute(
        "SELECT retention_until FROM media_origins WHERE media_id = ?",
        (rec.media_id,),
    ).fetchone()[0])

    # 执行历史查看
    mgr.touch_read(rec.media_id)

    after_ret = float(conn.execute(
        "SELECT retention_until FROM media_origins WHERE media_id = ?",
        (rec.media_id,),
    ).fetchone()[0])

    assert before_ret == after_ret


def test_pinned_origin_permanently_protected(test_db: DebataDB) -> None:
    """测试显式 pin 的来源获得永久保护。"""
    mgr = MediaCatalogManager(test_db)
    sha = "hash_pinned_3333333333333333333333333333333333333333333333333"
    path = "incoming/images/v2/pinned.jpg"

    rec = mgr.admit_media(
        persona_id="p1",
        origin_id="msg_pinned",
        sha256=sha,
        path=path,
        retention_hours=1.0,
        pinned=True,
    )

    # 1000 小时后依然受保护
    is_retained, _, is_pinned = mgr.get_effective_retention(rec.media_id, now=time.time() + 1000 * 3600)
    assert is_retained
    assert is_pinned

    # 解除 pin 后，超过保留期即可过期
    mgr.set_pin("p1", "msg_pinned", rec.media_id, pinned=False)
    is_retained_after, _, is_pinned_after = mgr.get_effective_retention(
        rec.media_id, now=time.time() + 1000 * 3600
    )
    assert not is_retained_after
    assert not is_pinned_after
