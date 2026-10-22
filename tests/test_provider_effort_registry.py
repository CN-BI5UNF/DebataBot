"""Registry 必须把预设的 reasoning_effort_style 传给 provider。"""

from __future__ import annotations

from types import SimpleNamespace

from app_config.schema import ProviderConfig
from providers.presets_loader import load_all_presets
from providers.registry import build_provider


def test_registry_propagates_effort_style_from_preset():
    presets = load_all_presets(
        __import__("pathlib").Path("providers/presets"), force_reload=True
    )
    cfg = ProviderConfig(preset="deepseek", api_key_id="k")
    provider = build_provider(
        "si",
        cfg,
        SimpleNamespace(get=lambda _key: "secret"),
        presets,
    )
    assert provider.reasoning_effort_style == "deepseek"
    assert provider.reasoning_style == "thinking_extra_body"
