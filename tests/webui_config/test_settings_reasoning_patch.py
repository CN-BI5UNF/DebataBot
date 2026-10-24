"""Agent PATCH：六档思考强度与省略/null 语义。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app_config.schema import RootConfig
from webui.api import settings as settings_api
from webui.api.settings import ProviderPatch

ROOT = Path(__file__).resolve().parents[2]


class FakeHost:
    def __init__(self, rt) -> None:
        self.rt = rt
        self.state = "running"
        self.last_error = None
        self.saved = 0

    def save_config(self) -> None:
        self.saved += 1


def _request(rt):
    host = FakeHost(rt)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(host=host)))


def _rt(preset: str, model: str, budget: str | None = "high"):
    from providers.presets_loader import load_all_presets

    presets = load_all_presets(ROOT / "providers" / "presets")
    reasoning = {"enabled": True}
    if budget is not None:
        reasoning["budget"] = budget
    config = RootConfig.model_validate(
        {
            "persona": {"name": "test_bot"},
            "adapters": {"default": {"type": "napcat"}},
            "providers": {
                "p": {"preset": preset, "api_key_id": "p_key"},
            },
            "agents": {
                "chat": {
                    "provider": "p",
                    "model": model,
                    "reasoning": reasoning,
                }
            },
        }
    )
    return SimpleNamespace(
        config=config,
        secrets=SimpleNamespace(),
        provider_registry=SimpleNamespace(presets=presets),
        provider_health={},
        providers={},
    )


@pytest.mark.asyncio
async def test_legacy_null_keeps_budget_and_reset_flag_clears_it():
    rt = _rt("openai", "gpt-5.5", "high")
    request = _request(rt)
    # 旧客户端 null = 不改，避免关闭 reasoning 时误清空档位。
    payload = ProviderPatch.model_validate({"reasoning_budget": None})
    await settings_api.patch_agent(request, "chat", payload)
    agent = rt.config.agents.chat
    assert agent.reasoning.enabled is True
    assert agent.reasoning.budget == "high"

    # 明确 reset 才恢复供应商 auto。
    await settings_api.patch_agent(
        request,
        "chat",
        ProviderPatch(reasoning_budget_reset=True),
    )
    assert rt.config.agents.chat.reasoning.enabled is True
    assert rt.config.agents.chat.reasoning.budget is None


@pytest.mark.asyncio
async def test_reset_and_budget_cannot_be_sent_together():
    rt = _rt("openai", "gpt-5.5", "high")
    request = _request(rt)
    with pytest.raises(HTTPException) as exc:
        await settings_api.patch_agent(
            request,
            "chat",
            ProviderPatch(reasoning_budget="high", reasoning_budget_reset=True),
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_omitted_budget_preserves_existing_choice():
    rt = _rt("openai", "gpt-5.5", "high")
    request = _request(rt)
    await settings_api.patch_agent(request, "chat", ProviderPatch())
    assert rt.config.agents.chat.reasoning.budget == "high"


@pytest.mark.asyncio
async def test_xhigh_is_accepted_for_openai_and_persisted():
    rt = _rt("openai", "gpt-5.5", "high")
    request = _request(rt)
    await settings_api.patch_agent(
        request,
        "chat",
        ProviderPatch(reasoning_budget="xhigh"),
    )
    assert rt.config.agents.chat.reasoning.budget == "xhigh"


@pytest.mark.asyncio
async def test_xhigh_is_rejected_for_deepseek_options():
    rt = _rt("deepseek", "deepseek-flash", "high")
    request = _request(rt)
    with pytest.raises(HTTPException) as exc:
        await settings_api.patch_agent(
            request,
            "chat",
            ProviderPatch(reasoning_budget="xhigh"),
        )
    assert exc.value.status_code == 400
    assert "xhigh" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_disabling_reasoning_keeps_selected_budget():
    rt = _rt("openai", "gpt-5.5", "xhigh")
    request = _request(rt)
    await settings_api.patch_agent(
        request,
        "chat",
        ProviderPatch(reasoning=False),
    )
    agent = rt.config.agents.chat
    assert agent.reasoning.enabled is False
    assert agent.reasoning.budget == "xhigh"


@pytest.mark.asyncio
async def test_explicit_reasoning_null_keeps_enabled_state():
    rt = _rt("openai", "gpt-5.5", "high")
    request = _request(rt)
    payload = ProviderPatch.model_validate({"reasoning": None})
    assert "reasoning" in payload.model_fields_set
    await settings_api.patch_agent(request, "chat", payload)
    assert rt.config.agents.chat.reasoning.enabled is True

    await settings_api.patch_agent(
        request,
        "chat",
        ProviderPatch.model_validate({"reasoning": False}),
    )
    assert rt.config.agents.chat.reasoning.enabled is False


def test_patch_model_accepts_all_six_levels():
    for level in ("minimal", "low", "medium", "high", "xhigh", "max"):
        payload = ProviderPatch.model_validate({"reasoning_budget": level})
        assert payload.reasoning_budget == level
