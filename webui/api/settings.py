"""设置页接口 —— 声明式字段读写、provider/密钥管理、黑名单。

写盘统一走 ``RuntimeHost.save_config``（等价原 GUI 的 ``_save_now``）：
    - ``needs_restart=False`` 的字段：热更新内存配置 + 立即落盘
    - ``needs_restart=True``  的字段：落盘后由前端提示「需重启 Runtime」

黑名单额外做了一次热应用（``pipeline.apply_blacklist``），保存即生效、无需重启。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from app_config import user_error_text
from webui.config_edit import apply_validated, validate_config
from webui.settings_spec import SETTINGS_GROUPS, field_map

from . import providers as providers_api
from .deps import get_path_in, require_runtime, set_path_in

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


# ============================================================
# 声明式字段
# ============================================================


@router.get("")
async def read_settings(request: Request) -> dict[str, Any]:
    """按声明表回读全部字段的当前值。"""
    rt = require_runtime(request)
    cfg = rt.config
    groups: list[dict[str, Any]] = []
    for group in SETTINGS_GROUPS:
        fields = []
        for field in group["fields"]:
            entry = dict(field)
            entry["value"] = get_path_in(cfg, field["path"], field.get("default"))
            fields.append(entry)
        groups.append({k: v for k, v in group.items() if k != "fields"} | {"fields": fields})

    return {
        "groups": groups,
        "paths": _paths_info(rt),
        "secrets_index": _secrets_index(rt),
    }


def _paths_info(rt: Any) -> dict[str, str]:
    paths = rt.paths
    out = {}
    for attr in ("PROJECT_ROOT", "DATA_DIR", "CONFIG_FILE", "LOGS_DIR", "MEMORY_DIR",
                 "PERSONAS_DIR", "WORKSPACE_DIR", "EMOJI_DIR",
                 "PROVIDER_PRESETS_DIR", "USER_PROVIDER_PRESETS_DIR"):
        value = getattr(paths, attr, None)
        if value is not None:
            out[attr] = str(value)
    return out


def _secrets_index(rt: Any) -> list[str]:
    """已保存的密钥 ID 列表（绝不返回密钥本身）。"""
    try:
        return sorted(rt.secrets.list_ids())
    except Exception:  # noqa: BLE001
        try:
            return sorted(rt.secrets._data.keys())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return []


class SettingPatch(BaseModel):
    path: str = Field(..., description="如 behavior.rate_limit.enabled")
    value: Any = None


@router.patch("")
async def patch_setting(
    request: Request, payload: Annotated[SettingPatch, Body()]
) -> dict[str, Any]:
    """改一个字段并立即落盘。"""
    rt = require_runtime(request)
    host = request.app.state.host
    spec = field_map().get(payload.path)
    if spec is None:
        raise HTTPException(status_code=400, detail=f"不认识的配置项：{payload.path}")

    value = _coerce(payload.value, spec)
    cfg = rt.config
    try:
        # 借 pydantic 校验一遍，避免写出非法组合（如枚举值拼错）。
        # 必须改在副本上：直接改内存再校验的话，校验失败虽然回了 400，
        # 坏值却留在 rt.config 里，会被下一次落盘顺手写进 config.yaml。
        apply_validated(cfg, lambda draft: set_path_in(draft, payload.path, value))
    except ValidationError as e:
        raise HTTPException(
            status_code=400, detail=f"设置值不合法：{user_error_text(e)}"
        ) from e
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"配置路径不存在：{e}") from e

    await asyncio.to_thread(host.save_config)
    await _apply_hot(rt, payload.path, value)
    logger.info("配置已更新 %s = %r", payload.path, value)

    return {
        "ok": True,
        "path": payload.path,
        "value": value,
        "needs_restart": bool(spec.get("needs_restart", True)),
        "message": "已保存",
    }


class VisionSettingPatch(BaseModel):
    """视觉设置批量保存。

    ``None`` 表示不修改；可空字段传空字符串表示清空。把校验和落盘收敛到
    一次请求，避免多次 PATCH 产生半配置、多份 config 备份和中间 ``enabled``
    状态。
    """

    main_model_images: Literal["auto", "on", "off"] | None = None
    gif_mode: Literal["native", "first_frame"] | None = None
    main_model_videos: Literal["auto", "on", "off"] | None = None
    enabled: bool | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_id: str | None = None
    image_token_reserve: int | None = None
    max_images_per_request: int | None = None
    video_token_reserve: int | None = None
    max_videos_per_request: int | None = None
    api_key: str | None = Field(default=None, max_length=4096)


def _apply_vision_patch(draft: Any, payload: VisionSettingPatch) -> None:
    vision = draft.features.vision
    if payload.main_model_images is not None:
        vision.main_model_images = payload.main_model_images
    if payload.gif_mode is not None:
        vision.gif_mode = payload.gif_mode
    if payload.main_model_videos is not None:
        vision.main_model_videos = payload.main_model_videos
    if payload.enabled is not None:
        vision.enabled = payload.enabled
    if payload.provider is not None:
        vision.provider = payload.provider.strip() or None
    if payload.model is not None:
        vision.model = payload.model.strip()
    if payload.base_url is not None:
        vision.base_url = payload.base_url.strip() or None
    if payload.api_key_id is not None:
        vision.api_key_id = payload.api_key_id.strip() or None
    api_key = (payload.api_key or "").strip()
    if api_key:
        # 独立视觉密钥固定写入专用 ID，不覆盖主 provider 的密钥。
        vision.api_key_id = "vision_key"
    if payload.image_token_reserve is not None:
        vision.image_token_reserve = payload.image_token_reserve
    if payload.max_images_per_request is not None:
        vision.image_limits.max_images_per_request = (
            payload.max_images_per_request
        )
    if payload.video_token_reserve is not None:
        vision.video_token_reserve = payload.video_token_reserve
    if payload.max_videos_per_request is not None:
        vision.video_limits.max_videos_per_request = (
            payload.max_videos_per_request
        )


@router.patch("/vision")
async def patch_vision_settings(
    request: Request, payload: Annotated[VisionSettingPatch, Body()]
) -> dict[str, Any]:
    """一次校验、一次落盘的视觉设置保存。"""
    rt = require_runtime(request)
    host = request.app.state.host
    draft = rt.config.model_copy(deep=True)
    _apply_vision_patch(draft, payload)
    try:
        validate_config(draft)
    except ValidationError as e:
        raise HTTPException(
            status_code=400, detail=f"视觉设置不合法：{user_error_text(e)}"
        ) from e

    api_key = (payload.api_key or "").strip()
    if api_key:
        try:
            await asyncio.to_thread(rt.secrets.set, "vision_key", api_key)
        except Exception as e:  # noqa: BLE001
            logger.exception("写入视觉密钥失败")
            raise HTTPException(
                status_code=500, detail=f"写入视觉密钥失败：{e}"
            ) from e

    apply_validated(
        rt.config, lambda cfg: _apply_vision_patch(cfg, payload)
    )
    await asyncio.to_thread(host.save_config)
    vision = rt.config.features.vision
    logger.info(
        "视觉设置已更新 enabled=%s provider=%s model=%s",
        vision.enabled,
        vision.provider,
        vision.model,
    )
    return {
        "ok": True,
        "needs_restart": True,
        "api_key_saved": bool(api_key),
        "value": {
            "main_model_images": vision.main_model_images,
            "gif_mode": vision.gif_mode,
            "main_model_videos": vision.main_model_videos,
            "enabled": vision.enabled,
            "provider": vision.provider or "",
            "model": vision.model,
            "base_url": vision.base_url or "",
            "api_key_id": vision.api_key_id or "",
            "image_token_reserve": vision.image_token_reserve,
            "max_images_per_request": (
                vision.image_limits.max_images_per_request
            ),
            "video_token_reserve": vision.video_token_reserve,
            "max_videos_per_request": (
                vision.video_limits.max_videos_per_request
            ),
        },
        "message": "视觉设置已保存；重启 Runtime 后生效",
    }


class WebSearchSettingPatch(BaseModel):
    """联网搜索批量保存；None 表示不修改。"""

    enabled: bool | None = None
    free_enabled: bool | None = None
    provider: Literal["bing_rss", "baidu", "searxng"] | None = None
    searxng_base_url: str | None = None
    max_results: int | None = None
    timeout_seconds: float | None = None
    official_enabled: bool | None = None
    official_provider: str | None = None
    official_model: str | None = None
    official_timeout_seconds: float | None = None


class WebSearchProbePayload(BaseModel):
    provider: Literal["bing_rss", "baidu", "searxng"] = "bing_rss"
    searxng_base_url: str = ""
    timeout_seconds: float = 10.0


def _apply_web_search_patch(
    draft: Any, payload: WebSearchSettingPatch
) -> None:
    search = draft.features.web_search
    if payload.enabled is not None:
        search.enabled = payload.enabled
    if payload.free_enabled is not None:
        search.free_enabled = payload.free_enabled
    if payload.provider is not None:
        search.provider = payload.provider
    if payload.searxng_base_url is not None:
        search.searxng_base_url = payload.searxng_base_url.strip()
    if payload.max_results is not None:
        search.max_results = payload.max_results
    if payload.timeout_seconds is not None:
        search.timeout_seconds = payload.timeout_seconds
    if payload.official_enabled is not None:
        search.official.enabled = payload.official_enabled
    if payload.official_provider is not None:
        search.official.provider = payload.official_provider.strip() or None
    if payload.official_model is not None:
        search.official.model = payload.official_model.strip()
    if payload.official_timeout_seconds is not None:
        search.official.timeout_seconds = payload.official_timeout_seconds


def _validate_official_search_channel(
    rt: Any, provider_id: str, model_id: str
) -> None:
    provider_id = (provider_id or "").strip()
    model_id = (model_id or "").strip()
    if not provider_id:
        raise HTTPException(
            status_code=400,
            detail="官方搜索需要选择 provider",
        )
    provider = (rt.config.providers or {}).get(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=400, detail=f"官方搜索 provider 不存在：{provider_id}"
        )
    preset_id = str(getattr(provider, "preset", "") or "").lower()
    preset = (getattr(rt.provider_registry, "presets", {}) or {}).get(preset_id)
    if preset is None:
        raise HTTPException(
            status_code=400,
            detail="官方搜索只支持带内置预设的 provider",
        )
    if getattr(preset, "search_channel", ""):
        # provider 级搜索（如 DeepSeek/Kimi Search）不绑定聊天模型。
        return
    if not model_id:
        raise HTTPException(
            status_code=400,
            detail="该 provider 的搜索通道需要选择具体模型",
        )
    model = next(
        (item for item in getattr(preset, "models", []) if item.id == model_id),
        None,
    )
    if (
        model is None
        or not getattr(model, "search_channel", "")
        or "web_search" not in (getattr(model, "capabilities", []) or [])
    ):
        raise HTTPException(
            status_code=400,
            detail=f"{preset_id}/{model_id} 没有已确认的官方搜索 API",
        )


@router.patch("/web-search")
async def patch_web_search_settings(
    request: Request, payload: Annotated[WebSearchSettingPatch, Body()]
) -> dict[str, Any]:
    """一次校验、一次落盘保存联网搜索配置。"""
    rt = require_runtime(request)
    host = request.app.state.host
    draft = rt.config.model_copy(deep=True)
    _apply_web_search_patch(draft, payload)
    try:
        validate_config(draft)
    except ValidationError as e:
        raise HTTPException(
            status_code=400, detail=f"搜索设置不合法：{user_error_text(e)}"
        ) from e
    search = draft.features.web_search
    if search.enabled and search.free_enabled:
        from features.web_search.engines import build_free_engine

        try:
            build_free_engine(
                search.provider,
                searxng_base_url=search.searxng_base_url,
                timeout_seconds=search.timeout_seconds,
            )
        except ValueError as e:
            raise HTTPException(
                status_code=400, detail=f"免费搜索配置不合法：{e}"
            ) from e
    if search.enabled and search.official.enabled:
        _validate_official_search_channel(
            rt,
            str(search.official.provider or ""),
            search.official.model,
        )
    apply_validated(
        rt.config, lambda cfg: _apply_web_search_patch(cfg, payload)
    )
    await asyncio.to_thread(host.save_config)
    search = rt.config.features.web_search
    return {
        "ok": True,
        "needs_restart": True,
        "value": {
            "enabled": search.enabled,
            "free_enabled": search.free_enabled,
            "provider": search.provider,
            "searxng_base_url": search.searxng_base_url,
            "max_results": search.max_results,
            "timeout_seconds": search.timeout_seconds,
            "official_enabled": search.official.enabled,
            "official_provider": search.official.provider or "",
            "official_model": search.official.model,
            "official_timeout_seconds": search.official.timeout_seconds,
        },
        "message": "搜索设置已保存；重启 Runtime 后生效",
    }


@router.post("/web-search/probe")
async def probe_web_search(
    request: Request, payload: Annotated[WebSearchProbePayload, Body()]
) -> dict[str, Any]:
    """测试免费搜索连通性；不调用官方付费搜索。"""
    require_runtime(request)
    from features.web_search.engines import build_free_engine

    try:
        engine = build_free_engine(
            payload.provider,
            searxng_base_url=payload.searxng_base_url,
            timeout_seconds=payload.timeout_seconds,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400, detail=f"免费搜索配置不合法：{e}"
        ) from e
    result = await engine.probe(
        timeout_seconds=min(max(1.0, payload.timeout_seconds), 8.0)
    )
    return {"ok": result.ok, **result.as_dict()}


def _coerce(value: Any, spec: dict[str, Any]) -> Any:
    kind = spec.get("type")
    if kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if kind in {"int", "int_or_none"}:
        if value in (None, "", "null") and kind == "int_or_none":
            return None
        try:
            if isinstance(value, float) and not value.is_integer():
                raise ValueError("not an integer")
            return int(value)
        except (TypeError, ValueError) as e:
            raise HTTPException(
                status_code=400, detail="这个设置需要整数"
            ) from e
    if kind == "float":
        try:
            return float(value)
        except (TypeError, ValueError) as e:
            raise HTTPException(
                status_code=400, detail="这个设置需要数字"
            ) from e
    if kind == "list_int":
        if value in (None, ""):
            return []
        try:
            if isinstance(value, str):
                parts = [p for p in value.replace("，", ",").split(",") if p.strip()]
                return [int(p.strip()) for p in parts]
            return [int(v) for v in value]
        except (TypeError, ValueError) as e:
            raise HTTPException(
                status_code=400, detail="这个设置需要整数列表"
            ) from e
    if kind == "list_str":
        if value in (None, ""):
            return []
        if isinstance(value, str):
            # 逗号或换行都当作分隔符：用户从别处粘贴列表时两种都常见
            parts = re.split(r"[,\n，]", value)
            return [p.strip() for p in parts if p.strip()]
        return [str(v) for v in value]
    if kind == "json":
        if value in (None, ""):
            return None
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError as e:
                raise HTTPException(
                    status_code=400, detail=f"不是合法的 JSON：{e.msg}（第 {e.lineno} 行）"
                ) from e
        return value
    if kind == "enum":
        allowed = {opt[0] for opt in spec.get("options", [])}
        if allowed and value not in allowed:
            raise HTTPException(status_code=400, detail=f"取值必须是 {sorted(allowed)} 之一")
        return value
    return value


async def _apply_hot(rt: Any, path: str, value: Any) -> None:
    """能立即生效的顺手应用掉。"""
    if path == "app.log_level":
        try:
            logging.getLogger().setLevel(getattr(logging, str(value).upper(), logging.INFO))
        except Exception:  # noqa: BLE001
            logger.debug("热更新日志级别失败", exc_info=True)
    elif path.startswith("adapters.") and ".blacklist" in path:
        _apply_blacklist_hot(rt)


def _apply_blacklist_hot(rt: Any) -> None:
    pipeline = getattr(rt, "pipeline", None)
    if pipeline is None:
        return
    try:
        adapter_cfg = next(iter(rt.config.adapters.values()))
    except StopIteration:
        return
    try:
        pipeline.apply_blacklist(adapter_cfg.blacklist)
    except Exception:  # noqa: BLE001
        logger.exception("热应用黑名单失败")


# ============================================================
# Provider / 密钥
# ============================================================


@router.get("/providers")
async def list_providers(request: Request) -> dict[str, Any]:
    """只读列出 provider 与 Agent 绑定。

    写操作在 ``/api/providers``（``webui/api/providers.py``）。这里保留是为了兼容
    旧前端调用，实现直接复用，避免两份逻辑各自漂移。
    """
    return await providers_api.list_all(request)


class ProviderPatch(BaseModel):
    """改 Agent 的模型绑定与采样参数。

    字段留 None = 不改。``provider`` 是换绑到另一个已存在的提供商——
    必须走这里而不是让前端直接改配置，否则很容易引用一个不存在的 ID。
    """

    provider: str | None = None
    model: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    reasoning: bool | None = None
    reasoning_budget: Literal[
        "minimal", "low", "medium", "high", "xhigh", "max"
    ] | None = None
    reasoning_budget_reset: bool = False
    first_token_timeout_seconds: float | None = None


@router.patch("/agents/{agent_name}")
async def patch_agent(
    request: Request,
    agent_name: str,
    payload: Annotated[ProviderPatch, Body()],
) -> dict[str, Any]:
    """改某个 Agent 的模型 / 参数。"""
    rt = require_runtime(request)
    host = request.app.state.host
    agent = getattr(rt.config.agents, agent_name, None)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"没有 {agent_name} 这个 Agent")

    if payload.provider is not None and payload.provider not in (rt.config.providers or {}):
        raise HTTPException(
            status_code=400,
            detail=f"没有 {payload.provider} 这个提供商，先在「添加提供商」里建好",
        )

    # 兼容既有客户端：JSON null 仍表示“不改”。恢复供应商 auto 必须显式
    # 发送 reasoning_budget_reset=true，避免关闭 reasoning 时误清空档位。
    if payload.reasoning_budget_reset and payload.reasoning_budget is not None:
        raise HTTPException(
            status_code=400,
            detail="reasoning_budget 与 reasoning_budget_reset 不能同时设置",
        )
    budget_supplied = (
        payload.reasoning_budget_reset or payload.reasoning_budget is not None
    )
    # reasoning:null 是旧客户端的 no-change，不是关闭；只有显式 true/false 才改开关。
    reasoning_supplied = (
        "reasoning" in payload.model_fields_set and payload.reasoning is not None
    )

    if payload.reasoning_budget is not None and not payload.reasoning_budget_reset:
        from webui.api.providers import _reasoning_effort_options

        chosen_provider = payload.provider or agent.provider
        chosen_model = payload.model or agent.model
        options = _reasoning_effort_options(rt, chosen_provider, chosen_model)
        if payload.reasoning_budget not in options:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"当前模型不支持思考强度 {payload.reasoning_budget}；"
                    f"可用: {', '.join(options)}"
                ),
            )

    def mutate(draft: Any) -> None:
        target = getattr(draft.agents, agent_name)
        if payload.provider is not None:
            target.provider = payload.provider
        if payload.model is not None:
            target.model = payload.model
        if payload.temperature is not None:
            target.temperature = float(payload.temperature)
        if payload.top_p is not None:
            target.top_p = float(payload.top_p)
        if payload.max_tokens is not None:
            target.max_tokens = int(payload.max_tokens)
        if payload.first_token_timeout_seconds is not None:
            target.first_token_timeout_seconds = float(payload.first_token_timeout_seconds)
        if reasoning_supplied or budget_supplied:
            if target.reasoning is None:
                from app_config.schema import ReasoningConfig

                target.reasoning = ReasoningConfig(enabled=bool(payload.reasoning))
            elif reasoning_supplied:
                target.reasoning.enabled = bool(payload.reasoning)
            if budget_supplied:
                # reset=true = 恢复供应商自动；省略/旧客户端 null = 保持原选择。
                target.reasoning.budget = (
                    None if payload.reasoning_budget_reset
                    else payload.reasoning_budget
                )

    try:
        apply_validated(rt.config, mutate)
    except ValidationError as e:
        raise HTTPException(
            status_code=400, detail=f"设置值不合法：{user_error_text(e)}"
        ) from e

    await asyncio.to_thread(host.save_config)
    return {"ok": True, "needs_restart": True, "message": f"{agent_name} 已更新，重启后生效"}


class KeyPayload(BaseModel):
    key_id: str = Field(..., min_length=1)
    value: str = Field("", description="留空表示删除这个密钥")


@router.put("/secrets")
async def put_secret(
    request: Request, payload: Annotated[KeyPayload, Body()]
) -> dict[str, Any]:
    """写入或删除一个密钥（只进 secrets.enc，不落 config.yaml）。"""
    rt = require_runtime(request)
    key_id = payload.key_id.strip()
    if not key_id:
        raise HTTPException(status_code=400, detail="key_id 不能为空")

    def _write() -> None:
        if payload.value:
            rt.secrets.set(key_id, payload.value)
        else:
            rt.secrets.delete(key_id)

    try:
        await asyncio.to_thread(_write)
    except Exception as e:  # noqa: BLE001
        logger.exception("写入密钥失败")
        raise HTTPException(status_code=500, detail=f"写入密钥失败：{e}") from e
    return {
        "ok": True,
        "deleted": not payload.value,
        "message": "密钥已删除" if not payload.value else "密钥已保存",
    }


# ============================================================
# 黑名单（独立页面，热生效）
# ============================================================


class BlacklistPayload(BaseModel):
    enabled: bool = True
    qq_ids: list[int] = Field(default_factory=list)
    group_ids: list[int] = Field(default_factory=list)


def _blacklist_cfg(rt: Any) -> Any:
    adapters = rt.config.adapters or {}
    if not adapters:
        raise HTTPException(status_code=404, detail="配置里没有任何渠道适配器")
    return next(iter(adapters.values())).blacklist


def _blacklist_json(cfg: Any) -> dict[str, Any]:
    return {
        "enabled": bool(cfg.enabled),
        "qq_ids": list(cfg.qq_ids),
        "group_ids": list(cfg.group_ids),
    }


@router.get("/blacklist")
async def get_blacklist(request: Request) -> dict[str, Any]:
    rt = require_runtime(request)
    data = _blacklist_json(_blacklist_cfg(rt))
    pipeline = getattr(rt, "pipeline", None)
    data["active_qq_ids"] = sorted(getattr(pipeline, "_blacklist_qq_ids", set()) or set())
    data["active_group_ids"] = sorted(getattr(pipeline, "_blacklist_group_ids", set()) or set())
    data["hot_applied"] = True
    return data


@router.put("/blacklist")
async def put_blacklist(
    request: Request,
    payload: Annotated[BlacklistPayload, Body()],
) -> dict[str, Any]:
    """整份替换黑名单，保存后**立即生效**（不用重启）。"""
    rt = require_runtime(request)
    host = request.app.state.host
    cfg = _blacklist_cfg(rt)
    cfg.enabled = payload.enabled
    cfg.qq_ids = sorted({int(x) for x in payload.qq_ids})
    cfg.group_ids = sorted({int(x) for x in payload.group_ids})

    await asyncio.to_thread(host.save_config)
    _apply_blacklist_hot(rt)
    logger.info(
        "黑名单已更新 enabled=%s qq=%s group=%s",
        cfg.enabled,
        cfg.qq_ids,
        cfg.group_ids,
    )
    return {
        "ok": True,
        "needs_restart": False,
        "message": "黑名单已保存并立即生效",
        **_blacklist_json(cfg),
    }


__all__ = ["router"]
