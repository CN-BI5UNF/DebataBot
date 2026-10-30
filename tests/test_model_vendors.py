"""Model-vendor detection for relay/custom providers."""

from __future__ import annotations

import pytest

from app_config.schema import ProviderConfig
from providers.model_capabilities import model_supports, resolve_model_capability
from providers.model_vendors import detect_model_vendor, resolve_model_vendor


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("gemini-2.5-flash", "gemini"),
        ("google/gemini-2.5-pro", "gemini"),
        ("claude-sonnet-4-6", "anthropic"),
        ("anthropic/claude-opus-4-8", "anthropic"),
        ("gpt-5.5", "openai"),
        ("openai/o3", "openai"),
        ("deepseek-v4-pro", "deepseek"),
        ("qwen3.7-max", "qwen"),
        ("glm-4.7", "glm"),
        ("kimi-k2", "moonshot"),
        ("grok-4", "xai"),
        ("doubao-seed-2-0-pro", "volcengine"),
        ("mimo-v2.5-pro", "mimo"),
        ("some-local-model", ""),
    ],
)
def test_detect_model_vendor(model_id, expected):
    assert detect_model_vendor(model_id) == expected


def test_explicit_vendor_and_preset_precedence():
    assert resolve_model_vendor(
        "gemini-2.5-flash", explicit="anthropic", preset="openai"
    ) == "anthropic"
    assert resolve_model_vendor("gemini-2.5-flash", preset="openai") == "openai"
    assert resolve_model_vendor("gemini-2.5-flash") == "gemini"


def test_relay_model_capability_uses_explicit_vendor_or_model_id():
    info, vendor = resolve_model_capability(
        "my_relay", "gemini-2.5-flash", vendor="gemini"
    )
    assert vendor == "gemini"
    assert info is not None and info.supports("vision")

    info, vendor = resolve_model_capability(
        "my_relay", "claude-sonnet-4-6"
    )
    assert vendor == "anthropic"
    assert info is not None and info.supports("vision")

    assert model_supports("my_relay", "gemini-2.5-flash", "vision")
    assert not model_supports("my_relay", "mystery-model", "vision")
    assert resolve_model_capability("my_relay", "mystery-model") == (None, "")


def test_provider_config_normalizes_model_vendor():
    cfg = ProviderConfig(
        protocol="openai_compat",
        base_url="https://relay.example.com/v1",
        model_vendor=" Gemini ",
    )
    assert cfg.model_vendor == "gemini"
    with pytest.raises(ValueError):
        ProviderConfig(
            protocol="openai_compat",
            base_url="https://relay.example.com/v1",
            model_vendor="bad vendor",
        )
