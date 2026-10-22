"""有效模型绑定与统一预算快照。

一个请求的预算不能只按“配置里的 max_context_tokens”计算：实际输出上限来自
AgentConfig，模型/渠道窗口来自模型能力元数据或运行时解析结果，安全余量也必须
进入同一个快照。本模块只做确定性的本地解析，不联网、不读取密钥。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from providers.model_capabilities import model_capability
from utils.token_budget import BudgetSnapshot, build_budget_snapshot


@dataclass(frozen=True, slots=True)
class EffectiveModelBinding:
    role: str
    provider_id: str
    model: str
    protocol: str
    context_limit: int | None
    context_source: str
    context_verified: bool
    max_output_tokens: int
    reasoning_enabled: bool = False
    reasoning_budget: str | None = None
    reasoning_max_tokens: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "provider_id": self.provider_id,
            "model": self.model,
            "protocol": self.protocol,
            "context_limit": self.context_limit,
            "context_source": self.context_source,
            "context_verified": self.context_verified,
            "max_output_tokens": self.max_output_tokens,
            "reasoning_enabled": self.reasoning_enabled,
            "reasoning_budget": self.reasoning_budget,
            "reasoning_max_tokens": self.reasoning_max_tokens,
        }


def resolve_agent_binding(
    config: Any,
    role: str,
    *,
    model_context_length: int | None = None,
) -> EffectiveModelBinding | None:
    """从 RootConfig + 已知模型元数据解析某个 Agent 的有效绑定。"""
    agents = getattr(config, "agents", None)
    agent_cfg = getattr(agents, role, None) if agents is not None else None
    if agent_cfg is None:
        return None
    provider_cfg = (getattr(config, "providers", {}) or {}).get(
        str(getattr(agent_cfg, "provider", "") or "")
    )
    if provider_cfg is None:
        return None

    preset_id = str(getattr(provider_cfg, "preset", "") or "")
    capability = model_capability(preset_id or str(agent_cfg.provider), str(agent_cfg.model))
    context_limit: int | None = None
    source = "unknown"
    if isinstance(model_context_length, int) and model_context_length > 0:
        context_limit = int(model_context_length)
        source = "runtime_model_meta"
    elif capability is not None and int(capability.context_length or 0) > 0:
        context_limit = int(capability.context_length)
        source = "model_capabilities"
    reasoning = getattr(agent_cfg, "reasoning", None)
    return EffectiveModelBinding(
        role=role,
        provider_id=str(agent_cfg.provider),
        model=str(agent_cfg.model),
        protocol=str(getattr(provider_cfg, "protocol", None) or "openai_compat"),
        context_limit=context_limit,
        context_source=source,
        context_verified=context_limit is not None,
        max_output_tokens=max(1, int(getattr(agent_cfg, "max_tokens", 1) or 1)),
        reasoning_enabled=bool(getattr(reasoning, "enabled", False)),
        reasoning_budget=getattr(reasoning, "budget", None),
        reasoning_max_tokens=getattr(reasoning, "max_tokens", None),
    )


def build_binding_budget(
    binding: EffectiveModelBinding,
    context_cfg: Any,
    summarize_cfg: Any,
    *,
    requested_window: int,
    actual_max_tokens: int | None = None,
) -> BudgetSnapshot:
    """把有效绑定与 behavior 配置合成统一预算快照。"""
    return build_budget_snapshot(
        window_requested=int(requested_window),
        model_window=binding.context_limit,
        model_limit_verified=binding.context_verified,
        window_source=(
            "configured" if getattr(context_cfg, "max_context_tokens", None) else "recommended"
        ),
        output_reserve=int(getattr(context_cfg, "reserve_output_tokens", 8192) or 0),
        actual_max_tokens=(
            int(actual_max_tokens)
            if actual_max_tokens is not None
            else binding.max_output_tokens
        ),
        safety_margin_percent=int(getattr(context_cfg, "safety_margin_percent", 8) or 0),
        safety_margin_min_tokens=int(
            getattr(context_cfg, "safety_margin_min_tokens", 1024) or 0
        ),
        policy_version=int(getattr(context_cfg, "budget_policy_version", 1) or 1),
        soft_percent=int(getattr(summarize_cfg, "trigger_at_context_percent", 50) or 50),
        target_percent=int(getattr(summarize_cfg, "target_after_context_percent", 30) or 30),
        retry_percent=int(
            getattr(summarize_cfg, "retry_target_after_context_percent", 20) or 20
        ),
        urgent_percent=int(getattr(summarize_cfg, "urgent_at_input_percent", 85) or 85),
    )


__all__ = [
    "EffectiveModelBinding",
    "build_binding_budget",
    "resolve_agent_binding",
]
