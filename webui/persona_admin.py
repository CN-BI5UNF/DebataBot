"""角色人格的生成 / 改写 / 落盘（纯函数为主，便于单测）。

原桌面端把这条链路放在两个对话框里（``ui/dashboard/persona_dialogs.py`` 的
``_PersonaCreatorDialog`` / ``_PersonaRefineDialog``），它们各自持有一份"当前主模型"
的临时状态来构造 ``PersonaGenAgent``。搬到 WebUI 后改成从**已持久化的配置**取：

    agents.persona_gen（人格生成 Agent 的专用配置）> agents.chat（跟随主模型）

这样不必在前后端之间来回复制 provider/base_url/api_key，也不会出现"设置页改了模型、
生成人格却还用着旧模型"的错位。

生成器本身（``agents/persona_gen_agent.py``）一行没动——它是完整的，只是一直
没接上入口。``webui/wizard/persona_render.py`` 的 ``render_minimal_persona``
同理，此前零调用，现在在这里被用上。

落盘约定（与 ``agents/persona_loader.py`` 一致）：
    personas/<name>/persona_prompt.py
覆盖已有角色前**一定先备份**到 ``personas/<name>/.backups/persona_prompt_<时间戳>.py``
——原 GUI 有这个行为，改写是 AI 做的，没备份等于把用户的角色交出去了。
"""

from __future__ import annotations

import ast
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from agents.persona_gen_agent import PersonaBrief

logger = logging.getLogger(__name__)

PERSONA_FILE = "persona_prompt.py"
BACKUP_DIR = ".backups"
MAX_PERSONA_PROMPT_CHARS = 20_000
"""直接提示词的长度护栏；超过会显著挤占每轮历史预算。"""

