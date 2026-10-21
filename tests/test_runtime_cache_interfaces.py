"""阶段 1 接口 smoke tests：不启动 Runtime，只验证公共契约。"""

from __future__ import annotations

import inspect

from agents.chat_agent import ChatAgent
from agents.context_builder import build_messages
from agents.runner import AgentRunner
from core.model_view import ModelViewPolicy as CoreModelViewPolicy
from utils.model_records import ModelViewPolicy as LowLevelModelViewPolicy
from utils.token_budget import TokenEstimator


def test_model_view_policy_has_a_single_source():
    assert CoreModelViewPolicy is LowLevelModelViewPolicy


def test_new_keywords_are_wired_through_public_signatures():
    runner_params = inspect.signature(AgentRunner.run).parameters
    chat_params = inspect.signature(ChatAgent.run).parameters
    build_params = inspect.signature(build_messages).parameters

    assert "model_view_policy" in runner_params
    assert "model_view_policy" in chat_params
    assert "cache_friendly_history" in build_params
    assert "model_view_policy" in build_params


def test_token_estimator_accepts_image_assets_flag():
    estimator = TokenEstimator(include_image_assets=True)
    assert estimator.include_image_assets is True
    assert TokenEstimator().include_image_assets is False
