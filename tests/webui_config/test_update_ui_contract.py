"""更新/Releases 前端契约。

前端没有构建步骤，也没有浏览器 DOM 测试依赖；这里只锁住那些“少了一行就会
静默失效”的结构：总览绿标、右下角版本位、版本日志折叠、退出时的不再提示、
镜像预设+自定义，以及更新 API 的超时选项。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "webui" / "static" / "js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_overview_has_green_update_orb_and_polls_cached_status():
    source = _read(JS / "pages" / "overview.js")
    assert "updateAvailable" in source
    assert "openUpdateDialog" in source
    assert "update-orb" in source
    assert "arrow-up" in source
    assert "UPDATE_REFRESH_MS" in source
    # 绿标必须只在有更新时渲染；无更新时清空。
    assert "info.available && info.release" in source
    assert "fill(updateHost)" in source


def test_version_mark_is_bottom_right_and_subtle():
    html = _read(ROOT / "webui" / "static" / "index.html")
    app_js = _read(JS / "app.js")
    css = _read(ROOT / "webui" / "static" / "app.css")
    assert 'id="version-mark"' in html
    assert "loadVersionMark" in app_js
    assert "开发版" in app_js
    assert ".version-mark" in css
    assert "var(--text-disabled)" in css
    assert "position: fixed" in css
    # 用户明确要求不要太显眼：浅灰 + 低不透明度，且不能拦截点击。
    assert "opacity: .75" in css
    assert "pointer-events: none" in css


def test_release_notes_can_collapse_and_expand():
    source = _read(JS / "releases.js")
    css = _read(ROOT / "webui" / "static" / "app.css")
    assert "release-notes" in source
    assert "collapsed" in source
    assert "展开" in source
    assert "收起" in source
    assert "release-notes.collapsed" in css
    # 旧 CSS 缓存不能阻止折叠功能本身：JS 同时写内联 max-height。
    assert "notes.style.maxHeight" in source
    assert "notes.style.overflow" in source
    # 每个版本卡片都要有日志和操作按钮。
    assert "notesBlock(release)" in source
    assert "更新到此版本" in source
    assert "回退到此版本" in source


def test_badge_exit_prompt_only_suppresses_when_checked_and_yes():
    source = _read(JS / "releases.js")
    assert "之后不再提示" in source
    assert "suppress_badge: true" in source
    assert "promptSuppress(ctx)" in source
    # 设置页内嵌列表不弹“之后不再提示”；只有总览弹窗的 onClose 调用。
    assert "onClose: () => {" in source


def test_settings_has_preset_and_custom_mirror_and_release_picker():
    source = _read(JS / "pages" / "settings.js")
    assert "mirror_presets" in source
    assert "预设镜像" in source
    assert "自定义镜像站" in source
    assert "update.mirror" in source
    assert "renderReleaseList" in source
    assert "showNotifyToggle: true" in source
    assert "检查更新" in source


def test_update_api_helpers_keep_long_git_timeout():
    source = _read(JS / "api.js")
    assert "post: (path, body, options)" in source
    assert "updateReleases" in source
    assert "updateAvailable" in source
    assert "updatePreferences" in source
    assert "timeoutMs: 330000" in source


def test_frontend_requests_target_tag_and_rollback_confirmation():
    source = _read(JS / "releases.js")
    assert "target_tag: release.tag" in source
    assert "confirm_rollback" in source
    assert "confirmation_token" in source
