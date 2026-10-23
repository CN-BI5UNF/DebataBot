"""Registry 必须把预设的 reasoning_effort_style 传给 provider。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app_config.schema import ProviderConfig
from providers.presets_loader import load_all_presets
from providers.registry import build_provider


def test_registry_propagates_effort_style_from_preset():
    presets = load_all_presets(Path("providers/presets"), force_reload=True)
    cfg = ProviderConfig(preset="deepseek", api_key_id="k")
    provider = build_provider(
        "si",
        cfg,
        SimpleNamespace(get=lambda _key: "secret"),
        presets,
    )
    assert provider.reasoning_effort_style == "deepseek"
    assert provider.reasoning_style == "thinking_extra_body"


def test_reasoning_effort_options_follow_preset_model_and_custom_fallback():
    from webui.api.providers import _reasoning_effort_options

    presets = load_all_presets(Path("providers/presets"), force_reload=True)
    rt = SimpleNamespace(
        config=SimpleNamespace(
            providers={
                "si": ProviderConfig(preset="deepseek", api_key_id="k"),
                "custom": ProviderConfig(
                    protocol="openai_compat",
                    base_url="https://example.com/v1",
                ),
            }
        ),
        provider_registry=SimpleNamespace(presets=presets),
    )
    assert _reasoning_effort_options(rt, "si", "deepseek-flash") == [
        "low", "high", "max"
    ]
    assert _reasoning_effort_options(rt, "si", "my-custom-model") == [
        "low", "medium", "high"
    ]
    assert _reasoning_effort_options(rt, "custom", "my-model") == [
        "low", "medium", "high"
    ]


def test_openai_preset_uses_model_specific_documented_effort_levels():
    from webui.api.providers import _reasoning_effort_options

    presets = load_all_presets(Path("providers/presets"), force_reload=True)
    rt = SimpleNamespace(
        config=SimpleNamespace(
            providers={
                "oa": ProviderConfig(preset="openai", api_key_id="k"),
            }
        ),
        provider_registry=SimpleNamespace(presets=presets),
    )
    assert _reasoning_effort_options(rt, "oa", "gpt-5.5") == [
        "low", "medium", "high", "xhigh"
    ]
    assert _reasoning_effort_options(rt, "oa", "o3") == [
        "low", "medium", "high"
    ]


def test_deepseek_preset_declares_vision_token_budget_and_no_detail():
    presets = load_all_presets(Path("providers/presets"), force_reload=True)
    deepseek = presets["deepseek"]
    assert deepseek.image_token_reserve == 384
    assert deepseek.image_detail_supported is False
    assert deepseek.models[0].id == "deepseek-flash"
