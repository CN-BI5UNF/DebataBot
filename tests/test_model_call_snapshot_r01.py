"""R01 专项测试：ModelCallSnapshot、真实协议解析、锁内消息重建与全角色预算护栏。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.persona_gen_agent import PersonaBrief, PersonaGenAgent
from agents.proactive_agent import ProactiveRouterAgent
from agents.social_agent import SocialAgent
from agents.summary_agent import SummaryAgent
from app_config.schema import AgentConfig
from core.model_binding import (
    EffectiveModelBinding,
    ModelCallSnapshot,
    build_binding_budget,
    build_model_call_snapshot,
    resolve_agent_binding,
)
from core.pipeline_turns import PipelineTurnsMixin
from providers.base import CompletionResult, IProvider, Usage


class DummyProvider(IProvider):
    def __init__(self, name: str = "dummy", protocol: str = "openai_compat") -> None:
        self.name = name
        self.protocol = protocol
        self.call_count = 0

    async def chat_completion(self, messages, **kwargs) -> CompletionResult:
        self.call_count += 1
        return CompletionResult(
            content="ok",
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    async def aclose(self) -> None:
        pass


def test_build_model_call_snapshot_fields_and_as_dict():
    binding = EffectiveModelBinding(
        role="chat",
        provider_id="deepseek-main",
        model="deepseek-chat",
        protocol="openai_compat",
        context_limit=131072,
        context_source="model_capabilities",
        context_verified=True,
        max_output_tokens=8192,
        reasoning_enabled=True,
        reasoning_budget="8192",
    )
    budget = build_binding_budget(
        binding,
        SimpleNamespace(
            max_context_tokens=131072,
            reserve_output_tokens=8192,
            safety_margin_percent=8,
            safety_margin_min_tokens=1024,
            budget_policy_version=1,
        ),
        SimpleNamespace(
            trigger_at_context_percent=50,
            target_after_context_percent=30,
            retry_target_after_context_percent=20,
            urgent_at_input_percent=85,
        ),
        requested_window=131072,
    )
    tools = [
        {
            "type": "function",
            "function": {"name": "send_msg", "parameters": {"type": "object"}},
        }
    ]
    snapshot = build_model_call_snapshot(
        binding,
        budget,
        tools_schema=tools,
        calib_ratio=1.05,
        protected_message_ids=["msg-1", "msg-2"],
        protected_inbound_seqs=[101, 102],
    )
    assert isinstance(snapshot, ModelCallSnapshot)
    assert snapshot.role == "chat"
    assert snapshot.protocol == "openai_compat"
    assert snapshot.tools_hash != "none"
    assert snapshot.protected_message_ids == ("msg-1", "msg-2")
    assert snapshot.protected_inbound_seqs == (101, 102)

    data = snapshot.as_dict()
    assert data["role"] == "chat"
    assert data["protocol"] == "openai_compat"
    assert "budget" in data
    assert data["protected_message_ids"] == ["msg-1", "msg-2"]


def test_resolve_agent_binding_protocol_and_role_fallback():
    # 模拟 RootConfig 结构
    config = SimpleNamespace(
        agents=SimpleNamespace(
            chat=AgentConfig(
                provider="my_provider",
                model="deepseek-chat",
                max_tokens=2048,
            ),
            # summary, persona_gen, proactive 留空
            summary=None,
            persona_gen=None,
            proactive=None,
        ),
        providers={
            "my_provider": SimpleNamespace(
                preset="deepseek",
                protocol="",
            )
        },
    )
    # 1. chat 解析：protocol 依据 preset 推断，不应留空
    chat_binding = resolve_agent_binding(config, "chat")
    assert chat_binding is not None
    assert chat_binding.role == "chat"
    assert chat_binding.protocol == "openai_compat"
    assert chat_binding.context_limit is not None

    # 2. summary 回退：未配置 summary agent 时安全使用 chat 的模型绑定
    summary_binding = resolve_agent_binding(config, "summary")
    assert summary_binding is not None
    assert summary_binding.role == "summary"
    assert summary_binding.model == "deepseek-chat"
    assert summary_binding.protocol == "openai_compat"

    # 3. persona_gen 回退
    gen_binding = resolve_agent_binding(config, "persona_gen")
    assert gen_binding is not None
    assert gen_binding.role == "persona_gen"


@pytest.mark.asyncio
async def test_proactive_agent_budget_guard():
    prov = DummyProvider()
    cfg = AgentConfig(provider="dummy", model="dummy-model", max_tokens=100)
    agent = ProactiveRouterAgent(
        prov,
        cfg,
        prompt_budget_tokens=10,  # 极低预算，必定超限
    )
    long_msgs = [{"role": "user", "content": "hello " * 200}]
    should_act, reason = await agent.should_act(long_msgs)
    assert should_act is False
    assert reason == ""
    # provider 没有被调用
    assert prov.call_count == 0


@pytest.mark.asyncio
async def test_social_agent_budget_guard():
    prov = DummyProvider()
    cfg = AgentConfig(provider="dummy", model="dummy-model", max_tokens=100)
    agent = SocialAgent(
        prov,
        cfg,
        prompt_budget_tokens=10,  # 极低预算
    )
    long_msgs = [{"role": "user", "content": "social conversation " * 200}]
    decision = await agent.decide(long_msgs)
    assert decision.get("decision") == "skip"
    assert prov.call_count == 0


@pytest.mark.asyncio
async def test_persona_gen_agent_budget_guard():
    prov = DummyProvider()
    cfg = AgentConfig(provider="dummy", model="dummy-model", max_tokens=100)
    agent = PersonaGenAgent(
        prov,
        cfg,
        prompt_budget_tokens=10,  # 极低预算
    )
    brief = PersonaBrief(name="test", personality="long personality " * 50)
    with pytest.raises(ValueError, match="人格生成提示词超出预算"):
        await agent.generate(brief)
    assert prov.call_count == 0


@pytest.mark.asyncio
async def test_summary_agent_budget_guard():
    prov = DummyProvider()
    cfg = AgentConfig(provider="dummy", model="dummy-model", max_tokens=100)
    agent = SummaryAgent(
        prov,
        cfg,
        SimpleNamespace(),
        prompt_budget_tokens=10,  # 极低预算
    )
    history_slice = [{"role": "user", "content": "historical content " * 100}]
    res = await agent.summarize_rolling(history_slice, "", "")
    assert res is None
    assert prov.call_count == 0


@pytest.mark.asyncio
async def test_prepare_locked_turn_holds_reply_lock_during_rebuild():
    """验证 run_one_turn 在取得 reply_lock 后才调用 _prepare_main_prompt_for_model。"""

    class DummyPipeline(PipelineTurnsMixin):
        def __init__(self):
            self.reply_lock = asyncio.Lock()
            self.lock_held_during_prepare = False
            self.history = MagicMock()
            self.history.add_system_note = AsyncMock()
            self.history.add_records = AsyncMock()
            self.features_cfg = SimpleNamespace(
                long_term_memory=SimpleNamespace(mode="none")
            )
            self.behavior_cfg = SimpleNamespace(auto_finish_after_send=True)
            self.persona = SimpleNamespace(name="dummy")
            self.tool_registry = MagicMock()
            self.tool_registry.get_executor = MagicMock(return_value=AsyncMock())
            self.tool_registry.get_schemas = MagicMock(return_value=[])
            self._send_manager = MagicMock()
            self.chat_agent = MagicMock()
            self.chat_agent.run = AsyncMock(
                return_value=SimpleNamespace(records=[], finish_reason="stop", loop_count=1)
            )
            self._token_calib_ratio = 1.0

        def mark_activity(self):
            pass

        async def _should_buffer_inbound_for_resting_persona(self):
            return False

        def _build_tool_context(self, **kwargs):
            return SimpleNamespace(collected=[])

        def _active_budget_snapshot(self):
            return SimpleNamespace(valid=True, input_budget=10000, errors=[])

        async def _execute_collected(self, collected):
            pass

        def _schedule_persona_after_turn(self, **kwargs):
            pass

        def _run_one_turn_chat_summary(self, **kwargs):
            return ""

        def _run_one_turn_persona_participants(self, **kwargs):
            return []

        def _runtime_event_callback(self, conversation_id):
            return None

        def _consume_send_receipts(self, conversation_id):
            return []

        def _model_view_policy(self):
            return None

        def _main_prompt_image_limits(self):
            return None

        def _provider_request_body_limit_bytes(self):
            return None

        def _token_estimator(self):
            return MagicMock()

        async def _prepare_main_prompt_for_model(self, **kwargs):
            # 关键断言：此时 reply_lock 必须已被持有！
            self.lock_held_during_prepare = self.reply_lock.locked()
            return SimpleNamespace(
                ok=True,
                messages=[{"role": "user", "content": "hi"}],
                estimated_tokens=50,
                budget_tokens=10000,
            )

    pipeline = DummyPipeline()
    await pipeline.run_one_turn("task_context")
    assert pipeline.lock_held_during_prepare is True
