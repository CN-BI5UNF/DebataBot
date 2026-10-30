"""有效模型绑定与统一预算快照。

一个请求的预算不能只按“配置里的 max_context_tokens”计算：实际输出上限来自
AgentConfig，模型/渠道窗口来自模型能力元数据或运行时解析结果，安全余量也必须
进入同一个快照。本模块只做确定性的本地解析，不联网、不读取密钥。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from providers.model_capabilities import resolve_model_capability
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


@dataclass(frozen=True, slots=True)
class ModelCallSnapshot:
    """R01: 完整单次请求快照。

    一笔请求的所有参数全从同 snapshot 派生，外部 extra 不能在 guard 后覆盖
    model/output/tools。记录真实 protocol、能力来源、限制、版本及受保护输入集合。
    """

    role: str
    provider_id: str
    model: str
    protocol: str
    endpoint_profile: str
    context_limit: int | None
    context_source: str
    context_verified: bool
    max_output_tokens: int
    reasoning_enabled: bool
    reasoning_budget: str | None
    reasoning_max_tokens: int | None
    image_limits: dict[str, Any] | None
    request_body_limit_bytes: int | None
    budget_snapshot: BudgetSnapshot
    estimator_hash: str
    tools_hash: str
    config_version: int
    profile_version: str
    calib_ratio: float
    calib_version: int
    source_digest: str
    summary_generation: int
    mutation_generation: int
    protected_message_ids: tuple[str, ...]
    protected_inbound_seqs: tuple[int, ...]
    created_at_mono: float
    created_at_utc: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "provider_id": self.provider_id,
            "model": self.model,
            "protocol": self.protocol,
            "endpoint_profile": self.endpoint_profile,
            "context_limit": self.context_limit,
            "context_source": self.context_source,
            "context_verified": self.context_verified,
            "max_output_tokens": self.max_output_tokens,
            "reasoning_enabled": self.reasoning_enabled,
            "reasoning_budget": self.reasoning_budget,
            "reasoning_max_tokens": self.reasoning_max_tokens,
            "image_limits": self.image_limits,
            "request_body_limit_bytes": self.request_body_limit_bytes,
            "budget": (
                self.budget_snapshot.as_dict()
                if hasattr(self.budget_snapshot, "as_dict")
                else str(self.budget_snapshot)
            ),
            "estimator_hash": self.estimator_hash,
            "tools_hash": self.tools_hash,
            "config_version": self.config_version,
            "profile_version": self.profile_version,
            "calib_ratio": self.calib_ratio,
            "calib_version": self.calib_version,
            "source_digest": self.source_digest,
            "summary_generation": self.summary_generation,
            "mutation_generation": self.mutation_generation,
            "protected_message_ids": list(self.protected_message_ids),
            "protected_inbound_seqs": list(self.protected_inbound_seqs),
            "created_at_utc": self.created_at_utc,
        }


def build_model_call_snapshot(
    binding: EffectiveModelBinding,
    budget_snapshot: BudgetSnapshot,
    *,
    tools_schema: list[dict[str, Any]] | None = None,
    estimator: Any = None,
    image_limits: Any = None,
    request_body_limit_bytes: int | None = None,
    endpoint_profile: str = "",
    config_version: int = 1,
    profile_version: str = "",
    calib_ratio: float = 1.0,
    calib_version: int = 0,
    source_digest: str = "",
    summary_generation: int = 0,
    mutation_generation: int = 0,
    protected_message_ids: tuple[str, ...] | list[str] = (),
    protected_inbound_seqs: tuple[int, ...] | list[int] = (),
) -> ModelCallSnapshot:
    """合成不可变的 ModelCallSnapshot。"""
    tools_hash = (
        hashlib.sha256(
            json.dumps(tools_schema, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        if tools_schema
        else "none"
    )
    estimator_hash = hashlib.sha256(
        f"{binding.model}:{calib_ratio:.4f}:{getattr(estimator, 'include_image_assets', False)}".encode()
    ).hexdigest()[:16]

    img_limits_dict: dict[str, Any] | None = None
    if image_limits is not None:
        if hasattr(image_limits, "as_dict") and callable(image_limits.as_dict):
            img_limits_dict = image_limits.as_dict()
        elif isinstance(image_limits, dict):
            img_limits_dict = dict(image_limits)
        else:
            img_limits_dict = {
                "max_images_per_request": getattr(image_limits, "max_images_per_request", None),
                "max_image_bytes": getattr(image_limits, "max_image_bytes", None),
                "max_encoded_image_bytes": getattr(
                    image_limits, "max_encoded_image_bytes", None
                ),
            }

    now_utc = datetime.now(timezone.utc).isoformat()
    return ModelCallSnapshot(
        role=binding.role,
        provider_id=binding.provider_id,
        model=binding.model,
        protocol=binding.protocol,
        endpoint_profile=endpoint_profile,
        context_limit=binding.context_limit,
        context_source=binding.context_source,
        context_verified=binding.context_verified,
        max_output_tokens=binding.max_output_tokens,
        reasoning_enabled=binding.reasoning_enabled,
        reasoning_budget=binding.reasoning_budget,
        reasoning_max_tokens=binding.reasoning_max_tokens,
        image_limits=img_limits_dict,
        request_body_limit_bytes=request_body_limit_bytes,
        budget_snapshot=budget_snapshot,
        estimator_hash=estimator_hash,
        tools_hash=tools_hash,
        config_version=int(config_version),
        profile_version=str(profile_version),
        calib_ratio=float(calib_ratio),
        calib_version=int(calib_version),
        source_digest=str(source_digest),
        summary_generation=int(summary_generation),
        mutation_generation=int(mutation_generation),
        protected_message_ids=tuple(str(mid) for mid in protected_message_ids),
        protected_inbound_seqs=tuple(int(s) for s in protected_inbound_seqs),
        created_at_mono=time.monotonic(),
        created_at_utc=now_utc,
    )


def resolve_agent_binding(
    config: Any,
    role: str,
    *,
    model_context_length: int | None = None,
) -> EffectiveModelBinding | None:
    """从 RootConfig + 已知模型元数据解析某个 Agent 的有效绑定。

    支持对未单独配置的 summary/persona_gen/social 角色安全回退至 chat/proactive，
    并解析真实 provider 协议与端点预设，不留空 protocol。
    """
    agents = getattr(config, "agents", None)
    agent_cfg = getattr(agents, role, None) if agents is not None else None
    if agent_cfg is None:
        if role in {"summary", "persona_gen"}:
            agent_cfg = getattr(agents, "chat", None) if agents is not None else None
        elif role == "social":
            agent_cfg = (
                getattr(agents, "proactive", None) or getattr(agents, "chat", None)
                if agents is not None
                else None
            )
    if agent_cfg is None:
        return None

    provider_id = str(getattr(agent_cfg, "provider", "") or "")
    providers = getattr(config, "providers", None)
    provider_cfg = None
    if isinstance(providers, dict):
        provider_cfg = providers.get(provider_id)
    elif hasattr(providers, "get"):
        provider_cfg = providers.get(provider_id)
    elif hasattr(providers, provider_id):
        provider_cfg = getattr(providers, provider_id)

    preset_id = str(getattr(provider_cfg, "preset", "") or "")
    protocol = str(getattr(provider_cfg, "protocol", "") or "")
    if not protocol:
        if preset_id in {"anthropic", "gemini", "openai", "openai_compat", "ollama", "azure_openai"}:
            protocol = preset_id
        else:
            protocol = "openai_compat"

    capability, _capability_vendor = resolve_model_capability(
        provider_id,
        str(agent_cfg.model),
        vendor=getattr(provider_cfg, "model_vendor", None),
        preset=preset_id,
    )
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
        provider_id=provider_id,
        model=str(agent_cfg.model),
        protocol=protocol,
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
    "ModelCallSnapshot",
    "build_binding_budget",
    "build_model_call_snapshot",
    "resolve_agent_binding",
]
