"""知识页与设置深链的静态契约。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "webui/static/js"


def test_knowledge_nav_page_and_settings_link():
    app = (JS / "app.js").read_text(encoding="utf-8")
    page = (JS / "pages/knowledge.js").read_text(encoding="utf-8")
    api = (JS / "api.js").read_text(encoding="utf-8")
    assert "title: '知识'" in app
    assert "pages/knowledge.js" in app
    assert "知识 / 热梗" in page
    assert "联网考究" in page
    assert "获取当前热梗" in page
    assert "共享给其他人格只读引用" in page
    assert "entry.shared" in page
    assert "ctx.navigate('settings', { section: 'features' })" in page
    assert "knowledgeList" in api and "knowledgeResearch" in api
    assert "/api/knowledge/trends" in api


def test_settings_deep_link_section_is_consumed():
    app = (JS / "app.js").read_text(encoding="utf-8")
    settings = (JS / "pages/settings.js").read_text(encoding="utf-8")
    slang = (JS / "pages/slang.js").read_text(encoding="utf-8")
    assert "pendingSettingsSection" in app
    assert "section: requestedSection" in app
    # location.hash 赋值会异步触发 hashchange；必须挡住自身重入，
    # 否则设置页会先按深链分页渲染，再被一次无 section 的重入覆盖回默认页。
    assert "state.navigating === key" in app
    assert "navigationSeq" in app
    assert "ctx.section" in settings
    assert "activeKey = ctx.section" in settings
    assert "await show(activeKey)" in settings
    assert "ctx.navigate('settings', { section: 'features' })" in slang


def test_knowledge_settings_are_exposed():
    from webui.settings_spec import field_map

    fields = field_map()
    for path in (
        "features.knowledge.enabled",
        "features.knowledge.inject_max",
        "features.knowledge.max_prompt_chars",
        "features.knowledge.auto_research_enabled",
        "features.knowledge.research_interval_hours",
        "features.knowledge.research_search_provider",
        "features.knowledge.research_max_notes",
        "features.knowledge.research_queries",
    ):
        assert path in fields
        assert fields[path].get("help")


def test_research_queries_are_editable_as_multiline_list():
    from webui.settings_spec import field_map

    spec = field_map()["features.knowledge.research_queries"]
    assert spec["type"] == "list_str"
    assert any("{year}" in query for query in spec["default"])
    settings = (JS / "pages/settings.js").read_text(encoding="utf-8")
    assert "kind === 'list_str'" in settings
    assert "h('textarea'" in settings


def test_knowledge_dedupe_route_is_registered():
    from webui.api import knowledge as knowledge_api

    paths = {route.path for route in knowledge_api.router.routes}
    assert "/api/knowledge/dedupe" in paths


def test_stale_page_and_tab_renders_cannot_overwrite_new_content():
    app = (JS / "app.js").read_text(encoding="utf-8")
    settings = (JS / "pages/settings.js").read_text(encoding="utf-8")
    knowledge = (JS / "pages/knowledge.js").read_text(encoding="utf-8")
    assert "previousContainer.cloneNode(false)" in app
    assert "result.destroy()" in app
    assert "bodyHost.replaceChildren(panelBody)" in settings
    assert "fill(panelBody, ...nodes)" in settings
    # Unix 秒不能被当作毫秒显示成 1970 年。
    assert "new Date(value * 1000)" in knowledge
