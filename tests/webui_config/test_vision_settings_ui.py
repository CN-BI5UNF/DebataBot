"""设置→功能→视觉：专用面板的交互契约。

视觉字段曾直接混在通用表单里，用户会误以为“视觉 provider / base_url”
是主模型看图的前置条件。这里锁住三件事：

- 通用表单不再重复渲染这些字段；
- 面板用已有 provider 下拉，并解释复用语义；
- 自定义端点才要求 base_url/API Key，且保存顺序不会产生
  ``enabled=true`` 的不完整中间状态。
"""

from __future__ import annotations

from pathlib import Path

from webui.settings_spec import field_map

ROOT = Path(__file__).resolve().parents[2]
SETTINGS_JS = ROOT / "webui/static/js/pages/settings.js"

VISION_PATHS = (
    "features.vision.main_model_images",
    "features.vision.enabled",
    "features.vision.provider",
    "features.vision.model",
    "features.vision.base_url",
    "features.vision.api_key_id",
    "features.vision.image_token_reserve",
    "features.vision.image_limits.max_images_per_request",
)


def test_vision_fields_are_hidden_from_generic_form():
    fields = field_map()
    for path in VISION_PATHS:
        spec = fields[path]
        assert spec.get("hidden") is True, f"{path} 不应再由通用表单渲染"
        assert spec.get("help"), f"{path} 缺少引导文案"


def test_generic_form_filters_vision_paths_even_without_hidden_flag():
    source = SETTINGS_JS.read_text(encoding="utf-8")
    assert "const VISION_FIELD_PATHS = new Set([" in source
    assert "!VISION_FIELD_PATHS.has(spec.path)" in source
    assert "if (key === 'features')" in source
    assert "await appendVisionPanel(nodes, group);" in source


def test_vision_panel_reuses_providers_and_only_requires_custom_endpoint_fields():
    source = SETTINGS_JS.read_text(encoding="utf-8")
    assert "await endpoints.providers()" in source
    assert "自定义 OpenAI 兼容端点" in source
    assert "复用 provider" in source
    assert "覆盖该 provider 的地址/密钥" in source
    assert "未被预设标记为 vision" in source
    assert "features.vision.api_key_id" in source
    assert "payload.api_key = keyValue" in source


def test_vision_panel_saves_in_one_batch():
    source = SETTINGS_JS.read_text(encoding="utf-8")
    # 关闭独立视觉时详情区会折叠，保存按钮必须留在折叠区外。
    assert "independentFields,\n      h('div', { class: 'row' }," in source
    assert "patchSilent" not in source
    assert "await endpoints.patchVision(payload)" in source
    api_source = (ROOT / "webui/static/js/api.js").read_text(encoding="utf-8")
    assert "patchVision: (payload) => api.patch('/api/settings/vision', payload)" in api_source
    start = source.index("saveBtn.addEventListener('click'")
    block = source[start:start + 5000]
    assert "payload.provider = providerValue" in block
    assert "payload.base_url = baseUrlValue" in block
    assert "payload.api_key_id = useOverride ? existingKeyId : ''" in block
    assert "payload.api_key = keyValue" in block
