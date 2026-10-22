"""向导交互回归：选择卡必须通过注入的 rerender 更新选中态。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_steps_use_injected_rerender_not_undefined_render():
    source = (ROOT / "webui/static/js/setup/steps.js").read_text(encoding="utf-8")
    assert "function rerender(ctx)" in source
    assert "ctx.rerender()" in source
    assert "render(host, ctx, state)" not in source
    assert "render(node)" not in source
    # 所有会改变条件渲染的控件都应走注入回调。
    assert source.count("rerender(ctx)") >= 10


def test_setup_injects_rerender_and_refreshes_view():
    source = (ROOT / "webui/static/js/pages/setup.js").read_text(encoding="utf-8")
    assert "ctx.rerender = rerenderStep" in source
    assert "view = renderer(stepHost, ctx, state)" in source
    assert "renderError()" in source


def test_reasoning_intensity_is_exposed_in_agent_settings():
    providers_js = (ROOT / "webui/static/js/providers.js").read_text(encoding="utf-8")
    assert "reasoningBudget" in providers_js
    assert "reasoning_budget: reasoning.checked ? reasoningBudget.value : null" in providers_js

    api_js = (ROOT / "webui/static/js/setup/steps.js").read_text(encoding="utf-8")
    assert "思考预算" in api_js
    assert "reasoning_budget: state.chat.reasoning_budget || null" in api_js
