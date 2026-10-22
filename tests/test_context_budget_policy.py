"""统一上下文预算快照与非法窗口拒绝。"""

from __future__ import annotations

import pytest

from utils.token_budget import TokenBudget, build_budget_snapshot


def test_budget_snapshot_131072_example():
    snapshot = build_budget_snapshot(
        window_requested=131072,
        model_window=131072,
        model_limit_verified=True,
        output_reserve=8192,
        actual_max_tokens=5120,
        safety_margin_percent=8,
        safety_margin_min_tokens=1024,
        policy_version=2,
    )

    assert snapshot.valid
    assert snapshot.window_effective == 131072
    assert snapshot.output_reserve == 8192
    assert snapshot.safety_margin == 10486
    assert snapshot.input_budget == 112394
    assert snapshot.soft_trigger == 56197
    assert snapshot.target_after == 33718
    assert snapshot.retry_target == 22478
    assert snapshot.urgent_trigger == 95534


def test_window_smaller_than_output_is_rejected_not_clamped_to_1024():
    snapshot = build_budget_snapshot(
        window_requested=4096,
        output_reserve=8192,
        actual_max_tokens=5120,
    )

    assert not snapshot.valid
    assert snapshot.input_budget == 0
    assert "output_reserve_exceeds_window" in snapshot.errors
    assert snapshot.as_dict()["input_budget"] == 0


def test_model_limit_tightens_effective_window():
    snapshot = build_budget_snapshot(
        window_requested=131072,
        model_window=64000,
        model_limit_verified=True,
        output_reserve=8192,
        safety_margin_percent=8,
        safety_margin_min_tokens=1024,
    )

    assert snapshot.window_effective == 64000
    assert snapshot.window_source == "model_limit"
    assert snapshot.output_reserve == 8192
    assert snapshot.safety_margin == 5120
    assert snapshot.input_budget == 50688


def test_budget_snapshot_rejects_bool_and_negative_window():
    with pytest.raises(ValueError):
        build_budget_snapshot(window_requested=True)
    snapshot = build_budget_snapshot(window_requested=-1)
    assert "invalid_window_requested" in snapshot.errors


def test_token_budget_never_fakes_positive_input_space():
    assert TokenBudget(4096, 8192, 256, 256).total_input_budget == 0
    assert TokenBudget(16384, 8192, 256, 256).total_input_budget == 8192


def test_effective_model_binding_resolves_preset_context_and_output():
    from app_config.schema import RootConfig
    from core.model_binding import build_binding_budget, resolve_agent_binding

    config = RootConfig.model_validate(
        {
            "persona": {"name": "x"},
            "adapters": {"default": {"type": "napcat"}},
            "providers": {
                "si": {"preset": "deepseek", "api_key_id": "k"},
            },
            "agents": {
                "chat": {
                    "provider": "si",
                    "model": "deepseek-flash",
                    "max_tokens": 5120,
                    "reasoning": {"enabled": True, "budget": "high"},
                },
                "summary": {
                    "provider": "si",
                    "model": "deepseek-flash",
                    "max_tokens": 1024,
                },
            },
        }
    )
    binding = resolve_agent_binding(config, "chat")
    assert binding is not None
    assert binding.context_limit == 1_000_000
    assert binding.context_verified is True
    assert binding.max_output_tokens == 5120
    assert binding.reasoning_budget == "high"

    snapshot = build_binding_budget(
        binding,
        config.behavior.context,
        config.behavior.summarize,
        requested_window=131072,
    )
    assert snapshot.valid
    assert snapshot.output_reserve == 8192
    assert snapshot.safety_margin == 10486
    assert snapshot.input_budget == 112394
