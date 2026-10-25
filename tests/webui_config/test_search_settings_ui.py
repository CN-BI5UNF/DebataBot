"""设置页搜索面板与更新测速的 UI 契约。"""

from __future__ import annotations

from pathlib import Path

from webui.settings_spec import field_map

ROOT = Path(__file__).resolve().parents[2]
SETTINGS_JS = ROOT / "webui/static/js/pages/settings.js"
API_JS = ROOT / "webui/static/js/api.js"

SEARCH_PATHS = (
    "features.web_search.enabled",
    "features.web_search.free_enabled",
    "features.web_search.provider",
    "features.web_search.searxng_base_url",
    "features.web_search.max_results",
    "features.web_search.timeout_seconds",
    "features.web_search.official.enabled",
    "features.web_search.official.provider",
    "features.web_search.official.model",
    "features.web_search.official.timeout_seconds",
)


def test_search_fields_are_hidden_from_generic_form():
    fields = field_map()
    for path in SEARCH_PATHS:
        assert fields[path].get("hidden") is True
        assert fields[path].get("help"), f"{path} 缺少引导文案"


def test_search_engine_options_exclude_untested_ddg():
    options = dict(field_map()["features.web_search.provider"]["options"])
    assert set(options) == {"bing_rss", "baidu", "searxng"}
    assert all("DuckDuckGo" not in label for label in options.values())
    assert "百度" in options["baidu"]


def test_generic_form_filters_search_paths():
    source = SETTINGS_JS.read_text(encoding="utf-8")
    assert "const SEARCH_FIELD_PATHS = new Set([" in source
    assert "!SEARCH_FIELD_PATHS.has(spec.path)" in source
    assert "await appendSearchPanel(nodes, group);" in source


def test_search_panel_has_free_official_and_probe():
    source = SETTINGS_JS.read_text(encoding="utf-8")
    api_source = API_JS.read_text(encoding="utf-8")
    assert "async function appendSearchPanel" in source
    assert "启用免费搜索（优先）" in source
    assert "启用官方搜索（仅在免费搜索不可用时回退）" in source
    assert "官方搜索会调用模型/搜索 API" in source
    assert "只有免费搜索测速或本次请求失败才回退到官方" in source
    assert "Bing RSS" in source
    assert "测速免费搜索" in source
    assert "patchSearch: (payload) => api.patch('/api/settings/web-search', payload)" in api_source
    assert "probeSearch: (payload)" in api_source


def test_update_mirror_card_has_probe_and_use_action():
    source = SETTINGS_JS.read_text(encoding="utf-8")
    api_source = API_JS.read_text(encoding="utf-8")
    assert "测速 GitHub / 镜像" in source
    assert "endpoints.updateProbe" in source
    assert "只执行 git ls-remote" in source
    assert "updateProbe: (payload = {})" in api_source
    assert "api.post('/api/update/probe'" in api_source


def test_search_toggles_stay_visible_when_details_are_collapsed():
    source = SETTINGS_JS.read_text(encoding="utf-8")
    # 开关必须在 details 外面，否则关闭后就无法再打开（官方搜索默认关闭）。
    assert "masterRow,\n      freeRow,\n      freeDetails," in source
    assert "officialRow,\n      officialDetails," in source
    assert (
        "const freeDetails = h('div', {\n"
        "      dataset: { searchFreeDetails: '1' },\n"
        "    },\n"
        "      freeRow,"
    ) not in source
    assert (
        "const officialDetails = h('div', {\n"
        "      dataset: { searchOfficialDetails: '1' },\n"
        "    },\n"
        "      officialRow,"
    ) not in source
