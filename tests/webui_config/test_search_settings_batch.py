"""联网搜索设置：批量保存、官方通道校验与免费测速接口。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app_config.schema import RootConfig, WebSearchFeatureConfig
from features.web_search.engines import ProbeResult
from providers.presets_loader import ProviderPreset
from webui.api.settings import (
    WebSearchSettingPatch,
    patch_web_search_settings,
    probe_web_search,
)


def test_legacy_ddg_provider_migrates_to_bing_rss():
    search = WebSearchFeatureConfig(provider="ddg")
    assert search.provider == "bing_rss"


class _Secrets:
    def list_ids(self):
        return []

    def get(self, key_id):
        return None


class _Host:
    def __init__(self, rt=None) -> None:
        self.rt = rt
        self.state = "running"
        self.last_error = None
        self.saves = 0

    def save_config(self) -> None:
        self.saves += 1


class _Rt:
    def __init__(self, preset: ProviderPreset | None = None) -> None:
        self.config = RootConfig.model_validate(
            {
                "persona": {"name": "test"},
                "adapters": {"default": {"type": "napcat"}},
                "providers": {
                    "demo": {
                        "preset": "demo",
                        "base_url": "https://api.example.com/v1",
                        "api_key_id": "demo_key",
                    }
                },
                "agents": {"chat": {"provider": "demo", "model": "demo-model"}},
            }
        )
        self.secrets = _Secrets()
        self.host = _Host(self)
        self.provider_registry = SimpleNamespace(
            presets={preset.id: preset} if preset is not None else {}
        )


def _request(rt: _Rt):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(host=rt.host))
    )


@pytest.mark.asyncio
async def test_batch_saves_free_search_config():
    rt = _Rt()
    result = await patch_web_search_settings(
        _request(rt),
        WebSearchSettingPatch(
            enabled=True,
            free_enabled=True,
            provider="bing_rss",
            max_results=7,
            timeout_seconds=12,
            official_enabled=False,
        ),
    )
    assert result["ok"] is True
    assert rt.host.saves == 1
    search = rt.config.features.web_search
    assert search.provider == "bing_rss"
    assert search.max_results == 7
    assert search.free_enabled is True


@pytest.mark.asyncio
async def test_batch_accepts_provider_level_official_channel():
    preset = ProviderPreset(
        id="demo",
        display_name="Demo",
        protocol="openai_compat",
        base_url="https://api.example.com/v1",
        search_channel="deepseek_tools_search",
        search_payload={"request": {"need_content": True}},
    )
    rt = _Rt(preset)
    result = await patch_web_search_settings(
        _request(rt),
        WebSearchSettingPatch(
            enabled=True,
            free_enabled=False,
            official_enabled=True,
            official_provider="demo",
            official_model="",
        ),
    )
    assert result["ok"] is True
    assert rt.config.features.web_search.official.provider == "demo"
    assert rt.config.features.web_search.official.model == ""


@pytest.mark.asyncio
async def test_batch_rejects_unsupported_official_model_atomically():
    preset = ProviderPreset(
        id="demo",
        display_name="Demo",
        protocol="openai_compat",
        base_url="https://api.example.com/v1",
        models=[],
    )
    rt = _Rt(preset)
    before = rt.config.features.web_search.model_dump()
    with pytest.raises(HTTPException) as exc:
        await patch_web_search_settings(
            _request(rt),
            WebSearchSettingPatch(
                enabled=True,
                free_enabled=False,
                official_enabled=True,
                official_provider="demo",
                official_model="demo-model",
            ),
        )
    assert exc.value.status_code == 400
    assert "官方搜索" in str(exc.value.detail)
    assert rt.config.features.web_search.model_dump() == before
    assert rt.host.saves == 0


@pytest.mark.asyncio
async def test_probe_free_engine_returns_latency(monkeypatch):
    class _Engine:
        name = "bing_rss"

        async def probe(self, *, timeout_seconds=None):
            return ProbeResult(ok=True, engine="bing_rss", latency_ms=42)

    monkeypatch.setattr(
        "features.web_search.engines.build_free_engine",
        lambda *args, **kwargs: _Engine(),
    )
    rt = _Rt()
    result = await probe_web_search(
        _request(rt),
        SimpleNamespace(
            provider="bing_rss",
            searxng_base_url="",
            timeout_seconds=10,
        ),
    )
    assert result["ok"] is True
    assert result["latency_ms"] == 42
    assert result["engine"] == "bing_rss"
