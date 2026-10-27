"""黑话管理页的静态契约。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "webui/static/js"


def test_slang_nav_and_page_contract():
    app = (JS / "app.js").read_text(encoding="utf-8")
    page = (JS / "pages/slang.js").read_text(encoding="utf-8")
    api = (JS / "api.js").read_text(encoding="utf-8")
    assert "title: '黑话'" in app
    assert "pages/slang.js" in app
    assert "群聊黑话" in page
    assert "从当前缓存学习" in page
    assert "保存并确认" in page
    assert "只有已确认词条能被模型按需查询" in page
    assert "查看依据" in page
    assert "slangList" in api and "slangLearn" in api
    assert "api.del(`/api/slang/" in api


def test_slang_settings_are_exposed():
    from webui.settings_spec import field_map

    fields = field_map()
    for path in (
        "features.slang.enabled",
        "features.slang.auto_learn",
        "features.slang.extract_min_messages",
        "features.slang.extract_cooldown_seconds",
        "features.slang.inject_max",
        "features.slang.max_prompt_chars",
    ):
        assert path in fields
        assert fields[path].get("help")