# 与 agents/persona_loader.py 的目录规则保持一致：支持中文人格名，但不允许
# 路径分隔符、点、空格和 Windows 保留名。目录名同时会出现在 Python 模块路径里。
_NAME_RE = re.compile(r"^[\w一-龥㐀-䶿＀-￯-]{1,64}$")
_RESERVED_NAMES = frozenset(
    {
        "__pycache__",
        "backups",
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)

BRIEF_FIELDS = (
    "name",
    "personality",
    "background",
    "voice_samples",
    "boundaries",
    "never_say",
    "relation_matrix",
    "sensitive_topics",
    "relation",
    "relation_detail",
    "admin_name",
    "admin_qq",
    "gender",
    "extra_notes",
)


class PersonaAdminError(Exception):
    """操作失败。消息直接展示给用户。"""


# ============================================================
# 校验与转换
# ============================================================


def validate_persona_name(name: str) -> str:
    """角色目录名：支持中文，但必须是安全、可加载的单段目录名。"""
    candidate = (name or "").strip()
    if not candidate:
        raise PersonaAdminError("人格名不能为空")
    if not _NAME_RE.match(candidate):
        raise PersonaAdminError(
            "人格名只能含中文、字母、数字、下划线或短横线，最长 64 个字符"
            "（它会同时作为目录名，不能包含空格、斜杠或点）"
        )
    if candidate.startswith((".", "_", "-")):
        raise PersonaAdminError("人格名不能以点、下划线或短横线开头")
    if candidate.casefold() in _RESERVED_NAMES:
        raise PersonaAdminError(f"{candidate} 是保留名，换一个")
    return candidate


def brief_from_payload(payload: dict[str, Any]) -> PersonaBrief:
    """把请求体翻成 PersonaBrief。只取已知字段，多余的忽略。"""
    values: dict[str, Any] = {k: payload.get(k) or "" for k in BRIEF_FIELDS}
    age = payload.get("age")
    values["age"] = int(age) if age not in (None, "") else None
    admins = payload.get("admins")
    values["admins"] = [dict(x) for x in admins if isinstance(x, dict)] if isinstance(admins, list) else []
    if not str(values.get("name") or "").strip():
        raise PersonaAdminError("人格名不能为空")
    return PersonaBrief(**values)


def extract_prompt_source(text: str) -> str:
    """从 persona_prompt.py 里取出 PERSONA_PROMPT 的字面量。

    用 ``ast`` 而不是 exec：人格文件是用户/AI 写的代码，这里只需要那个字符串常量，
    没有理由把整个模块执行一遍。解析不出来时退回正则，再不行就原样返回。
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _extract_by_regex(text)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "PERSONA_PROMPT":
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    return _extract_by_regex(text)
                return value if isinstance(value, str) else _extract_by_regex(text)
    return _extract_by_regex(text)


def _extract_by_regex(text: str) -> str:
    match = re.search(r"PERSONA_PROMPT\s*=\s*(['\"]{3})(.*?)\1", text, re.DOTALL)
    if match:
        return match.group(2)
    return text


def extract_persona_vars(text: str) -> dict[str, Any]:
    """Read PERSONA_VARS without executing the persona module."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "PERSONA_VARS":
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    return {}
                return dict(value) if isinstance(value, dict) else {}
    return {}


# ============================================================
# 生成 / 改写
# ============================================================


def resolve_gen_agent_config(rt: Any) -> Any:
    """人格生成用哪个 Agent 配置：专用配置优先，否则跟随主聊天模型。"""
    persona_gen = getattr(rt.config.agents, "persona_gen", None)
    if persona_gen is not None:
        return persona_gen
    return rt.config.agents.chat


def build_gen_agent(rt: Any):
    """构造 PersonaGenAgent。

    优先复用 Runtime 已经建好的 provider 实例（连接池、代理设置都是现成的）；
    Runtime 没起来时按配置临时构造一个，用完由调用方关闭。
    """
    from agents import PersonaGenAgent
    from providers import build_provider
    from providers.base import ProviderError

    cfg = resolve_gen_agent_config(rt)
    provider = getattr(rt, "providers", {}).get(cfg.provider)
    temporary = False
    if provider is None:
        presets = dict(getattr(rt.provider_registry, "presets", {}) or {})
        try:
            provider = build_provider(cfg.provider, rt.config.providers[cfg.provider], rt.secrets, presets)
        except (KeyError, ProviderError) as e:
            raise PersonaAdminError(
                f"人格生成用的提供商 {cfg.provider!r} 不可用：{e}。"
                "到「设置 → 模型」确认它已配置好密钥。"
            ) from e
        temporary = True

    agent = PersonaGenAgent(
        provider,
        cfg,
        usage_recorder=getattr(rt, "_record_model_usage", None),
        status_callback=getattr(rt, "_update_model_activity", None),
    )
    return agent, provider if temporary else None


async def generate_draft(rt: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """生成初版人格（不落盘）。"""
    brief = brief_from_payload(payload)
    agent, temporary = build_gen_agent(rt)
    try:
        result = await agent.generate(brief)
    except Exception as e:  # noqa: BLE001
        logger.exception("生成人格失败")
        raise PersonaAdminError(f"生成未能完成：{e}") from e
    finally:
        if temporary is not None:
            await _close_quietly(temporary)
    if not (result.persona_prompt or "").strip():
        raise PersonaAdminError("模型没有返回内容，换一个模型或稍后再试")
    return {
        "ok": True,
        "xml": result.persona_prompt,
        "name": result.display_name or brief.name,
        "reasoning": result.reasoning or "",
        "refined_count": result.refined_count,
    }


async def refine_draft(rt: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """按用户反馈出修订版（不落盘）。可反复调用，``refined_count`` 递增。"""
    prev = str(payload.get("xml") or "")
    feedback = str(payload.get("feedback") or "").strip()
    if not prev.strip():
        raise PersonaAdminError("没有可改写的底稿")
    if not feedback:
        raise PersonaAdminError("说说想改哪里")

    agent, temporary = build_gen_agent(rt)
    try:
        result = await agent.refine(
            prev,
            feedback,
            int(payload.get("refined_count") or 0),
            edit_history=payload.get("edit_history") or None,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("改写人格失败")
        raise PersonaAdminError(f"改写未能完成：{e}") from e
    finally:
        if temporary is not None:
            await _close_quietly(temporary)
    return {
        "ok": True,
        "xml": result.persona_prompt,
        "name": result.display_name,
        "reasoning": result.reasoning or "",
        "refined_count": result.refined_count,
    }


async def _close_quietly(provider: Any) -> None:
    try:
        await provider.aclose()
    except Exception:  # noqa: BLE001
        logger.debug("关闭临时 provider 失败", exc_info=True)


# ============================================================
# 落盘
# ============================================================


def persona_dir(paths: Any, name: str) -> Path:
    return Path(paths.PERSONAS_DIR) / name


def backup_persona(paths: Any, name: str) -> Path | None:
    """把现有 persona_prompt.py 备份到 .backups/。没有原文件则返回 None。"""
    source = persona_dir(paths, name) / PERSONA_FILE
    if not source.is_file():
        return None
    backup_dir = persona_dir(paths, name) / BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    # 时间戳到秒；同一秒内连续覆盖会加序号，避免互相盖掉
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = backup_dir / f"persona_prompt_{stamp}.py"
    index = 1
    while target.exists():
        target = backup_dir / f"persona_prompt_{stamp}_{index}.py"
        index += 1
    try:
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as e:
        raise PersonaAdminError(f"备份失败（未做任何修改）：{e}") from e
    logger.info("人格已备份: %s", target)
    return target


def _normalize_admins(admins: Any) -> list[dict[str, object]] | None:
    """Accept a UI-shaped admin list; None means "preserve whatever exists"."""
    if admins is None:
        return None
    if not isinstance(admins, list):
        return []
    out: list[dict[str, object]] = []
    for item in admins:
        if not isinstance(item, dict):
            continue
        qq = str(item.get("qq", "")).strip()
        if not qq:
            continue
        try:
            qq_value = int(qq)
        except ValueError as exc:
            raise PersonaAdminError(f"管理员 QQ 不是数字：{qq!r}") from exc
        entry: dict[str, object] = {"qq": qq_value, "role": str(item.get("role") or "owner")}
        for key in ("name", "relation"):
            value = str(item.get(key, "") or "").strip()
            if value:
                entry[key] = value
        out.append(entry)
    return out


def _normalize_age(age: Any) -> int | None:
    if age in (None, ""):
        return None
    try:
        value = int(age)
    except (TypeError, ValueError) as exc:
        raise PersonaAdminError("年龄必须是整数") from exc
    if value < 0 or value > 1000:
        raise PersonaAdminError("年龄需要在 0 到 1000 之间")
    return value


def _atomic_write_persona(target: Path, text: str) -> None:
    """Write via a same-directory temp file so a crash cannot truncate a persona."""
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def save_persona(
    paths: Any,
    name: str,
    xml: str,
    *,
    brief: PersonaBrief | None = None,
    overwrite: bool = False,
    raw: bool = False,
    display_name: str = "",
    gender: str = "",
    age: Any = None,
    admins: Any = None,
    preserve_vars: bool = False,
) -> dict[str, Any]:
    """把人格提示词写成 ``personas/<name>/persona_prompt.py``。

    ``raw=True`` 用于“直接写提示词”：不经过 AI、不解析 XML，提示词原样保存。
    覆盖已有角色时**先备份**。``preserve_vars=True`` 会保留现有 PERSONA_VARS
    （例如手写角色里已有的 admins / age / 自定义字段），只更新用户明确改动的项。
    """
    from webui.wizard.persona_render import (
        admin_entries_from_brief,
        render_persona_file,
    )

    safe_name = validate_persona_name(name)
    content = xml or ""
    if not content.strip():
        raise PersonaAdminError("内容为空，没什么可保存的")
    if len(content) > MAX_PERSONA_PROMPT_CHARS:
        raise PersonaAdminError(
            f"提示词太长（{len(content)} 字符），上限是 {MAX_PERSONA_PROMPT_CHARS} 字符。"
            "长提示词会挤占每轮对话的历史预算。"
        )

    target_dir = persona_dir(paths, safe_name)
    target = target_dir / PERSONA_FILE
    existed = target.is_file()
    if existed and not overwrite:
        raise PersonaAdminError(f"{safe_name} 已存在。换个名字，或确认覆盖（会先自动备份）")

    backup: Path | None = None
    if existed:
        backup = backup_persona(paths, safe_name)

    existing_vars: dict[str, Any] = {}
    if preserve_vars and existed:
        try:
            existing_vars = extract_persona_vars(target.read_text(encoding="utf-8"))
        except OSError:
            existing_vars = {}
    vars_out = dict(existing_vars) if preserve_vars else {}

    final_display = (
        (display_name or "").strip()
        or str(vars_out.get("name") or "").strip()
        or ((brief.name or "").strip() if brief is not None else "")
        or safe_name
    )
    final_gender = (gender or "").strip()
    if not final_gender:
        final_gender = str(vars_out.get("gender") or "").strip()
    if not final_gender and brief is not None:
        final_gender = (brief.gender or "").strip()

    final_age = _normalize_age(age)
    if final_age is None and "age" in vars_out:
        final_age = _normalize_age(vars_out.get("age"))
    if final_age is None and brief is not None:
        final_age = _normalize_age(brief.age)

    final_admins = _normalize_admins(admins)
    if final_admins is None:
        if brief is not None:
            final_admins = admin_entries_from_brief(brief)
        elif "admins" in vars_out:
            final_admins = _normalize_admins(vars_out.get("admins")) or []
        else:
            final_admins = []

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        rendered = render_persona_file(
            safe_name,
            content,
            display_name=final_display,
            admins=final_admins,
            gender=final_gender,
            age=final_age,
            extra_vars={
                key: value
                for key, value in vars_out.items()
                if key not in {"name", "admins", "gender", "age"}
            },
        )
        _atomic_write_persona(target, rendered)
    except OSError as e:
        raise PersonaAdminError(f"写入失败：{e}") from e

    logger.info(
        "人格已写入: %s（raw=%s，备份: %s）", target, raw, backup or "无"
    )
    return {
        "ok": True,
        "id": safe_name,
        "path": str(target),
        "created": not existed,
        "raw": bool(raw),
        "size": len(content),
        "backup": str(backup) if backup else "",
        "message": f"{'已更新' if existed else '已创建'}人格 {safe_name}",
    }

def duplicate_persona(paths: Any, name: str, new_name: str) -> str:
    """复制一个角色（连同它的非备份资源）。"""
    source_dir = persona_dir(paths, name)
    if not (source_dir / PERSONA_FILE).is_file():
        raise PersonaAdminError(f"没有 {name} 这个人格")

    safe_new = validate_persona_name(new_name)
    target_dir = persona_dir(paths, safe_new)
    if target_dir.exists():
        raise PersonaAdminError(f"{safe_new} 已存在，换个名字")

    try:
        target_dir.mkdir(parents=True)
        for item in source_dir.iterdir():
            if item.name == BACKUP_DIR:
                continue  # 别人的历史备份不必跟着复制
            if item.is_dir():
                continue
            (target_dir / item.name).write_bytes(item.read_bytes())
    except OSError as e:
        raise PersonaAdminError(f"复制失败：{e}") from e

    logger.info("人格已复制: %s → %s", name, safe_new)
    return safe_new


__all__ = [
    "BRIEF_FIELDS",
    "PERSONA_FILE",
    "PersonaAdminError",
    "backup_persona",
    "brief_from_payload",
    "build_gen_agent",
    "duplicate_persona",
    "extract_prompt_source",
    "generate_draft",
    "persona_dir",
    "refine_draft",
    "resolve_gen_agent_config",
    "save_persona",
    "validate_persona_name",
]
